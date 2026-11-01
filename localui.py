#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
صفحهٔ محلی ماژول (لوکال‌یوآی) — فقط با HTTP، بدون وابستگی خارجی
----------------------------------------------------------------
وقتی با گوشی/لپ‌تاپ به هات‌اسپات خود ماژول وصل می‌شوید، این صفحه روی
`http://10.42.0.1:8080` بالا می‌آید و همین‌جا می‌شود:

  * وضعیت ماژول: شبکهٔ فعلی، IP، نسخه، وضعیت هات‌اسپات و کلاینت‌هایش
  * اسکن و اتصال به وای‌فای (همان nmcli که وب‌اپ استفاده می‌کند)
  * روشن/خاموش کردن هات‌اسپات با SSID/رمز دلخواه
  * کنسول: تِیل لاگ سرویس (`journalctl -u cryptochart`) + راهنمای SSH

امنیت: کارهای کنترلی فقط از شبکهٔ هات‌اسپات (10.42.0.0/24) یا خود دستگاه
پذیرفته می‌شود؛ از LAN بقیه فقط خواندن وضعیت. صفحه روی همهٔ اینترفیس‌ها
گوش می‌دهد، پس محدودسازی درخواستی است، نه وابسته به bind.
"""

import json
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wifi import scan as wifi_scan, current as wifi_current, connect as wifi_connect

try:
    import hotspot
except Exception:            # ماژول خراب نباید صفحهٔ محلی را از کار بیندازد
    hotspot = None

DEFAULT_PORT = 8080
AP_PREFIX = '10.42.'
LOCAL_IPS = ('127.0.0.1', '::1', 'localhost')

STATE = {'hotspot': {}, 'version': '', 'started': 0.0}
_LOCK = threading.Lock()
_SERVER = None


# ----------------------------- کمکی‌ها -----------------------------
def _run(args, timeout=20):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def service_log(lines=60):
    """تِیل لاگ سرویس — کنسول خواندنی برای عیب‌یابی روی گوشی."""
    try:
        n = max(10, min(int(lines), 200))
    except (TypeError, ValueError):
        n = 60
    code, out = _run(['journalctl', '-u', 'cryptochart', '-n', str(n),
                      '--no-pager', '-o', 'cat'], timeout=12)
    if code == 0 and out.strip():
        return out[-20000:]
    # بدون systemd (اجرای دستی) → لاگ فایل خود ماژول اگر بود
    for path in ('/opt/cryptochart/cryptochart.log', 'cryptochart.log'):
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                return ''.join(f.readlines()[-n:])[-20000:]
        except OSError:
            continue
    return out[-4000:] or 'no logs yet'


def snapshot():
    ssid, ip = wifi_current()
    hs = dict(STATE.get('hotspot') or {})
    if hotspot and not hs:
        try:
            hs = hotspot.summary()
        except Exception:
            hs = {}
    return {
        'ok': True,
        'wifi': {'connected': ssid, 'ip': ip},
        'hotspot': {k: hs.get(k) for k in ('enabled', 'ssid', 'ip', 'clients', 'error')},
        'version': STATE.get('version') or '',
        'localUrl': ('http://%s:%d' % (hs.get('ip') or '10.42.0.1', DEFAULT_PORT)),
        'uptime': int(STATE.get('started') or 0),
    }


# ----------------------------- HTTP -----------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = 'CryptoChartLocal/1.0'

    def log_message(self, *a):          # لاگ پیش‌فرض پرحرف است
        pass

    # ---- ابزار پاسخ ----
    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), 'application/json; charset=utf-8')

    def _client_ip(self):
        return (self.client_address[0] or '').strip()

    def _is_local(self):
        ip = self._client_ip()
        # ::ffff:10.42.0.2 → IPv4 داخل IPv6
        if ip.startswith('::ffff:'):
            ip = ip[7:]
        return ip in LOCAL_IPS or ip.startswith(AP_PREFIX)

    def _body(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 8192:
            return {}
        try:
            j = json.loads(self.rfile.read(n).decode('utf-8', 'replace'))
        except (ValueError, OSError):
            return {}
        return j if isinstance(j, dict) else {}

    # ---- GET ----
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip('/') or '/'
        if path == '/':
            self._send(200, PAGE.replace('{{LOCAL}}', self._client_ip()), 'text/html; charset=utf-8')
            return
        if path == '/api/status':
            self._json(snapshot())
            return
        if path == '/api/log':
            if not self._is_local():
                self._json({'ok': False, 'error': 'connect to the module hotspot first'}, 403)
                return
            q = dict(urllib.parse.parse_qsl(u.query))
            self._json({'ok': True, 'log': service_log(q.get('n') or 60)})
            return
        self._send(404, 'not found', 'text/plain; charset=utf-8')

    # ---- POST ----
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip('/') or '/'
        if not self._is_local():
            # کنترل ماژول فقط از شبکهٔ خودش (یا خود دستگاه)
            self._json({'ok': False,
                        'error': 'connect to the module hotspot (10.42.0.x) to change settings'}, 403)
            return
        body = self._body()
        if path == '/api/scan':
            try:
                nets = wifi_scan()
            except Exception as e:
                self._json({'ok': False, 'error': str(e)[:120]})
                return
            self._json({'ok': True, 'scan': nets})
            return
        if path == '/api/connect':
            ssid = str(body.get('ssid') or '').strip()[:32]
            password = str(body.get('pass') or '')
            if not ssid:
                self._json({'ok': False, 'error': 'ssid required'})
                return
            try:
                ok, info = wifi_connect(ssid, password)
            except Exception as e:
                self._json({'ok': False, 'error': str(e)[:120]})
                return
            if ok:
                self._json({'ok': True, 'ssid': ssid, 'ip': info})
            else:
                self._json({'ok': False, 'error': info})
            return
        if path == '/api/hotspot':
            if not hotspot:
                self._json({'ok': False, 'error': 'hotspot module unavailable'})
                return
            cfg = {'enabled': bool(body.get('enabled')),
                   'ssid': body.get('ssid') or '',
                   'pass': body.get('pass') or ''}
            try:
                ok = hotspot.apply(cfg, force=True)
                info = hotspot.summary()
            except Exception as e:
                self._json({'ok': False, 'error': str(e)[:120]})
                return
            STATE['hotspot'] = info
            self._json({'ok': ok, 'hotspot': info,
                        'error': '' if ok else (info.get('error') or 'hotspot failed')})
            return
        self._json({'ok': False, 'error': 'unknown action'}, 404)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(port=DEFAULT_PORT, state=None, host='0.0.0.0'):
    """راه‌اندازی در ترد جدا؛ اگر پورت اشغال بود، استثنا می‌دهد (صداکننده می‌گیرد)."""
    global _SERVER
    if state:
        STATE.update(state)
    if not STATE.get('started'):
        import time
        STATE['started'] = time.time()
    srv = _Server((host, int(port)), Handler)
    _SERVER = srv
    t = threading.Thread(target=srv.serve_forever, name='localui', daemon=True)
    t.start()
    return srv


def stop():
    global _SERVER
    if _SERVER:
        try:
            _SERVER.shutdown()
        except Exception:
            pass
        _SERVER = None


PAGE = """<!doctype html>
<html lang="fa" dir="rtl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CryptoChart — Module</title>
<style>
:root{--bg:#0f1216;--card:#171c22;--line:#26303a;--txt:#e8eef5;--dim:#93a1b1;--a:#22d3a5;--r:#ff5d5d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 system-ui,Segoe UI,Tahoma,sans-serif}
header{padding:16px 16px 8px;border-bottom:1px solid var(--line)}
h1{margin:0;font-size:18px}
h1 span{color:var(--a)}
main{padding:14px;max-width:640px;margin:auto;display:grid;gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
h2{margin:0 0 10px;font-size:14px;color:var(--dim);font-weight:600}
.row{display:flex;justify-content:space-between;gap:8px;padding:3px 0}
.row b{font-variant-numeric:tabular-nums}
button{cursor:pointer;border:1px solid var(--line);background:#1e252d;color:var(--txt);
  border-radius:9px;padding:9px 12px;font:inherit}
button.primary{background:var(--a);border-color:var(--a);color:#04120d;font-weight:700}
button.ghost{background:transparent}
button:disabled{opacity:.55;cursor:default}
input{width:100%;padding:9px 10px;border-radius:9px;border:1px solid var(--line);
  background:#12171d;color:var(--txt);font:inherit}
.field{display:grid;gap:6px;margin:8px 0}
.net{display:flex;justify-content:space-between;align-items:center;width:100%;
  background:#141a20;border:1px solid var(--line);border-radius:9px;padding:9px 10px;margin:6px 0}
.net.active{border-color:var(--a)}
.muted{color:var(--dim);font-size:13px}
pre{background:#0b0e12;border:1px solid var(--line);border-radius:9px;padding:10px;
  max-height:280px;overflow:auto;font-size:12px;direction:ltr;text-align:left;white-space:pre-wrap}
.err{color:var(--r)}.ok{color:var(--a)}
code{background:#0b0e12;padding:1px 5px;border-radius:5px;direction:ltr;display:inline-block}
</style></head>
<body>
<header><h1>CryptoChart <span>module</span></h1>
<div class="muted">connected from {{LOCAL}}</div></header>
<main>
  <div class="card">
    <h2>وضعیت / Status</h2>
    <div class="row"><span>شبکهٔ وای‌فای / Wi-Fi</span><b id="wifiSsid">—</b></div>
    <div class="row"><span>IP ماژول / Module IP</span><b id="wifiIp">—</b></div>
    <div class="row"><span>آدرس این صفحه</span><b id="pageUrl">http://10.42.0.1</b></div>
    <div class="row"><span>هات‌اسپات / Hotspot</span><b id="hsState">—</b></div>
    <div class="row"><span>SSID هات‌اسپات</span><b id="hsSsid">—</b></div>
    <div class="row"><span>آدرس هات‌اسپات</span><b id="hsIp">—</b></div>
    <div class="row"><span>کلاینت‌ها / Clients</span><b id="hsClients">0</b></div>
    <div class="row"><span>نسخه / Version</span><b id="ver">—</b></div>
    <p class="muted">کنسول کامل: <code>ssh <span id="sshUser">pi</span>@<span id="sshHost">10.42.0.1</span></code></p>
  </div>

  <div class="card">
    <h2>وای‌فای / Wi-Fi</h2>
    <p class="muted ok" id="connNow">—</p>
    <button id="btnScan" class="primary">🔍 اسکن شبکه‌ها / Scan</button>
    <div id="nets"></div>
    <div class="field" id="connBox" hidden>
      <div class="muted">اتصال به: <b id="connSsid"></b></div>
      <input id="connPass" type="password" maxlength="64" placeholder="رمز وای‌فای / password">
      <div style="display:flex;gap:8px">
        <button id="btnConnect" class="primary">اتصال / Connect</button>
        <button id="btnCancel" class="ghost">لغو</button>
      </div>
    </div>
    <p class="muted" id="wifiHint">اگر اتصال شکست بخورد، ماژول به شبکهٔ قبلی برمی‌گردد.</p>
  </div>

  <div class="card">
    <h2>هات‌اسپات / Hotspot</h2>
    <div class="field"><span class="muted">SSID</span><input id="hsSsidIn" maxlength="32" placeholder="CryptoChart"></div>
    <div class="field"><span class="muted">رمز (خالی = شبکهٔ باز، حداقل ۸ کاراکتر)</span>
      <input id="hsPassIn" type="password" maxlength="63" placeholder="••••••••"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button id="btnHsOn" class="primary">روشن / On</button>
      <button id="btnHsOff" class="ghost">خاموش / Off</button>
    </div>
    <p class="muted" id="hsHint">هات‌اسپات هم‌زمان با اتصال به وای‌فای کار می‌کند (اینترفیس مجازی ap0).</p>
  </div>

  <div class="card">
    <h2>کنسول / Console</h2>
    <div style="display:flex;gap:8px;margin-bottom:8px">
      <button id="btnLog" class="ghost">بازخوانی / Refresh log</button>
    </div>
    <pre id="log">…</pre>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
async function api(path, body) {
  const opt = body ? {method:'POST', headers:{'Content-Type':'application/json'},
                      body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  let j = {};
  try { j = await r.json(); } catch (e) {}
  if (!r.ok && !j.error) j.error = 'HTTP ' + r.status;
  return j;
}
function renderStatus(s) {
  $('wifiSsid').textContent = (s.wifi && s.wifi.connected) || '—';
  $('wifiIp').textContent = (s.wifi && s.wifi.ip) || '—';
  const h = s.hotspot || {};
  $('hsState').textContent = h.enabled ? 'روشن / on' : 'خاموش / off';
  $('hsState').className = h.enabled ? 'ok' : '';
  $('hsSsid').textContent = h.ssid || '—';
  $('hsIp').textContent = h.ip || '—';
  $('hsClients').textContent = h.clients || 0;
  $('ver').textContent = s.version || '—';
  const w = s.wifi || {};
  $('connNow').textContent = w.connected
    ? ('✓ وصل شده به ' + w.connected + (w.ip ? '  —  ' + w.ip : ''))
    : 'به هیچ وای‌فایی وصل نیست — یک شبکه انتخاب کنید';
  $('connNow').className = w.connected ? 'muted ok' : 'muted';
  if (h.ip) { $('sshHost').textContent = h.ip; }
  if (h.ip) { $('pageUrl').textContent = 'http://' + h.ip; }
  if (h.error) { $('hsHint').textContent = h.error; $('hsHint').className = 'muted err'; }
  if (!$('hsSsidIn').value && h.ssid) $('hsSsidIn').value = h.ssid;
}
function renderNets(nets) {
  const box = $('nets'); box.innerHTML = '';
  (nets || []).forEach((n) => {
    const b = document.createElement('button');
    b.className = 'net' + (n.active ? ' active' : '');
    b.innerHTML = '<span>' + n.ssid + (n.active ? ' ✓' : '') + '</span>' +
                  '<span class="muted">' + (n.signal || 0) + '% ' +
                  (n.security === 'open' ? '🔓' : '🔒') + '</span>';
    b.onclick = () => {
      if (n.active) return;
      $('connBox').hidden = false;
      $('connSsid').textContent = n.ssid;
      $('connSsid').dataset.ssid = n.ssid;
      $('connPass').focus();
    };
    box.appendChild(b);
  });
}
async function refresh() {
  try { renderStatus(await api('/api/status')); } catch (e) {}
}
async function loadLog() {
  $('log').textContent = '…';
  const j = await api('/api/log?n=80');
  $('log').textContent = j.log || j.error || '—';
}
async function loadScan(auto) {
  const b = $('btnScan');
  b.disabled = true; b.textContent = '… در حال اسکن / scanning';
  const j = await api('/api/scan', {});
  b.disabled = false; b.textContent = '🔍 اسکن شبکه‌ها / Scan';
  if (j.ok) {
    renderNets(j.scan);
    const n = (j.scan || []).length;
    $('wifiHint').textContent = n ? (n + ' شبکه پیدا شد — روی نام شبکه بزنید و رمز را وارد کنید.')
                                  : 'شبکه‌ای پیدا نشد — دوباره اسکن کنید.';
    $('wifiHint').className = 'muted';
  } else {
    $('wifiHint').textContent = j.error || 'scan failed';
    $('wifiHint').className = 'muted err';
  }
}
$('btnScan').onclick = () => loadScan(false);
$('btnCancel').onclick = () => { $('connBox').hidden = true; };
$('btnConnect').onclick = async () => {
  const ssid = $('connSsid').dataset.ssid;
  const pass = $('connPass').value;
  if (!ssid) return;
  $('wifiHint').textContent = 'اتصال… / connecting…';
  const j = await api('/api/connect', {ssid, pass});
  $('wifiHint').textContent = j.ok ? ('✓ متصل شد — ' + (j.ip || '')) : ('✗ ' + (j.error || ''));
  $('wifiHint').className = j.ok ? 'muted ok' : 'muted err';
  $('connPass').value = '';
  $('connBox').hidden = true;
  refresh();
  if (j.ok) setTimeout(() => { loadScan(true); refresh(); }, 3000);
};
async function setHotspot(enabled) {
  const ssid = $('hsSsidIn').value.trim() || 'CryptoChart';
  const pass = $('hsPassIn').value;
  $('hsHint').textContent = '… applying';
  $('hsHint').className = 'muted';
  const j = await api('/api/hotspot', {enabled, ssid, pass});
  $('hsHint').textContent = j.ok ? (enabled ? '✓ روشن شد' : '✓ خاموش شد')
                                 : ('✗ ' + (j.error || ''));
  $('hsHint').className = j.ok ? 'muted ok' : 'muted err';
  $('hsPassIn').value = '';
  refresh();
}
$('btnHsOn').onclick = () => setHotspot(true);
$('btnHsOff').onclick = () => setHotspot(false);
$('btnLog').onclick = loadLog;
refresh(); loadLog(); loadScan(true);
setInterval(refresh, 5000);
</script>
</body></html>
"""
