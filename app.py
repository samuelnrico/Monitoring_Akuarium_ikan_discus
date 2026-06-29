"""
Server Flask Final — Sistem Peringatan Dini Kualitas Air Ikan Discus
Database  : Supabase (PostgreSQL)
Deploy    : Render.com
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

COOLDOWN_DETIK = 300
MODEL_FILE     = "model.pkl"

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
        saved   = pickle.load(f)
    model   = saved["model"]
    encoder = saved["encoder"]
    logger.info(f"Model loaded — kelas: {list(encoder.classes_)}")
except Exception as e:
    logger.error(f"Gagal load model: {e}")
    model, encoder = None, None

# ================================================================
# STATE COOLDOWN NOTIFIKASI
# ================================================================
last_notif_time = {}
notif_count     = {}

# ================================================================
# FUNGSI SIMPAN KE SUPABASE
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
        else:
            logger.error(f"[Supabase] Gagal: {resp.status_code} — {resp.text}")
            return False
    except Exception as e:
        logger.error(f"[Supabase] Error: {e}")
        return False

# ================================================================
# FUNGSI KIRIM TELEGRAM
# ================================================================
def kirim_telegram(label, suhu, ph, turb, prob):
    global last_notif_time

    sekarang = datetime.now().timestamp()
    terakhir = last_notif_time.get(label, 0)

    if sekarang - terakhir < COOLDOWN_DETIK:
        sisa = int(COOLDOWN_DETIK - (sekarang - terakhir))
        logger.info(f"[Telegram] Cooldown {label} — {sisa}s tersisa")
        return False

    notif_count[label] = notif_count.get(label, 0) + 1
    waktu_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    emoji = "⚠️" if label == "Waspada" else "🚨"
    judul = "PERINGATAN KUALITAS AIR" if label == "Waspada" else "BAHAYA KUALITAS AIR"

    pesan = (
        f"{emoji} *{judul}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🌡 Suhu      : *{suhu:.2f} °C*\n"
        f"🧪 pH        : *{ph:.3f}*\n"
        f"💧 Turbidity : *{turb:.2f} NTU*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 Probabilitas:\n"
        f"   Aman     : {prob.get('Aman',0)*100:.1f}%\n"
        f"   Waspada  : {prob.get('Waspada',0)*100:.1f}%\n"
        f"   Bahaya   : {prob.get('Bahaya',0)*100:.1f}%\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕐 Waktu : {waktu_str}\n"
        f"⚡ Alert ke-{notif_count[label]}"
    )

    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id":    CHAT_ID,
            "text":       pesan,
            "parse_mode": "Markdown"
        }, timeout=10)
        if resp.status_code == 200:
            last_notif_time[label] = sekarang
            logger.info(f"[Telegram] Alert {label} terkirim")
            return True
        else:
            logger.error(f"[Telegram] Gagal: {resp.text}")
            return False
    except Exception as e:
        logger.error(f"[Telegram] Error: {e}")
        return False

# ================================================================
# ENDPOINT UTAMA — TERIMA DATA DARI ESP32
# ================================================================
@app.route("/data", methods=["POST"])
def terima_data():

    # 1 — Ambil JSON dari ESP32
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

    # 4 — Prediksi XGBoost
    if model is None:
        return jsonify({"status": "error", "msg": "model tidak tersedia"}), 500

    try:
        X        = [[suhu, ph, turb]]
        pred_enc = model.predict(X)[0]
        prob_arr = model.predict_proba(X)[0]
        label    = encoder.inverse_transform([pred_enc])[0]
        prob_dict = {
            kls: float(prob_arr[i])
            for i, kls in enumerate(encoder.classes_)
        }
    except Exception as e:
        logger.error(f"Prediksi error: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500

    logger.info(
        f"[{ts}] suhu={suhu} pH={ph} turb={turb} → {label} "
        f"(A:{prob_dict.get('Aman',0):.2f} "
        f"W:{prob_dict.get('Waspada',0):.2f} "
        f"B:{prob_dict.get('Bahaya',0):.2f})"
    )

    # 5 — Simpan ke Supabase (data sensor + hasil prediksi)
    simpan_supabase(ts, suhu, ph, turb, label, prob_dict)

    # 6 — Tentukan aksi berdasarkan label
    notif_terkirim = False
    if label == "Waspada":
        notif_terkirim = kirim_telegram(label, suhu, ph, turb, prob_dict)
    elif label == "Bahaya":
        notif_terkirim = kirim_telegram(label, suhu, ph, turb, prob_dict)

    # 7 — Tentukan perintah aktuator untuk ESP32
    aktuator = {
        "pompa_kuras": label == "Bahaya",
        "pompa_isi":   label == "Bahaya",
        "heater":      suhu < 25.0,
        "filter":      label in ["Waspada", "Bahaya"]
    }

    # 8 — Kembalikan respons ke ESP32
    return jsonify({
        "status":         "ok",
        "prediksi":       label,
        "probabilitas":   {k: round(v, 4) for k, v in prob_dict.items()},
        "aktuator":       aktuator,
        "notif_terkirim": notif_terkirim,
        "timestamp":      ts
    }), 200

# ================================================================
# ENDPOINT STATUS SERVER
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
        headers = {
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}"
        }
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
        headers = {
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}"
        }
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
