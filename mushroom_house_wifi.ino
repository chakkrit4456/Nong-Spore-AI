/*
 * Nong Spore AI - เฟิร์มแวร์ ESP32 (ต่อยอดจาก Mushroom_House.ino)
 * ESP32 + DHT11 (GPIO4) + รีเลย์พัดลม (GPIO26, Active LOW) + LCD I2C 16x2 (SDA21, SCL22)
 *
 * บอร์ดส่งค่าไปยังเซิร์ฟเวอร์ทุก 10 วินาที (POST /api/ingest)
 * แล้วรับค่าตั้งกลับมา ได้แก่โหมด สถานะพัดลมที่สั่ง และเกณฑ์อุณหภูมิ
 * ถ้าเซิร์ฟเวอร์หรือ Wi-Fi ล่ม บอร์ดยังควบคุมพัดลมตามเกณฑ์ล่าสุดได้เอง
 *
 * ไลบรารี: DHT sensor library (Adafruit), LiquidCrystal I2C, ArduinoJson v7
 */
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <DHT.h>
#include <LiquidCrystal_I2C.h>
#include <Preferences.h>

// ---------- ตั้งค่า (แก้ตรงนี้) ----------
const char* WIFI_SSID    = "YOUR_WIFI";
const char* WIFI_PASS    = "YOUR_PASSWORD";
const char* SERVER_URL   = "https://nong-spore.onrender.com"; // URL ของเซิร์ฟเวอร์ (ไม่มี / ท้าย)
const char* DEVICE_TOKEN = "PASTE_DEVICE_TOKEN"; // Render > nong-spore > Environment > DEVICE_TOKEN

#define DHTPIN 4
#define DHTTYPE DHT11
#define RELAY_PIN 26
#define READ_EVERY 2500UL
#define PUSH_EVERY 10000UL

LiquidCrystal_I2C lcd(0x27, 16, 2);
DHT dht(DHTPIN, DHTTYPE);
Preferences prefs;

float tempOn = 30.0, tempOff = 28.0;
float temperature = NAN, humidity = NAN;
bool fanState = false, autoMode = true, sensorOk = false, serverOk = false;
unsigned long lastRead = 0, lastPush = 0, lastWifi = 0, fanOnMs = 0, fanSince = 0;

void setFan(bool on) {
  if (on && !fanState) fanSince = millis();
  if (!on && fanState) fanOnMs += millis() - fanSince;
  fanState = on;
  digitalWrite(RELAY_PIN, fanState ? LOW : HIGH);   // Active LOW
}

void saveCfg() {
  prefs.begin("nsa", false);
  prefs.putFloat("on", tempOn); prefs.putFloat("off", tempOff); prefs.putBool("auto", autoMode);
  prefs.end();
}

void lcdShow() {
  lcd.setCursor(0, 0);
  if (!sensorOk) lcd.print("SENSOR ERROR    ");
  else {
    lcd.print("T:"); lcd.print(temperature, 1); lcd.print((char)223);
    lcd.print("C H:"); lcd.print(humidity, 0); lcd.print("%   ");
  }
  lcd.setCursor(0, 1);
  lcd.print("Fan:"); lcd.print(fanState ? "ON " : "OFF");
  lcd.print(autoMode ? " A" : " M");
  lcd.print(WiFi.status() != WL_CONNECTED ? " NOWIFI " : serverOk ? " ONLINE " : " NOSRV  ");
}

void control() {
  if (!sensorOk) { if (autoMode) setFan(false); return; }   // อ่านค่าไม่ได้ ปิดพัดลม (เหมือนเดิม)
  if (autoMode) {
    if (temperature >= tempOn) setFan(true);
    else if (temperature < tempOff) setFan(false);
  }
}

void readSensor() {
  float h = dht.readHumidity(), t = dht.readTemperature();
  sensorOk = !(isnan(h) || isnan(t));
  if (sensorOk) { temperature = t; humidity = h; }
  control();
  if (sensorOk) Serial.printf("Temperature: %.1f C | Humidity: %.0f %% | Fan: %s\n", t, h, fanState ? "ON" : "OFF");
  else Serial.println("SENSOR ERROR");
  lcdShow();
}

void push() {
  if (WiFi.status() != WL_CONNECTED) { serverOk = false; return; }
  JsonDocument d;
  d["sensorOk"] = sensorOk;
  if (sensorOk) { d["temp"] = round(temperature * 10) / 10.0; d["hum"] = round(humidity * 10) / 10.0; }
  d["fan"] = fanState; d["auto"] = autoMode; d["rssi"] = WiFi.RSSI();
  d["uptime"] = millis() / 1000;
  d["fanOnSec"] = (fanOnMs + (fanState ? millis() - fanSince : 0)) / 1000;
  String body; serializeJson(d, body);

  HTTPClient http;
  String url = String(SERVER_URL) + "/api/ingest";
  WiFiClientSecure tls;
  if (url.startsWith("https")) { tls.setInsecure(); http.begin(tls, url); } else http.begin(url);
  http.setTimeout(5000);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  int code = http.POST(body);
  serverOk = (code == 200);
  if (serverOk) {
    JsonDocument r;
    if (!deserializeJson(r, http.getString())) {
      bool a = r["auto"] | autoMode;
      float on = r["tempOn"] | tempOn, off = r["tempOff"] | tempOff;
      bool changed = (a != autoMode) || on != tempOn || off != tempOff;
      autoMode = a; tempOn = on; tempOff = off;
      if (!autoMode) setFan(r["fan"] | fanState);
      if (changed) { saveCfg(); control(); }
    }
  } else {
    Serial.printf("Server error: %d\n", code);
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  Wire.begin(21, 22);
  dht.begin();
  lcd.init(); lcd.backlight(); lcd.clear();
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, HIGH);

  prefs.begin("nsa", true);
  tempOn = prefs.getFloat("on", 30.0); tempOff = prefs.getFloat("off", 28.0); autoMode = prefs.getBool("auto", true);
  prefs.end();

  lcd.setCursor(0, 0); lcd.print("NONG SPORE AI");
  lcd.setCursor(0, 1); lcd.print("WIFI CONNECT...");
  WiFi.mode(WIFI_STA); WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) delay(300);
  lcd.clear();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("IP: "); Serial.println(WiFi.localIP());
    lcd.print("WIFI OK"); lcd.setCursor(0, 1); lcd.print(WiFi.localIP());
  } else lcd.print("NO WIFI-OFFLINE");
  delay(2000); lcd.clear();
  Serial.println("NONG SPORE AI READY");
}

void loop() {
  unsigned long now = millis();
  if (now - lastRead >= READ_EVERY) { lastRead = now; readSensor(); }
  if (now - lastPush >= PUSH_EVERY) { lastPush = now; push(); }
  if (WiFi.status() != WL_CONNECTED && now - lastWifi >= 30000) { lastWifi = now; WiFi.reconnect(); }
}
