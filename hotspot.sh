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
#     sudo bash hotspot.sh diag          # عیب‌یابی: ابزارها/راننده/rfkill/کانال
#
#  صفحهٔ محلی روی پورت 8080 است؛ پورت 80 روی اینترفیس هات‌اسپات به همان
#  ریدایرکت می‌شود، پس کلاینت فقط `http://10.42.0.1` را در مرورگر می‌زند.
#
#  چرا اینترفیس مجازی: brcmfmac روی پای هم‌زمان AP+STA می‌دهد، ولی هر کدام
#  باید اینترفیس خودش را داشته باشد و **هر دو روی یک کانال** کار می‌کنند؛
#  پس ap0 از روی همان wlan0 ساخته می‌شود و کانال AP را از کانال لینک
#  کلاینت می‌گیریم (اگر STA روی 5GHz باشد و AP روی 2.4 بماند، AP بالا نمی‌آید
#  و SSID هیچ‌جا دیده نمی‌شود).
#
#  پله‌های تلاش در `on`:
#     ۱) ap0 با کانال لینک کلاینت       (هم وای‌فای کلاینت می‌ماند، هم هات‌اسپات)
#     ۲) ap0 بدون قید باند/کانال        (درایورهای سخت‌گیر)
#     ۳) خود wlan0 به‌صورت AP            (اتصال کلاینت قطع می‌شود، ولی حتماً
#                                        SSID دیده می‌شود و می‌شود از صفحهٔ
#                                        محلی وای‌فای تازه داد)
#
#  خروجی: یک خط JSON. رمز هرگز در لاگ/خروجی چاپ نمی‌شود.
# ============================================================
set -u

AP_CON="cryptochart-ap"
AP_IF="ap0"
AP_NET="10.42.0.0/24"
AP_GW="10.42.0.1/24"
AP_IP="10.42.0.1"
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

out_json() {   # out_json ok enabled ssid ip clients iface wlan error [mode]
  printf '{"ok":%s,"enabled":%s,"ssid":"%s","ip":"%s","clients":%s,"iface":"%s","wlan":"%s","error":"%s","mode":"%s"}\n' \
    "$1" "$2" "$(jesc "$3")" "$(jesc "$4")" "${5:-0}" "$(jesc "$6")" "$(jesc "$7")" "$(jesc "${8:-}")" "$(jesc "${9:-}")"
}

fail_json() { out_json false 0 "${SSID:-}" "" 0 "$AP_IF" "$(wlan_if)" "$1"; exit 0; }

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

# اینترفیسی که الان واقعاً آدرس هات‌اسپات را دارد (ap0 یا خود رادیو)
ap_if() {
  local i
  for i in "$AP_IF" "$(wlan_if)"; do
    [ -n "$i" ] || continue
    if ip -4 -o addr show "$i" 2>/dev/null | grep -q "inet $AP_IP/"; then
      printf '%s' "$i"
      return 0
    fi
  done
  printf '%s' "$AP_IF"
}

# کانال فعلی لینک کلاینت — AP باید روی همین کانال بالا بیاید
sta_channel() {
  local wlan
  wlan="$(wlan_if)"
  [ -n "$wlan" ] || return 0
  iw dev "$wlan" info 2>/dev/null | awk '/channel/ {print $2; exit}'
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

# profile <ifname> [band] [channel]  — پروفایل AP را برای آن اینترفیس می‌سازد
profile() {
  local ifc="${1:-$AP_IF}" band="${2:-}" chan="${3:-}" has=0
  [ -n "$ifc" ] || return 1
  nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$AP_CON" && has=1
  if [ "$has" != "1" ]; then
    nmcli connection add type wifi ifname "$ifc" con-name "$AP_CON" \
      ssid "$SSID" >/dev/null 2>&1 || return 1
  fi
  nmcli connection modify "$AP_CON" \
    connection.interface-name "$ifc" \
    connection.autoconnect yes \
    802-11-wireless.mode ap \
    802-11-wireless.ssid "$SSID" \
    ipv4.method shared \
    ipv4.addresses "$AP_GW" \
    ipv6.method ignore >/dev/null 2>&1 || return 1
  # کانال: اولویت با کانال لینک کلاینت (AP+STA روی یک رادیو باید هم‌کانال باشد)
  if [ -n "$chan" ]; then
    # باند و کانال جدا تنظیم می‌شوند: اگر یکی رد شود، دیگری هم اجرا شود
    nmcli connection modify "$AP_CON" 802-11-wireless.band "" >/dev/null 2>&1 || true
    nmcli connection modify "$AP_CON" 802-11-wireless.channel "$chan" \
      >/dev/null 2>&1 || true
  elif [ -n "$band" ]; then
    nmcli connection modify "$AP_CON" 802-11-wireless.band "$band" \
      802-11-wireless.channel 0 >/dev/null 2>&1 || true
  else
    nmcli connection modify "$AP_CON" 802-11-wireless.band "" \
      802-11-wireless.channel 0 >/dev/null 2>&1 || true
  fi
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

# up_ap <ifname> — بالا می‌آورد و منتظر می‌ماند تا آدرس 10.42.0.1 بیاید
up_ap() {
  local ifc="${1:-$AP_IF}" i=0
  [ -n "$ifc" ] || return 1
  nmcli connection down "$AP_CON" >/dev/null 2>&1 || true
  nmcli connection up "$AP_CON" >/dev/null 2>&1 || true
  while [ "$i" -lt 12 ]; do
    if ip -4 -o addr show "$ifc" 2>/dev/null | grep -q "inet $AP_IP/"; then
      return 0
    fi
    sleep 0.5
    i=$((i + 1))
  done
  return 1
}

# اینترنتِ وای‌فای کلاینت را با کلاینت‌های هات‌اسپات شریک می‌کند (اختیاری)
share_internet() {
  local out ap
  ap="$(ap_if)"
  out="$(nmcli -t -f DEVICE,STATE device 2>/dev/null \
         | awk -F: -v ap="$ap" '$2=="connected" && $1!=ap {print $1; exit}')"
  [ "$out" = "$ap" ] && out=""
  echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null || true
  if have nft; then
    nft list table ip cryptochart >/dev/null 2>&1 || nft add table ip cryptochart 2>/dev/null || true
    nft list chain ip cryptochart post >/dev/null 2>&1 \
      || nft 'add chain ip cryptochart post { type nat hook postrouting priority 100; }' 2>/dev/null || true
    nft list chain ip cryptochart fwd >/dev/null 2>&1 \
      || nft 'add chain ip cryptochart fwd { type filter hook forward priority 0; }' 2>/dev/null || true
    if nft list chain ip cryptochart post 2>/dev/null | grep -q '10\.42\.0\.0/24'; then :; else
      nft add rule ip cryptochart post ip saddr "$AP_NET" oifname != "$ap" masquerade 2>/dev/null || true
    fi
    if nft list chain ip cryptochart fwd 2>/dev/null | grep -q "iifname \"$ap\""; then :; else
      nft add rule ip cryptochart fwd iifname "$ap" accept 2>/dev/null || true
      nft add rule ip cryptochart fwd oifname "$ap" accept 2>/dev/null || true
    fi
  elif have iptables; then
    iptables -t nat -C POSTROUTING -s "$AP_NET" ! -o "$ap" -j MASQUERADE 2>/dev/null \
      || iptables -t nat -A POSTROUTING -s "$AP_NET" ! -o "$ap" -j MASQUERADE 2>/dev/null || true
    iptables -C FORWARD -i "$ap" -j ACCEPT 2>/dev/null \
      || iptables -A FORWARD -i "$ap" -j ACCEPT 2>/dev/null || true
    iptables -C FORWARD -o "$ap" -j ACCEPT 2>/dev/null \
      || iptables -A FORWARD -o "$ap" -j ACCEPT 2>/dev/null || true
  fi
}

# پورت 80 هات‌اسپات → صفحهٔ محلی 8080 (کلاینت فقط IP را در مرورگر می‌زند)
webshare() {
  local ap
  ap="$(ap_if)"
  if have nft; then
    nft list table ip cryptochart >/dev/null 2>&1 || nft add table ip cryptochart 2>/dev/null || true
    nft list chain ip cryptochart pre >/dev/null 2>&1 \
      || nft 'add chain ip cryptochart pre { type nat hook prerouting priority -100; }' 2>/dev/null || true
    if nft list chain ip cryptochart pre 2>/dev/null | grep -q 'redirect'; then :; else
      nft add rule ip cryptochart pre iifname "$ap" tcp dport 80 redirect to :8080 2>/dev/null || true
    fi
  elif have iptables; then
    iptables -t nat -C PREROUTING -i "$ap" -p tcp --dport 80 -j REDIRECT --to-ports 8080 2>/dev/null \
      || iptables -t nat -A PREROUTING -i "$ap" -p tcp --dport 80 -j REDIRECT --to-ports 8080 2>/dev/null || true
  fi
}

ap_active() {
  local ap
  ap="$(ap_if)"
  nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx "$AP_CON" && return 0
  ip -4 -o addr show "$ap" >/dev/null 2>&1 || return 1
  ip -4 -o addr show "$ap" 2>/dev/null | grep -q "inet $AP_IP/"
}

status() {
  local on=0 ip="" ssid="" clients=0 wlan ap mode=""
  wlan="$(wlan_if)"
  ap="$(ap_if)"
  ap_active && on=1
  ip="$(ip -4 -o addr show "$ap" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
  ssid="$(nmcli -t -f 802-11-wireless.ssid connection show "$AP_CON" 2>/dev/null \
          | awk -F: '{print $2; exit}')"
  [ -z "$ssid" ] && ssid="$SSID"
  clients="$(iw dev "$ap" station dump 2>/dev/null | grep -c '^Station' || true)"
  [ -z "$clients" ] && clients=0
  if [ "$ap" = "$AP_IF" ]; then mode="ap0"; elif [ -n "$ap" ]; then mode="direct"; fi
  out_json true "$on" "$ssid" "$ip" "$clients" "$ap" "$wlan" "" "$mode"
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

# تصویر کامل محیط برای عیب‌یابی (ابزارهای نصب‌شده، راننده، rfkill، کانال)
diag() {
  local wlan ap chan drv flags rf on
  wlan="$(wlan_if)"; ap="$(ap_if)"; chan="$(sta_channel)"
  drv=""
  [ -n "$wlan" ] && drv="$(basename "$(readlink -f "/sys/class/net/$wlan/device/driver" 2>/dev/null)" 2>/dev/null)"
  flags=""
  for t in nmcli iw rfkill nft iptables; do
    have "$t" && flags="$flags $t"
  done
  rf="?"
  if have rfkill; then
    if rfkill list wifi 2>/dev/null | grep -q 'blocked: yes'; then rf="blocked"; else rf="ok"; fi
  fi
  on=0; ap_active && on=1
  printf '{"ok":true,"mode":"diag","wlan":"%s","ap":"%s","channel":"%s","driver":"%s","tools":"%s","rfkill":"%s","enabled":%s,"ssid":"%s"}\n' \
    "$(jesc "$wlan")" "$(jesc "$ap")" "$(jesc "$chan")" "$(jesc "$drv")" \
    "$(jesc "${flags# }")" "$rf" "$on" "$(jesc "${SSID:-}")"
}

# ۱) با کانال لینک کلاینت  ۲) بدون قید  ۳) AP روی خود رادیو
bring_up() {
  local wlan chan
  wlan="$(wlan_if)"
  [ -n "$wlan" ] || return 1
  ensure_iface || return 1
  chan="$(sta_channel)"
  if [ -n "$chan" ] && profile "$AP_IF" "" "$chan" && up_ap "$AP_IF"; then
    printf 'ap0'
    return 0
  fi
  if profile "$AP_IF" bg && up_ap "$AP_IF"; then
    printf 'ap0'
    return 0
  fi
  # آخرین راه: خود رادیو AP می‌شود (SSID حتماً دیده می‌شود، اتصال کلاینت می‌رود)
  iw dev "$AP_IF" del >/dev/null 2>&1 || true
  if profile "$wlan" bg && up_ap "$wlan"; then
    printf 'direct'
    return 0
  fi
  return 1
}

case "$CMD" in
  on)
    load_state >/dev/null 2>&1 || true
    [ -z "${SSID:-}" ] && SSID="CryptoChart"
    have nmcli || fail_json "nmcli نیست (network-manager نصب شود)"
    have iw || fail_json "iw نیست — روی ماژول: sudo apt install -y iw dnsmasq-base"
    have rfkill && { rfkill unblock wifi >/dev/null 2>&1 || true; }
    mode="$(bring_up)" || fail_json "هات‌اسپات بالا نیامد — لاگ: journalctl -u cryptochart -n 50"
    share_internet
    webshare
    status
    ;;
  off)
    nmcli connection down "$AP_CON" >/dev/null 2>&1 || true
    if have iw; then
      iw dev "$AP_IF" del >/dev/null 2>&1 || true
    fi
    out_json true 0 "${SSID:-}" "" 0 "$AP_IF" "$(wlan_if)" "" ""
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
  diag)
    load_state >/dev/null 2>&1 || true
    diag
    ;;
  *)
    fail_json "unknown command: $CMD"
    ;;
esac
