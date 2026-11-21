"""Mushroom House server: รับค่าจาก ESP32, เก็บลง MariaDB, ให้บริการแดชบอร์ด"""
import csv
import hmac
import io
import os
import time
from datetime import datetime, timedelta

import pymysql
from flask import Flask, Response, abort, g, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)

DB = dict(
    host=os.environ.get("DB_HOST", "localhost"),
    user=os.environ.get("DB_USER", "mushroom"),
    password=os.environ.get("DB_PASS", ""),
    database=os.environ.get("DB_NAME", "mushroom_house"),
    unix_socket=os.environ.get("DB_SOCKET") or None,
    charset="utf8mb4",
    autocommit=True,
    cursorclass=pymysql.cursors.DictCursor,
)
DEVICE_TOKEN = os.environ.get("DEVICE_TOKEN", "")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "180"))
ONLINE_SEC = 30

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS readings (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        ts DATETIME NOT NULL,
        temp DECIMAL(4,1) NULL,
        hum DECIMAL(4,1) NULL,
        fan TINYINT(1) NOT NULL,
        auto_mode TINYINT(1) NOT NULL,
        sensor_ok TINYINT(1) NOT NULL,
        rssi SMALLINT NULL,
        INDEX idx_ts (ts)
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS device_state (
        id TINYINT PRIMARY KEY,
        auto_mode TINYINT(1) NOT NULL DEFAULT 1,
        fan_cmd TINYINT(1) NOT NULL DEFAULT 0,
        temp_on DECIMAL(4,1) NOT NULL DEFAULT 30.0,
        temp_off DECIMAL(4,1) NOT NULL DEFAULT 28.0,
        last_seen DATETIME NULL,
        ip VARCHAR(45) NULL,
        uptime INT NULL,
        fan_on_sec INT NULL,
        species VARCHAR(20) NOT NULL DEFAULT 'oyster'
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS events (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        ts DATETIME NOT NULL,
        level VARCHAR(10) NOT NULL,
        msg VARCHAR(255) NOT NULL,
        INDEX idx_ts (ts)
    ) ENGINE=InnoDB""",
    "INSERT IGNORE INTO device_state (id) VALUES (1)",
]


def db():
    if "db" not in g:
        g.db = pymysql.connect(**DB)
    return g.db


@app.teardown_appcontext
def close_db(_):
    c = g.pop("db", None)
    if c:
        c.close()


def q(sql, args=(), one=False):
    with db().cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def event(level, msg):
    q("INSERT INTO events (ts, level, msg) VALUES (NOW(), %s, %s)", (level, msg[:255]))


def num(v):
    return None if v is None else float(v)


def state():
    return q("SELECT * FROM device_state WHERE id=1", one=True)


def settings_json(s):
    return {"auto": bool(s["auto_mode"]), "fan": bool(s["fan_cmd"]),
            "tempOn": num(s["temp_on"]), "tempOff": num(s["temp_off"])}


# ---------------- ESP32 ----------------
@app.post("/api/ingest")
def ingest():
    tok = request.headers.get("X-Device-Token", "")
    if not DEVICE_TOKEN or not hmac.compare_digest(tok, DEVICE_TOKEN):
        abort(401)
    d = request.get_json(force=True, silent=True) or {}
    ok = bool(d.get("sensorOk"))
    temp, hum = (d.get("temp"), d.get("hum")) if ok else (None, None)
    fan = bool(d.get("fan"))
    s = state()
    last = q("SELECT fan, sensor_ok, ts FROM readings ORDER BY id DESC LIMIT 1", one=True)

    q("INSERT INTO readings (ts, temp, hum, fan, auto_mode, sensor_ok, rssi) VALUES (NOW(),%s,%s,%s,%s,%s,%s)",
      (temp, hum, fan, bool(d.get("auto", True)), ok, d.get("rssi")))
    q("UPDATE device_state SET last_seen=NOW(), ip=%s, uptime=%s, fan_on_sec=%s WHERE id=1",
      (request.headers.get("X-Real-IP", request.remote_addr), d.get("uptime"), d.get("fanOnSec")))

    if last is None or (datetime.now() - last["ts"]).total_seconds() > 120:
        event("info", "บอร์ด ESP32 ออนไลน์")
    if last is not None:
        if bool(last["fan"]) != fan:
            mode = "อัตโนมัติ" if d.get("auto", True) else "สั่งเอง"
            event("info", f"พัดลม{'เปิด' if fan else 'ปิด'} ({mode})" + (f" ที่ {temp}°C" if temp is not None else ""))
        if bool(last["sensor_ok"]) != ok:
            event("info" if ok else "error", "เซนเซอร์กลับมาอ่านค่าได้" if ok else "อ่านค่าเซนเซอร์ DHT11 ไม่ได้")

    if int(time.time()) % 600 < 10:  # ลบข้อมูลเก่าประมาณทุก 10 นาที
        q("DELETE FROM readings WHERE ts < NOW() - INTERVAL %s DAY", (RETENTION_DAYS,))
        q("DELETE FROM events WHERE ts < NOW() - INTERVAL %s DAY", (RETENTION_DAYS,))
    return jsonify(settings_json(s))


# ---------------- Dashboard ----------------
@app.get("/api/status")
def status():
    s = state()
    r = q("SELECT * FROM readings ORDER BY id DESC LIMIT 1", one=True)
    last_ok = q("SELECT temp, hum, ts FROM readings WHERE sensor_ok=1 ORDER BY id DESC LIMIT 1", one=True)
    online = bool(s["last_seen"] and (datetime.now() - s["last_seen"]).total_seconds() < ONLINE_SEC)
    return jsonify({
        "online": online,
        "lastSeen": s["last_seen"].isoformat() if s["last_seen"] else None,
        "temp": num(last_ok["temp"]) if last_ok else None,
        "hum": num(last_ok["hum"]) if last_ok else None,
        "sensorOk": bool(r["sensor_ok"]) if r else False,
        "fan": bool(r["fan"]) if r else False,
        "deviceAuto": bool(r["auto_mode"]) if r else True,
        "rssi": r["rssi"] if r else None,
        "ip": s["ip"], "uptime": s["uptime"], "fanOnSec": s["fan_on_sec"],
        "species": s["species"],
        **settings_json(s),
    })


@app.get("/api/history")
def history():
    hours = max(1, min(int(request.args.get("hours", 24)), 24 * 90))
    bucket = max(10, hours * 3600 // 400)  # ไม่เกินประมาณ 400 จุด
    rows = q("""SELECT FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(ts)/%s)*%s) AS t,
                       ROUND(AVG(temp),1) AS temp, ROUND(AVG(hum),1) AS hum, ROUND(AVG(fan),2) AS fan,
                       MIN(temp) AS tmin, MAX(temp) AS tmax
                FROM readings WHERE ts >= NOW() - INTERVAL %s HOUR AND sensor_ok=1
                GROUP BY t ORDER BY t""", (bucket, bucket, hours))
    agg = q("""SELECT MIN(temp) tmin, AVG(temp) tavg, MAX(temp) tmax, MIN(hum) hmin, AVG(hum) havg, MAX(hum) hmax,
                      AVG(fan) fan, COUNT(*) n FROM readings WHERE ts >= NOW() - INTERVAL %s HOUR AND sensor_ok=1""",
            (hours,), one=True)
    return jsonify({
        "bucket": bucket,
        "data": [{"ts": r["t"].isoformat(), "temp": num(r["temp"]), "hum": num(r["hum"]), "fan": num(r["fan"])} for r in rows],
        "summary": {k: (num(v) if k != "n" else v) for k, v in agg.items()},
    })


@app.get("/api/events")
def events():
    rows = q("SELECT ts, level, msg FROM events ORDER BY id DESC LIMIT 100")
    return jsonify([{"ts": r["ts"].isoformat(), "level": r["level"], "msg": r["msg"]} for r in rows])


@app.post("/api/control")
def control():
    d = request.get_json(force=True, silent=True) or {}
    s = state()
    auto, fan = s["auto_mode"], s["fan_cmd"]
    on, off = num(s["temp_on"]), num(s["temp_off"])
    if "fan" in d:
        fan, auto = bool(d["fan"]), False
    if "auto" in d:
        auto = bool(d["auto"])
    if "tempOn" in d or "tempOff" in d:
        on, off = float(d.get("tempOn", on)), float(d.get("tempOff", off))
        if not (10 <= off < on <= 50):
            return jsonify({"error": "ค่าปิดต้องน้อยกว่าค่าเปิด และอยู่ในช่วง 10-50°C"}), 400
    species = d.get("species", s["species"])
    if species not in ("oyster", "straw", "ear", "lingzhi"):
        species = s["species"]
    q("UPDATE device_state SET auto_mode=%s, fan_cmd=%s, temp_on=%s, temp_off=%s, species=%s WHERE id=1",
      (auto, fan, on, off, species))
    if auto != s["auto_mode"]:
        event("info", "เปลี่ยนเป็นโหมด" + ("อัตโนมัติ" if auto else "สั่งเอง"))
    if "fan" in d:
        event("info", f"สั่ง{'เปิด' if fan else 'ปิด'}พัดลมจากแดชบอร์ด")
    if on != num(s["temp_on"]) or off != num(s["temp_off"]):
        event("info", f"ตั้งเกณฑ์พัดลมใหม่ เปิด {on}°C / ปิด {off}°C")
    return status()


@app.delete("/api/events")
def clear_events():
    q("DELETE FROM events")
    return jsonify({"ok": True})


@app.get("/api/export.csv")
def export_csv():
    hours = max(1, min(int(request.args.get("hours", 24)), 24 * 365))
    rows = q("SELECT ts, temp, hum, fan, auto_mode, sensor_ok FROM readings WHERE ts >= NOW() - INTERVAL %s HOUR ORDER BY ts",
             (hours,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "temperature_c", "humidity_pct", "fan", "auto", "sensor_ok"])
    for r in rows:
        w.writerow([r["ts"].isoformat(), r["temp"], r["hum"], r["fan"], r["auto_mode"], r["sensor_ok"]])
    name = f"mushroom_house_{datetime.now():%Y%m%d}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/healthz")
def healthz():
    q("SELECT 1")
    return "ok"


@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE, "static"), "index.html")


@app.get("/<path:p>")
def static_files(p):
    return send_from_directory(os.path.join(BASE, "static"), p)


def init_db():
    c = pymysql.connect(**DB)
    with c.cursor() as cur:
        for s in SCHEMA:
            cur.execute(s)
    c.close()


if __name__ == "__main__":
    import sys
    if "--init-db" in sys.argv:
        init_db()
        print("database ready")
    else:
        app.run(host="127.0.0.1", port=8000, debug=False)
