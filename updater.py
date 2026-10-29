#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
به‌روزرسانی آنلاین ماژول — فرمان از وب‌اپ می‌آید (backend/index.php?api=update).

check    → مقایسهٔ نسخهٔ نصب‌شده با مخزن راه‌دور؛ بدون هیچ تغییری
install  → آپدیت + نصب/به‌روزرسانی سرویس systemd + ری‌استارت خود سرویس
report   → نتیجه به سرور گزارش می‌شود (UpdateReporter)

دو حالت نصب پشتیبانی می‌شود و هر دو از «Check for updates» کار می‌کنند:
  1) نصب git (مخزنی در پوشهٔ ماژول یا بالاتر):  git pull --ff-only
  2) نصب کپی‌شده (install.sh فایل‌ها را در /opt/cryptochart کپی می‌کند و
     .git ندارد): آرشیو همان commit از مخزن راه‌دور دانلود و فایل‌های pi/
     کپی می‌شوند. این مسیر به هیچ ابزار بیرونی نیاز ندارد (urllib + tarfile
     از کتابخانهٔ استاندارد پایتون)، پس روی Pi Zero هم کار می‌کند.

آدرس مخزن: از git (حالت ۱)، وگرنه فایل REPO_URL که install.sh می‌نویسد،
وگرنه DEFAULT_REPO.
"""

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PI_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_NAME = 'cryptochart'
SERVICE_FILE = f'/etc/systemd/system/{SERVICE_NAME}.service'
DEFAULT_REPO = 'https://github.com/omidgholami74/rasberypicrypto.git'
DEFAULT_BRANCH = 'main'

# فایل‌هایی که ماژول از آن‌ها ساخته می‌شود — آپدیت فقط همین‌ها را عوض می‌کند
MODULE_FILES = ('chart.py', 'main.py', 'mini_chart.py', 'wifi.py', 'market.py',
                'tgju.py', 'updater.py', 'install.sh', 'netfix.sh')

UA = 'CryptoChart-Pi/1.0'
HTTP_TIMEOUT = 30
ARCHIVE_HOST = 'https://codeload.github.com'
API_HOST = 'https://api.github.com'
# مودول با کاربر سرویس (dietpi/pi) اجرا می‌شود، پس برای بازنویسی یونیت systemd
# به sudo نیاز دارد؛ install.sh یک قاعدهٔ NOPASSWD برای همین یک فرمان می‌نویسد.
# (ری‌استارت سرویس نیازی به root ندارد: پروسه با systemd Restart=always خودش
#  بالا می‌آید — main.py بعد از install از حلقه خارج می‌شود.)
SUDO_CMD = '/usr/bin/sudo'
RECOMMENDED_ACTION = ('اگر مجوز نوشتن نبود، یک بار install.sh جدید را با sudo '
                      'اجرا کنید (chown پوشهٔ ماژول + قاعدهٔ sudo را تنظیم می‌کند)')


def _run(cmd, timeout=60, cwd=None, env=None):
    try:
        p = subprocess.run(cmd, cwd=cwd or PI_DIR, timeout=timeout, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, (p.stdout or b'').decode('utf-8', 'replace')
    except FileNotFoundError:
        return 127, f'{cmd[0]}: not found'
    except subprocess.TimeoutExpired:
        return 124, f'{cmd[0]}: timeout'
    except OSError as e:
        return 1, str(e)


def _read(path, limit=400):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip()[:limit]
    except OSError:
        return ''


def _write(path, text):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return True
    except OSError:
        return False


# ------------------------- محل مخزن git -------------------------
def repo_root():
    """پوشهٔ مخزن git که ماژول داخل آن است ('' اگر نصب کپی‌شده باشد).
    هم نصب‌های /opt/cryptochart/pi (کلون کامل) و هم مخزن‌های بالاتر پوشش
    داده می‌شوند."""
    d = PI_DIR
    for _ in range(4):
        if os.path.isdir(os.path.join(d, '.git')):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ''


def git_rev(short=True):
    """هش نسخهٔ نصب‌شده — از git اگر مخزن باشد، وگرنه فایل VERSION که
    install.sh می‌نویسد. خالی یعنی نسخه نامعلوم است."""
    root = repo_root()
    if root:
        code, out = _run(['git', 'rev-parse', '--short', 'HEAD'] if short
                         else ['git', 'rev-parse', 'HEAD'], timeout=10, cwd=root)
        if code == 0 and out.strip():
            return out.strip()
    return _read(os.path.join(PI_DIR, 'VERSION'), 64)


def repo_url():
    """آدرس مخزن برای حالت کپی‌شده: REPO_URL → تنظیمات git → پیش‌فرض پروژه."""
    root = repo_root()
    if root:
        code, out = _run(['git', 'remote', 'get-url', 'origin'], timeout=10, cwd=root)
        if code == 0 and out.strip():
            return out.strip()
    return _read(os.path.join(PI_DIR, 'REPO_URL')) or DEFAULT_REPO


def _parse_repo(url):
    """'https://github.com/o/r.git' یا 'git@github.com:o/r.git' → (owner, repo)."""
    u = (url or '').strip()
    if u.endswith('.git'):
        u = u[:-4]
    if u.startswith('git@'):
        rest = u.split('@', 1)[1]
        if ':' not in rest:
            return '', ''
        host, path = rest.split(':', 1)
    else:
        p = urllib.parse.urlparse(u)
        host, path = p.netloc, p.path
    if not host.endswith('github.com'):
        return '', ''
    parts = [x for x in path.split('/') if x]
    if len(parts) < 2:
        return '', ''
    return parts[0], parts[1]


def branch_name():
    """شاخهٔ ردیابی‌شده در حالت git، وگرنه main."""
    root = repo_root()
    if root:
        code, out = _run(['git', 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'],
                         timeout=10, cwd=root)
        if code == 0 and '/' in out.strip():
            return out.strip().split('/', 1)[1]
    return DEFAULT_BRANCH


# ------------------------- شبکه -------------------------
def _http_get(url, timeout=HTTP_TIMEOUT, token=''):
    headers = {
        'User-Agent': UA,
        'Accept': 'application/vnd.github+json, application/octet-stream',
        'Connection': 'close',
    }
    if token:                       # مخزن خصوصی → با توکن، وگرنه 404 می‌دهد
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def remote_sha():
    """هش کامل commit شاخه در مخزن راه‌دور — GitHub API، با fallback به
    git ls-remote (اگر git نصب باشد). خالی یعنی نشد."""
    owner, name = _parse_repo(repo_url())
    branch = branch_name()
    if owner and name:
        url = f'{API_HOST}/repos/{owner}/{name}/commits/{urllib.parse.quote(branch)}'
        try:
            j = json.loads(_http_get(url, token=_repo_token()).decode('utf-8', 'replace'))
            sha = str((j or {}).get('sha') or '')
            if len(sha) >= 7:
                return sha
        except Exception:
            pass
    code, out = _run(['git', 'ls-remote', repo_url(), f'refs/heads/{branch}'], timeout=45)
    if code == 0:
        for line in out.splitlines():
            parts = line.split()
            if parts and len(parts[0]) >= 7 and parts[0].isalnum():
                return parts[0]
    return ''


def _remote_rev_git():
    """هش کوتاه HEAD شاخهٔ ردیابی‌شده (حالت git) — با fetch تازه."""
    root = repo_root()
    code, out = _run(['git', 'fetch', 'origin'], timeout=90, cwd=root)
    if code != 0:
        return '', 'git fetch failed: ' + out.strip()[:120]
    code, out = _run(['git', 'rev-parse', '--short', '@{u}'], timeout=10, cwd=root)
    if code != 0 or not out.strip():
        return '', 'شاخهٔ ردیابی‌شده (upstream) تنظیم نیست'
    return out.strip(), ''


def _ready():
    """(آماده‌بودن آپدیت آنلاین، پیام خطا)."""
    if repo_root():
        return True, ''
    owner, name = _parse_repo(repo_url())
    if not owner or not name:
        return False, ('آدرس مخزن نامعتبر است — فایل REPO_URL را با آدرس مخزن '
                       'گیت‌هاب پر کنید یا یک بار با git clone نصب کنید')
    return True, ''


def _installed_rev_is_known():
    rev = git_rev()
    return bool(rev) and rev.isalnum()


def _file_ok(path):
    """فایل پایتون سالم است؟ فقط کامپایل می‌شود (اجرا نمی‌شود)."""
    if not path.endswith('.py'):
        return True, ''
    try:
        with open(path, 'rb') as f:
            compile(f.read(), path, 'exec')
        return True, ''
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _copy_module_files(src_dir):
    """فایل‌های ماژول را از پوشهٔ منبع (آرشیو بازشده) روی نصب می‌ریزد.

    دو محافظ مهم (علت «بعد از آپدیت ماژول بالا نمی‌آید»):
      1) همهٔ فایل‌های دانلودی قبل از دست‌زدن به نصب کامپایل می‌شوند؛ نسخهٔ
         ناقص/خراب اصلاً نصب نمی‌شود.
      2) نسخهٔ قبلی نگه داشته می‌شود و اگر کپی یا کامپایل فایل نصب‌شده خطا
         داد، همان نسخهٔ سالم برمی‌گردد (نصب نیمه‌کاره نه).
    خروجی: (موفق، پیام خطا)."""
    missing = [f for f in MODULE_FILES if not os.path.isfile(os.path.join(src_dir, f))]
    if missing:
        return False, 'فایل‌های لازم در آرشیو نیستند: ' + ','.join(missing[:4])
    for f in MODULE_FILES:
        ok, err = _file_ok(os.path.join(src_dir, f))
        if not ok:
            return False, f'نسخهٔ دانلودی سالم نیست ({f}: {err}) — نصب انجام نشد'

    old = {}
    for f in MODULE_FILES:
        try:
            with open(os.path.join(PI_DIR, f), 'rb') as fh:
                old[f] = fh.read()
        except OSError:
            old[f] = None

    def restore():
        for name, data in old.items():
            if data is None:
                continue
            try:
                with open(os.path.join(PI_DIR, name), 'wb') as fh:
                    fh.write(data)
            except OSError:
                pass

    for f in MODULE_FILES:
        try:
            shutil.copyfile(os.path.join(src_dir, f), os.path.join(PI_DIR, f))
        except OSError as e:
            restore()
            return False, (f'کپی {f} نشد ({e.strerror or e}) — ' + RECOMMENDED_ACTION)
        ok, err = _file_ok(os.path.join(PI_DIR, f))
        if not ok:
            restore()
            return False, (f'فایل نصب‌شده سالم نیست ({f}: {err}) — نسخهٔ قبلی '
                           'برگردانده شد')
    return True, ''


# ------------------------- بررسی و نصب -------------------------
def check():
    """بررسی وجود نسخهٔ جدید — بدون هیچ تغییری."""
    ok, err = _ready()
    if not ok:
        return dict(status='error', message=err, rev=git_rev())
    local = git_rev()
    if repo_root():
        remote, err = _remote_rev_git()
    else:
        sha = remote_sha()
        remote, err = (sha[:7], '') if sha else ('', 'دریافت نسخهٔ مخزن راه‌دور ناموفق بود')
    if not remote:
        return dict(status='error', message=err or 'نسخهٔ راه‌دور مشخص نشد', rev=local)
    if local == remote:
        return dict(status='uptodate', message=f'ماژول به‌روز است ({local})', rev=local)
    return dict(status='available', message=f'نسخهٔ جدید موجود است: {local} → {remote}',
                rev=local)


def _is_root():
    """root هستیم؟ (روی سیستم‌هایی که geteuid ندارند، مسیر بدون sudo)."""
    try:
        return os.geteuid() == 0
    except AttributeError:
        return True


def _refresh_service(env=None, timeout=180):
    """install.sh --update را اجرا می‌کند تا یونیت/نسخه تازه شود (apt نمی‌زند).
    این مرحله «بهترین تلاش» است: اگر دسترسی root نبود، کد تازه همین حالا در
    پوشهٔ ماژول است و پروسه با خروج خودش توسط systemd بالا می‌آید.
    خروجی: (موفق، خروجی/پیام)."""
    script = os.path.join(PI_DIR, 'install.sh')
    if not os.path.isfile(script):
        return False, 'install.sh در پوشهٔ ماژول نیست'
    cmd = ['bash', 'install.sh', '--update']
    if not _is_root() and SUDO_CMD and os.path.isfile(SUDO_CMD):
        cmd = [SUDO_CMD, '-n', '/usr/bin/bash', script, '--update']
    code, out = _run(cmd, timeout=timeout, cwd=PI_DIR, env=env)
    return code == 0, out


def _repo_token():
    """توکن اختیاری مخزن خصوصی: فایل REPO_TOKEN کنار ماژول یا متغیر GITHUB_TOKEN.
    بدون توکن همه‌چیز مثل قبل کار می‌کند (مخزن عمومی)."""
    tok = _read(os.path.join(PI_DIR, 'REPO_TOKEN'), 200) or os.environ.get('GITHUB_TOKEN', '')
    return tok.strip()


def _archive_urls(sha):
    """آدرس‌های آرشیو همان commit — codeload اول (سبک)، API به‌عنوان پشتیبان."""
    owner, name = _parse_repo(repo_url())
    if not owner or not name:
        return []
    return [f'{ARCHIVE_HOST}/{owner}/{name}/tar.gz/{sha}',
            f'{API_HOST}/repos/{owner}/{name}/tarball/{sha}']


def _extract_tar(raw, tmp):
    """آرشیو را در tmp باز می‌کند؛ خروجی: (تعداد فایل، پیام خطا).
    فقط فایل‌های معمولی و مسیرهای سالم استخراج می‌شوند."""
    tarpath = os.path.join(tmp, 'src.tar.gz')
    with open(tarpath, 'wb') as f:
        f.write(raw)
    with tarfile.open(tarpath, 'r:gz') as tf:
        members = []
        for m in tf.getmembers():
            if not m.isfile():
                continue
            name_m = m.name.replace('\\', '/')
            if name_m.startswith('/') or '..' in name_m.split('/'):
                continue
            members.append(m)
        if not members:
            return 0, 'آرشیو هیچ فایلی نداشت'
        tf.extractall(tmp, members=members)
    return len(members), ''


def _has_module_files(d):
    return all(os.path.isfile(os.path.join(d, f)) for f in MODULE_FILES)


def _describe_tree(tmp, limit=6):
    """خلاصهٔ ساختار آرشیو برای پیام خطا (چه چیزی واقعاً دانلود شده؟)"""
    try:
        items = sorted(os.listdir(tmp))
    except OSError:
        return 'ساختار آرشیو خوانده نشد'
    items = [x for x in items if x != 'src.tar.gz']
    shown = [x + '/' if os.path.isdir(os.path.join(tmp, x)) else x for x in items[:limit]]
    return ', '.join(shown) + ('…' if len(items) > limit else '')


def _find_module_dir(tmp):
    """پوشهٔ ماژول داخل آرشیو بازشده — مستقل از ساختار آرشیو.
    ترتیب: <ریشه>/pi (استاندارد گیت‌هاب) → هر پوشه‌ای تا عمق ۲ که همهٔ فایل‌های
    ماژول را دارد (مونوریپو/پوشهٔ تودرتو) → خود ریشه (آرشیو تخت یا ./‌محور).
    خروجی: (مسیر یا ''، توضیح ساختار برای پیام خطا)."""
    tops = sorted(x for x in os.listdir(tmp)
                  if os.path.isdir(os.path.join(tmp, x)))
    for r in tops:
        base = os.path.join(tmp, r)
        cand = os.path.join(base, 'pi')
        if os.path.isdir(cand) and _has_module_files(cand):
            return cand, ''                       # <repo>-<sha>/pi
        if _has_module_files(base):
            return base, ''                       # module files at repo root
                                                  # (e.g. raspberry-module)
    for r in tops:                       # هر عمقی تا ۲ سطح زیر ریشه
        base = os.path.join(tmp, r)
        for cur, dirs, _files in os.walk(base):
            depth = os.path.relpath(cur, base).count(os.sep)
            if depth >= 2:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            if cur != base and _has_module_files(cur):
                return cur, ''
    for cand in (tmp, os.path.join(tmp, 'pi')):    # آرشیو تخت (بدون پوشهٔ بالا)
        if _has_module_files(cand):
            return cand, ''
    return '', _describe_tree(tmp)


def _download_tree(sha):
    """آرشیو همان commit را دانلود و باز می‌کند؛ خروجی: (پوشهٔ موقت، پوشهٔ ماژول)
    یا (None، پیام خطا). ساختار آرشیو مهم نیست: پوشهٔ حاوی فایل‌های ماژول پیدا
    می‌شود (استاندارد <repo>-<sha>/pi یا تودرتو یا تخت)."""
    urls = _archive_urls(sha)
    if not urls:
        return None, ('آدرس مخزن نامعتبر است — فایل REPO_URL را با آدرس مخزن '
                      'گیت‌هاب پر کنید یا یک بار با git clone نصب کنید')
    token = _repo_token()
    tmp = tempfile.mkdtemp(prefix='cryptochart-upd-')
    errs = []
    try:
        for url in urls:
            try:
                raw = _http_get(url, timeout=120, token=token)
            except urllib.error.HTTPError as e:
                errs.append('%s → HTTP %s' % (url, e.code))
                if e.code == 404 and not token:
                    errs.append('(مخزن خصوصی است یا آدرسش اشتباه — برای مخزن '
                                'خصوصی توکن را در فایل REPO_TOKEN بگذارید)')
                continue
            except Exception as e:
                errs.append('%s → %s: %s' % (url, type(e).__name__, e))
                continue
            if len(raw) < 200:
                errs.append('%s → پاسخ خالی (%d بایت)' % (url, len(raw)))
                continue
            try:
                nfiles, err = _extract_tar(raw, tmp)
            except Exception as e:
                errs.append('%s → آرشیو باز نشد (%s)' % (url, type(e).__name__))
                continue
            if err:
                errs.append('%s → %s' % (url, err))
                continue
            src, desc = _find_module_dir(tmp)
            if src:
                return tmp, src
            errs.append('%s → پوشهٔ ماژول پیدا نشد؛ داخل آرشیو: %s (%d فایل)'
                        % (url, desc or 'خالی', nfiles))
            for x in os.listdir(tmp):        # پاک‌سازی برای تلاش بعدی
                p = os.path.join(tmp, x)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        msg = ' | '.join(errs)[:280] or 'آرشیو در دسترس نبود'
    except Exception as e:
        msg = '%s: %s' % (type(e).__name__, e)
    shutil.rmtree(tmp, ignore_errors=True)
    return None, msg


def install():
    """آپدیت + نصب + ری‌استارت سرویس. خروجی فقط برای لاگ systemd است؛
    خود ری‌استارت توسط systemd انجام می‌شود (exec tail → systemctl)."""
    ok, err = _ready()
    if not ok:
        return dict(status='error', message=err, rev=git_rev())

    # ---------- حالت git ----------
    if repo_root():
        local0 = git_rev()
        code, out = _run(['git', 'pull', '--ff-only', 'origin'], timeout=120,
                         cwd=repo_root())
        if code != 0:
            return dict(status='error', message='git pull failed: ' + out.strip()[:120],
                        rev=local0)
        local1 = git_rev()
        if local1 == local0:
            return dict(status='uptodate', message=f'همین حالا به‌روز بود ({local1})',
                        rev=local1)
        _refresh_service()          # بهترین تلاش (یونیت/نسخه)
        return dict(status='installed',
                    message=f'نصب شد ({local1}) — ماژول ری‌استارت می‌شود', rev=local1)

    # ---------- حالت نصب کپی‌شده (بدون git) ----------
    local0 = git_rev()
    sha = remote_sha()
    if not sha:
        return dict(status='error', rev=local0,
                    message='دریافت نسخهٔ مخزن راه‌دور ناموفق بود '
                            f'({repo_url()}) — اینترنت/آدرس مخزن را بررسی کنید')
    short = sha[:7]
    if local0 == short:
        return dict(status='uptodate', message=f'همین حالا به‌روز بود ({short})', rev=short)
    tmp, src = _download_tree(sha)
    if not tmp:
        return dict(status='error', message='دانلود آرشیو ناموفق: ' + src, rev=local0)
    try:
        ok, err = _copy_module_files(src)
        if not ok:
            return dict(status='error', message=err[:300], rev=local0)
        _write(os.path.join(PI_DIR, 'VERSION'), short)
        env = dict(os.environ)
        env['CRYPTOCHART_REV'] = short
        _refresh_service(env=env)
        return dict(status='installed',
                    message=f'نصب شد ({short}) — ماژول ری‌استارت می‌شود', rev=short)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    هر فرمان فقط یک بار اجرا می‌شود (فقط اگر ts جدید باشد) و نتیجه
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
    print('install dir :', PI_DIR)
    print('repo root   :', repo_root() or '(copy install — archive mode)')
    print('repo url    :', repo_url())
    print('branch      :', branch_name())
    pprint.pprint(check())


if __name__ == '__main__':
    _selftest()  # اگر به‌روزرسانی آنلاین فرق کرد، اینجا می‌بینی
