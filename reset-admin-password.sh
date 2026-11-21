#!/usr/bin/env bash
# =============================================================
#  Nong Spore AI - รีเซ็ตรหัสผ่านผู้ดูแลระบบ (ใช้ตอนลืมรหัสผ่าน)
#  ต้องรันเป็น root บนเซิร์ฟเวอร์
#
#  ใช้:  nong-spore-reset-password            รีเซ็ต admin และสุ่มรหัสใหม่
#        nong-spore-reset-password somchai    รีเซ็ตผู้ใช้ somchai
#        nong-spore-reset-password admin -p   พิมพ์รหัสผ่านใหม่เอง
#  ถ้ายังไม่มีผู้ใช้ชื่อนี้ ระบบจะสร้างให้
#  (หรือรันจากโฟลเดอร์โปรเจค: bash reset-admin-password.sh)
# =============================================================
set -euo pipefail
APP_DIR=/opt/nong-spore-ai
ENV_FILE=/etc/nong-spore-ai.env

die(){ printf '\033[31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "กรุณาล็อกอินเป็น root แล้วรันใหม่"
[[ -f "$ENV_FILE" && -f "$APP_DIR/app.py" ]] || die "ยังไม่ได้ติดตั้ง Nong Spore AI (รัน install.sh ก่อน)"

USER_NAME=${1:-admin}
if [[ "${2:-}" == "-p" ]]; then
  read -rsp "รหัสผ่านใหม่ (อย่างน้อย 8 ตัว): " P1; echo
  read -rsp "ยืนยันรหัสผ่าน: " P2; echo
  [[ "$P1" == "$P2" ]] || die "รหัสผ่านไม่ตรงกัน"
  NEW_PASS=$P1
else
  NEW_PASS=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)
fi

( set -a; . "$ENV_FILE"; set +a; cd "$APP_DIR" && NSA_USER="$USER_NAME" NSA_PASS="$NEW_PASS" python3 app.py --set-password )

echo "--------------------------------------------"
echo "ผู้ใช้     : $USER_NAME"
[[ "${2:-}" == "-p" ]] || echo "รหัสผ่านใหม่: $NEW_PASS"
echo "ผู้ใช้คนนี้จะถูกออกจากระบบในทุกอุปกรณ์ ให้เข้าสู่ระบบใหม่ด้วยรหัสผ่านนี้"
echo "--------------------------------------------"
