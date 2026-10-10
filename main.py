#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CryptoChart Client — ماژول رزبری پای زیرو
------------------------------------------
چرخهٔ کار:
  1) long-poll به سرور → پاسخ شامل کانفیگ + کندل‌ها + قیمت است
  2) اگر کانفیگ تغییر کرده باشد، بلافاصله رندر با تنظیمات جدید
  3) در خطای شبکه → صفحهٔ OFFLINE و تلاش مجدد با backoff

اجرا:  sudo python3 main.py --url https://YOUR-SERVER/path/backend
"""

import argparse
import json
import signal
import socket
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

from chart import ChartRenderer, Screen
from market import MarketFeed
from updater import check as update_check, install as update_install, git_rev, UpdateReporter
from wifi import scan as wifi_scan, current as wifi_current, connect as wifi_connect, WifiReporter

USER_AGENT = 'CryptoChart-Pi/1.0'
POLL_WAIT = 45          # سرور تا ۱۵ ثانیه نگه می‌دارد + زمان دریافت فید؛ حاشیهٔ امن
RETRY_BASE = 3
RETRY_MAX = 30


def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


class ApiClient:
    """کلاینت HTTP سبک روی urllib — بدون وابستگی خارجی."""

    def __init__(self, base_url, timeout=30):
        self.base = base_url.rstrip('/')
        self.timeout = timeout

    def _fetch(self, url, timeout):
        req = urllib.request.Request(url, headers={
            'User-Agent': USER_AGENT,
            'Accept': 'application/json',
            'Connection': 'close',
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace')
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            # سرور جواب داد ولی JSON نبود → احتمالاً آدرس API اشتباه است
            head = ' '.join(raw.split())[:60]
            raise ValueError(
                f'پاسخ سرور JSON نیست — آدرس API اشتباه است؟ ({self.base}): {head}') from e

    def health(self):
        return self._fetch(f'{self.base}/index.php?api=health', timeout=10)

    def poll(self, version):
        url = f'{self.base}/index.php?api=poll&v={version}'
        data = self._fetch(url, timeout=POLL_WAIT)
        if not isinstance(data, dict) or 'config' not in data:
            # سرور جواب داد ولی این API ما نیست (مثلاً صفحهٔ پارکینگ یا ریدایرکت)
            raise ValueError(f'پاسخ نامعتبر از {url} — آدرس API اشتباه است؟')
        return data

    def feed(self):
        url = f'{self.base}/index.php?api=feed'
        return self._fetch(url, timeout=15)


def wait_for_network(deadline_s=120):
    """صبر تا بالا آمدن لینک شبکه (وای‌فای یا اترنت) بعد از بوت."""
    log('waiting for network...')
    start = time.time()
    while time.time() - start < deadline_s:
        try:
            socket.create_connection(('1.1.1.1', 53), timeout=2).close()
            log('network is up')
            return True
        except OSError:
            time.sleep(2)
    log('network timeout — continuing anyway')
    return False


def validate_config(cfg):
    """دفاع در برابر کانفیگ خراب از سرور."""
    if not isinstance(cfg, dict):
        return False
    for k in ('symbol', 'interval', 'chartType', 'theme'):
        if not isinstance(cfg.get(k), str) or not cfg.get(k):
            return False
    return True


def handle_wifi_cmd(cmd, reporter):
    """اجرای فرمان وای‌فای دریافتی از سرور و گزارش نتیجه."""
    action = cmd.get('cmd')
    log(f'wifi command: {action}')
    if action == 'scan':
        nets = wifi_scan()
        ssid, ip = wifi_current()
        reporter.report(scan=nets, status='scanned', connected=ssid, ip=ip)
        log(f'wifi scan: {len(nets)} networks')
    elif action == 'connect':
        ssid = str(cmd.get('ssid', ''))
        ok, info = wifi_connect(ssid, str(cmd.get('pass', '')))
        log(f'wifi connect {ssid}: {"ok" if ok else info}')
        reporter.report(status='connected' if ok else 'error',
                        connected=ssid if ok else '', ip=info if ok else '',
                        error='' if ok else info)


def handle_update_cmd(cmd, reporter, update_reporter):
    """اجرای فرمان به‌روزرسانی آنلاین (check/install) و گزارش نتیجه.
    install فقط وقتی ری‌استارت می‌شود که pull واقعاً نسخهٔ تازه آورده باشد؛
    ری‌استارت با systemctl (سرویس خودش را restart می‌کند) انجام می‌شود."""
    action = cmd.get('cmd')
    log(f'update command: {action}')
    if action == 'check':
        res = update_check() or {}
        update_reporter.report(status=res.get('status', 'error'),
                               message=res.get('message', ''), rev=res.get('rev', ''))
        log(f'update check: {res.get("status")} — {res.get("message", "")[:120]}')
    elif action == 'install':
        res = update_install() or {}
        update_reporter.report(status=res.get('status', 'error'),
                               message=res.get('message', ''), rev=res.get('rev', ''))
        log(f'update install: {res.get("status")} — {res.get("message", "")[:120]}')
        if res.get('status') == 'installed':
            time.sleep(2)   # فرصت برای رسیدن گزارش به سرور
            log('restarting service for update...')
            code, out = _run_systemctl_restart()
            log(f'systemctl restart: rc={code} {out.strip()[:80]}')


def _run_systemctl_restart():
    try:
        import subprocess
        p = subprocess.run(['systemctl', 'restart', 'cryptochart'], timeout=30,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, (p.stdout or b'').decode('utf-8', 'replace')
    except Exception as e:
        return 1, str(e)[:120]


NET_ERRORS = (urllib.error.URLError, OSError, TimeoutError, ConnectionError)


def unpack_feed(res, default_source='module'):
    """
    market.feed() در نسخهٔ فعلی (candles, source) برمی‌گرداند؛ برای سازگاری با
    نسخهٔ قدیمی که فقط لیست کندل برمی‌گرداند، شکل‌های مختلف را می‌پذیرد.
    خروجی: (candles|None, source)
    """
    if isinstance(res, tuple):
        return res[0], (res[1] or default_source)
    return res, default_source


def build_price(market, symbol, server_price, iran_info):
    """
    قیمت لحظه‌ای را از ماژول می‌گیرد (نوبیتکس برای کریپتو، BrsApi برای ایران)
    و با دادهٔ سرور تکمیل می‌کند؛ اگر ماژول جواب نداد، قیمت سرور استفاده می‌شود.
    خروجی: (price_info, source)
    """
    q = market.quote(symbol) or {}
    sp = server_price or {}
    price = {'price': float(q.get('price') or 0.0),
             'changePct': float(q.get('changePct') or 0.0)}
    if not price['price']:
        price = {'price': float(sp.get('price') or 0.0),
                 'changePct': float(sp.get('changePct') or 0.0)}
    # نام و واحد: اول از ماژول، بعد از سرور
    for k in ('unit', 'name', 'change_value'):
        if q.get(k):
            price[k] = q[k]
        elif (iran_info or {}).get(k) is not None:
            price[k] = iran_info[k]
    if not price.get('name'):
        price['name'] = sp.get('name') or ''
    return price, (q.get('source') or 'server')


def main():
    ap = argparse.ArgumentParser(description='CryptoChart Pi client')
    ap.add_argument('--url', required=True, help='آدرس بک‌اند، مثل https://example.com/cc')
    ap.add_argument('--fb', default='/dev/fb0', help='مسیر فریم‌بافر')
    ap.add_argument('--rotate', action='store_true', help='چرخش ۹۰ درجهٔ صفحه (LCD عمودی)')
    args = ap.parse_args()

    api = ApiClient(args.url)
    wifi_reporter = WifiReporter(args.url)
    update_reporter = UpdateReporter(args.url)
    market = MarketFeed()   # همهٔ دادهٔ بازار مستقیم از ماژول (نوبیتکس/برس‌آپ)
    last_source = ''

    # ---------- راه‌اندازی صفحه ----------
    try:
        screen = Screen(args.fb)
    except (OSError, RuntimeError) as e:
        print(f'fatal: cannot open framebuffer ({e})', file=sys.stderr)
        print('hint: run as root, or add user to "video" group:  sudo usermod -aG video $USER',
              file=sys.stderr)
        sys.exit(1)

    renderer = ChartRenderer(screen, rotate=args.rotate)
    renderer.boot_splash()
    log(f'screen {screen.W}x{screen.H} @ {screen.bpp}bpp (logical {renderer.W}x{renderer.H})')

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # ---------- حلقهٔ اصلی ----------
    version = 0
    retry = 0
    wait_for_network()

    # سلام اولیه — نمایش وضعیت سرور روی اسپلش
    try:
        h = api.health()
        version = int(h.get('version', 0))
        log(f'server ok — config v{version}')
    except Exception as e:
        log(f'health check failed: {e}')
        renderer.offline(retry_in=RETRY_BASE)

    while running:
        try:
            data = api.poll(version)
            retry = 0

            cfg = data.get('config') or {}
            if not validate_config(cfg):
                log('bad config from server — skipping frame')
                time.sleep(5)
                continue

            if data.get('changed'):
                log(f"config changed → v{cfg.get('version')} "
                    f"({cfg.get('symbol')} {cfg.get('interval')} {cfg.get('chartType')})")
                version = int(cfg.get('version', version))

            # ---- کلید BrsApi از کانفیگ سرور (خواندن API کاملاً روی ماژول) ----
            bkey = (data.get('apiKeys') or {}).get('brs') or ''
            if bkey:
                market.set_key(bkey)

            # ---- دادهٔ بازار: کاملاً مستقیم از ماژول ----
            # کریپتو → نوبیتکس | بازار ایران → BrsApi؛ سرور فقط کنترل/تنظیمات است.
            # اگر هیچ منبعی جواب نداد، کندل/قیمت سرور به‌عنوان fallback می‌آید.
            limit = int(cfg.get('limit') or 90)
            candles, source = unpack_feed(
                market.feed(cfg.get('symbol'), cfg.get('interval', '15m'),
                            limit, int(cfg.get('rangeFrom') or 0),
                            int(cfg.get('rangeTo') or 0)))
            if candles is None:
                candles = data.get('candles') or []
                source = 'server'
            if source != last_source:
                log(f'data source: {source or "none"} ({len(candles)} candles, '
                    f'{cfg.get("symbol")} {cfg.get("interval")})')
                last_source = source

            price, _src = build_price(market, cfg.get('symbol'),
                                      data.get('price'), data.get('iranInfo'))

            panes = data.get('panes') or {}
            if panes and cfg.get('layout', 'single') != 'single':
                for key, pf in panes.items():
                    if not isinstance(pf, dict) or ':' not in key:
                        continue
                    psym, piv = key.split(':', 1)
                    # بازهٔ تاریخی و بازهٔ قیمتی هر پنل مستقل است
                    # بودجهٔ کندل پنل = همان limit سراسری با سقف قابل‌رندر در پنل
                    # (۴۰۰ کندل — بیشتر از این در عرض ۲۳۷px پنل بی‌معنی است)
                    pf_from = int(pf.get('rangeFrom') or 0)
                    pf_to = int(pf.get('rangeTo') or 0)
                    nc, _src = unpack_feed(market.feed(psym, piv, min(400, limit),
                                                       pf_from, pf_to))
                    if nc is not None:
                        pf['candles'] = nc
                    pq = market.quote(psym)
                    if pq:
                        pf['price'] = {'price': pq['price'],
                                       'changePct': pq['changePct']}

            renderer.render(candles, cfg, price, server_ok=not data.get('stale'),
                            ts=int(data.get('serverTime') or time.time()),
                            panes=panes)

            # فرمان وای‌فای در انتظار؟
            cmd = data.get('wifiCmd') or {}
            if cmd.get('cmd') and (time.time() - float(cmd.get('ts') or 0)) < 120:
                try:
                    handle_wifi_cmd(cmd, wifi_reporter)
                    # بعد از اتصال جدید، poll بعدی با شبکهٔ تازه ادامه می‌یابد
                except Exception as e:
                    log(f'wifi error: {e}')
                    try:
                        wifi_reporter.report(status='error', error=str(e)[:200])
                    except Exception:
                        pass

            # فرمان به‌روزرسانی آنلاین در انتظار؟ (فایل فرمان یک‌بارمصرف است)
            ucmd = data.get('updateCmd') or {}
            if ucmd.get('cmd'):
                try:
                    handle_update_cmd(ucmd, wifi_reporter, update_reporter)
                except Exception as e:
                    log(f'update error: {e}')
                    try:
                        update_reporter.report(status='error', message=f'{type(e).__name__}: {e}'[:280],
                                               rev=git_rev())
                    except Exception:
                        pass

            # فاصلهٔ کوتاه بین دو poll — مصرف CPU نزدیک صفر
            for _ in range(2):
                if not running:
                    break
                time.sleep(0.5)

        except KeyboardInterrupt:
            break
        except urllib.error.HTTPError as e:
            # پاسخ 4xx/5xx یعنی سرور در دسترس است ولی URL یا دسترسی غلط است
            wait_s = min(RETRY_MAX, RETRY_BASE * (2 ** min(retry, 4)))
            retry += 1
            code = e.code
            hint = ('آدرس API اشتباه است یا سرور اجازهٔ دسترسی نمی‌دهد'
                    if code in (401, 403, 404) else 'خطای سمت سرور')
            log(f'HTTP {code} from {api.base} — {hint} — retry in {wait_s}s')
            try:
                renderer.config_error(code, hint, url=api.base, retry_in=wait_s)
            except Exception:
                pass
            for _ in range(wait_s * 2):
                if not running:
                    break
                time.sleep(0.5)
        except Exception as e:
            wait_s = min(RETRY_MAX, RETRY_BASE * (2 ** min(retry, 4)))
            retry += 1
            if isinstance(e, NET_ERRORS):
                log(f'network error: {e} — retry in {wait_s}s')
                try:
                    renderer.offline(retry_in=wait_s)
                except Exception:
                    pass
            else:
                # خطای منطقی/رندر است نه شبکه — نباید صفحه را «آفلاین» نشان دهد
                traceback.print_exc()
                log(f'ERROR (not network): {type(e).__name__}: {e} — retry in {wait_s}s')
                try:
                    renderer.error(f'{type(e).__name__}: {e}'[:80], retry_in=wait_s)
                except Exception:
                    pass
            for _ in range(wait_s * 2):
                if not running:
                    break
                time.sleep(0.5)

    renderer.sc.close()
    log('bye')


if __name__ == '__main__':
    main()
