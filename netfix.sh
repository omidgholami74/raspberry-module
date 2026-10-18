#!/bin/bash
# ============================================================
#  CryptoChart — پایدارسازی شبکهٔ Pi Zero (اختیاری، یک‌بار)
# ------------------------------------------------------------
#  اجرا:  sudo bash netfix.sh
#
#  چرا؟ خطای تکرارشوندهٔ «Temporary failure in name resolution» روی Pi Zero
#  تقریباً همیشه از power-save وای‌فای (خواب رفتن کارت در فاصلهٔ بین pollها)
#  یا از DNS کندِ روتر می‌آید. این اسکریپت هر دو را درمان می‌کند:
#    ۱) power-save وای‌فای را دائمی خاموش می‌کند
#    ۲) DNS جایگزین (Cloudflare/Quad9) را بعد از DNS فعلی اضافه می‌کند
#       (DNS فعلی حذف نمی‌شود؛ فقط وقتی جواب نداد، جایگزین امتحان می‌شود)
#
#  هیچ‌چیز به کد ماژول وابسته نیست و چند بار اجرا شدنش بی‌خطر است.
# ============================================================
set -u

if [ "$(id -u)" != "0" ]; then
  echo "usage: sudo bash netfix.sh" >&2
  exit 1
fi

echo "==> DNS فعلی:"
sed -n 's/^nameserver /    /p' /etc/resolv.conf 2>/dev/null || true

# ---------- ۱) power-save وای‌فای ----------
for dev in /sys/class/net/wlan*; do
  [ -e "$dev" ] || continue
  ifname="$(basename "$dev")"
  if command -v iw >/dev/null 2>&1; then
    iw dev "$ifname" set power_save off 2>/dev/null \
      && echo "    power_save off: $ifname" \
      || echo "    (power_save برای $ifname تنظیم نشد — نادیده)"
  fi
done

# پایدار برای بوت‌های بعدی (NetworkManager)
if [ -d /etc/NetworkManager ]; then
  mkdir -p /etc/NetworkManager/conf.d
  cat > /etc/NetworkManager/conf.d/cryptochart-wifi.conf <<'EOF'
# CryptoChart — قطع‌های کوتاه power-save باعث خطای DNS و آفلاین‌شدن چارت می‌شد
[connection]
wifi.powersave = 2
EOF
  systemctl reload NetworkManager 2>/dev/null || systemctl restart NetworkManager 2>/dev/null || true
  echo "    wifi.powersave = 2 (پایدار در /etc/NetworkManager/conf.d)"
fi

# ---------- ۲) DNS جایگزین ----------
# فقط به‌عنوان fallback اضافه می‌شود؛ DNS فعلی (روتر/ISP) اول می‌ماند.
FALLBACK_DNS="1.1.1.1 9.9.9.9"

if command -v nmcli >/dev/null 2>&1; then
  con="$(nmcli -t -f NAME connection show --active 2>/dev/null | head -1)"
  if [ -n "$con" ]; then
    cur="$(nmcli -g ipv4.dns connection show "$con" 2>/dev/null | tr ',' ' ')"
    if echo " $cur " | grep -q " 1.1.1.1 "; then
      echo "    DNS جایگزین از قبل روی «$con» هست"
    else
      # shellcheck disable=SC2086
      nmcli connection modify "$con" +ipv4.dns "$FALLBACK_DNS" 2>/dev/null \
        && echo "    DNS جایگزین به «$con» اضافه شد: $FALLBACK_DNS" \
        || echo "    (افزودن DNS با nmcli انجام نشد — نادیده)"
      # اعمال بدون قطع کامل اتصال
      nmcli connection up "$con" >/dev/null 2>&1 || true
    fi
  fi
else
  # بدون NetworkManager: فقط اگر resolv.conf ثابت (غیرقابل بازنویسی) باشد
  if ! grep -q '^# cryptochart' /etc/resolv.conf 2>/dev/null; then
    {
      echo '# cryptochart — DNS جایگزین (fallback)'
      for ns in $FALLBACK_DNS; do echo "nameserver $ns"; done
    } >> /etc/resolv.conf 2>/dev/null \
      && echo "    DNS جایگزین به /etc/resolv.conf اضافه شد" \
      || echo "    (اجازهٔ نوشتن resolv.conf نبود — نادیده)"
  fi
fi

# ---------- ۳) تست کوتاه ----------
echo "==> تست DNS:"
for host in apiapp.futurefounderco.com apiv2.nobitex.ir api.brsapi.ir; do
  ip="$(getent hosts "$host" 2>/dev/null | awk 'NR==1{print $1}')"
  if [ -n "$ip" ]; then
    echo "    OK   $host → $ip"
  else
    echo "    FAIL $host (DNS جواب نداد)"
  fi
done

echo "==> تمام. سرویس را با «sudo systemctl restart cryptochart» تازه کنید (اختیاری)."
