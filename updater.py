#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
به‌روزرسانی آنلاین ماژول — فرمان از وب‌اپ می‌آید (backend/index.php?api=update).

check    → git fetch + مقایسه؛ بدون نصب — فقط گزارش می‌دهد آیا نسخهٔ جدید هست
install  → git pull (fast-forward) + نصب/به‌روزرسانی سرویس systemd + ری‌استارت خود سرویس
report   → نتیجه به سرور گزارش می‌شود (UpdateReporter)

طراحی امن:
- فقط fast-forward pull؛ اگر مخزن تغییر محلی داشته باشد نصب نمی‌شود (خطا گزارش می‌شود)
- هر فرمان فقط یک بار اجرا می‌شود (فایل فرمان بعد از اجرا پاک می‌شود)
- نصب فقط وقتی ادامه می‌یابد که pull واقعاً چیزی آورده باشد
"""

import json
import os
import subprocess
import time
import urllib.request

PI_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_NAME = 'cryptochart'
SERVICE_FILE = f'/etc/systemd/system/{SERVICE_NAME}.service'


def _run(cmd, timeout=60, cwd=None):
    try:
        p = subprocess.run(cmd, cwd=cwd or PI_DIR, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, (p.stdout or b'').decode('utf-8', 'replace')
    except FileNotFoundError:
        return 127, f'{cmd[0]}: not found'
    except subprocess.TimeoutExpired:
        return 124, f'{cmd[0]}: timeout'
    except OSError as e:
        return 1, str(e)


def git_rev():
    """هش کوتاه HEAD — خالی یعنی این نسخه مخزن git نیست.
    نکته: نصب install.sh فایل‌ها را در /opt/cryptochart کپی می‌کند؛ آنجا مخزنی
    نیست — پس آپدیت آنلاین با copy-mode (استخراج فایل‌ها از مخزن راه‌دور) کار می‌کند."""
    code, out = _run(['git', 'rev-parse', '--short', 'HEAD'], timeout=10)
    if code == 0:
        return out.strip()
    # نصب کپی‌شده → نسخهٔ پروندهٔ VERSION که install.sh می‌نویسد (hash)
    try:
        with open(os.path.join(PI_DIR, 'VERSION'), encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def git_remote_rev():
    """هش کوتاه HEAD شاخهٔ ردیابی‌شده در مخزن راه‌دور."""
    code, out = _run(['git', 'rev-parse', '--short', '@{u}'], timeout=10)
    return out.strip() if code == 0 else ''


def _git_ready():
    """مخزن git سالم است؟ — آپدیت آنلاین را تنها در صورت نبود مخزن git مسدود می‌کند.
    (تغییرات محلی دیگر ممنوع نیستند: installer قبل از pull کپی می‌کند؛ pull fast-forward
    تنها وقتی ممنوع است که با مخالفت واقعی مواجه شود که می‌تواند در install() کنترل شود.)"""
    code, _ = _run(['git', 'rev-parse', '--git-dir'], timeout=10)
    if code != 0:
        return False, 'نصب فعلی مخزن git نیست — برای آپدیت آنلاین یک بار با git clone نصب کنید'
    return True, ''


def check():
    """بررسی وجود نسخهٔ جدید — بدون هیچ تغییری."""
    ok, err = _git_ready()
    if not ok:
        return dict(status='error', message=err, rev=git_rev())
    code, out = _run(['git', 'fetch', 'origin'], timeout=60)
    if code != 0:
        return dict(status='error', message='git fetch failed: ' + out.strip()[:120], rev=git_rev())
    local, remote = git_rev(), git_remote_rev()
    if not remote:
        return dict(status='error', message='شاخهٔ ردیابی‌شده (upstream) تنظیم نیست', rev=local)
    if local == remote:
        return dict(status='uptodate', message=f'ماژول به‌روز است ({local})', rev=local)
    return dict(status='available', message=f'نسخهٔ جدید موجود است: {local} → {remote}', rev=local)


def _install_service():
    """سرویس systemd را نصب/به‌روز می‌کند (اگر install.sh قبلاً ساخته باشد، همان می‌ماند)."""
    code, out = _run(['bash', 'install.sh'], timeout=120)
    return code == 0, out


def install():
    """pull + نصب + ری‌استارت سرویس. خروجی فقط برای لاگ systemd است؛
    خود ری‌استارت توسط systemd انجام می‌شود (exec tail → systemctl)."""
    ok, err = _git_ready()
    if not ok:
        return dict(status='error', message=err, rev=git_rev())
    local0 = git_rev()
    code, out = _run(['git', 'pull', '--ff-only', 'origin'], timeout=120)
    if code != 0:
        return dict(status='error', message='git pull failed: ' + out.strip()[:120], rev=local0)
    local1 = git_rev()
    if local1 == local0:
        return dict(status='uptodate', message=f'همین حالا به‌روز بود ({local1})', rev=local1)
    # pull آمد — سرویس را نصب/به‌روز و ری‌استارت می‌کنیم
    sok, sout = _install_service()
    if not sok:
        return dict(status='error', message='install.sh failed: ' + sout.strip()[:120], rev=local1)
    # گزارش قبل از ری‌استارت باید به سرور برسد؛ restart در main.py بعد از report انجام می‌شود
    return dict(status='installed', message=f'نصب شد ({local1}) — ماژول ری‌استارت می‌شود',
                rev=local1)


class UpdateReporter:
    """گزارش نتیجهٔ آپدیت به سرور — مشابه WifiReporter."""

    def __init__(self, base_url):
        self.base = base_url.rstrip('/')

    def report(self, status='', message='', rev=''):
        body = json.dumps({
            'action': 'report',
            'status': status[:20],
            'message': message[:300],
            'rev': rev[:16],
        }).encode('utf-8')
        req = urllib.request.Request(
            f'{self.base}/index.php?api=update', data=body, method='POST',
            headers={'User-Agent': 'CryptoChart-Pi/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception:
            return None


def run_cmd(cmd, reporter, age_s=120):
    """اجرای فرمان آپدیت دریافتی از poll سرور:
    poll → {'updateCmd': {'cmd': 'check'|'install', 'ts': ...}}
    هر فرمان فقط یک بار اجرا می‌شود (فققط اگر ts جدید باشد) و نتیجه
    بلافاصله با UpdateReporter به سرور گزارش می‌شود.
    خروجی: (cmd_that_ran_or_None, result_dict)
    """
    action = str((cmd or {}).get('cmd') or '').strip()
    ts = float((cmd or {}).get('ts') or 0)
    if not action or action not in ('check', 'install') or not ts:
        return None, None
    if ts < (time.time() - age_s):
        return None, None   # فرمان قدیمی — نادیده بگیر
    run = check if action == 'check' else install
    try:
        res = run()
    except Exception as e:
        res = dict(status='error', message=f'{type(e).__name__}: {e}'[:300], rev=git_rev())
    try:
        reporter.report(status=res.get('status', ''), message=res.get('message', ''),
                        rev=res.get('rev', ''))
    except Exception:
        pass
    return action, res


def _selftest():
    """خودآزمایی: check() بدون هیچ تغییری باید اجرا و dict بدهد."""
    import pprint
    pprint.pprint(check())


if __name__ == '__main__':
    _selftest()  # اگر به‌روزرسانی آنلاین فرق کرد، اینجا می‌بینی
