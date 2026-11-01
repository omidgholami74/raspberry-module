#!/bin/bash
# ============================================================
#  CryptoChart — هات‌اسپات هم‌زمان با وای‌فای کلاینت
# ------------------------------------------------------------
#  ماژول هم به روتر وصل می‌ماند و هم شبکهٔ خودش را پخش می‌کند تا با گوشی/
#  لپ‌تاپ مستقیم به آن وصل شوید (صفحهٔ محلی: اسکن/اتصال وای‌فای + کنسول).
#
#  کاربرد (فقط با root — main.py از طریق قاعدهٔ باریک sudo صدایش می‌زند):
#     sudo bash hotspot.sh status
#     sudo bash hotspot.sh on   [--ssid NAME] [--pass PASSWORD]
#     sudo bash hotspot.sh off
#     sudo bash hotspot.sh ensure        # از hotspot.json آخرین کانفیگ
#
#  صفحهٔ محلی روی پورت 8080 است؛ پورت 80 روی اینترفیس هات‌اسپات به همان
#  ریدایرکت می‌شود، پس کلاینت فقط `http://10.42.0.1` را در مرورگر می‌زند.
#
#  چرا اینترفیس مجازی: brcmfmac روی پای هم‌زمان AP+STA می‌دهد، ولی هر کدام
#  باید اینترفیس خودش را داشته باشد؛ پس ap0 روی همان wlan0 ساخته می‌شود و
#  هر دو روی یک کانال کار می‌کنند (اتصال کلاینت قطع نمی‌شود).
#
#  خروجی: یک خط JSON. رمز هرگز در لاگ/خروجی چاپ نمی‌شود.
# ============================================================
set -u

AP_CON="cryptochart-ap"
AP_IF="ap0"
AP_NET="10.42.0.0/24"
AP_GW="10.42.0.1/24"
MODULE_DIR="${CRYPTOCHART_DIR:-/opt/cryptochart}"
STATE_FILE="$MODULE_DIR/hotspot.json"

CMD="${1:-status}"
shift 2>/dev/null || true
# SSID/رمز به‌طور پیش‌فرض از hotspot.json خوانده می‌شوند (نه از آرگومان) تا
# رمز در لیست پروسه‌ها دیده نشود؛ آرگومان فقط برای تست/دستی است.
SSID=""
PASS=""
SSID_GIVEN=0
PASS_GIVEN=0
ENABLED=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ssid) SSID="${2:-}"; SSID_GIVEN=1; shift 2 2>/dev/null || shift ;;
    --pass) PASS="${2:-}"; PASS_GIVEN=1; shift 2 2>/dev/null || shift ;;
    *) shift ;;
  esac
done

# --- ابزار -------------------------------------------------------------
jesc() { printf '%s' "${1:-}" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\r//g'; }

out_json() {   # out_json ok enabled ssid ip clients iface wlan error
  printf '{"ok":%s,"enabled":%s,"ssid":"%s","ip":"%s","clients":%s,"iface":"%s","wlan":"%s","error":"%s"}\n' \
    "$1" "$2" "$(jesc "$3")" "$(jesc "$4")" "${5:-0}" "$(jesc "$6")" "$(jesc "$7")" "$(jesc "${8:-}")"
}

fail_json() { out_json false 0 "${SSID:-}" "" 0 "$AP_IF" "" "$1"; exit 0; }

have() { command -v "$1" >/dev/null 2>&1; }

# اینترفیس وای‌فای اصلی (نه خود ap0)
wlan_if() {
  local d=""
  if have nmcli; then
    d="$(nmcli -t -f DEVICE,TYPE device 2>/dev/null \
         | awk -F: -v ap="$AP_IF" '$2=="wifi" && $1!=ap {print $1; exit}')"
  fi
  if [ -z "$d" ] && have iw; then
    d="$(iw dev 2>/dev/null | awk -v ap="$AP_IF" '/Interface/ && $2!=ap {print $2; exit}')"
  fi
  printf '%s' "$d"
}

ensure_iface() {
  local wlan
  wlan="$(wlan_if)"
  [ -z "$wlan" ] && return 1
  if ! iw dev "$AP_IF" info >/dev/null 2>&1; then
    iw dev "$wlan" interface add "$AP_IF" type __ap >/dev/null 2>&1 || return 1
  fi
  nmcli device set "$AP_IF" managed yes >/dev/null 2>&1 || true
  ip link set "$AP_IF" up >/dev/null 2>&1 || true
  return 0
}

profile() {
  local has=0
  nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$AP_CON" && has=1
  if [ "$has" != "1" ]; then
    nmcli connection add type wifi ifname "$AP_IF" con-name "$AP_CON" \
      ssid "$SSID" >/dev/null 2>&1 || return 1
  fi
  nmcli connection modify "$AP_CON" \
    connection.interface-name "$AP_IF" \
    connection.autoconnect yes \
    802-11-wireless.mode ap \
    802-11-wireless.ssid "$SSID" \
    802-11-wireless.band bg \
    ipv4.method shared \
    ipv4.addresses "$AP_GW" \
    ipv6.method ignore >/dev/null 2>&1 || return 1
  if [ -n "$PASS" ] && [ "${#PASS}" -ge 8 ]; then
    nmcli connection modify "$AP_CON" \
      802-11-wireless-security.key-mgmt wpa-psk \
      802-11-wireless-security.psk "$PASS" >/dev/null 2>&1 || return 1
  else
    # بدون رمز = شبکهٔ باز برای راه‌اندازی اولیه (در وب‌اپ می‌توان رمز گذاشت)
    nmcli connection modify "$AP_CON" \
      802-11-wireless-security.key-mgmt "" >/dev/null 2>&1 || true
  fi
  return 0
}

# اینترنتِ وای‌فای کلاینت را با کلاینت‌های هات‌اسپات شریک می‌کند (اختیاری)
share_internet() {
  local out
  out="$(nmcli -t -f DEVICE,STATE device 2>/dev/null \
         | awk -F: -v ap="$AP_IF" '$2=="connected" && $1!=ap {print $1; exit}')"
  [ "$out" = "$AP_IF" ] && out=""
  echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null || true
  if have nft; then
    nft list table ip cryptochart >/dev/null 2>&1 || nft add table ip cryptochart 2>/dev/null || true
    nft list chain ip cryptochart post >/dev/null 2>&1 \
      || nft 'add chain ip cryptochart post { type nat hook postrouting priority 100; }' 2>/dev/null || true
    nft list chain ip cryptochart fwd >/dev/null 2>&1 \
      || nft 'add chain ip cryptochart fwd { type filter hook forward priority 0; }' 2>/dev/null || true
    if nft list chain ip cryptochart post 2>/dev/null | grep -q '10\.42\.0\.0/24'; then :; else
      nft add rule ip cryptochart post ip saddr "$AP_NET" oifname != "$AP_IF" masquerade 2>/dev/null || true
    fi
    if nft list chain ip cryptochart fwd 2>/dev/null | grep -q 'iifname "ap0"'; then :; else
      nft add rule ip cryptochart fwd iifname "$AP_IF" accept 2>/dev/null || true
      nft add rule ip cryptochart fwd oifname "$AP_IF" accept 2>/dev/null || true
    fi
  elif have iptables; then
    iptables -t nat -C POSTROUTING -s "$AP_NET" ! -o "$AP_IF" -j MASQUERADE 2>/dev/null \
      || iptables -t nat -A POSTROUTING -s "$AP_NET" ! -o "$AP_IF" -j MASQUERADE 2>/dev/null || true
    iptables -C FORWARD -i "$AP_IF" -j ACCEPT 2>/dev/null \
      || iptables -A FORWARD -i "$AP_IF" -j ACCEPT 2>/dev/null || true
    iptables -C FORWARD -o "$AP_IF" -j ACCEPT 2>/dev/null \
      || iptables -A FORWARD -o "$AP_IF" -j ACCEPT 2>/dev/null || true
  fi
}

# پورت 80 هات‌اسپات → صفحهٔ محلی 8080 (کلاینت فقط IP را در مرورگر می‌زند)
webshare() {
  if have nft; then
    nft list table ip cryptochart >/dev/null 2>&1 || nft add table ip cryptochart 2>/dev/null || true
    nft list chain ip cryptochart pre >/dev/null 2>&1 \
      || nft 'add chain ip cryptochart pre { type nat hook prerouting priority -100; }' 2>/dev/null || true
    if nft list chain ip cryptochart pre 2>/dev/null | grep -q 'redirect'; then :; else
      nft add rule ip cryptochart pre iifname "$AP_IF" tcp dport 80 redirect to :8080 2>/dev/null || true
    fi
  elif have iptables; then
    iptables -t nat -C PREROUTING -i "$AP_IF" -p tcp --dport 80 -j REDIRECT --to-ports 8080 2>/dev/null \
      || iptables -t nat -A PREROUTING -i "$AP_IF" -p tcp --dport 80 -j REDIRECT --to-ports 8080 2>/dev/null || true
  fi
}

ap_active() {
  nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx "$AP_CON" && return 0
  ip -4 -o addr show "$AP_IF" >/dev/null 2>&1
}

status() {
  local on=0 ip="" ssid="" clients=0 wlan
  wlan="$(wlan_if)"
  ap_active && on=1
  ip="$(ip -4 -o addr show "$AP_IF" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
  ssid="$(nmcli -t -f 802-11-wireless.ssid connection show "$AP_CON" 2>/dev/null \
          | awk -F: '{print $2; exit}')"
  [ -z "$ssid" ] && ssid="$SSID"
  clients="$(iw dev "$AP_IF" station dump 2>/dev/null | grep -c '^Station' || true)"
  [ -z "$clients" ] && clients=0
  out_json true "$on" "$ssid" "$ip" "$clients" "$AP_IF" "$wlan" ""
}

# کانفیگ آخر را از فایل حالت ماژول بخوان (بوتِ بدون شبکه هم هات‌اسپات بماند).
# فقط مقادیری پر می‌شوند که با آرگومان داده نشده‌اند.
load_state() {
  [ -f "$STATE_FILE" ] || return 1
  local line enabled ssid pass
  line="$(tr -d '\n' < "$STATE_FILE" 2>/dev/null)"
  [ -z "$line" ] && return 1
  enabled="$(printf '%s' "$line" | sed -n 's/.*"enabled"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p')"
  ssid="$(printf '%s' "$line" | sed -n 's/.*"ssid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  pass="$(printf '%s' "$line" | sed -n 's/.*"pass"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  ENABLED="$enabled"
  if [ "$SSID_GIVEN" != "1" ] && [ -n "$ssid" ]; then SSID="$ssid"; fi
  if [ "$PASS_GIVEN" != "1" ]; then PASS="$pass"; fi
  return 0
}

case "$CMD" in
  on)
    load_state >/dev/null 2>&1 || true
    [ -z "${SSID:-}" ] && SSID="CryptoChart"
    have nmcli || fail_json "nmcli نیست (network-manager نصب شود)"
    have iw || fail_json "iw نیست (apt install iw)"
    ensure_iface || fail_json "اینترفیس AP ساخته نشد (رادیو وای‌فای یا درایور؟)"
    profile || fail_json "پروفایل هات‌اسپات ساخته/تنظیم نشد"
    if ! nmcli connection up "$AP_CON" >/dev/null 2>&1; then
      fail_json "بالا آمدن هات‌اسپات ناموفق بود"
    fi
    share_internet
    webshare
    status
    ;;
  off)
    nmcli connection down "$AP_CON" >/dev/null 2>&1 || true
    if have iw; then iw dev "$AP_IF" del >/dev/null 2>&1 || true; fi
    out_json true 0 "${SSID:-}" "" 0 "$AP_IF" "" ""
    ;;
  ensure)
    # بوت: اگر هیچ کانفیگی ذخیره نشده (نصب تازه) پیش‌فرض روشن است تا بدون
    # هیچ شبکه‌ای هم بتوان به ماژول وصل شد؛ فقط خاموشی صریح مانع می‌شود.
    if ! load_state; then ENABLED="true"; fi
    [ "$ENABLED" = "true" ] || { out_json true 0 "${SSID:-}" "" 0 "$AP_IF" "" "disabled"; exit 0; }
    exec bash "$0" on
    ;;
  status)
    status
    ;;
  *)
    fail_json "unknown command: $CMD"
    ;;
esac
