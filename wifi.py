#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WiFi Manager — مدیریت وای‌فای رزبری از راه دور
------------------------------------------------
nmcli را اجرا می‌کند:
  * scan      → لیست شبکه‌ها با قدرت سیگنال
  * connect   → اتصال به شبکه با پسورد (ذخیرهٔ دائمی پروفایل)
نکتهٔ امنیتی: پسورد فقط در حافظه می‌ماند و به nmcli داده می‌شود؛
در لاگ‌ها هرگز نوشته نمی‌شود.
"""

import subprocess
import urllib.parse
import urllib.request
import json


def _run(args, timeout=25):
    """اجرای امن بدون shell — آرگومان‌ها لیست هستند (بدون ریسک تزریق)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, str(e)


def scan():
    """اسکن شبکه‌ها: [(ssid, signal, security), ...]"""
    _run(['nmcli', 'device', 'wifi', 'rescan'], timeout=8)
    code, out = _run(['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY,ACTIVE',
                      'device', 'wifi', 'list'])
    nets, seen = [], set()
    for line in (out or '').splitlines():
        parts = line.split(':')
        if len(parts) < 4:
            continue
        ssid, sig, sec, active = parts[0], parts[1], parts[2], parts[3]
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        try:
            sig = int(sig)
        except ValueError:
            sig = 0
        nets.append({
            'ssid': ssid[:32],
            'signal': sig,
            'security': 'open' if sec in ('', '--') else 'wpa',
            'active': active == 'yes',
        })
    nets.sort(key=lambda n: (-int(n['active']), -n['signal']))
    return nets[:15]


def current():
    """نام شبکه و IP فعلی (اگر وای‌فای متصل باشد)."""
    code, out = _run(['nmcli', '-t', '-f', 'NAME,TYPE,DEVICE', 'connection', 'show', '--active'])
    ssid = ''
    for line in (out or '').splitlines():
        parts = line.split(':')
        if len(parts) >= 3 and parts[1] == '802-11-wireless':
            ssid = parts[0]
            break
    code, out = _run(['hostname', '-I'])
    ip = (out or '').split()[0] if (out or '').split() else ''
    return ssid, ip


def connect(ssid, password):
    """اتصال به شبکه — پروفایل دائمی ذخیره می‌شود تا در بوت بعدی خودش وصل شود."""
    if password:
        code, out = _run(['nmcli', 'device', 'wifi', 'connect', ssid,
                          'password', password], timeout=40)
    else:
        code, out = _run(['nmcli', 'device', 'wifi', 'connect', ssid], timeout=40)
    if code != 0:
        # تلاش دوم: اتصال به پروفایل موجود
        code2, out2 = _run(['nmcli', 'connection', 'up', ssid], timeout=30)
        if code2 != 0:
            msg = (out or out2 or '').strip().splitlines()
            err = msg[-1] if msg else 'unknown error'
            return False, err[:120]
    ssid_now, ip = current()
    return True, ip


class WifiReporter:
    """ارسال گزارش وضعیت وای‌فای به سرور."""

    def __init__(self, base_url):
        self.base = base_url.rstrip('/')

    def report(self, scan=None, status='', connected='', ip='', error=''):
        body = json.dumps({
            'action': 'report',
            'scan': scan,
            'status': status,
            'connected': connected,
            'ip': ip,
            'error': error[:200],
        }).encode('utf-8')
        req = urllib.request.Request(
            f'{self.base}/index.php?api=wifi', data=body, method='POST',
            headers={'User-Agent': 'CryptoChart-Pi/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception:
            return {}
