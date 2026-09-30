/*
 * Sistem Peringatan Dini Kualitas Air Ikan Discus — KODE FINAL (Revisi)
 * Hardware : ESP32 DOIT DevKit V1
 * ADC      : ADS1115 (I2C addr 0x48) — SDA=GPIO21, SCL=GPIO22
 * pH       : pH-4502C → ADS1115 A0
 * Turbidity: SEN0189  → ADS1115 A1
 * Suhu     : DS18B20  → GPIO4 (One-Wire)
 * Relay    : GPIO25 = Pompa buffer ASAM (turunkan pH saat air terlalu basa)
 *            GPIO26 = Pompa buffer BASA (naikkan pH saat air terlalu asam)
 *            GPIO27 = Heater
 * Interval : 2 menit
 * Server   : Flask di Render.com (HTTP POST JSON)
 *
 */

#define BLYNK_TEMPLATE_ID   "TMPL6kwa9KGqn"
#define BLYNK_TEMPLATE_NAME "Monitoring Kualitas Air Akuarium"
#define BLYNK_AUTH_TOKEN    "kQpLrM41sJyTOC9eyMnW6IL6Yh3rjkUy"

#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Adafruit_ADS1X15.h>
#include <time.h>
#include <BlynkSimpleEsp32.h>

// ╔══════════════════════════════════════════════════╗
// ║              KONFIGURASI UTAMA                  ║
// ╚══════════════════════════════════════════════════╝
//   Nita11
// 1234567890 
// Student
// tourTech@2024
const char* WIFI_SSID     = "B3 - 14";
const char* WIFI_PASSWORD = "12345678";
char auth[]               = BLYNK_AUTH_TOKEN;

const char* SERVER_URL    = "https://monitoring-akuarium-ikan-discus.onrender.com/data";

const long  GMT_OFFSET_SEC = 7 * 3600; // WIB UTC+7

// ╔══════════════════════════════════════════════════╗
// ║              KONSTANTA PIN & SISTEM             ║
// ╚══════════════════════════════════════════════════╝

#define PIN_DS18B20    4
#define ADS_ADDR       0x48
#define ADS_CH_PH      0
#define ADS_CH_TURB    1

// Relay aktif LOW — HIGH = OFF, LOW = ON
#define RELAY_ASAM    25   // Pompa buffer ASAM (menurunkan pH)
#define RELAY_BASA    26   // Pompa buffer BASA (menaikkan pH)
#define RELAY_HEATER  27   // Heater

const unsigned long INTERVAL_MS    = 2UL * 60 * 1000; // 2 menit
const int           RETRY_WIFI_MAX = 30;
const int           DOSING_DETIK_DEFAULT = 5; // fallback jika server tak kirim

// ╔══════════════════════════════════════════════════╗
// ║         VARIABEL DELTA CHECK (FILTER SPIKE)     ║
// ╚══════════════════════════════════════════════════╝

// float ph_sebelumnya   = 7.0;
// float turb_sebelumnya = 30.0;
// bool  data_pertama    = true;

// ╔══════════════════════════════════════════════════╗
// ║           VARIABEL KALIBRASI pH                 ║
// ╚══════════════════════════════════════════════════╝

float calibration_value = 22.44;
int16_t buffer_arr[10];
unsigned long int avgval;
float ph_act;

// ╔══════════════════════════════════════════════════╗
// ║           STATUS RELAY MANUAL BLYNK             ║
// ╚══════════════════════════════════════════════════╝

bool manual_asam   = false;
bool manual_basa   = false;
bool manual_heater = false;

// Timestamp terakhir kirim
unsigned long waktu_terakhir = 0;
unsigned long nomor_pengujian = 0;

// Objek sensor
OneWire           oneWire(PIN_DS18B20);
DallasTemperature ds18b20(&oneWire);
Adafruit_ADS1115  ads;

// ════════════════════════════════════════════════════
// FUNGSI KONTROL RELAY
// ════════════════════════════════════════════════════

void setRelay(int pin, bool nyala) {
  // Relay aktif LOW: nyala=true → LOW, nyala=false → HIGH
  digitalWrite(pin, nyala ? LOW : HIGH);
}

void relaySemuaOff() {
  setRelay(RELAY_ASAM,   false);
  setRelay(RELAY_BASA,   false);
  setRelay(RELAY_HEATER, false);
}

// Dosing pompa buffer: pulsa nyala selama `detik` lalu mati kembali.
// Ini yang membuat pompa hanya menetes sebentar (mis. 5 detik) tiap
// pembacaan, memberi waktu air tercampur & pH terbaca ulang.
unsigned long dosingPompa(int pin, int detik) {
  if (detik <= 0) detik = DOSING_DETIK_DEFAULT;
  Serial.printf("[DOSING] Pin %d ON selama %d detik...\n", pin, detik);
  setRelay(pin, true);

  // Waktu ini adalah titik akhir pengujian Bahaya: relay mulai ON.
  unsigned long waktuAktif = millis();

  delay((unsigned long)detik * 1000UL);
  setRelay(pin, false);
  Serial.printf("[DOSING] Pin %d OFF.\n", pin);
  return waktuAktif;
}

// ════════════════════════════════════════════════════
// KONTROL RELAY MANUAL DARI BLYNK (untuk testing)
// Setiap perubahan ON/OFF dikirim ke endpoint /aktuator agar Flask
// meneruskan notifikasi manual ke Telegram.
// ════════════════════════════════════════════════════

void kirimNotifAktuatorManual(const char* namaAktuator, bool state) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[MANUAL] Notifikasi gagal: WiFi tidak terhubung");
    return;
  }

  String manualUrl = String(SERVER_URL);
  int posisiData = manualUrl.lastIndexOf("/data");
  if (posisiData >= 0) {
    manualUrl = manualUrl.substring(0, posisiData) + "/aktuator";
  } else {
    manualUrl += "/aktuator";
  }

  StaticJsonDocument<192> docManual;
  docManual["nama"] = namaAktuator;
  docManual["state"] = state;

  String payloadManual;
  serializeJson(docManual, payloadManual);

  HTTPClient httpManual;
  httpManual.begin(manualUrl);
  httpManual.addHeader("Content-Type", "application/json");
  httpManual.setTimeout(15000);
  int kodeManual = httpManual.POST(payloadManual);
  String responsManual = kodeManual > 0 ? httpManual.getString() : "";
  httpManual.end();

  if (kodeManual == 200) {
    Serial.printf(
      "[MANUAL] Notifikasi Telegram terkirim | %s | %s\n",
      namaAktuator, state ? "ON" : "OFF"
    );
  } else {
    Serial.printf(
      "[MANUAL] Notifikasi gagal | HTTP=%d | Respons=%s\n",
      kodeManual, responsManual.c_str()
    );
  }
}

BLYNK_WRITE(V4) {
  bool stateBaru = param.asInt() == 1;
  bool berubah = manual_asam != stateBaru;
  setRelay(RELAY_ASAM, stateBaru);
  manual_asam = stateBaru;
  Serial.printf("[Blynk] Pompa Asam: %s\n", stateBaru ? "ON" : "OFF");
  if (berubah) {
    kirimNotifAktuatorManual("Pompa buffer asam", stateBaru);
  }
}

BLYNK_WRITE(V5) {
  bool stateBaru = param.asInt() == 1;
  bool berubah = manual_basa != stateBaru;
  setRelay(RELAY_BASA, stateBaru);
  manual_basa = stateBaru;
  Serial.printf("[Blynk] Pompa Basa: %s\n", stateBaru ? "ON" : "OFF");
  if (berubah) {
    kirimNotifAktuatorManual("Pompa buffer basa", stateBaru);
  }
}

BLYNK_WRITE(V6) {
  bool stateBaru = param.asInt() == 1;
  bool berubah = manual_heater != stateBaru;
  setRelay(RELAY_HEATER, stateBaru);
  manual_heater = stateBaru;
  Serial.printf("[Blynk] Heater: %s\n", stateBaru ? "ON" : "OFF");
  if (berubah) {
    kirimNotifAktuatorManual("Heater", stateBaru);
  }
}

// ════════════════════════════════════════════════════
// FUNGSI BACA SENSOR
// ════════════════════════════════════════════════════

float bacaSuhu() {
  ds18b20.requestTemperatures();
  delay(750);
  float t = ds18b20.getTempCByIndex(0);
  if (t == DEVICE_DISCONNECTED_C || t == -127.0) {
    Serial.println("[ERROR] DS18B20 disconnect!");
    return -999.0;
  }
  return t;
}

float bacaPH() {
  for (int i = 0; i < 10; i++) {
    buffer_arr[i] = ads.readADC_SingleEnded(ADS_CH_PH);
    delay(50);
  }
  for (int i = 0; i < 9; i++) {
    for (int j = i + 1; j < 10; j++) {
      if (buffer_arr[i] > buffer_arr[j]) {
        int16_t tmp   = buffer_arr[i];
        buffer_arr[i] = buffer_arr[j];
        buffer_arr[j] = tmp;
      }
    }
  }
  avgval = 0;
  for (int i = 2; i < 8; i++) avgval += buffer_arr[i];

  float volt = ((float)avgval / 6.0) * 0.000125;
  ph_act = -6.06 * volt + calibration_value;
  ph_act = constrain(ph_act, 0.0, 14.0);

  Serial.printf("  pH  : voltage=%.4fV, pH=%.3f\n", volt, ph_act);
  return ph_act;
}

float bacaTurbidity() {
  long totalADC = 0;
  for (int i = 0; i < 100; i++) {
    totalADC += ads.readADC_SingleEnded(ADS_CH_TURB);
    delay(5);
  }
  float adcAvg  = totalADC / 100.0;
  float voltage = adcAvg * 0.000125;
  float ntu = (-100.0 * voltage) + 390.0;
  if (ntu < 0)
    ntu = 0;

  Serial.printf("  Turb: voltage=%.4fV, NTU=%.2f\n", voltage, ntu);
  return ntu;
}

String getTimestamp() {
  struct tm timeinfo;
  if (getLocalTime(&timeinfo)) {
    char buf[25];
    strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &timeinfo);
    return String(buf);
  }
  return String("NO-NTP-") + String(millis());
}

// ════════════════════════════════════════════════════
// KIRIM HASIL PENGUJIAN WAKTU RESPONS KE FLASK
// Pemanggilan dilakukan SETELAH waktu akhir dicatat, sehingga proses
// penyimpanan tidak menambah nilai waktu respons.
// ════════════════════════════════════════════════════
void kirimLogResponseTime(
  const String &requestId,
  const String &statusUji,
  const String &jenisRespons,
  unsigned long waktuResponsMs,
  float suhu,
  float ph,
  float turbidity
) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[RESPONSE TIME] WiFi terputus, mencoba reconnect...");
    hubungWiFi();
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[RESPONSE TIME] Gagal simpan: WiFi tidak terhubung");
    return;
  }

  String logUrl = String(SERVER_URL);
  int posisiData = logUrl.lastIndexOf("/data");
  if (posisiData >= 0) {
    logUrl = logUrl.substring(0, posisiData) + "/response-time";
  } else {
    logUrl += "/response-time";
  }

  StaticJsonDocument<384> logDoc;
  logDoc["request_id"] = requestId;
  logDoc["status"] = statusUji;
  logDoc["titik_akhir"] = jenisRespons;
  logDoc["waktu_respons_ms"] = waktuResponsMs;
  logDoc["suhu"] = round(suhu * 100.0) / 100.0;
  logDoc["ph"] = round(ph * 1000.0) / 1000.0;
  logDoc["turbidity"] = round(turbidity * 100.0) / 100.0;

  String logPayload;
  serializeJson(logDoc, logPayload);

  // Maksimal tiga kali percobaan. Retry dilakukan setelah waktu akhir
  // dicatat, sehingga tidak mengubah nilai pengukuran.
  for (int percobaan = 1; percobaan <= 3; percobaan++) {
    HTTPClient logHttp;
    logHttp.begin(logUrl);
    logHttp.addHeader("Content-Type", "application/json");
    logHttp.setTimeout(15000);
    int kodeLog = logHttp.POST(logPayload);
    String responsLog = kodeLog > 0 ? logHttp.getString() : "";
    logHttp.end();

    if (kodeLog == 200) {
      Serial.printf(
        "[RESPONSE TIME] Tersimpan | ID=%s | Status=%s | Target=%s | Waktu=%lu ms\n",
        requestId.c_str(), statusUji.c_str(), jenisRespons.c_str(), waktuResponsMs
      );
      return;
    }

    Serial.printf(
      "[RESPONSE TIME] Percobaan simpan %d gagal | HTTP=%d | Respons=%s\n",
      percobaan, kodeLog, responsLog.c_str()
    );
    delay(1000);
  }

  Serial.println("[RESPONSE TIME] Gagal tersimpan setelah 3 percobaan");
}

// ════════════════════════════════════════════════════
// FUNGSI UTAMA: BACA SENSOR → KIRIM → PROSES RESPONS
// ════════════════════════════════════════════════════

void bacaDanKirim() {
  // T0: titik awal satu-satunya untuk Waspada dan Bahaya.
  // Dicatat sebelum pembacaan sensor pertama dimulai.
  unsigned long waktuMulaiRespons = millis();
  nomor_pengujian++;

  Serial.println("\n--- Membaca sensor ---");

float suhu = bacaSuhu();
float ph   = bacaPH();
float turb = bacaTurbidity();
String ts  = getTimestamp();

// ID unik menggabungkan timestamp, millis awal, dan nomor siklus.
String requestId = String("ESP32-") + ts + String("-") +
                   String(waktuMulaiRespons) + String("-") +
                   String(nomor_pengujian);
requestId.replace(":", "");
Serial.printf("Request ID : %s\n", requestId.c_str());
// ==========================
// DELTA CHECK DENGAN RETRY
// ==========================
// if (!data_pertama) {

//     const byte MAX_RETRY = 5;
//     byte retry = 0;

//     while (retry < MAX_RETRY) {

//         float deltaPH   = abs(ph - ph_sebelumnya);
//         float deltaTurb = abs(turb - turb_sebelumnya);

//         bool lonjakan = false;

//         if (deltaPH > 1.5)
//             lonjakan = true;

//         if (deltaTurb > 30)
//             lonjakan = true;

//         // Data sudah normal
//         if (!lonjakan) {
//             break;
//         }

//         retry++;

//         Serial.printf(
//             "[RETRY %d/%d] Lonjakan terdeteksi (ΔpH=%.2f | ΔTurb=%.2f)\n",
//             retry,
//             MAX_RETRY,
//             deltaPH,
//             deltaTurb
//         );

//         delay(1000);

//         // Baca ulang semua sensor
//         suhu = bacaSuhu();
//         ph   = bacaPH();
//         turb = bacaTurbidity();
//         ts   = getTimestamp();
//     }

//     // Setelah 5 kali masih lonjakan
//     if (retry >= MAX_RETRY) {
//         Serial.println("[SKIP] Data tidak stabil setelah 5 kali pembacaan.");
//         return;
//     }
// }
  // ph_sebelumnya   = ph;
  // turb_sebelumnya = turb;
  // data_pertama    = false;

  // Kirim ke Blynk dashboard
  Blynk.virtualWrite(V1, ph);
  Blynk.virtualWrite(V2, suhu);
  Blynk.virtualWrite(V3, turb);

  Serial.println("--- Hasil Bacaan ---");
  Serial.printf("Timestamp : %s\n", ts.c_str());
  Serial.printf("Suhu      : %.2f °C\n", suhu);
  Serial.printf("pH        : %.3f\n", ph);
  Serial.printf("Turbidity : %.2f NTU\n", turb);

  // Payload JSON
  StaticJsonDocument<384> doc;
  doc["timestamp"]  = ts;
  doc["request_id"] = requestId;
  doc["suhu"]       = round(suhu * 100.0) / 100.0;
  doc["ph"]         = round(ph   * 1000.0) / 1000.0;
  doc["turbidity"]  = round(turb * 100.0) / 100.0;

  String payload;
  serializeJson(doc, payload);
  Serial.printf("Payload   : %s\n", payload.c_str());

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] Terputus, reconnect...");
    hubungWiFi();
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[ERR] WiFi gagal. Data tidak dikirim.");
    return;
  }

  // ── KIRIM KE SERVER FLASK ──
  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(15000);

  int kode = http.POST(payload);

  if (kode == 200) {
    String respStr = http.getString();
    Serial.printf("[OK] Respons: %s\n", respStr.c_str());
    http.end();  // tutup koneksi dulu sebelum dosing (delay panjang)

    // ── PARSE RESPONS JSON ──
    StaticJsonDocument<1024> resp;
    DeserializationError err = deserializeJson(resp, respStr);

    if (!err) {
      const char* prediksi = resp["prediksi"] | "Aman";
      const char* statusFinal = resp["status_final"] | prediksi;
      const char* penyebab = resp["penyebab"] | "-";
      bool telegramTerkirim = resp["telegram_terkirim"] | false;
      int dosing_detik = resp["dosing_detik"] | DOSING_DETIK_DEFAULT;

      Serial.printf(
        "[MODEL] Prediksi: %s | Status final: %s | Penyebab: %s\n",
        prediksi, statusFinal, penyebab
      );

      bool waktuResponsSiap = false;
      String jenisRespons = "";
      unsigned long waktuResponsMs = 0;

      // Titik akhir Waspada: ESP32 menerima konfirmasi bahwa Telegram API
      // berhasil menerima notifikasi.
      if (String(statusFinal) == "Waspada") {
        if (telegramTerkirim) {
          waktuResponsMs = millis() - waktuMulaiRespons;
          jenisRespons = "Telegram";
          waktuResponsSiap = true;
        } else {
          Serial.println(
            "[RESPONSE TIME] Waspada tidak dicatat karena Telegram gagal"
          );
        }
      }

      if (resp.containsKey("probabilitas")) {
        Serial.printf("  P(Aman)    : %.2f%%\n", (float)resp["probabilitas"]["Aman"]    * 100);
        Serial.printf("  P(Waspada) : %.2f%%\n", (float)resp["probabilitas"]["Waspada"] * 100);
        Serial.printf("  P(Bahaya)  : %.2f%%\n", (float)resp["probabilitas"]["Bahaya"]  * 100);
      }

      // ── KONTROL AKTUATOR OTOMATIS ──
      if (resp.containsKey("aktuator")) {
        bool a_asam = resp["aktuator"]["pompa_asam"] | false;
        bool a_basa = resp["aktuator"]["pompa_basa"] | false;
        bool a_heat = resp["aktuator"]["heater"]     | false;

        Serial.println("[AKTUATOR] Perintah server:");
        Serial.printf("  Pompa Asam : %s\n", a_asam ? "DOSING" : "OFF");
        Serial.printf("  Pompa Basa : %s\n", a_basa ? "DOSING" : "OFF");
        Serial.printf("  Heater     : %s\n", a_heat ? "ON" : "OFF");

        // Catat hanya aktuator PERTAMA yang mulai aktif. Pengujian
        // Bahaya tidak dibedakan berdasarkan tiga jenis aktuator.
        unsigned long waktuAktuatorPertama = 0;

        // Heater = kontinu (nyala/mati langsung mengikuti server)
        if (a_heat) {
          setRelay(RELAY_HEATER, true);
          waktuAktuatorPertama = millis();
        } else {
          setRelay(RELAY_HEATER, false);
        }
        Blynk.virtualWrite(V6, a_heat ? 1 : 0);

        // Pompa buffer = DOSING (pulsa singkat lalu mati)
        if (a_basa) {
          Blynk.virtualWrite(V5, 1);
          unsigned long waktuPompaBasa = dosingPompa(RELAY_BASA, dosing_detik);
          if (waktuAktuatorPertama == 0) waktuAktuatorPertama = waktuPompaBasa;
          Blynk.virtualWrite(V5, 0);
        }
        if (a_asam) {
          Blynk.virtualWrite(V4, 1);
          unsigned long waktuPompaAsam = dosingPompa(RELAY_ASAM, dosing_detik);
          if (waktuAktuatorPertama == 0) waktuAktuatorPertama = waktuPompaAsam;
          Blynk.virtualWrite(V4, 0);
        }

        // Titik akhir Bahaya: aktuator pertama menerima perintah relay ON.
        if (String(statusFinal) == "Bahaya") {
          if (waktuAktuatorPertama > 0) {
            waktuResponsMs = waktuAktuatorPertama - waktuMulaiRespons;
            jenisRespons = "Aktuator";
            waktuResponsSiap = true;
          } else {
            Serial.println(
              "[RESPONSE TIME] Bahaya tidak dicatat karena tidak ada aktuator aktif"
            );
          }
        }
      }

      // Simpan otomatis hanya untuk Waspada atau Bahaya yang mencapai
      // titik akhir pengujian masing-masing.
      if (waktuResponsSiap) {
        kirimLogResponseTime(
          requestId, String(statusFinal), jenisRespons, waktuResponsMs,
          suhu, ph, turb
        );
      }
    } else {
      Serial.printf("[WARN] Parse JSON gagal: %s\n", err.c_str());
    }

  } else {
    Serial.printf("[ERR] HTTP %d\n", kode);
    if (kode < 0) {
      Serial.println("[INFO] Kemungkinan server cold start, coba lagi siklus berikutnya...");
    }
    http.end();
  }
}

// ════════════════════════════════════════════════════
// FUNGSI KONEKSI WIFI
// ════════════════════════════════════════════════════

void hubungWiFi() {
  Serial.printf("[WiFi] Menghubungkan ke '%s'", WIFI_SSID);
  WiFi.disconnect(true);
  delay(500);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int n = 0;
  while (WiFi.status() != WL_CONNECTED && n < RETRY_WIFI_MAX) {
    delay(500); Serial.print("."); n++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[OK] IP ESP32: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\n[ERR] Gagal koneksi WiFi!");
  }
}

// ════════════════════════════════════════════════════
// SETUP
// ════════════════════════════════════════════════════

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n=== Sistem Peringatan Dini Kualitas Air Discus ===");
  Serial.println("Versi FINAL Revisi — XGBoost + Pompa Buffer Dosing");

  Wire.begin(21, 22);

  if (!ads.begin(ADS_ADDR)) {
    Serial.println("[ERROR] ADS1115 tidak ditemukan!");
    while (1) { delay(1000); }
  }
  ads.setGain(GAIN_ONE);
  Serial.println("[OK] ADS1115 — GAIN_ONE");

  ds18b20.begin();
  Serial.printf("[OK] DS18B20: %d sensor\n", ds18b20.getDeviceCount());

  pinMode(RELAY_ASAM,   OUTPUT);
  pinMode(RELAY_BASA,   OUTPUT);
  pinMode(RELAY_HEATER, OUTPUT);
  relaySemuaOff();
  Serial.println("[OK] Relay diinisialisasi — semua OFF");

  hubungWiFi();

  Blynk.config(auth);
  Blynk.connect();
  Serial.println("[OK] Blynk terhubung");

  configTime(GMT_OFFSET_SEC, 0, "pool.ntp.org", "time.google.com");
  Serial.print("[...] Sinkronisasi NTP");
  struct tm timeinfo;
  int ntp_try = 0;
  while (!getLocalTime(&timeinfo) && ntp_try < 20) {
    Serial.print("."); delay(500); ntp_try++;
  }
  Serial.println(getLocalTime(&timeinfo) ? " OK" : " GAGAL");

  Serial.println("=================================================");
  Serial.println("Sistem siap. Baca pertama dimulai...");
  delay(2000);

  bacaDanKirim();
  waktu_terakhir = millis();
}

// ════════════════════════════════════════════════════
// LOOP
// ════════════════════════════════════════════════════

void loop() {
  Blynk.run();

  unsigned long skrg = millis();
  if (skrg - waktu_terakhir >= INTERVAL_MS) {
    waktu_terakhir = skrg;
    bacaDanKirim();
  }
}
