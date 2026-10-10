#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
پیش‌نمایش رندرر بدون فریم‌بافر — روی هر کامپیوتری اجرا می‌شود:
    python3 dev_screenshot.py
خروجی: PNGهای پیش‌نمایش در پوشهٔ preview/ کنار همین اسکریپت
"""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chart import ChartRenderer  # noqa: E402


class FakeScreen:
    """جایگزین Screen برای تست — خروجی را PNG ذخیره می‌کند."""

    def __init__(self, w=1280, h=720):
        self.W, self.H, self.bpp = w, h, 32
        self.red_off = 0
        self.rotate = False

    def flush(self, img_rgb, rect=None):
        self.last = img_rgb.copy()

    def close(self):
        pass


def make_candles(n=90, base=43000.0, seed=7):
    rnd = random.Random(seed)
    out = []
    t = 1759000000 - n * 900
    p = base
    for i in range(n):
        drift = (rnd.random() - 0.47) * 260
        o = p
        c = max(100.0, p + drift)
        h = max(o, c) + rnd.random() * 150
        l = min(o, c) - rnd.random() * 150
        v = 40 + rnd.random() * 900
        out.append({'t': t + i * 900, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v})
        p = c
    return out


def main():
    # خروجی همیشه در pi/preview کنار همین اسکریپت (مستقل از مسیر اجرا)
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'preview')
    os.makedirs(out_dir, exist_ok=True)
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    candles = make_candles()

    combos = [
        dict(name='candles_dark',    cfg=dict(chartType='candles', theme='dark',     candleMode='solid',
                                              indicators=dict(ma=True, ema=False, bbands=True, rsi=True, macd=False))),
        dict(name='heikin_midnight', cfg=dict(chartType='heikin',  theme='midnight', candleMode='solid',
                                              indicators=dict(ma=False, ema=True, bbands=False, rsi=False, macd=True))),
        dict(name='area_ocean',      cfg=dict(chartType='area',    theme='ocean',    candleMode='solid',
                                              indicators=dict(ma=False, ema=False, bbands=False, rsi=False, macd=False))),
        dict(name='hollow_light',    cfg=dict(chartType='hollow',  theme='light',    candleMode='hollow',
                                              indicators=dict(ma=True, ema=True, bbands=False, rsi=False, macd=False))),
        dict(name='bars_matrix',     cfg=dict(chartType='bars',    theme='matrix',   candleMode='solid',
                                              indicators=dict(ma=False, ema=False, bbands=False, rsi=True, macd=True))),
        dict(name='line_dark_min',   cfg=dict(chartType='line',    theme='dark',     candleMode='solid',
                                              indicators=dict(ma=False, ema=False, bbands=False, rsi=False, macd=False),
                                              showVolume=False)),
    ]

    price = {'price': candles[-1]['c'], 'changePct': 2.47}
    for c in combos:
        sc = FakeScreen()
        r = ChartRenderer(sc)
        cfg = dict(symbol='BTCUSDT', displayName='BTC / USDT', interval='15m',
                   feedMode='spot', limit=90, showVolume=True, rotate=False) | c['cfg']
        r.render(candles, cfg, price, server_ok=True, ts=1759000000)
        out = f"preview/{c['name']}.png"
        sc.last.save(out)
        print('saved', out)

    # ---------- قیمت بیرون از کندل‌ها (باید باز هم روی چارت باشد) ----------
    sc = FakeScreen(480, 320)
    r = ChartRenderer(sc)
    cfg = dict(symbol='BTCUSDT', displayName='BTC', interval='4h', feedMode='spot',
               limit=90, showVolume=True, rotate=False, theme='dark',
               chartType='candles', candleMode='solid', xAxisCal='jalali',
               indicators=dict(ma=False, ema=False, bbands=False, rsi=False, macd=False))
    base_cfg_480 = dict(cfg)
    r.render(candles, cfg, {'price': candles[-1]['c'] * 1.35, 'changePct': 3.1},
             server_ok=True, ts=1759000000)
    sc.last.save('preview/price_outside.png')
    print('saved preview/price_outside.png')

    # ---------- بازهٔ قیمتی نباید محور Y را تغییر دهد (جفت کاملاً یکسان) ----------
    for nm, extra in (('mark_baseline', {}),
                      ('mark_scale', dict(markFrom=candles[-1]['c'] * 5,
                                          markTo=candles[-1]['c'] * 9))):
        sc = FakeScreen(480, 320)
        r = ChartRenderer(sc)
        r.render(candles, dict(cfg, **extra),
                 {'price': candles[-1]['c'], 'changePct': 2.47},
                 server_ok=True, ts=1759000000)
        sc.last.save(f'preview/{nm}.png')
        print(f'saved preview/{nm}.png')

    # ---------- آلارم: قیمت داخل بازه → تم چشمکی قرمز/سبز ----------
    for phase, name in ((1, 'alert_on'), (2, 'alert_off')):
        sc = FakeScreen(480, 320)
        r = ChartRenderer(sc)
        cfg = dict(cfg)
        cfg.update(markFrom=candles[-1]['c'] * 0.98, markTo=candles[-1]['c'] * 1.02,
                   alertRange=True)
        for _ in range(phase):
            r.render(candles, cfg, {'price': candles[-1]['c'], 'changePct': 1.2},
                     server_ok=True, ts=1759000000)
        sc.last.save(f'preview/{name}.png')
        print(f'saved preview/{name}.png')

    # ---------- آلارم روی کارت قیمت ایران ----------
    sc = FakeScreen(480, 320)
    r = ChartRenderer(sc)
    cfg = dict(symbol='USD', displayName='دلار آمریکا', interval='1d', feedMode='spot',
               limit=90, showVolume=False, rotate=False, theme='dark',
               chartType='candles', candleMode='solid', priceFont=180,
               markFrom=100000, markTo=140000, alertRange=True,
               indicators=dict(ma=False, ema=False, bbands=False, rsi=False, macd=False))
    r.render([], cfg, {'price': 120000, 'changePct': 1.5, 'unit': 'تومان'},
             server_ok=True, ts=1759000000)
    sc.last.save('preview/alert_iran_card.png')
    print('saved preview/alert_iran_card.png')

    # ---------- بازه‌های قیمتی چندگانه با رنگ اختصاصی ----------
    c_last = candles[-1]['c']
    sc = FakeScreen(480, 320)
    r = ChartRenderer(sc)
    cfg_mr = dict(base_cfg_480, marks={'BTCUSDT': {'from': c_last * 1.04, 'to': c_last * 1.06,
                                                   'color': '',
                                                   'list': [
        {'from': c_last * 0.96, 'to': c_last * 0.98, 'color': '#26c281'},
        {'from': c_last * 1.04, 'to': c_last * 1.06, 'color': '#f5526c'},
    ]}})
    r.render(candles, cfg_mr, {'price': c_last, 'changePct': 2.47},
             server_ok=True, ts=1759000000)
    sc.last.save('preview/colored_ranges.png')
    print('saved preview/colored_ranges.png')

    # ---------- خطوط رسم‌شدهٔ کاربر (trendLines) ----------
    sc = FakeScreen(480, 320)
    r = ChartRenderer(sc)
    cfg_tl = dict(base_cfg_480, trendLines={'BTCUSDT': [
        {'pts': [[4, 70], [46, 55], [92, 20]], 'color': '#00e5ff'},
        {'pts': [[10, 25], [85, 78]], 'color': '#ffd600'},
    ]})
    r.render(candles, cfg_tl, {'price': c_last, 'changePct': 2.47},
             server_ok=True, ts=1759000000)
    sc.last.save('preview/trend_lines.png')
    print('saved preview/trend_lines.png')

    # ---------- خطوط لنگرشده به قیمت (py) — همان مسیر Y(price) در ماژول ----------
    # اسکیل پنل قیمت عمداً مثل chart.py تکرار می‌شود (lo/hi از l/h + قیمت زنده،
    # pad ۵٪)؛ چیدمان جدید (هدر حذف): y0=۰، فول=۲۰، vol=۴۴، date_h=۱۴ → y_bot=۲۳۸
    sc = FakeScreen(480, 320)
    r = ChartRenderer(sc)
    _lo, _hi = min(c['l'] for c in candles), max(c['h'] for c in candles)
    _lo, _hi = min(_lo, c_last), max(_hi, c_last)
    _pad = (_hi - _lo) * 0.05 or 1e-9
    _lo, _hi = _lo - _pad, _hi + _pad

    def _inv(fy):
        return _lo + (1.0 - fy / 100.0) * (_hi - _lo)

    cfg_tlp = dict(base_cfg_480, trendLines={'BTCUSDT': [
        {'pts': [[10, 50], [85, 50]], 'color': '#00e5ff'},                # درصد مرجع
        {'pts': [[10, 48], [85, 48]], 'color': '#ffd600',
         'py': [_inv(48), _inv(48)]},                                     # لنگر قیمت
        {'pts': [[10, 70], [85, 70]], 'color': '#ff4dd8', 'py': [1.0]},   # py نادرست → درصد
    ]})
    r.render(candles, cfg_tlp, {'price': c_last, 'changePct': 2.47},
             server_ok=True, ts=1759000000)
    sc.last.save('preview/trend_lines_price.png')
    print('saved preview/trend_lines_price.png')

    # ---------- اندازهٔ فونت‌های تاریخ/نام/تایم‌فریم + تیتر روی چارت ----------
    sc = FakeScreen(480, 320)
    r = ChartRenderer(sc)
    cfg_fs = dict(base_cfg_480, displayName='BTC / USDT',
                  fontScales={'date': 170, 'symbol': 160, 'timeframe': 150, 'title': 170})
    r.render(candles, cfg_fs, {'price': c_last, 'changePct': 2.47},
             server_ok=True, ts=1759000000)
    sc.last.save('preview/font_scaled.png')
    print('saved preview/font_scaled.png')

    # ---------- سه Moving Average (ma1/ma2/ma3) با دوره‌های کاربر ----------
    sc = FakeScreen(480, 320)
    r = ChartRenderer(sc)
    cfg_ma = dict(base_cfg_480, chartType='line',
                  indicators=dict(ma1=True, ma2=True, ma3=True, rsi=False, macd=False),
                  indParams=dict(ma1={'period': 8}, ma2={'period': 20}, ma3={'period': 60},
                                 rsi={'period': 14},
                                 macd={'fast': 12, 'slow': 26, 'signal': 9}))
    r.render(candles, cfg_ma, {'price': c_last, 'changePct': 2.47},
             server_ok=True, ts=1759000000)
    sc.last.save('preview/three_mas.png')
    print('saved preview/three_mas.png')

    # ---------- فونت محور Y (قیمت‌ها) — پایه و بزرگ‌شده ----------
    for nm, axs in (('axis_base', 100), ('axis_scaled', 190)):
        sc = FakeScreen(480, 320)
        r = ChartRenderer(sc)
        cfg_ax = dict(base_cfg_480, fontScales={'axis': axs})
        r.render(candles, cfg_ax, {'price': c_last, 'changePct': 2.47},
                 server_ok=True, ts=1759000000)
        sc.last.save(f'preview/{nm}.png')
        print(f'saved preview/{nm}.png')

    # تست صفحهٔ خطای منطقی (نه آفلاین)
    sc = FakeScreen(480, 320)
    r = ChartRenderer(sc)
    r.error('TypeError: cannot unpack non-sequence', retry_in=7)
    sc.last.save('preview/error_screen.png')
    print('saved preview/error_screen.png')

    # تست صفحهٔ آفلاین
    sc = FakeScreen()
    r = ChartRenderer(sc)
    r.offline(retry_in=7)
    sc.last.save('preview/offline.png')
    print('saved preview/offline.png')

    # ---------- تست چندچارت ----------
    from mini_chart import MiniChart  # noqa: F401  (صرفاً چک ایمپورت)

    panes_syms = [
        ('ETHUSDT', '15m', 'candles'), ('SOLUSDT', '1h', 'area'),
        ('BNBUSDT', '15m', 'line'),    ('XRPUSDT', '1h', 'candles'),
    ]
    pane_feeds = {}
    for i, (sym, iv, ct) in enumerate(panes_syms):
        cs = make_candles(n=40, base=100 + i * 220, seed=i + 11)
        pane_feeds[f'{sym}:{iv}'] = {
            'stale': False,
            'price': {'price': cs[-1]['c'], 'changePct': (i - 1.5) * 1.7},
            'candles': cs,
        }

    for layout, fname in (('dual', 'multi_dual'), ('quad', 'multi_quad')):
        sc = FakeScreen()
        r = ChartRenderer(sc)
        cfg = dict(symbol='BTCUSDT', displayName='BTC / USDT', interval='15m',
                   feedMode='spot', limit=90, showVolume=True, rotate=False,
                   theme='dark', chartType='candles', candleMode='solid',
                   indicators=dict(ma=True, ema=False, bbands=False, rsi=False, macd=False),
                   layout=layout,
                   panes=[dict(symbol=s, interval=i, chartType=c) for s, i, c in panes_syms])
        r.render(candles, cfg, price, server_ok=True, ts=1759000000, panes=pane_feeds)
        out = f'preview/{fname}.png'
        sc.last.save(out)
        print('saved', out)

    # ---------- چندچارت + آلارم سراسری (markFrom/markTo سطح cfg) ----------
    # بعد از اصلاح: تم/بنر سراسری در چندچارت نباید فعال شود — فقط پنل همان نماد
    for phase, fname in ((1, 'multi_alert_global_on'), (2, 'multi_alert_global_off')):
        sc = FakeScreen(480, 320)
        r = ChartRenderer(sc)
        cfg_g = dict(symbol='BTCUSDT', displayName='BTC / USDT', interval='15m',
                     feedMode='spot', limit=90, showVolume=True, rotate=False,
                     theme='dark', chartType='candles', candleMode='solid',
                     layout='dual', alertRange=True,
                     markFrom=c_last * 0.98, markTo=c_last * 1.02,
                     panes=[dict(symbol=s_, interval=i, chartType=c) for s_, i, c in panes_syms[:2]])
        for _ in range(phase):
            r.render(candles, cfg_g, {'price': c_last, 'changePct': 0.4},
                     server_ok=True, ts=1759000000, panes=pane_feeds)
        sc.last.save(f'preview/{fname}.png')
        print(f'saved preview/{fname}.png')


    # ---------- آلارم مستقل هر پنل چندچارت ----------
    for phase, fname in ((1, 'multi_alert_on'), (2, 'multi_alert_off')):
        sc = FakeScreen(480, 320)
        r = ChartRenderer(sc)
        cfg2 = dict(cfg)
        cfg2.update(layout='dual', alertRange=True,
                    panes=[dict(symbol=s_, interval=i, chartType=c) for s_, i, c in panes_syms[:2]])
        feeds2 = {}
        for i, (sym, iv, ct) in enumerate(panes_syms[:2]):
            cs = make_candles(n=40, base=100 + i * 220, seed=i + 11)
            p_now = cs[-1]['c']
            feeds2[f'{sym}:{iv}'] = {
                'stale': False,
                'price': {'price': p_now, 'changePct': 1.4},
                'candles': cs,
                # پنل اول داخل بازه (آلارم)، پنل دوم بیرون از بازه
                'markFrom': p_now * 0.98 if i == 0 else p_now * 1.4,
                'markTo': p_now * 1.02 if i == 0 else p_now * 1.5,
            }
        for _ in range(phase):
            r.render(candles, cfg2, {'price': candles[-1]['c'], 'changePct': 0.4},
                     server_ok=True, ts=1759000000, panes=feeds2)
        sc.last.save(f'preview/{fname}.png')
        print(f'saved preview/{fname}.png')

    # ---------- فونت قیمت زنده در پنل‌های چندچارت (priceFont) ----------
    # قیمت لحظه‌ای هدرِ مینی باید مثل حالت تکی با priceFont بزرگ شود؛
    # برای مقایسهٔ پیکسلی دو بار با priceFont پایه (۱۵۰) و بزرگ (۲۸۰) رندر می‌شود
    for pfn, fname in ((100, 'multi_pfont_base'), (280, 'multi_pfont_big')):
        sc = FakeScreen(480, 320)
        r = ChartRenderer(sc)
        cfg_p = dict(symbol='BTCUSDT', displayName='BTC / USDT', interval='15m',
                     feedMode='spot', limit=90, showVolume=True, rotate=False,
                     theme='dark', chartType='candles', candleMode='solid',
                     layout='dual', priceFont=pfn,
                     panes=[dict(symbol=s_, interval=i, chartType=c) for s_, i, c in panes_syms[:2]])
        r.render(candles, cfg_p, {'price': c_last, 'changePct': 0.4},
                 server_ok=True, ts=1759000000, panes=pane_feeds)
        sc.last.save(f'preview/{fname}.png')
        print(f'saved preview/{fname}.png')

if __name__ == '__main__':
    main()
