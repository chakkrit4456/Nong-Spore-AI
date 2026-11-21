#!/usr/bin/env bash
# =============================================================
#  Nong Spore AI - ติดตั้งบน Debian 13 (trixie) แบบ standard
#  ติดตั้ง: MariaDB + Flask/Gunicorn + Nginx (มีรหัสผ่านป้องกันหน้าเว็บ)
#
#  วิธีใช้ (ล็อกอินเป็น root บนเซิร์ฟเวอร์ ในโฟลเดอร์โปรเจค):
#     bash install.sh
#  พอร์ตที่ใช้: เว็บ 80/tcp (Nginx, เปลี่ยนได้ด้วย HTTP_PORT)
#              แอป 8000 (Gunicorn, ฟังเฉพาะ 127.0.0.1)  MariaDB 3306 (เฉพาะ localhost)
#  ตัวเลือก (environment):
#     DOMAIN=example.com   ชื่อโดเมน (ค่าเริ่มต้น _ = รับทุกชื่อ/IP)
#     HTTP_PORT=80         พอร์ตของเว็บ
#     ADMIN_USER=admin     ชื่อผู้ใช้สำหรับเข้าหน้าเว็บ
#  รันซ้ำได้: รหัสผ่านและ token เดิมจะถูกเก็บไว้ ไม่สร้างใหม่
# =============================================================
set -euo pipefail

APP=nong-spore-ai
APP_DIR=/opt/$APP
ENV_FILE=/etc/$APP.env
CRED_FILE=/root/$APP-credentials.txt
SVC_USER=nongspore
DOMAIN=${DOMAIN:-_}
HTTP_PORT=${HTTP_PORT:-80}
ADMIN_USER=${ADMIN_USER:-admin}
CHARTJS_URL="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

c_ok(){ printf '\033[32m[OK]\033[0m %s\n' "$*"; }
c_step(){ printf '\n\033[36m==> %s\033[0m\n' "$*"; }
die(){ printf '\033[31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "กรุณาล็อกอินเป็น root แล้วรันใหม่"
[[ -f "$SRC/server/app.py" ]] || die "ไม่พบ server/app.py ให้รันสคริปต์จากโฟลเดอร์โปรเจค"
. /etc/os-release
[[ "${ID:-}" == "debian" ]] || echo "คำเตือน: สคริปต์นี้ทดสอบกับ Debian เท่านั้น (พบ ${PRETTY_NAME:-unknown})"

rand(){ tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }

# ---------- 1. แพ็กเกจ ----------
c_step "ติดตั้งแพ็กเกจ"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q mariadb-server python3 python3-flask python3-pymysql gunicorn \
  nginx apache2-utils curl ca-certificates
systemctl enable --now mariadb
c_ok "ติดตั้งแพ็กเกจแล้ว"

# ---------- 2. รหัสผ่าน / token ----------
c_step "เตรียมค่าความลับ"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  c_ok "ใช้ค่าเดิมจาก $ENV_FILE"
fi
DB_NAME=${DB_NAME:-nong_spore_ai}
DB_USER=${DB_USER:-nongspore}
DB_PASS=${DB_PASS:-$(rand 32)}
DEVICE_TOKEN=${DEVICE_TOKEN:-$(rand 40)}
RETENTION_DAYS=${RETENTION_DAYS:-180}
NEW_ADMIN_PASS=""
if [[ ! -f /etc/nginx/$APP.htpasswd ]]; then NEW_ADMIN_PASS=$(rand 16); fi

id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER"

umask 077
cat >"$ENV_FILE" <<EOF
DB_HOST=localhost
DB_NAME=$DB_NAME
DB_USER=$DB_USER
DB_PASS=$DB_PASS
DEVICE_TOKEN=$DEVICE_TOKEN
RETENTION_DAYS=$RETENTION_DAYS
EOF
chown root:"$SVC_USER" "$ENV_FILE"; chmod 640 "$ENV_FILE"
umask 022

# ---------- 3. ฐานข้อมูล ----------
c_step "สร้างฐานข้อมูล MariaDB"
mariadb <<SQL
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
ALTER USER '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
FLUSH PRIVILEGES;
SQL
c_ok "ฐานข้อมูล $DB_NAME พร้อม"

# ---------- 4. ไฟล์แอป ----------
c_step "คัดลอกไฟล์แอปไปที่ $APP_DIR"
mkdir -p "$APP_DIR/static/vendor" "$APP_DIR/static/img"
cp "$SRC/server/app.py" "$APP_DIR/"
cp -r "$SRC/server/static/." "$APP_DIR/static/"
shopt -s nullglob nocaseglob
imgs=()
for f in "$SRC"/*.jfif "$SRC"/*.jpg "$SRC"/*.jpeg "$SRC"/*.png "$SRC"/*.webp; do
  cp "$f" "$APP_DIR/static/img/"; imgs+=("$(basename "$f")")
done
shopt -u nullglob nocaseglob
python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${imgs[@]}" >"$APP_DIR/static/img/list.json"
if [[ ! -s "$APP_DIR/static/vendor/chart.umd.min.js" ]]; then
  curl -fsSL "$CHARTJS_URL" -o "$APP_DIR/static/vendor/chart.umd.min.js" \
    || die "ดาวน์โหลด Chart.js ไม่สำเร็จ ตรวจอินเทอร์เน็ตของเซิร์ฟเวอร์"
fi
chown -R root:root "$APP_DIR"; chmod -R a+rX "$APP_DIR"
c_ok "คัดลอกไฟล์แล้ว (รูป ${#imgs[@]} ไฟล์)"

c_step "สร้างตารางในฐานข้อมูล"
( set -a; . "$ENV_FILE"; set +a; cd "$APP_DIR" && python3 app.py --init-db )

# ---------- 5. systemd ----------
c_step "ตั้งค่า systemd service"
cat >/etc/systemd/system/$APP.service <<EOF
[Unit]
Description=Nong Spore AI dashboard
After=network.target mariadb.service
Requires=mariadb.service

[Service]
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/gunicorn --workers 3 --bind 127.0.0.1:8000 --access-logfile - app:app
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable $APP >/dev/null
systemctl restart $APP
c_ok "service $APP ทำงานแล้ว"

# ---------- 6. nginx ----------
c_step "ตั้งค่า Nginx"
if [[ -n "$NEW_ADMIN_PASS" ]]; then
  htpasswd -bcB /etc/nginx/$APP.htpasswd "$ADMIN_USER" "$NEW_ADMIN_PASS" >/dev/null
fi
chown root:www-data /etc/nginx/$APP.htpasswd; chmod 640 /etc/nginx/$APP.htpasswd

cat >/etc/nginx/sites-available/$APP <<EOF
server {
    listen $HTTP_PORT;
    listen [::]:$HTTP_PORT;
    server_name $DOMAIN;
    client_max_body_size 64k;

    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options SAMEORIGIN;

    # ESP32 ส่งข้อมูล (ยืนยันตัวตนด้วย X-Device-Token)
    location = /api/ingest {
        auth_basic off;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
    location = /healthz {
        auth_basic off;
        proxy_pass http://127.0.0.1:8000;
    }
    # หน้าเว็บและ API อื่นต้องใส่รหัสผ่าน
    location / {
        auth_basic "Nong Spore AI";
        auth_basic_user_file /etc/nginx/$APP.htpasswd;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }
}
EOF
ln -sf /etc/nginx/sites-available/$APP /etc/nginx/sites-enabled/$APP
if [[ "$HTTP_PORT" == "80" ]]; then rm -f /etc/nginx/sites-enabled/default; fi
nginx -t
systemctl enable nginx >/dev/null
systemctl reload nginx || systemctl restart nginx
c_ok "Nginx พร้อม"

# ---------- 7. สำรองฐานข้อมูลรายวัน ----------
c_step "ตั้งค่าสำรองฐานข้อมูลทุกวัน (เก็บ 14 วัน)"
mkdir -p /var/backups/$APP; chmod 700 /var/backups/$APP
cat >/etc/cron.daily/$APP-backup <<EOF
#!/bin/sh
mariadb-dump --single-transaction $DB_NAME | gzip > /var/backups/$APP/\$(date +%F).sql.gz
find /var/backups/$APP -name '*.sql.gz' -mtime +14 -delete
EOF
chmod 755 /etc/cron.daily/$APP-backup
c_ok "สำรองไว้ที่ /var/backups/$APP"

# ---------- 8. ตรวจสอบ ----------
c_step "ตรวจสอบระบบ"
for i in {1..10}; do
  if curl -fs "http://127.0.0.1:$HTTP_PORT/healthz" >/dev/null; then c_ok "เว็บตอบสนองปกติ"; break; fi
  sleep 1
  [[ $i == 10 ]] && die "เว็บไม่ตอบสนอง ดู log ด้วย: journalctl -u $APP -n 50"
done

IP=$(hostname -I | awk '{print $1}')
URL="http://${IP}$([[ "$HTTP_PORT" == "80" ]] || echo ":$HTTP_PORT")"
{
  echo "Nong Spore AI - ติดตั้งเมื่อ $(date '+%F %T')"
  echo "URL หน้าเว็บ     : $URL"
  echo "พอร์ตเว็บ       : $HTTP_PORT/tcp"
  echo "ผู้ใช้หน้าเว็บ    : $ADMIN_USER"
  [[ -n "$NEW_ADMIN_PASS" ]] && echo "รหัสผ่านหน้าเว็บ : $NEW_ADMIN_PASS"
  echo "SERVER_URL (ESP32)  : $URL"
  echo "DEVICE_TOKEN (ESP32): $DEVICE_TOKEN"
} > "$CRED_FILE.new"
if [[ -z "$NEW_ADMIN_PASS" && -f "$CRED_FILE" ]]; then
  grep 'รหัสผ่านหน้าเว็บ' "$CRED_FILE" >> "$CRED_FILE.new" || true
fi
mv "$CRED_FILE.new" "$CRED_FILE"; chmod 600 "$CRED_FILE"

echo
echo "================================================================"
cat "$CRED_FILE"
echo "----------------------------------------------------------------"
echo "ข้อมูลนี้บันทึกไว้ที่ $CRED_FILE (อ่านได้เฉพาะ root)"
echo "นำ SERVER_URL และ DEVICE_TOKEN ไปใส่ใน mushroom_house_wifi.ino แล้วอัปโหลดลงบอร์ด"
echo "เปลี่ยนรหัสผ่านเว็บ: htpasswd -B /etc/nginx/$APP.htpasswd $ADMIN_USER"
echo "ดู log: journalctl -u $APP -f"
echo "พอร์ตที่ใช้:"
echo "  - $HTTP_PORT/tcp  หน้าเว็บและ API (Nginx)  <- เปิด firewall พอร์ตนี้"
echo "  - 8000/tcp  Gunicorn (ฟังเฉพาะ 127.0.0.1 ไม่ต้องเปิด)"
echo "  - 3306/tcp  MariaDB (ฟังเฉพาะ localhost ไม่ต้องเปิด)"
echo "แนะนำให้ใช้ HTTPS เมื่อมีโดเมน: apt install certbot python3-certbot-nginx && certbot --nginx"
echo "================================================================"
