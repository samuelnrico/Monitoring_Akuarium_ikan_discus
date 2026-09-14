"""
Server Flask FINAL (Revisi) — Sistem Peringatan Dini Kualitas Air Ikan Discus
================================================================
Perubahan utama dibanding versi sebelumnya:
  1. XGBoost hanya dipakai untuk STATUS (Aman/Waspada/Bahaya).
     Penyebab spesifik (pH asam/basa, suhu dingin/panas, turbidity)
     ditentukan oleh RULE-BASED THRESHOLD pada nilai sensor mentah,
     karena model klasifikasi tidak tahu ARAH penyimpangan.
  2. Logika pompa buffer diperbaiki (netralisasi):
       - pH terlalu ASAM  -> Pompa buffer BASA aktif  (menaikkan pH)
       - pH terlalu BASA  -> Pompa buffer ASAM aktif  (menurunkan pH)
  3. Pompa bekerja DOSING: nyala singkat (default 5 detik) tiap
     pembacaan. Kalau 2 menit lagi masih lewat batas -> dosing lagi.
     ESP32 yang mengeksekusi pulsa 5 detiknya (field "dosing_detik").
  4. Format notifikasi Telegram disamakan dengan laporan
     (SISTEM AKTIF / WASPADA / BAHAYA / AKTUATOR AKTIF / NONAKTIF).
  5. Notifikasi ON aktuator dikirim pada SETIAP perintah aktif/dosing.
     Notifikasi OFF dikirim ketika kondisi kembali normal.
================================================================
"""

from flask import Flask, request, jsonify
import pickle, logging, requests, threading, time, os
from datetime import datetime

app = Flask(__name__)

# ================================================================
# KONFIGURASI
# ================================================================

TELEGRAM_TOKEN = "8679942687:AAEwcgjXqkzGeLiiBTBajK3ULrp85LEjkYY"
CHAT_ID        = "7282560281"

SUPABASE_URL   = "https://dedoyprhqrontosullhb.supabase.co"
SUPABASE_KEY   = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImRlZG95cHJocXJvbnRvc3VsbGhiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI2MTEyMDQsImV4cCI6MjA5ODE4NzIwNH0.kKGRsFGOSY-zAWADvESaFUgHryf14RYcLbSwt0T_W5M"
SUPABASE_TABLE = "sensor_log"
RESPONSE_TIME_TABLE = "response_time_log"

MODEL_FILE = "model.pkl"
if not os.path.exists(MODEL_FILE) and os.path.exists("model_final.pkl"):
    MODEL_FILE = "model_final.pkl"

# ----------------------------------------------------------------
# AMBANG BATAS (RULE-BASED) — SESUAI TABEL LAPORAN
#
#   Parameter   | Aman        | Waspada                | Bahaya
#   Suhu (°C)   | 25.0-31.0   | 20.0-24.9 / 31.1-33.0  | <20.0 / >33.0
#   pH          | 6.0-7.0     | 5.5-5.9 / 7.1-7.5      | <5.5  / >7.5
#   Turbidity   | 0-25        | >25-35                 | >35
#
# Aktuator hanya diaktifkan pada level BAHAYA per-parameter.
# Kondisi Waspada -> hanya peringatan, tidak menyalakan aktuator.
# ----------------------------------------------------------------
# pH
PH_AMAN_MIN     = 6.0
PH_AMAN_MAX     = 7.0
PH_BAHAYA_ASAM  = 5.5      # pH < 5.5  -> terlalu asam -> pompa BASA
PH_BAHAYA_BASA  = 7.5      # pH > 7.5  -> terlalu basa -> pompa ASAM

# Suhu
SUHU_AMAN_MIN      = 25.0
SUHU_AMAN_MAX      = 31.0
SUHU_BAHAYA_DINGIN = 20.0  # < 20 -> terlalu dingin -> heater
SUHU_BAHAYA_PANAS  = 33.0  # > 33 -> terlalu panas (tidak ada cooler)

# Turbidity
TURB_AMAN_MAX      = 25.0  # 0-25 -> Aman
TURB_BAHAYA        = 35.0  # >25-35 -> Waspada, >35 -> Bahaya

DOSING_DETIK       = 5     # durasi pulsa pompa buffer (detik) — dieksekusi ESP32

# Tingkat keparahan untuk membandingkan status
RANK = {"Aman": 0, "Waspada": 1, "Bahaya": 2}

# Cooldown notifikasi kondisi (detik)
#   Bahaya  = 0   -> selalu kirim setiap deteksi (sesuai kebutuhanmu)
#   Waspada = 0   -> kirim pada setiap pembacaan Waspada
COOLDOWN = {
    "Bahaya":  0,
    "Waspada": 0
}

# Nama tampilan aktuator
NAMA_AKTUATOR = {
    "pompa_asam": "Pompa buffer asam",
    "pompa_basa": "Pompa buffer basa",
    "heater":     "Heater",
}

# ================================================================
# LOGGING
# ================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ================================================================
# LOAD MODEL XGBOOST
# ================================================================
try:
    with open(MODEL_FILE, "rb") as f:
        saved = pickle.load(f)
    model   = saved["model"]
    encoder = saved["encoder"]
    logger.info(f"Model loaded — kelas: {list(encoder.classes_)}")
except Exception as e:
    logger.error(f"Gagal load model: {e}")
    model, encoder = None, None

# ================================================================
# STATE
# ================================================================
last_notif_time = {}
# Status LOGIS aktuator (True = sedang dosing/aktif). Dipakai untuk
# mendeteksi transisi ON/OFF, bukan status pulsa fisik.
status_aktuator = {
    "pompa_asam": False,
    "pompa_basa": False,
    "heater":     False,
}

# Jeda ini memastikan notifikasi kedua tetap terkirim ketika beberapa
# aktuator aktif pada pembacaan yang sama.
telegram_lock = threading.Lock()
last_telegram_send = 0.0
TELEGRAM_MIN_INTERVAL = 1.1

# ================================================================
# FUNGSI KIRIM TELEGRAM (generik)
# parse_mode HTML + <pre> supaya perataan kolom (spasi) rapi
# ================================================================
def kirim_telegram_pesan(pesan, monospace=True):
    global last_telegram_send

    teks = f"<pre>{pesan}</pre>" if monospace else pesan
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"

    # Pesan dikirim satu per satu agar notifikasi aktuator berikutnya
    # tidak hilang akibat rate limit Telegram.
    with telegram_lock:
        jeda = TELEGRAM_MIN_INTERVAL - (time.monotonic() - last_telegram_send)
        if jeda > 0:
            time.sleep(jeda)

        for percobaan in range(2):
            try:
                resp = requests.post(url, json={
                    "chat_id":    CHAT_ID,
                    "text":       teks,
                    "parse_mode": "HTML"
                }, timeout=10)
                last_telegram_send = time.monotonic()

                if resp.status_code == 200:
                    logger.info("[Telegram] Pesan terkirim")
                    return True

                if resp.status_code == 429 and percobaan == 0:
                    try:
                        retry_after = float(
                            resp.json().get("parameters", {}).get("retry_after", 1)
                        )
                    except Exception:
                        retry_after = 1.0
                    logger.warning(
                        f"[Telegram] Rate limit, mencoba lagi dalam {retry_after}s"
                    )
                    time.sleep(max(1.0, retry_after))
                    continue

                logger.error(f"[Telegram] Gagal: {resp.text}")
                return False
            except Exception as e:
                logger.error(f"[Telegram] Error: {e}")
                if percobaan == 0:
                    time.sleep(1)
                    continue
                return False
    return False

# ================================================================
# TEMPLATE NOTIFIKASI — SESUAI FORMAT LAPORAN
# ================================================================
def notif_sistem_aktif():
    pesan = (
        "SISTEM AKTIF\n"
        "Status  : Sistem monitoring kualitas air aktif\n"
        "Server  : Terhubung"
    )
    kirim_telegram_pesan(pesan)

def pesan_kondisi_waspada(suhu, ph, turb, penyebab):
    return (
        "PERINGATAN KUALITAS AIR\n"
        "Status    : WASPADA\n"
        f"Suhu      : {suhu:.2f} °C\n"
        f"pH        : {ph:.2f}\n"
        f"Turbidity : {turb:.2f} NTU\n"
        "Penyebab  :\n"
        f"{penyebab}"
    )


def pesan_kondisi_bahaya(suhu, ph, turb, penyebab):
    return (
        "PERINGATAN KUALITAS AIR\n"
        "Status    : BAHAYA\n"
        f"Suhu      : {suhu:.2f} °C\n"
        f"pH        : {ph:.2f}\n"
        f"Turbidity : {turb:.2f} NTU\n"
        "Penyebab  :\n"
        f"{penyebab}"
    )


def pesan_aktuator_on(nama_aktuator):
    return (
        "AKTUATOR AKTIF\n"
        f"Aktuator : {nama_aktuator}\n"
        "Status   : ON"
    )


def pesan_aktuator_off(nama_aktuator):
    return (
        "AKTUATOR NONAKTIF\n"
        f"Aktuator : {nama_aktuator}\n"
        "Status   : OFF"
    )

# Kirim notifikasi sistem hidup saat server start
notif_sistem_aktif()

# ================================================================
# EVALUASI KONDISI (RULE-BASED, MULTI-PARAMETER)
# XGBoost hanya memberi STATUS. Fungsi ini memeriksa SETIAP parameter
# secara independen memakai ambang tabel laporan, sehingga:
#   - beberapa masalah bisa dilaporkan sekaligus (mis. pH + turbidity)
#   - beberapa aktuator bisa hidup bersamaan (mis. pompa + heater)
#   - satu masalah tidak "menendang" masalah lain
# Return: (penyebab_str, aktuator_dict, aksi_list, status_rule)
# ================================================================
def evaluasi_kondisi(suhu, ph, turb):
    masalah_bahaya = []
    masalah_waspada = []
    aksi = []
    aktuator = {"pompa_asam": False, "pompa_basa": False, "heater": False}
    rank = 0  # 0=Aman, 1=Waspada, 2=Bahaya

    # ---------------- pH ----------------
    if ph > PH_BAHAYA_BASA:              # > 7.5 -> Bahaya
        masalah_bahaya.append("pH terlalu basa")
        aktuator["pompa_asam"] = True
        aksi.append("Pompa buffer asam")
        rank = max(rank, 2)
    elif ph < PH_BAHAYA_ASAM:            # < 5.5 -> Bahaya
        masalah_bahaya.append("pH terlalu asam")
        aktuator["pompa_basa"] = True
        aksi.append("Pompa buffer basa")
        rank = max(rank, 2)
    elif ph > PH_AMAN_MAX:               # 7.1-7.5 -> Waspada
        masalah_waspada.append("pH cenderung basa")
        rank = max(rank, 1)
    elif ph < PH_AMAN_MIN:               # 5.5-5.9 -> Waspada
        masalah_waspada.append("pH cenderung asam")
        rank = max(rank, 1)

    # ---------------- Suhu ----------------
    if suhu > SUHU_BAHAYA_PANAS:         # > 33 -> Bahaya, tanpa cooler
        masalah_bahaya.append("Suhu terlalu panas")
        rank = max(rank, 2)
    elif suhu < SUHU_BAHAYA_DINGIN:      # < 20 -> Bahaya, heater aktif
        masalah_bahaya.append("Suhu terlalu dingin")
        aktuator["heater"] = True
        aksi.append("Heater")
        rank = max(rank, 2)
    elif suhu > SUHU_AMAN_MAX:           # 31.1-33 -> Waspada
        masalah_waspada.append("Suhu tinggi")
        rank = max(rank, 1)
    elif suhu < SUHU_AMAN_MIN:           # 20-24.9 -> Waspada, heater tetap OFF
        masalah_waspada.append("Suhu rendah")
        rank = max(rank, 1)

    # ---------------- Turbidity ----------------
    if turb > TURB_BAHAYA:               # > 35 -> Bahaya, tanpa aktuator
        masalah_bahaya.append("Turbidity terlalu tinggi")
        rank = max(rank, 2)
    elif turb > TURB_AMAN_MAX:           # > 25 sampai 35 -> Waspada
        masalah_waspada.append("Turbidity mulai meningkat")
        rank = max(rank, 1)

    status_rule = ["Aman", "Waspada", "Bahaya"][rank]

    rincian = (
        [f"- {item} (Bahaya)" for item in masalah_bahaya] +
        [f"- {item} (Waspada)" for item in masalah_waspada]
    )
    penyebab_str = "\n".join(rincian) if rincian else "- Semua parameter normal"
    return penyebab_str, aktuator, aksi, status_rule

# ================================================================
# NOTIFIKASI KONDISI (dengan cooldown per label)
# ================================================================
def kirim_notif_kondisi(label, suhu, ph, turb, penyebab):
    sekarang = datetime.now().timestamp()
    terakhir = last_notif_time.get(label, 0)
    cooldown = COOLDOWN.get(label, 120)

    if cooldown > 0 and (sekarang - terakhir) < cooldown:
        sisa = int(cooldown - (sekarang - terakhir))
        logger.info(f"[Telegram] Cooldown {label} — {sisa}s tersisa")
        return False

    if label == "Waspada":
        pesan = pesan_kondisi_waspada(suhu, ph, turb, penyebab)
    else:  # Bahaya
        pesan = pesan_kondisi_bahaya(suhu, ph, turb, penyebab)

    if kirim_telegram_pesan(pesan):
        last_notif_time[label] = sekarang
        return True
    return False

# ================================================================
# NOTIFIKASI AKTUATOR
# ON dikirim pada SETIAP perintah aktif, termasuk setiap siklus dosing.
# OFF dikirim ketika kondisi berubah dari aktif menjadi nonaktif.
# ================================================================
def proses_notif_aktuator(aktuator_baru):
    for key, val in aktuator_baru.items():
        lama = status_aktuator.get(key, False)
        if val:
            kirim_telegram_pesan(pesan_aktuator_on(NAMA_AKTUATOR[key]))
        elif (not val) and lama:
            kirim_telegram_pesan(pesan_aktuator_off(NAMA_AKTUATOR[key]))
        status_aktuator[key] = val

# ================================================================
# SIMPAN KE SUPABASE
# ================================================================
def simpan_supabase(ts, suhu, ph, turb, label, prob_dict):
    url     = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal"
    }
    payload = {
        "timestamp":  ts,
        "suhu":       round(suhu, 2),
        "ph":         round(ph,   3),
        "turbidity":  round(turb, 2),
        "prediksi":   label,
        "p_aman":     round(prob_dict.get("Aman",    0), 4),
        "p_waspada":  round(prob_dict.get("Waspada", 0), 4),
        "p_bahaya":   round(prob_dict.get("Bahaya",  0), 4)
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code in [200, 201]:
            logger.info(f"[Supabase] Tersimpan: {label}")
            return True
        logger.error(f"[Supabase] Gagal: {resp.status_code} — {resp.text}")
        return False
    except Exception as e:
        logger.error(f"[Supabase] Error: {e}")
        return False

# ================================================================
# DATA MENTAH PENGUJIAN WAKTU RESPONS END-TO-END
# Durasi dihitung ESP32. Flask hanya menerima, mencatat di Render Logs,
# lalu menyimpan ke Supabase. Tidak ada perhitungan statistik otomatis.
# ================================================================
def simpan_response_time(
    request_id, status_uji, titik_akhir, waktu_respons_ms,
    suhu, ph, turbidity
):
    logger.info(
        "[RAW_RESPONSE_TIME] "
        f"request_id={request_id} | status={status_uji} | "
        f"titik_akhir={titik_akhir} | waktu_respons_ms={waktu_respons_ms} | "
        f"suhu={suhu:.2f} | ph={ph:.3f} | turbidity={turbidity:.2f}"
    )

    # on_conflict + merge-duplicates membuat pengiriman ulang dengan
    # request_id yang sama tidak menambah baris ganda.
    url = (
        f"{SUPABASE_URL}/rest/v1/{RESPONSE_TIME_TABLE}"
        "?on_conflict=request_id"
    )
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal"
    }
    payload = {
        "request_id": request_id,
        "status": status_uji,
        "titik_akhir": titik_akhir,
        "waktu_respons_ms": waktu_respons_ms,
        "suhu": round(suhu, 2),
        "ph": round(ph, 3),
        "turbidity": round(turbidity, 2)
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code in [200, 201, 204]:
            logger.info(f"[RAW_RESPONSE_TIME] Supabase tersimpan: {request_id}")
            return True
        logger.error(
            f"[RAW_RESPONSE_TIME] Supabase gagal {resp.status_code}: {resp.text}"
        )
        return False
    except Exception as e:
        logger.error(f"[RAW_RESPONSE_TIME] Supabase error: {e}")
        return False


@app.route("/response-time", methods=["POST"])
def terima_response_time():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "msg": "invalid JSON"}), 400

    try:
        request_id = str(data["request_id"]).strip()
        status_uji = str(data["status"]).strip()
        titik_akhir = str(data["titik_akhir"]).strip()
        waktu_respons_ms = int(data["waktu_respons_ms"])
        suhu = float(data["suhu"])
        ph = float(data["ph"])
        turbidity = float(data["turbidity"])
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"status": "error", "msg": f"data tidak valid: {e}"}), 400

    if not request_id or len(request_id) > 100:
        return jsonify({"status": "error", "msg": "request_id tidak valid"}), 400
    if status_uji not in ["Waspada", "Bahaya"]:
        return jsonify({"status": "error", "msg": "status harus Waspada/Bahaya"}), 400
    if titik_akhir not in ["Telegram", "Aktuator"]:
        return jsonify({"status": "error", "msg": "titik akhir tidak valid"}), 400
    if status_uji == "Waspada" and titik_akhir != "Telegram":
        return jsonify({"status": "error", "msg": "Waspada harus berakhir di Telegram"}), 400
    if status_uji == "Bahaya" and titik_akhir != "Aktuator":
        return jsonify({"status": "error", "msg": "Bahaya harus berakhir di aktuator"}), 400
    if not (0 < waktu_respons_ms <= 120000):
        return jsonify({"status": "error", "msg": "waktu respons di luar batas"}), 400
    if not (10 <= suhu <= 45 and 0 <= ph <= 14 and turbidity >= 0):
        return jsonify({"status": "error", "msg": "nilai sensor tidak valid"}), 400

    berhasil = simpan_response_time(
        request_id, status_uji, titik_akhir, waktu_respons_ms,
        suhu, ph, turbidity
    )
    return jsonify({
        "status": "ok" if berhasil else "error",
        "tersimpan": berhasil,
        "request_id": request_id
    }), 200 if berhasil else 500


# ================================================================
# ENDPOINT UTAMA — TERIMA DATA DARI ESP32
# ================================================================
@app.route("/data", methods=["POST"])
def terima_data():

    # 1 — Ambil JSON
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "msg": "invalid JSON"}), 400

    # 2 — Parse nilai sensor
    try:
        suhu = float(data["suhu"])
        ph   = float(data["ph"])
        turb = float(data["turbidity"])
        ts   = data.get("timestamp",
               datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        request_id = str(data.get(
            "request_id", f"ESP32-{int(time.time() * 1000)}"
        ))
    except (KeyError, ValueError) as e:
        return jsonify({"status": "error", "msg": f"data tidak valid: {e}"}), 400

    # 3 — Validasi range fisik
    if not (10 <= suhu <= 45):
        return jsonify({"status": "skip", "msg": "suhu di luar range"}), 200
    if not (0 <= ph <= 14):
        return jsonify({"status": "skip", "msg": "pH di luar range"}), 200
    if turb < 0:
        return jsonify({"status": "skip", "msg": "turbidity negatif"}), 200

    # 4 — Prediksi XGBoost (STATUS saja)
    if model is None:
        return jsonify({"status": "error", "msg": "model tidak tersedia"}), 500
    try:
        X         = [[suhu, ph, turb]]
        pred_enc  = model.predict(X)[0]
        prob_arr  = model.predict_proba(X)[0]
        label     = encoder.inverse_transform([pred_enc])[0]
        prob_dict = {kls: float(prob_arr[i]) for i, kls in enumerate(encoder.classes_)}
    except Exception as e:
        logger.error(f"Prediksi error: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500

    # 5 — Evaluasi kondisi (RULE-BASED multi-parameter, sesuai tabel laporan)
    #     XGBoost -> status prediksi; aturan -> penyebab + aktuator + arah.
    penyebab, aktuator, aksi_list, status_rule = evaluasi_kondisi(suhu, ph, turb)

    # Status operasional, notifikasi, dan aktuator mengikuti seluruh
    # rentang batas parameter. Prediksi XGBoost tetap disimpan untuk penelitian.
    # Ini mencegah Waspada palsu ketika semua nilai sebenarnya normal.
    status = status_rule

    logger.info(
        f"[{ts}] suhu={suhu} pH={ph} turb={turb} | XGBoost={label} "
        f"aturan={status_rule} -> final={status} | penyebab={penyebab}"
    )

    # 6 — Simpan ke Supabase (label XGBoost tetap disimpan sebagai prediksi)
    simpan_supabase(ts, suhu, ph, turb, label, prob_dict)

    # 7 — Notifikasi kondisi
    # Untuk Waspada nilai ini menjadi titik konfirmasi pengukuran ESP32.
    # Pada Bahaya nilainya juga dibuat sesuai hasil pengiriman agar respons
    # tidak lagi menampilkan false ketika notifikasi sebenarnya terkirim.
    telegram_terkirim = False
    if status == "Bahaya":
        telegram_terkirim = kirim_notif_kondisi(
            "Bahaya", suhu, ph, turb, penyebab
        )
    elif status == "Waspada":
        telegram_terkirim = kirim_notif_kondisi(
            "Waspada", suhu, ph, turb, penyebab
        )

    # 8 — Notifikasi aktuator: hanya nama aktuator dan status ON/OFF.
    # Setiap aktuator aktif tetap mendapat pesan tersendiri.
    proses_notif_aktuator(aktuator)

    # 9 — Respons ke ESP32
    return jsonify({
        "status":       "ok",
        "prediksi":     label,        # hasil XGBoost (untuk dashboard/thesis)
        "status_final": status,       # status yang benar-benar dipakai sistem
        "probabilitas": {k: round(v, 4) for k, v in prob_dict.items()},
        "penyebab":     penyebab,
        "aktuator":     aktuator,
        "dosing_detik": DOSING_DETIK,
        "timestamp":    ts,
        "request_id":   request_id,
        "telegram_terkirim": telegram_terkirim
    }), 200

# ================================================================
# ENDPOINT AKTUATOR MANUAL (opsional, dari Blynk/user)
# ================================================================
@app.route("/aktuator", methods=["POST"])
def aktuator_manual():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error"}), 400

    nama   = data.get("nama", "unknown")
    state  = data.get("state", False)
    pesan = (
        "KONTROL MANUAL\n"
        f"Aktuator : {nama}\n"
        f"Mode     : Manual\n"
        f"Status   : {'ON' if state else 'OFF'}"
    )
    kirim_telegram_pesan(pesan)
    return jsonify({"status": "ok"}), 200

# ================================================================
# ENDPOINT STATUS
# ================================================================
@app.route("/status", methods=["GET"])
def status():
    supabase_ok = False
    total_data  = 0
    try:
        url     = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?select=count"
        headers = {
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Prefer":        "count=exact"
        }
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            supabase_ok = True
            cr = resp.headers.get("Content-Range", "0/0")
            total_data = int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        pass

    return jsonify({
        "status":       "online",
        "model":        "loaded" if model else "error",
        "database":     "supabase — OK" if supabase_ok else "supabase — error",
        "total_data":   total_data,
        "waktu_server": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    }), 200

# ================================================================
# ENDPOINT DATA TERBARU
# ================================================================
@app.route("/last", methods=["GET"])
def last_data():
    try:
        url     = (f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
                   f"?select=*&order=created_at.desc&limit=1")
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            hasil = resp.json()
            return jsonify(hasil[0] if hasil else {"msg": "belum ada data"}), 200
        return jsonify({"error": resp.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================================================================
# ENDPOINT HISTORY
# ================================================================
@app.route("/history", methods=["GET"])
def history():
    limit = request.args.get("limit", 50)
    try:
        url     = (f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
                   f"?select=*&order=created_at.desc&limit={limit}")
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json()), 200
        return jsonify({"error": resp.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================================================================
# JALANKAN SERVER
# ================================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
