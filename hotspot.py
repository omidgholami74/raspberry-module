#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
هات‌اسپات ماژول — پوستهٔ نازک روی hotspot.sh
------------------------------------------------
ماژول هم به وای‌فای کلاینت وصل می‌ماند و هم شبکهٔ خودش را پخش می‌کند
(seamless AP+STA روی همان رادیو؛ اینترفیس مجازی ap0).

  * apply(cfg)  → on/off بر اساس کانفیگ سرور ({'enabled','ssid','pass'})
  * status()    → وضعیت زنده (SSID/IP/تعداد کلاینت) با کش کوتاه
  * ensure()    → در بوت، از hotspot.json آخرین کانفیگ را بالا می‌آورد
                  (حتی وقتی هنوز شبکه/سرور در دسترس نیست)

کارهای روت با hotspot.sh انجام می‌شود؛ این ماژول با قاعدهٔ باریک sudo
(`${RUN_USER} … NOPASSWD: /usr/bin/bash /opt/cryptochart/hotspot.sh …`)
صدایش می‌زند. هیچ‌جا رمز چاپ نمی‌شود و هیچ استثنایی به حلقهٔ چارت نمی‌رسد.
"""

import json
import os
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# مسیر فایل حالت (تست/دستی قابل جابه‌جایی است)
STATE_FILE = os.environ.get('CRYPTOCHART_HOTSPOT_STATE') \
    or os.path.join(HERE, 'hotspot.json')
STATUS_TTL = 10          # ثانیه — وضعیت در هر poll دوباره از nmcli پرسیده نشود
APPLY_TIMEOUT = 70

_DEFAULTS = {'enabled': False, 'ssid': 'CryptoChart', 'pass': ''}

_last_status = {}
_last_status_ts = 0.0
_applied_key = None
_last_error = ''


def _script():
    p = os.path.join(HERE, 'hotspot.sh')
    return p if os.path.isfile(p) else ''


def _run(args, timeout):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _parse(out):
    """آخرین خط JSON خروجی اسکریپت → dict (یا None)."""
    for line in reversed([x.strip() for x in (out or '').splitlines() if x.strip()]):
        if not line.startswith('{'):
            continue
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if isinstance(j, dict):
            return j
    return None


def _call(cmd, timeout=APPLY_TIMEOUT):
    """اجرای hotspot.sh. اول با sudo بدون رمز، بعد (اگر polkit اجازه بدهد) مستقیم.

    SSID/رمز با آرگومان نمی‌روند؛ write_state() آن‌ها را در hotspot.json
    می‌نویسد و اسکریپت همان را می‌خواند (رمز در `ps` دیده نشود).
    """
    sh = _script()
    if not sh:
        return {'ok': False, 'error': 'hotspot.sh در پوشهٔ ماژول نیست'}
    tries = []
    if os.geteuid() == 0:
        tries.append(['bash', sh, cmd])
    else:
        tries.append(['sudo', '-n', 'bash', sh, cmd])
        tries.append(['bash', sh, cmd])
    last = ''
    for args in tries:
        try:
            code, out = _run(args, timeout)
        except Exception as e:          # sudo/nmbcli غایب، pty خراب و…
            last = '%s: %s' % (type(e).__name__, str(e)[:160])
            continue
        j = _parse(out)
        if j is not None:
            if 'ok' not in j:
                j['ok'] = (code == 0)
            return j
        last = (out or '').strip()[-200:]
        if code != 0 and 'sudo' in args[0]:
            continue
    return {'ok': False, 'error': last or 'hotspot.sh خروجی JSON نداد'}


def norm(cfg):
    """کانفیگ سرور → {'enabled','ssid','pass'} با مقادیر امن."""
    if not isinstance(cfg, dict):
        return dict(_DEFAULTS)
    ssid = str(cfg.get('ssid') or '').strip()[:32] or _DEFAULTS['ssid']
    password = str(cfg.get('pass') or '')
    if password and len(password) < 8:
        password = ''          # WPA2 کمتر از ۸ کاراکتر قبول نمی‌کند
    return {
        'enabled': bool(cfg.get('enabled')),
        'ssid': ssid,
        'pass': password[:63],
    }


def write_state(cfg):
    """کانفیگ را برای بوتِ بعدی (ensure) ذخیره می‌کند؛ فقط خواندنی برای روت."""
    data = norm(cfg)
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass
    return data


def read_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return norm(json.load(f))
    except (OSError, ValueError):
        return dict(_DEFAULTS)


def ensure():
    """بوت: اگر آخرین کانفیگ «روشن» بود، هات‌اسپات را بالا بیاور (بدون شبکه)."""
    if _script():
        _call('ensure', timeout=APPLY_TIMEOUT)
    return status(force=True)


def apply(cfg, force=False):
    """اعمال کانفیگ سرور؛ فقط وقتی چیزی عوض شده (یا force) فرمان می‌رود."""
    global _applied_key, _last_error
    data = write_state(cfg)
    key = (data['enabled'], data['ssid'], data['pass'])
    if not force and key == _applied_key:
        return True
    res = _call('on' if data['enabled'] else 'off')
    ok = bool(res.get('ok'))
    if ok:
        _applied_key = key
        _last_error = ''
        _remember(res)
    else:
        _last_error = str(res.get('error') or '')[:160]
    return ok


def _remember(res):
    global _last_status, _last_status_ts
    _last_status = res
    _last_status_ts = time.time()


def status(force=False):
    """وضعیت هات‌اسپات (کش ۱۰ ثانیه‌ای). هرگز استثنا نمی‌دهد."""
    global _last_error
    if not force and _last_status and (time.time() - _last_status_ts) < STATUS_TTL:
        return _last_status
    if not _script():
        return {'ok': False, 'enabled': 0, 'error': 'hotspot.sh نیست'}
    res = _call('status', timeout=30)
    if 'enabled' not in res:
        res['enabled'] = 1 if res.get('ok') else 0
    res['error'] = res.get('error') or _last_error
    _remember(res)
    return res


def local_url():
    """آدرس صفحهٔ محلی ماژول از دید کلاینت هات‌اسپات."""
    ip = str((status() or {}).get('ip') or '')
    return f'http://{ip}:8080' if ip else ''


def summary():
    """خلاصهٔ کوتاه برای گزارش به سرور/صفحهٔ وب‌اپ."""
    s = status() or {}
    return {
        'enabled': bool(s.get('enabled')),
        'ssid': str(s.get('ssid') or ''),
        'ip': str(s.get('ip') or ''),
        'clients': int(s.get('clients') or 0),
        'error': str(s.get('error') or '')[:120],
    }
