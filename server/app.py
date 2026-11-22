"""Mushroom House server: รับค่าจาก ESP32, เก็บลง MariaDB, ให้บริการแดชบอร์ด"""
import csv
import hmac
import io
import os
import time
from datetime import datetime, timedelta

import re

import pymysql
from flask import Flask, Response, abort, g, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=7))
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASS = 8

DB = dict(
    host=os.environ.get("DB_HOST", "localhost"),
    user=os.environ.get("DB_USER", "mushroom"),
    password=os.environ.get("DB_PASS", ""),
    database=os.environ.get("DB_NAME", "mushroom_house"),
    port=int(os.environ.get("DB_PORT", "3306")),
    unix_socket=os.environ.get("DB_SOCKET") or None,
    init_command="SET time_zone='%s'" % os.environ.get("DB_TZ", "+07:00"),
    connect_timeout=10,
    charset="utf8mb4",
    autocommit=True,
    cursorclass=pymysql.cursors.DictCursor,
)
if os.environ.get("DB_SSL") == "1":  # ฐานข้อมูลบนคลาวด์ (เช่น TiDB Cloud) บังคับใช้ TLS
    DB["ssl"] = {"ca": os.environ.get("DB_SSL_CA", "/etc/ssl/certs/ca-certificates.crt")}
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
    """CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(32) NOT NULL UNIQUE,
        password_hash VARCHAR(255) NOT NULL,
        created_at DATETIME NOT NULL,
        last_login DATETIME NULL
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


# ---------------- Auth ----------------
PUBLIC = {"/api/ingest", "/healthz", "/login", "/api/login"}
_fails = {}  # ip -> (count, first_ts) จำกัดการเดารหัสผ่าน


def client_ip():
    return request.headers.get("CF-Connecting-IP") or request.headers.get("X-Real-IP") or request.remote_addr


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    u = q("SELECT id, username, password_hash FROM users WHERE id=%s", (uid,), one=True)
    if not u or session.get("pv") != u["password_hash"][-16:]:
        session.clear()
        return None
    return u


@app.before_request
def require_login():
    if request.path in PUBLIC:
        return None
    g.user = current_user()
    if g.user is None:
        if request.path.startswith("/api/"):
            return jsonify({"error": "กรุณาเข้าสู่ระบบ"}), 401
        return redirect("/login")
    # ป้องกัน CSRF: คำสั่งที่เปลี่ยนข้อมูลต้องส่งเป็น JSON
    if request.method in ("POST", "PUT", "DELETE") and not request.is_json:
        return jsonify({"error": "ต้องส่งข้อมูลแบบ JSON"}), 415
    return None


@app.get("/login")
def login_page():
    return send_from_directory(os.path.join(BASE, "static"), "login.html")


@app.post("/api/login")
def login():
    ip = client_ip()
    cnt, first = _fails.get(ip, (0, time.time()))
    if time.time() - first > 900:
        cnt, first = 0, time.time()
    if cnt >= 10:
        return jsonify({"error": "ลองผิดหลายครั้งเกินไป กรุณารอ 15 นาที"}), 429
    d = request.get_json(force=True, silent=True) or {}
    u = q("SELECT * FROM users WHERE username=%s", (str(d.get("username", "")),), one=True)
    if not u or not check_password_hash(u["password_hash"], str(d.get("password", ""))):
        _fails[ip] = (cnt + 1, first)
        return jsonify({"error": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"}), 401
    _fails.pop(ip, None)
    session.clear()
    session.permanent = True
    session["uid"], session["pv"] = u["id"], u["password_hash"][-16:]
    q("UPDATE users SET last_login=NOW() WHERE id=%s", (u["id"],))
    event("info", f"ผู้ใช้ {u['username']} เข้าสู่ระบบ")
    return jsonify({"ok": True})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    return jsonify({"id": g.user["id"], "username": g.user["username"]})


def check_new(username, password):
    if username is not None and not USER_RE.match(username):
        return "ชื่อผู้ใช้ต้องมี 3-32 ตัว ใช้ได้เฉพาะ A-Z a-z 0-9 _ . -"
    if len(password) < MIN_PASS:
        return f"รหัสผ่านต้องยาวอย่างน้อย {MIN_PASS} ตัวอักษร"
    return None


@app.get("/api/users")
def users():
    rows = q("SELECT id, username, created_at, last_login FROM users ORDER BY username")
    return jsonify([{"id": r["id"], "username": r["username"], "me": r["id"] == g.user["id"],
                     "createdAt": r["created_at"].isoformat(),
                     "lastLogin": r["last_login"].isoformat() if r["last_login"] else None} for r in rows])


@app.post("/api/users")
def add_user():
    d = request.get_json(force=True, silent=True) or {}
    name, pw = str(d.get("username", "")).strip(), str(d.get("password", ""))
    err = check_new(name, pw)
    if err:
        return jsonify({"error": err}), 400
    if q("SELECT id FROM users WHERE username=%s", (name,), one=True):
        return jsonify({"error": "มีชื่อผู้ใช้นี้แล้ว"}), 409
    q("INSERT INTO users (username, password_hash, created_at) VALUES (%s,%s,NOW())",
      (name, generate_password_hash(pw)))
    event("info", f"{g.user['username']} เพิ่มผู้ใช้ {name}")
    return jsonify({"ok": True})


@app.put("/api/users/<int:uid>/password")
def change_password(uid):
    d = request.get_json(force=True, silent=True) or {}
    pw = str(d.get("password", ""))
    err = check_new(None, pw)
    if err:
        return jsonify({"error": err}), 400
    u = q("SELECT username FROM users WHERE id=%s", (uid,), one=True)
    if not u:
        return jsonify({"error": "ไม่พบผู้ใช้"}), 404
    h = generate_password_hash(pw)
    q("UPDATE users SET password_hash=%s WHERE id=%s", (h, uid))
    if uid == g.user["id"]:
        session["pv"] = h[-16:]
    event("info", f"{g.user['username']} เปลี่ยนรหัสผ่านของ {u['username']}")
    return jsonify({"ok": True})


@app.delete("/api/users/<int:uid>")
def delete_user(uid):
    if uid == g.user["id"]:
        return jsonify({"error": "ลบบัญชีของตัวเองไม่ได้"}), 400
    u = q("SELECT username FROM users WHERE id=%s", (uid,), one=True)
    if not u:
        return jsonify({"error": "ไม่พบผู้ใช้"}), 404
    q("DELETE FROM users WHERE id=%s", (uid,))
    event("info", f"{g.user['username']} ลบผู้ใช้ {u['username']}")
    return jsonify({"ok": True})


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
    last = q("SELECT fan, sensor_ok, TIMESTAMPDIFF(SECOND, ts, NOW()) AS age FROM readings ORDER BY id DESC LIMIT 1", one=True)

    q("INSERT INTO readings (ts, temp, hum, fan, auto_mode, sensor_ok, rssi) VALUES (NOW(),%s,%s,%s,%s,%s,%s)",
      (temp, hum, fan, bool(d.get("auto", True)), ok, d.get("rssi")))
    q("UPDATE device_state SET last_seen=NOW(), ip=%s, uptime=%s, fan_on_sec=%s WHERE id=1",
      (client_ip(), d.get("uptime"), d.get("fanOnSec")))

    if last is None or last["age"] > 120:
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
    age = q("SELECT TIMESTAMPDIFF(SECOND, last_seen, NOW()) AS a FROM device_state WHERE id=1", one=True)["a"]
    r = q("SELECT * FROM readings ORDER BY id DESC LIMIT 1", one=True)
    last_ok = q("SELECT temp, hum, ts FROM readings WHERE sensor_ok=1 ORDER BY id DESC LIMIT 1", one=True)
    online = age is not None and age < ONLINE_SEC
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


def set_password(name, pw):
    c = pymysql.connect(**DB)
    with c.cursor() as cur:
        cur.execute("""INSERT INTO users (username, password_hash, created_at) VALUES (%s,%s,NOW())
                       ON DUPLICATE KEY UPDATE password_hash=VALUES(password_hash)""",
                    (name, generate_password_hash(pw)))
        cur.execute("INSERT INTO events (ts, level, msg) VALUES (NOW(),'info',%s)",
                    (f"ตั้งรหัสผ่านของ {name} จากเซิร์ฟเวอร์",))
    c.close()


def auto_setup():
    """ใช้บนโฮสต์อย่าง Render: สร้างตารางและผู้ดูแลคนแรกจาก environment ตอนเริ่มรัน"""
    init_db()
    name, pw = os.environ.get("ADMIN_USER", "admin"), os.environ.get("ADMIN_PASSWORD", "")
    if not pw or check_new(name, pw):
        return
    c = pymysql.connect(**DB)
    with c.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM users")
        n = cur.fetchone()["n"]
    c.close()
    if n == 0 or os.environ.get("ADMIN_RESET") == "1":
        set_password(name, pw)


if os.environ.get("AUTO_INIT") == "1":
    auto_setup()


if __name__ == "__main__":
    import sys
    if "--init-db" in sys.argv:
        init_db()
        print("database ready")
    elif "--count-users" in sys.argv:
        c = pymysql.connect(**DB)
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) n FROM users")
            print(cur.fetchone()["n"])
    elif "--set-password" in sys.argv:
        # NSA_USER=admin NSA_PASS=xxx python3 app.py --set-password  (สร้างผู้ใช้ให้ถ้ายังไม่มี)
        name, pw = os.environ.get("NSA_USER", "admin"), os.environ.get("NSA_PASS", "")
        err = check_new(name, pw)
        if err:
            sys.exit(err)
        set_password(name, pw)
        print(f"ตั้งรหัสผ่านของ {name} แล้ว")
    else:
        app.run(host="127.0.0.1", port=8000, debug=False)
