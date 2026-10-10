#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
دموی حالت‌های نمایش قیمت — خروجی با رندرر واقعی ماژول
اجرا:  python3 price_demo.py      →  preview/price_style_*.png
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chart import ChartRenderer  # noqa: E402
from dev_screenshot import FakeScreen, make_candles  # noqa: E402

STYLES = [
    ('chart', 'بزرگ روی چارت (پیش‌فرض)',
     'همان اندازهٔ درشت هدر، ولی روی خود چارت — گوشهٔ بالا-راست'),
    ('corner', 'گوشهٔ بالا-راست، ساده',
     'یک عدد متوسط سبز/قرمز با درصد تغییر کنارش'),
    ('badge', 'کادر رنگی (Badge)',
     'قیمت داخل یک کادر پررنگ، خوانا از فاصلهٔ دور'),
    ('callout', 'خط راهنما (Callout)',
     'قیمت بالا + خط باریک تا خط‌چین قیمت لحظه‌ای'),
    ('header', 'هدر (وضعیت فعلی)',
     'قیمت در نوار بالای صفحه، نه داخل چارت'),
]


def main():
    os.makedirs('preview', exist_ok=True)
    candles = make_candles(n=90, base=43000.0)
    price = candles[-1]['c']

    for style, fa, desc in STYLES:
        for w, h, tag in ((480, 320, '480'), (320, 240, '320')):
            sc = FakeScreen(w, h)
            r = ChartRenderer(sc)
            cfg = dict(symbol='BTC', displayName='BTC', interval='4h', feedMode='spot',
                       limit=90, showVolume=True, rotate=False, theme='dark',
                       chartType='candles', candleMode='solid', xAxisCal='jalali',
                       priceFont=100, priceStyle=style,
                       indicators=dict(ma=True, ema=False, bbands=False, rsi=False, macd=False))
            r.render(candles, cfg, {'price': price, 'changePct': 2.47},
                     server_ok=True, ts=1759000000)
            out = f'preview/price_style_{style}_{tag}.png'
            sc.last.save(out)
            print('saved', out)

    # نسخهٔ فارسی برای صفحهٔ دمو HTML
    with open('preview/price_styles.json', 'w', encoding='utf-8') as fh:
        import json
        json.dump([{'id': s, 'fa': f, 'desc': d} for s, f, d in STYLES], fh,
                  ensure_ascii=False, indent=1)
    print('saved preview/price_styles.json')


if __name__ == '__main__':
    main()