#!/bin/bash
# ============================================================
#  CryptoChart — نصب روی Raspberry Pi OS Lite / DietPi (headless)
# ------------------------------------------------------------
#  کاربرد:  sudo bash install.sh <server-url> [--rotate]
#  مثال:    sudo bash install.sh https://example.com/cc
# ============================================================
set -e

URL=""
ROTATE_FLAG=""
for arg in "$@"; do
  case "$arg" in
    --rotate) ROTATE_FLAG="--rotate" ;;
    *) URL="$arg" ;;
  esac
done

if [ -z "$URL" ]; then
  echo "usage: sudo bash install.sh <server-url> [--rotate]"
  exit 1
fi

echo "==> CryptoChart installer"
echo "    server : $URL"
echo "    rotate : ${ROTATE_FLAG:-no}"

echo "==> installing Pillow + network tools (may take a few minutes on Pi Zero)..."
apt-get update -qq
# حیاتی: Pillow — اگر python3 نبود، خودش به‌عنوان وابستگی نصب می‌شود (DietPi معمولاً python3 ندارد)
apt-get install -y -qq python3-pil
# مدیریت وای‌فای از PWA (nmcli) — اگر نصب نشد فقط این قابلیت از کار می‌افتد، چارت مشکلی ندارد
apt-get install -y -qq network-manager \
  || echo "    (network-manager missing — PWA Wi-Fi feature off, chart unaffected)"
# فونت‌ها + shaping فارسی — در DietPi/دبیان قدیمی ممکن است بعضی بسته‌ها نباشند؛ ادامه می‌دهیم
apt-get install -y -qq fonts-vazirmatn fonts-noto-core fonts-dejavu-core fonts-freefont-ttf libraqm0 \
  || echo "    (some font packages missing — continuing; Persian glyphs may fall back)"

INSTALL_DIR="/opt/cryptochart"
echo "==> copying files to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
# حیاتی: updater.py هم باید کپی شود — آپدیت آنلاین ماژول (check/install از وب‌اپ) بدون آن کار نمی‌کند
cp chart.py main.py mini_chart.py wifi.py market.py updater.py "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/main.py"

# نسخهٔ نصب‌شده — در نصب کپی‌شده (بدون git) منبع git_rev() همین فایل است
# و وب‌اپ از آن به عنوان «Module version» نمایش می‌دهد
GIT_REV="$(git rev-parse --short HEAD 2>/dev/null || true)"
if [ -n "$GIT_REV" ]; then
  echo "$GIT_REV" > "$INSTALL_DIR/VERSION"
else
  echo $(date +%s) > "$INSTALL_DIR/VERSION"   # fallback: زمان نصب به‌عنوان شناسهٔ نسخه
fi

# اجرای سرویس با کاربر غیر root؛ دسترسی فریم‌بافر از طریق گروه video
# کاربر اجرای سرویس: DietPi → dietpi ، Raspberry Pi OS → pi
RUN_USER="${SUDO_USER:-}"
id "$RUN_USER" >/dev/null 2>&1 || RUN_USER="dietpi"
id "$RUN_USER" >/dev/null 2>&1 || RUN_USER="pi"
id "$RUN_USER" >/dev/null 2>&1 || RUN_USER="root"
id -Gn "$RUN_USER" | grep -qw video || usermod -aG video "$RUN_USER"
# مدیریت وای‌فای بدون root:
id -Gn "$RUN_USER" | grep -qw netdev || usermod -aG netdev "$RUN_USER"

echo "==> writing systemd service ..."
cat > /etc/systemd/system/cryptochart.service <<EOF
[Unit]
Description=CryptoChart framebuffer client (Pi Zero)
# شروع بعد از آماده شدن شبکه (وای‌فای/اترنت)
After=network-online.target
Wants=network-online.target
# اگر در لحظهٔ بوت fb0 آماده نبود، بی‌نهایت تلاش مجدد کن (پیش‌فرض بعد از ۵ بار رها می‌کند)
StartLimitIntervalSec=0

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/main.py --url ${URL} ${ROTATE_FLAG}
Restart=always
RestartSec=5
# دسترسی مستقیم به /dev/fb0 بدون root:
SupplementaryGroups=video
Nice=10
CPUSchedulingPolicy=batch

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable cryptochart.service
systemctl restart cryptochart.service

echo "==> done!  status:  systemctl status cryptochart"
echo "    logs:        journalctl -u cryptochart -f"
