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
  5. Notifikasi aktuator hanya dikirim saat TERJADI PERUBAHAN status
     logis aktuator (mulai dosing / berhenti dosing), bukan tiap pulsa.
================================================================
"""

from flask import Flask, request, jsonify
import pickle, logging, requests
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

MODEL_FILE = "model.pkl"

# ----------------------------------------------------------------
# AMBANG BATAS (RULE-BASED) — SESUAI TABEL LAPORAN
#
#   Parameter   | Aman        | Waspada                | Bahaya
#   Suhu (°C)   | 25.0-31.0   | 20.0-24.9 / 31.1-33.0  | <20.0 / >33.0
#   pH          | 6.0-7.0     | 5.5-5.9 / 7.1-7.5      | <5.5  / >7.5
#   Turbidity   | 0-25        | ~25                    | >25
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
TURB_BAHAYA        = 25.0  # > 25 -> melewati batas (tidak ada aktuator di HW ini)

DOSING_DETIK       = 5     # durasi pulsa pompa buffer (detik) — dieksekusi ESP32

# Tingkat keparahan untuk membandingkan status
RANK = {"Aman": 0, "Waspada": 1, "Bahaya": 2}

# Cooldown notifikasi kondisi (detik)
#   Bahaya  = 0   -> selalu kirim setiap deteksi (sesuai kebutuhanmu)
#   Waspada = 120 -> maksimal tiap 2 menit
COOLDOWN = {
    "Bahaya":  0,
    "Waspada": 120
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

# ================================================================
# FUNGSI KIRIM TELEGRAM (generik)
# parse_mode HTML + <pre> supaya perataan kolom (spasi) rapi
# ================================================================
def kirim_telegram_pesan(pesan, monospace=True):
    try:
        teks = f"<pre>{pesan}</pre>" if monospace else pesan
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id":    CHAT_ID,
            "text":       teks,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code == 200:
            logger.info("[Telegram] Pesan terkirim")
            return True
        logger.error(f"[Telegram] Gagal: {resp.text}")
        return False
    except Exception as e:
        logger.error(f"[Telegram] Error: {e}")
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
        f"Turbidity : {turb:.2f}\n"
        f"Penyebab  : {penyebab}\n"
        "Tindakan  : Sistem mengirim peringatan kepada pengguna"
    )

def pesan_kondisi_bahaya(suhu, ph, turb, penyebab, aksi):
    return (
        "PERINGATAN KUALITAS AIR\n"
        "Status     : BAHAYA\n"
        f"Suhu       : {suhu:.2f} °C\n"
        f"pH         : {ph:.2f}\n"
        f"Turbidity  : {turb:.2f}\n"
        f"Penyebab   : {penyebab}\n"
        f"Aksi       : {aksi}"
    )

def pesan_aktuator_on(nama_aktuator, pemicu):
    return (
        "AKTUATOR AKTIF\n"
        f"Aktuator   : {nama_aktuator}\n"
        f"Pemicu     : {pemicu}\n"
        "Mode       : Otomatis\n"
        "Status     : ON"
    )

def pesan_aktuator_off(nama_aktuator):
    return (
        "AKTUATOR NONAKTIF\n"
        f"Aktuator   : {nama_aktuator}\n"
        "Mode       : Otomatis\n"
        "Status     : OFF"
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
    penyebab = []   # daftar semua masalah terdeteksi
    aksi     = []   # daftar nama aktuator yang diaktifkan
    aktuator = {"pompa_asam": False, "pompa_basa": False, "heater": False}
    rank     = 0    # 0=Aman, 1=Waspada, 2=Bahaya

    # ---------------- pH ----------------
    if ph > PH_BAHAYA_BASA:              # > 7.5  -> BAHAYA (basa)
        penyebab.append("pH terlalu basa")
        aktuator["pompa_asam"] = True
        aksi.append("Pompa buffer asam")
        rank = max(rank, 2)
    elif ph < PH_BAHAYA_ASAM:            # < 5.5  -> BAHAYA (asam)
        penyebab.append("pH terlalu asam")
        aktuator["pompa_basa"] = True
        aksi.append("Pompa buffer basa")
        rank = max(rank, 2)
    elif ph > PH_AMAN_MAX:               # 7.1 - 7.5 -> WASPADA
        penyebab.append("pH mulai menyimpang (cenderung basa)")
        rank = max(rank, 1)
    elif ph < PH_AMAN_MIN:               # 5.5 - 5.9 -> WASPADA
        penyebab.append("pH mulai menyimpang (cenderung asam)")
        rank = max(rank, 1)

    # ---------------- Suhu ----------------
    if suhu > SUHU_BAHAYA_PANAS:         # > 33 -> BAHAYA (panas, tak ada cooler)
        penyebab.append("Suhu terlalu panas")
        rank = max(rank, 2)
    elif suhu < SUHU_BAHAYA_DINGIN:      # < 20 -> BAHAYA (dingin)
        penyebab.append("Suhu terlalu dingin")
        aktuator["heater"] = True
        aksi.append("Heater")
        rank = max(rank, 2)
    elif suhu > SUHU_AMAN_MAX:           # 31.1 - 33 -> WASPADA
        penyebab.append("Suhu mulai menyimpang (panas)")
        rank = max(rank, 1)
    elif suhu < SUHU_AMAN_MIN:           # 20 - 24.9 -> WASPADA
        penyebab.append("Suhu mulai menyimpang (dingin)")
        rank = max(rank, 1)

    # ---------------- Turbidity ----------------
    if turb > TURB_BAHAYA:               # > 25 -> BAHAYA (tak ada aktuator)
        penyebab.append("Turbidity melewati batas")
        rank = max(rank, 2)

    status_rule  = ["Aman", "Waspada", "Bahaya"][rank]
    penyebab_str = "; ".join(penyebab) if penyebab else "Semua parameter normal"
    return penyebab_str, aktuator, aksi, status_rule

# ================================================================
# NOTIFIKASI KONDISI (dengan cooldown per label)
# ================================================================
def kirim_notif_kondisi(label, suhu, ph, turb, penyebab, aksi):
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
        pesan = pesan_kondisi_bahaya(suhu, ph, turb, penyebab, aksi)

    if kirim_telegram_pesan(pesan):
        last_notif_time[label] = sekarang
        return True
    return False

# ================================================================
# NOTIFIKASI AKTUATOR — hanya saat TRANSISI status logis
# ================================================================
def proses_notif_aktuator(aktuator_baru, pemicu):
    for key, val in aktuator_baru.items():
        lama = status_aktuator.get(key, False)
        if val and not lama:
            kirim_telegram_pesan(pesan_aktuator_on(NAMA_AKTUATOR[key], pemicu))
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

    # Status final = tingkat PALING PARAH antara prediksi XGBoost & aturan,
    # supaya sistem tidak pernah UNDER-warning.
    status = label if RANK.get(label, 0) >= RANK[status_rule] else status_rule

    logger.info(
        f"[{ts}] suhu={suhu} pH={ph} turb={turb} | XGBoost={label} "
        f"aturan={status_rule} -> final={status} | penyebab={penyebab}"
    )

    # 6 — Simpan ke Supabase (label XGBoost tetap disimpan sebagai prediksi)
    simpan_supabase(ts, suhu, ph, turb, label, prob_dict)

    # 7 — Notifikasi kondisi
    if status == "Bahaya":
        aksi = (", ".join(aksi_list) + " diaktifkan") if aksi_list \
               else "Sistem mengirim peringatan kepada pengguna"
        kirim_notif_kondisi("Bahaya", suhu, ph, turb, penyebab, aksi)
    elif status == "Waspada":
        kirim_notif_kondisi("Waspada", suhu, ph, turb, penyebab, None)

    # 8 — Notifikasi aktuator (hanya saat transisi ON/OFF)
    proses_notif_aktuator(aktuator, penyebab)

    # 9 — Respons ke ESP32
    return jsonify({
        "status":       "ok",
        "prediksi":     label,        # hasil XGBoost (untuk dashboard/thesis)
        "status_final": status,       # status yang benar-benar dipakai sistem
        "probabilitas": {k: round(v, 4) for k, v in prob_dict.items()},
        "penyebab":     penyebab,
        "aktuator":     aktuator,
        "dosing_detik": DOSING_DETIK,
        "timestamp":    ts
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
