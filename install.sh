#!/bin/bash
# ============================================================
#  CryptoChart — نصب روی Raspberry Pi OS Lite / DietPi (headless)
# ------------------------------------------------------------
#  کاربرد:  sudo bash install.sh [server-url] [--rotate] [--repo <git-url>] [--update]
#  مثال:    sudo bash install.sh https://example.com/cc
#
#  --update : آپدیت ماژول — فقط فایل‌ها را کپی و سرویس را ری‌استارت می‌کند
#             (apt/وابستگی‌ها دست نمی‌خورد؛ updater.py همین را صدا می‌زند)
#  server-url : اگر داده نشود از فایل SERVER_URL (نصب قبلی) یا یونیت systemd
#               موجود خوانده می‌شود — پس آپدیت بدون دانستن آدرس هم کار می‌کند
# ============================================================
set -e

INSTALL_DIR="/opt/cryptochart"
URL=""
ROTATE_FLAG=""
REPO_URL=""
UPDATE_ONLY=""
WANT_REPO=""

for arg in "$@"; do
  case "$arg" in
    --rotate) ROTATE_FLAG="--rotate" ;;
    --update) UPDATE_ONLY="1" ;;
    --repo)   WANT_REPO="1" ;;
    *) if [ -n "$WANT_REPO" ]; then REPO_URL="$arg"; WANT_REPO=""; else URL="$arg"; fi ;;
  esac
done

# پوشهٔ همین اسکریپت = پوشهٔ فایل‌های ماژول (chart.py، main.py، ...)
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- آدرس سرور: آرگومان → SERVER_URL → یونیت موجود ---------------------------
if [ -z "$URL" ] && [ -f "$INSTALL_DIR/SERVER_URL" ]; then
  URL="$(head -1 "$INSTALL_DIR/SERVER_URL" | tr -d '\r\n')"
fi
if [ -z "$URL" ] && [ -f "/etc/systemd/system/cryptochart.service" ]; then
  URL="$(sed -n 's/.*--url \([^ ]*\).*/\1/p' /etc/systemd/system/cryptochart.service | head -1)"
fi
if [ -z "$URL" ]; then
  echo "usage: sudo bash install.sh [server-url] [--rotate] [--repo <git-url>] [--update]"
  echo "       (server-url را یک بار بدهید؛ بعد از آن از SERVER_URL خوانده می‌شود)"
  exit 1
fi
# چرخش هم از یونیت موجود حفظ می‌شود
if [ -z "$ROTATE_FLAG" ] && [ -f "/etc/systemd/system/cryptochart.service" ]; then
  if grep -q -- '--rotate' /etc/systemd/system/cryptochart.service; then
    ROTATE_FLAG="--rotate"
  fi
fi

# --- آدرس مخزن git (برای آپدیت آنلاین نصب‌های کپی‌شده) ----------------------
if [ -z "$REPO_URL" ] && [ -f "$INSTALL_DIR/REPO_URL" ]; then
  REPO_URL="$(head -1 "$INSTALL_DIR/REPO_URL" | tr -d '\r\n')"
fi
if [ -z "$REPO_URL" ]; then
  REPO_URL="$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null | head -1 || true)"
fi
if [ -z "$REPO_URL" ]; then
  REPO_URL="https://github.com/omidgholami74/rasberypicrypto.git"
fi

echo "==> CryptoChart installer"
echo "    server : $URL"
echo "    rotate : ${ROTATE_FLAG:-no}"
echo "    repo   : $REPO_URL"
echo "    update : ${UPDATE_ONLY:-no}"

if [ -z "$UPDATE_ONLY" ]; then
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
fi

echo "==> copying files to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
# حیاتی: updater.py و install.sh هم کپی می‌شوند — آپدیت آنلاین ماژول
# (check/install از وب‌اپ) بدون آن‌ها کار نمی‌کند
cp "$SRC_DIR/chart.py" "$SRC_DIR/main.py" "$SRC_DIR/mini_chart.py" \
   "$SRC_DIR/wifi.py" "$SRC_DIR/market.py" "$SRC_DIR/updater.py" \
   "$SRC_DIR/install.sh" "$INSTALL_DIR/"
# اختیاری: پایدارسازی شبکهٔ Pi (DNS/power-save) — نبودش نصب را خراب نمی‌کند
cp "$SRC_DIR/netfix.sh" "$INSTALL_DIR/" 2>/dev/null || true
chmod +x "$INSTALL_DIR/main.py" 2>/dev/null || true
chmod +x "$INSTALL_DIR/netfix.sh" 2>/dev/null || true

# --- نسخهٔ نصب‌شده ----------------------------------------------------------
# منبع git_rev() در نصب کپی‌شده همین فایل است و وب‌اپ آن را «Module version»
# نشان می‌دهد. اولویت: CRYPTOCHART_REV (آپدیت آنلاین) → git → VERSION قبلی
GIT_REV="${CRYPTOCHART_REV:-}"
if [ -z "$GIT_REV" ]; then
  GIT_REV="$(git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || true)"
fi
if [ -z "$GIT_REV" ]; then
  GIT_REV="$(head -1 "$INSTALL_DIR/VERSION" 2>/dev/null | tr -d '\r\n' || true)"
fi
if [ -n "$GIT_REV" ]; then
  echo "$GIT_REV" > "$INSTALL_DIR/VERSION"
else
  echo $(date +%s) > "$INSTALL_DIR/VERSION"   # fallback: زمان نصب به‌عنوان شناسهٔ نسخه
fi

# آدرس سرور و مخزن برای آپدیت‌های بعدی (updater.py همین‌ها را می‌خواند)
echo "$URL" > "$INSTALL_DIR/SERVER_URL"
echo "$REPO_URL" > "$INSTALL_DIR/REPO_URL"

# اجرای سرویس با کاربر غیر root؛ دسترسی فریم‌بافر از طریق گروه video
# کاربر اجرای سرویس: DietPi → dietpi ، Raspberry Pi OS → pi
RUN_USER="${SUDO_USER:-}"
id "$RUN_USER" >/dev/null 2>&1 || RUN_USER="dietpi"
id "$RUN_USER" >/dev/null 2>&1 || RUN_USER="pi"
id "$RUN_USER" >/dev/null 2>&1 || RUN_USER="root"
id -Gn "$RUN_USER" | grep -qw video || usermod -aG video "$RUN_USER"
# مدیریت وای‌فای بدون root:
id -Gn "$RUN_USER" | grep -qw netdev || usermod -aG netdev "$RUN_USER"

# --- اجازهٔ آپدیت آنلاین به خود ماژول ---------------------------------------
# ماژول با کاربر سرویس (نه root) اجرا می‌شود: باید بتواند فایل‌های تازه را در
# همین پوشه بنویسد. ری‌استارت سرویس نیازی به اجازه ندارد (systemd با
# Restart=always خودش پروسهٔ خارج‌شده را بالا می‌آورد)؛ فقط تازه‌کردن یونیت
# به root نیاز دارد که با یک قاعدهٔ باریک NOPASSWD انجام می‌شود.
if [ "$(id -u)" = "0" ]; then
  chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR" 2>/dev/null || true
  SUDOERS=/etc/sudoers.d/cryptochart
  if [ -d /etc/sudoers.d ] && command -v visudo >/dev/null 2>&1; then
    cat > "$SUDOERS" <<EOF
# CryptoChart — آپدیت آنلاین ماژول (فقط همین یک فرمان، بدون رمز)
${RUN_USER} ALL=(root) NOPASSWD: /usr/bin/bash ${INSTALL_DIR}/install.sh --update
EOF
    if visudo -c -f "$SUDOERS" >/dev/null 2>&1; then
      chmod 440 "$SUDOERS"
      echo "    sudo rule : $SUDOERS"
    else
      rm -f "$SUDOERS"
      echo "    (sudo rule skipped — invalid file, online update of the unit needs root)"
    fi
  fi
fi

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
