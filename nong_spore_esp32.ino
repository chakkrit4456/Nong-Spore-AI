/*
 * Nong Spore AI - เฟิร์มแวร์ ESP32 สำหรับโรงเรือนเพาะเห็ด
 * ทำงานคู่กับ index.html (แดชบอร์ด)
 *
 * ไลบรารีที่ต้องติดตั้ง (Arduino IDE > Library Manager):
 *   - ArduinoJson (Benoit Blanchon) v7
 *   - DHT sensor library (Adafruit) + Adafruit Unified Sensor
 *   - LiquidCrystal I2C (Frank de Brabander)
 *
 * การต่อสาย (แก้ตามที่ต่อจริงได้ในส่วน PIN ด้านล่าง):
 *   DHT22 data  -> GPIO 4
 *   รีเลย์ IN1  -> GPIO 26 (พัดลม)
 *   รีเลย์ IN2  -> GPIO 27 (พ่นหมอก)
 *   รีเลย์ IN3  -> GPIO 25 (ไฟ)  * ต้องเพิ่มรีเลย์ช่องที่ 3
 *   LCD I2C     -> SDA 21, SCL 22
 *   MH-Z19 CO2  -> TX->GPIO16, RX->GPIO17 (ไม่บังคับ ตั้ง HAS_CO2 = false ถ้าไม่มี)
 */
#include <WiFi.h>
#include <WebServer.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <Preferences.h>
#include <time.h>

// ---------- ตั้งค่า Wi-Fi ----------
const char* WIFI_SSID = "YOUR_WIFI";
const char* WIFI_PASS = "YOUR_PASSWORD";

// ---------- PIN ----------
#define PIN_DHT    4
#define PIN_FAN    26
#define PIN_MIST   27
#define PIN_LIGHT  25
#define RELAY_ACTIVE_LOW true   // โมดูลรีเลย์ Songle ส่วนใหญ่ทำงานเมื่อ LOW
#define HAS_CO2    false        // เปลี่ยนเป็น true เมื่อต่อ MH-Z19
#define CO2_RX     16
#define CO2_TX     17

DHT dht(PIN_DHT, DHT22);
LiquidCrystal_I2C lcd(0x27, 16, 2);   // ถ้าจอไม่ขึ้นลอง 0x3F
WebServer server(80);
Preferences prefs;

// ---------- สถานะ ----------
float temp = NAN, hum = NAN; int co2 = -1;
bool fanOn = false, mistOn = false, lightOn = false, autoMode = true;
unsigned long mistStart = 0, mistOffAt = 0;

struct Cfg { float tmin=25, tmax=30, hmin=80, hmax=90; int cmax=1000, mistMax=30; char lOn[6]="06:00", lOff[6]="18:00"; } cfg;

// ---------- ประวัติ 24 ชม. (ทุก 2 นาที = 720 จุด) ----------
#define HIST_N 720
struct Rec { int16_t t10, h10, co2; };
Rec hist[HIST_N]; int histHead = 0, histCount = 0; unsigned long lastHist = 0;

void relay(int pin, bool on) { digitalWrite(pin, (on ^ RELAY_ACTIVE_LOW) ? HIGH : LOW); }
void applyRelays() { relay(PIN_FAN, fanOn); relay(PIN_MIST, mistOn); relay(PIN_LIGHT, lightOn); }

void loadCfg() {
  prefs.begin("nsa", true);
  cfg.tmin = prefs.getFloat("tmin", cfg.tmin); cfg.tmax = prefs.getFloat("tmax", cfg.tmax);
  cfg.hmin = prefs.getFloat("hmin", cfg.hmin); cfg.hmax = prefs.getFloat("hmax", cfg.hmax);
  cfg.cmax = prefs.getInt("cmax", cfg.cmax);  cfg.mistMax = prefs.getInt("mist", cfg.mistMax);
  prefs.getString("lon", cfg.lOn, 6); prefs.getString("loff", cfg.lOff, 6);
  autoMode = prefs.getBool("auto", true);
  prefs.end();
}
void saveCfg() {
  prefs.begin("nsa", false);
  prefs.putFloat("tmin", cfg.tmin); prefs.putFloat("tmax", cfg.tmax);
  prefs.putFloat("hmin", cfg.hmin); prefs.putFloat("hmax", cfg.hmax);
  prefs.putInt("cmax", cfg.cmax);  prefs.putInt("mist", cfg.mistMax);
  prefs.putString("lon", cfg.lOn); prefs.putString("loff", cfg.lOff);
  prefs.putBool("auto", autoMode);
  prefs.end();
}

#if HAS_CO2
int readCO2() {
  const uint8_t cmd[9] = {0xFF,0x01,0x86,0,0,0,0,0,0x79};
  while (Serial2.available()) Serial2.read();
  Serial2.write(cmd, 9);
  uint8_t r[9]; unsigned long t0 = millis(); int n = 0;
  while (n < 9 && millis() - t0 < 200) if (Serial2.available()) r[n++] = Serial2.read();
  if (n < 9 || r[0] != 0xFF || r[1] != 0x86) return -1;
  uint8_t sum = 0; for (int i = 1; i < 8; i++) sum += r[i];
  if ((uint8_t)(0xFF - sum + 1) != r[8]) return -1;
  return r[2] * 256 + r[3];
}
#endif

void readSensors() {
  float t = dht.readTemperature(), h = dht.readHumidity();
  if (!isnan(t)) temp = t;
  if (!isnan(h)) hum = h;
#if HAS_CO2
  int c = readCO2(); if (c > 0) co2 = c;
#endif
}

bool lightSchedule() {
  struct tm ti; if (!getLocalTime(&ti, 10)) return lightOn;
  char hm[6]; strftime(hm, 6, "%H:%M", &ti);
  if (strcmp(cfg.lOn, cfg.lOff) < 0) return strcmp(hm, cfg.lOn) >= 0 && strcmp(hm, cfg.lOff) < 0;
  return strcmp(hm, cfg.lOn) >= 0 || strcmp(hm, cfg.lOff) < 0;
}

void autoControl() {
  if (!autoMode || isnan(temp) || isnan(hum)) return;
  unsigned long now = millis();
  // พ่นหมอก: พ่นเมื่อชื้นต่ำ หยุดเมื่อถึงเป้าหรือครบเวลา แล้วพัก 60 วินาที
  if (!mistOn && hum < cfg.hmin && now - mistOffAt > 60000UL) { mistOn = true; mistStart = now; }
  else if (mistOn && (hum >= cfg.hmax - 2 || now - mistStart > (unsigned long)cfg.mistMax * 1000UL)) { mistOn = false; mistOffAt = now; }
  // พัดลม
  bool co2High = co2 > 0 && co2 > cfg.cmax;
  bool need = co2High || temp > cfg.tmax || hum > cfg.hmax + 3;
  bool ok = (co2 < 0 || co2 < cfg.cmax - 150) && temp < cfg.tmax - 0.5 && hum <= cfg.hmax;
  if (!fanOn && need) fanOn = true; else if (fanOn && ok) fanOn = false;
  lightOn = lightSchedule();
}

void updateLCD() {
  char l1[17], l2[17];
  snprintf(l1, 17, "T:%4.1fC H:%2d%%   ", isnan(temp) ? 0 : temp, isnan(hum) ? 0 : (int)hum);
  if (co2 > 0) snprintf(l2, 17, "CO2:%-4d %c%c%c %c  ", co2, fanOn?'F':'-', mistOn?'M':'-', lightOn?'L':'-', autoMode?'A':'M');
  else         snprintf(l2, 17, "CO2:--   %c%c%c %c  ", fanOn?'F':'-', mistOn?'M':'-', lightOn?'L':'-', autoMode?'A':'M');
  lcd.setCursor(0, 0); lcd.print(l1); lcd.setCursor(0, 1); lcd.print(l2);
}

// ---------- HTTP ----------
void cors() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.sendHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  server.sendHeader("Access-Control-Allow-Headers", "Content-Type");
}
void sendJson(JsonDocument& d) { String s; serializeJson(d, s); cors(); server.send(200, "application/json", s); }
void bad(const char* m) { cors(); server.send(400, "application/json", String("{\"error\":\"") + m + "\"}"); }

void hStatus() {
  JsonDocument d;
  d["temp"] = isnan(temp) ? 0 : round(temp * 10) / 10.0;
  d["hum"]  = isnan(hum) ? 0 : round(hum * 10) / 10.0;
  if (co2 > 0) d["co2"] = co2; else d["co2"] = nullptr;
  d["fan"] = fanOn; d["mist"] = mistOn; d["light"] = lightOn; d["auto"] = autoMode;
  d["uptime"] = millis() / 1000;
  sendJson(d);
}
void hHistory() {
  // ส่งเป็น ago (วินาทีก่อนหน้า) เพื่อไม่ต้องพึ่งนาฬิกาฝั่งบอร์ด
  cors(); server.setContentLength(CONTENT_LENGTH_UNKNOWN); server.send(200, "application/json", "[");
  unsigned long nowS = millis() / 1000; String buf;
  for (int i = 0; i < histCount; i++) {
    int idx = (histHead - histCount + i + HIST_N) % HIST_N;
    unsigned long ago = (unsigned long)(histCount - 1 - i) * 120 + (nowS - lastHist / 1000);
    buf += (i ? "," : ""); buf += "{\"ago\":" + String(ago) + ",\"temp\":" + String(hist[idx].t10 / 10.0, 1) + ",\"hum\":" + String(hist[idx].h10 / 10.0, 1);
    buf += ",\"co2\":" + (hist[idx].co2 > 0 ? String(hist[idx].co2) : String("null")) + "}";
    if (buf.length() > 1000) { server.sendContent(buf); buf = ""; }
  }
  buf += "]"; server.sendContent(buf); server.sendContent("");
}
void hRelay() {
  JsonDocument d; if (deserializeJson(d, server.arg("plain"))) return bad("json");
  String n = d["name"] | ""; bool s = d["state"] | false;
  if (autoMode) return bad("auto mode on");
  if (n == "fan") fanOn = s; else if (n == "mist") { mistOn = s; if (s) mistStart = millis(); } else if (n == "light") lightOn = s; else return bad("name");
  applyRelays(); hStatus();
}
void hAuto() {
  JsonDocument d; if (deserializeJson(d, server.arg("plain"))) return bad("json");
  autoMode = d["auto"] | true; saveCfg(); hStatus();
}
void hConfig() {
  JsonDocument d; if (deserializeJson(d, server.arg("plain"))) return bad("json");
  cfg.tmin = d["tmin"] | cfg.tmin; cfg.tmax = d["tmax"] | cfg.tmax;
  cfg.hmin = d["hmin"] | cfg.hmin; cfg.hmax = d["hmax"] | cfg.hmax;
  cfg.cmax = d["cmax"] | cfg.cmax; cfg.mistMax = d["mistMax"] | cfg.mistMax;
  strlcpy(cfg.lOn,  d["lightOn"]  | cfg.lOn,  6);
  strlcpy(cfg.lOff, d["lightOff"] | cfg.lOff, 6);
  saveCfg(); hStatus();
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_FAN, OUTPUT); pinMode(PIN_MIST, OUTPUT); pinMode(PIN_LIGHT, OUTPUT); applyRelays();
  dht.begin();
#if HAS_CO2
  Serial2.begin(9600, SERIAL_8N1, CO2_RX, CO2_TX);
#endif
  Wire.begin(21, 22); lcd.init(); lcd.backlight();
  lcd.print("Nong Spore AI"); lcd.setCursor(0, 1); lcd.print("Connecting WiFi");
  loadCfg();

  WiFi.mode(WIFI_STA); WiFi.begin(WIFI_SSID, WIFI_PASS);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) delay(500);
  lcd.clear();
  if (WiFi.status() == WL_CONNECTED) { lcd.print("IP:"); lcd.print(WiFi.localIP()); Serial.println(WiFi.localIP()); }
  else lcd.print("WiFi FAILED");
  configTzTime("ICT-7", "pool.ntp.org", "time.navy.mi.th");
  delay(2500);

  server.on("/api/status",  HTTP_GET,  hStatus);
  server.on("/api/history", HTTP_GET,  hHistory);
  server.on("/api/relay",   HTTP_POST, hRelay);
  server.on("/api/auto",    HTTP_POST, hAuto);
  server.on("/api/config",  HTTP_POST, hConfig);
  server.onNotFound([]() { if (server.method() == HTTP_OPTIONS) { cors(); server.send(204); } else { cors(); server.send(404, "text/plain", "not found"); } });
  server.begin();
}

unsigned long lastRead = 0;
void loop() {
  server.handleClient();
  unsigned long now = millis();
  if (now - lastRead >= 2500) {           // DHT22 อ่านได้ไม่ถี่กว่า 2 วินาที
    lastRead = now; readSensors(); autoControl(); applyRelays(); updateLCD();
  }
  if (!isnan(temp) && (lastHist == 0 || now - lastHist >= 120000UL)) {
    lastHist = now;
    hist[histHead] = { (int16_t)(temp * 10), (int16_t)(hum * 10), (int16_t)co2 };
    histHead = (histHead + 1) % HIST_N; if (histCount < HIST_N) histCount++;
  }
  if (WiFi.status() != WL_CONNECTED && now % 30000 < 5) WiFi.reconnect();
}
