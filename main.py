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
import random
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
from wifi import scan as wifi_scan, current as wifi_current, connect as wifi_connect, WifiReporter
import updater
from updater import UpdateReporter

USER_AGENT = 'CryptoChart-Pi/1.0'
POLL_WAIT = 45          # سرور تا ۱۵ ثانیه نگه می‌دارد + زمان دریافت فید؛ حاشیهٔ امن
RETRY_BASE = 2          # خطاهای کوتاه سریع جبران شوند (چارت روی صفحه می‌ماند)
RETRY_MAX = 15
PANE_BUDGET = 12        # سقف زمان فچ پنل‌های چندچارت در هر سیکل (ثانیه)


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
    _last_update_ts = 0.0   # last handled updateCmd ts — هر فرمان فقط بار اجرا شود

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
    last_ok = 0.0           # زمان آخرین فریم سالم (مبنای نشانگر وضعیت)
    have_frame = False      # آیا چارتی روی صفحه هست؟
    was_offline = False
    last_key = None         # کلید فریم سالم: (نماد، تایم‌فریم، بازه)
    last_candles = None
    last_price = None
    last_panes = {}         # کندل‌های سالم هر پنل: 'ETH:15m' → candles

    def next_wait():
        """تأخیر تلاش مجدد با jitter — خطاهای کوتاه سریع جبران می‌شوند."""
        w = min(RETRY_MAX, RETRY_BASE * (2 ** min(retry, 4)))
        return w + random.uniform(0, 0.9)

    def offline_notice(note=''):
        """قطعی موقت: چارت قبلی روی صفحه می‌ماند؛ فقط نشانگر قرمز می‌شود."""
        nonlocal was_offline
        was_offline = True
        try:
            if have_frame:
                renderer.status(False, note)
            else:
                renderer.offline(retry_in=int(next_wait()))
        except Exception:
            pass

    def pause(seconds):
        """خواب بین تلاش‌ها با به‌روزرسانی نشانگر — چارت دست‌نخورده می‌ماند."""
        end = time.time() + seconds
        while running and time.time() < end:
            time.sleep(0.5)
            if have_frame:
                try:
                    renderer.status(False, f'{int(time.time() - last_ok)}s')
                except Exception:
                    pass

    wait_for_network()

    # سلام اولیه — نمایش وضعیت سرور روی اسپلش
    try:
        h = api.health()
        version = int(h.get('version', 0))
        log(f'server ok — config v{version}')
    except Exception as e:
        log(f'health check failed: {e}')
        offline_notice()

    while running:
        try:
            data = api.poll(version)
            retry = 0
            if was_offline:
                log('connection restored')
                was_offline = False

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
            r_from = int(cfg.get('rangeFrom') or 0)
            r_to = int(cfg.get('rangeTo') or 0)
            frame_key = (str(cfg.get('symbol')), str(cfg.get('interval')), r_from, r_to)
            candles, source = unpack_feed(
                market.feed(cfg.get('symbol'), cfg.get('interval', '15m'),
                            limit, r_from, r_to))
            stale_frame = bool(data.get('stale'))
            if candles is None:
                candles = data.get('candles') or []
                source = 'server'
            if not candles and last_candles and frame_key == last_key:
                # منبع لحظه‌ای قطع شد ولی آخرین کندل‌های سالم را داریم →
                # چارت روی صفحه می‌ماند (فقط نشانگر قرمز می‌شود)
                candles = last_candles
                source = 'cache'
                stale_frame = True
            if source != last_source:
                log(f'data source: {source or "none"} ({len(candles)} candles, '
                    f'{cfg.get("symbol")} {cfg.get("interval")})')
                last_source = source

            price, _src = build_price(market, cfg.get('symbol'),
                                      data.get('price'), data.get('iranInfo'))
            if not float(price.get('price') or 0) and last_price:
                price = dict(last_price)      # قیمت آخرین فریم سالم بماند
                stale_frame = True

            panes = data.get('panes') or {}
            if panes and cfg.get('layout', 'single') != 'single':
                # چهار پنل × منابع کند اینترنتی می‌توانست تا ۴۰ ثانیه سیکل را
                # عقب بیندازد و poll بعدی دیر شود (وب‌اپ «آفلاین» می‌دید).
                # پس پنل‌ها بودجه دارند: هرچه نرسید، کندل‌های سالم قبلی‌اش می‌ماند.
                pane_deadline = time.time() + PANE_BUDGET
                for key, pf in panes.items():
                    if not isinstance(pf, dict) or ':' not in key:
                        continue
                    if time.time() > pane_deadline:
                        if last_panes.get(key):
                            pf['candles'] = last_panes[key]
                            pf['stale'] = True
                        continue
                    psym, piv = key.split(':', 1)
                    # بازهٔ تاریخی و بازهٔ قیمتی هر پنل مستقل است
                    pf_from = int(pf.get('rangeFrom') or 0)
                    pf_to = int(pf.get('rangeTo') or 0)
                    nc, _src = unpack_feed(market.feed(psym, piv, min(60, limit),
                                                       pf_from, pf_to))
                    if nc is not None:
                        pf['candles'] = nc
                        if nc:
                            pf['stale'] = False
                    elif last_panes.get(key):
                        pf['candles'] = last_panes[key]   # پنل خالی نپرد
                        pf['stale'] = True
                    pq = market.quote(psym)
                    if pq:
                        pf['price'] = {'price': pq['price'],
                                       'changePct': pq['changePct']}

            renderer.render(candles, cfg, price, server_ok=not stale_frame,
                            ts=int(data.get('serverTime') or time.time()),
                            panes=panes)
            have_frame = True
            last_ok = time.time()
            last_key = frame_key
            if candles:
                last_candles = candles
            if float(price.get('price') or 0):
                last_price = price
            for key, pf in panes.items():
                if pf.get('candles'):
                    last_panes[key] = pf['candles']

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

            # فرمان آپدیت (check/install از وب‌اپ) — هر ts فقط یک بار اجرا می‌شود
            ucmd = data.get('updateCmd') or {}
            uts = float(ucmd.get('ts') or 0)
            if ucmd.get('cmd') and uts > _last_update_ts and (time.time() - uts) < 120:
                _last_update_ts = uts
                act = str(ucmd.get('cmd'))
                log(f'update command: {act}')
                try:
                    _ran, res = updater.run_cmd(ucmd, update_reporter)
                    log(f'update {act}: {(res or {}).get("status")} — {(res or {}).get("message", "")[:120]}')
                except Exception as e:
                    log(f'update error: {e}')
                    try:
                        update_reporter.report(status='error', message=f'{type(e).__name__}: {e}'[:300],
                                               rev=updater.git_rev())
                    except Exception:
                        pass
                if act == 'install':
                    # install.sh سرویس را ری‌استارت می‌کند؛ اگر به اینجا رسیدیم
                    # یعنی ری‌استارت انجام نشده — خودمان خارج می‌شویم تا systemd بکشد بالا
                    log('update install done — exiting for service restart')
                    break

            # فاصلهٔ کوتاه بین دو poll — مصرف CPU نزدیک صفر
            for _ in range(2):
                if not running:
                    break
                time.sleep(0.5)

        except KeyboardInterrupt:
            break
        except urllib.error.HTTPError as e:
            # 401/403/404 = آدرس/دسترسی غلط (تا اصلاح نشود چارت معنی ندارد)
            # 5xx/502/504 = خطای گذاری هاست اشتراکی → چارت بماند، نشانگر قرمز
            code = e.code
            wait_s = next_wait()
            retry += 1
            if code in (401, 403, 404):
                hint = 'آدرس API اشتباه است یا سرور اجازهٔ دسترسی نمی‌دهد'
                log(f'HTTP {code} from {api.base} — {hint} — retry in {int(wait_s)}s')
                have_frame = False
                try:
                    renderer.config_error(code, hint, url=api.base,
                                          retry_in=int(wait_s))
                except Exception:
                    pass
            else:
                log(f'HTTP {code} from {api.base} — خطای سمت سرور — retry in {int(wait_s)}s')
                offline_notice(f'HTTP {code}')
            pause(wait_s)
        except Exception as e:
            wait_s = next_wait()
            retry += 1
            if isinstance(e, NET_ERRORS):
                log(f'network error: {e} — retry in {int(wait_s)}s')
                offline_notice()
            else:
                # خطای منطقی/رندر است نه شبکه — نباید صفحه را «آفلاین» نشان دهد
                traceback.print_exc()
                log(f'ERROR (not network): {type(e).__name__}: {e} — retry in {int(wait_s)}s')
                try:
                    renderer.error(f'{type(e).__name__}: {e}'[:80], retry_in=int(wait_s))
                    have_frame = False
                except Exception:
                    pass
            pause(wait_s)

    renderer.sc.close()
    log('bye')


if __name__ == '__main__':
    main()
