#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniChart — مینی‌چارت فشرده برای پنل‌های حالت چندچارت.
با محور Y (قیمت) و محور X (تاریخ/زمان) مستقل برای هر پنل.
"""

import time

from PIL import ImageDraw

# نکته: این ماژول عمداً چیزی از chart ایمپورت نمی‌کند (ایمپورت حلقوی)؛
# همهٔ منابع (theme، فونت، ابزار رسم) از renderer پاس‌داده‌شده می‌آیند.


class MiniChart:
    """یک پنل مینی‌چارت داخل مستطیل مشخص از تصویر اصلی."""

    def __init__(self, renderer):
        self.r = renderer  # دسترسی به img/d/theme/fonts

    def draw(self, rect, candles, symbol, interval, chart_type, price, change_pct, stale,
             alert=False, mark_from=0.0, mark_to=0.0, mark_color='', marks_list=None,
             trend_lines=None, pane_ind=None, ind_params=None, fonts=None,
             price_scale=1.0, x_cal='jalali'):
        d = self.r.d
        t = self.r.theme
        x0, y0, x1, y1 = rect
        pad = 6
        # ایمپورت تنبل (مانند fmt_price) تا حلقهٔ ایمپورت ایجاد نشود
        from chart import alert_phase, hex_to_rgb, scale
        fonts = fonts or {}
        pane_ind = pane_ind or {}

        # قاب پنل — در آلارم به‌صورت چشمکی قرمز ↔ سبز
        blink = getattr(self.r, '_blink', False)
        ph = alert_phase(blink) if alert else None
        if alert:
            fill, edge = ph['panel'], ph['edge']
        else:
            fill, edge = t['panel'], t['sep']
        d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=fill, outline=edge,
                    width=2 if alert else 1)

        if len(candles) < 2:
            self.r._text(((x0 + x1) // 2, (y0 + y1) // 2), '…', 14, t['dim'], anchor='mm')
            self.r._mark(rect)
            return

        closes = [c['c'] for c in candles]
        lo, hi = min(closes), max(closes)
        # بازهٔ قیمتی هدف این پنل وارد اسکیل نمی‌شود (عدم اثر روی محور)
        marks = [float(m) for m in (mark_from, mark_to) if m and m > 0]
        pad_v = (hi - lo) * 0.08 or 1e-9
        lo, hi = lo - pad_v, hi + pad_v

        # ---------- محور Y (قیمت) و محور X (تاریخ) هر پنل ----------
        # اندازهٔ فونت برچسب‌های محور از fontScales.pane (٪)
        from chart import fmt_axis as _fa
        pax_size = max(6, int(9 * scale(None, fonts.get('pane', 100))))
        f_pax = self.r.fonts.get(pax_size)
        axw = int(max(d.textlength(_fa(v), font=f_pax) for v in (lo, hi,
                    lo + (hi - lo) / 2))) + 8
        axw = max(26, min(axw, int((x1 - x0) * 0.34)))
        # نوار تاریخ پایین پنل — فقط اگر پنل به‌اندازهٔ کافی بلند باشد
        date_h = max(10, pax_size + 4) if (y1 - y0) >= 92 else 0

        # هدر: نماد + تایم‌فریم (بالا-راست) — فونت مقیاس‌پذیر
        sym_size = max(9, int(12 * scale(None, fonts.get('symbol', 100))))
        tf_size = max(8, int(10 * scale(None, fonts.get('timeframe', 100))))
        # قیمت لحظه‌ای هم مثل حالت تکی با priceFont بزرگ می‌شود؛
        # (!_price_scale روی renderer) ناسازگار → تک‌تک پنل‌ها مستقل از cfg،
        # پس scale از _draw_multi پاس داده می‌شود.
        price_sc = max(0.6, min(3.0, float(price_scale or 1.0)))
        price_size = max(9, int(11 * price_sc))
        chg_size = max(8, int(10 * price_sc))
        # ارتفاع هدر با بزرگ‌شدن فونت قیمت هماهنگ تا قیمت/درصد جای خود را داشته باشند
        head_h = max(22, sym_size + 10, price_size + 8)
        self.r._text((x0 + pad, y0 + head_h // 2), f'{symbol[:10]}', sym_size, t['text'], True, 'lm')
        sw = d.textlength(symbol[:10], font=self.r.fonts.get(sym_size, True))
        self.r._text((x0 + pad + sw + 8, y0 + head_h // 2), interval.upper(), tf_size, t['accent'], anchor='lm')

        # قیمت + درصد (بالا-چپ) — فونت درشت قیمت زنده
        ch_col = t['up'] if change_pct >= 0 else t['down']
        if price:
            from chart import fmt_price as _fp
            ps = _fp(price)
            # اگر عدد بزرگ جا نشد، اندازه به‌صورت خودتنظیم کوچک می‌شود (نه کمتر از پایه)
            p_size = price_size
            while p_size > 11 and d.textlength(ps, font=self.r.fonts.get(p_size, True)) > (x1 - x0) * 0.55:
                p_size -= 1
            pw = d.textlength(ps, font=self.r.fonts.get(p_size, True))
            self.r._text((x1 - pad - pw, y0 + head_h // 2), ps, p_size, ch_col, True, 'lm')
        chs = ('+' if change_pct >= 0 else '') + f'{change_pct:.1f}%'
        cw = d.textlength(chs, font=self.r.fonts.get(chg_size, True))
        self.r._text((x1 - pad - cw, y0 + head_h + 13), chs, chg_size, ch_col, anchor='lm')

        # ناحیهٔ رسم + خط بازهٔ هدف (با فونت بزرگ درصد، پلات کمی پایین‌تر شروع می‌شود)
        gx0, gy0 = x0 + pad, y0 + head_h + max(26, chg_size + 16)
        gx1, gy1 = x1 - pad - axw, y1 - pad - 4 - date_h
        gw, gh = gx1 - gx0, gy1 - gy0
        if gw < 20 or gh < 14:
            self.r._mark(rect)
            return

        # شبکهٔ افقی + برچسب قیمت سمت راست پنل (محور Y)
        for i in range(1, 5):
            gy = gy0 + gh * i / 5
            self.r._dashed_h(int(gy), gx0, gx1, t['grid'])
            self.r._text((gx1 + 4, gy), _fa(hi - (hi - lo) * i / 5),
                         pax_size, t['grid_txt'], anchor='lm')
        self.r.d.line([(gx1, gy0), (gx1, gy1)], fill=t['sep'])

        fg = None
        if alert:
            # محتوای پنل هم با فاز قرمز/سبز آلارم هماهنگ می‌شود
            fg, fg2 = ph['text'], ph['dim']
            t = dict(t, panel=fill, up=fg, down=fg2, line=fg, bb=fg2,
                     grid=fg2, text=fg, dim=fg2, accent=fg2, sep=fg2)
            ch_col = fg

        n = len(candles)
        slot = gw / n

        def Y(v):
            return gy1 - (v - lo) / (hi - lo) * gh

        def clipY(v):
            return int(max(gy0, min(Y(v), gy1)))

        # خط راهنمای میانی
        mid = (gy0 + gy1) // 2
        self.r._dashed_h(mid, gx0, gx1, t['grid'], 3, 5)

        if chart_type in ('candles', 'heikin'):
            bw = max(1, min(int(slot * 0.6), 8))
            for i, c in enumerate(candles):
                x = int(gx0 + i * slot + slot / 2)
                col = t['up'] if c['c'] >= c['o'] else t['down']
                d.line([(x, int(Y(c['h']))), (x, int(Y(c['l'])))], fill=col)
                # ترتیب مهم است: در تصویر، کندل صعودی (c >= o) بازهٔ y کوچکتری
                # دارد؛ اگر پیش از بررسی «بدنهٔ نازک» min/max نکنیم، bot - top
                # منفی می‌شود و بدنهٔ کندل‌های سبز به یک خط یک‌پیکسلی تا می‌خورد
                # (LCD با ادیتور و حالت تکی یکی نمی‌شد).
                yo, yc = int(Y(c['o'])), int(Y(c['c']))
                top, bot = min(yo, yc), max(yo, yc)
                if bot - top < 1:
                    bot = top + 1
                d.rectangle([x - bw // 2, top, x + bw // 2, bot], fill=col)
        else:
            # line / area با فیل EMA سبک روی دیتای اصلی
            pts = [(int(gx0 + i * slot + slot / 2), Y(v)) for i, v in enumerate(closes)]
            col = t['line']
            if chart_type == 'area':
                poly = [(gx0, gy1)] + pts + [(gx1, gy1)]
                d.polygon(poly, fill=t['bb'])  # رنگ ملایم بولینگر به‌عنوان فیل
            self.r._polyline(pts, col, 2)

        # اندیکاتورهای پنل — سه Moving Average (ma1/ma2/ma3) با پارامترهای هر پنل.
        # فرمت قدیمی (ma/ema/bbands) نادیده گرفته می‌شود؛ ma قدیمی → ma1.
        try:
            from chart import sma_series
            closes_all = closes
            ip = ind_params or {}
            for mid, dflt, ckey in (('ma1', 20, 'accent'), ('ma2', 50, 'ema'), ('ma3', 200, 'bb')):
                ent = pane_ind.get(mid)
                if not isinstance(ent, dict) and mid == 'ma1':
                    ent = pane_ind.get('ma')
                if not (isinstance(ent, dict) and ent.get('on')):
                    continue
                try:
                    per = max(2, int(ent.get('period') or (ip.get(mid) or {}).get('period') or dflt))
                except (TypeError, ValueError):
                    per = dflt
                pts = [(int(gx0 + i * slot + slot / 2), Y(v))
                       for i, v in enumerate(sma_series(closes_all, per)) if v is not None]
                self.r._polyline(pts, fg or t[ckey], 1)
        except Exception:
            pass

        # بازه‌های قیمتی چندگانهٔ رنگی + بازهٔ سادهٔ قدیمی (بدون تغییر اسکیل)
        seen = False
        for mrec in (marks_list or []):
            lo_m, hi_m = float(mrec.get('from') or 0), float(mrec.get('to') or 0)
            if not lo_m and not hi_m:
                continue
            mcol = hex_to_rgb(mrec.get('color'), None) or fg or t['accent']
            vals_m = sorted(v for v in (lo_m, hi_m) if v > 0)
            if len(vals_m) == 2 and vals_m[0] != vals_m[1]:
                ya, yb = clipY(vals_m[-1]), clipY(vals_m[0])
                d.rectangle([gx0, ya, gx1, max(yb, ya + 1)], outline=mcol)
            else:
                self.r._dashed_h(clipY(vals_m[0]), gx0, gx1, mcol, 3, 4)
            seen = True
        if not seen and marks:
            mcol = hex_to_rgb(mark_color, None) or fg or t['accent']
            if len(marks) == 2 and marks[0] != marks[1]:
                ya, yb = clipY(max(marks)), clipY(min(marks))
                d.rectangle([gx0, ya, gx1, max(yb, ya + 1)], outline=mcol)
            else:
                self.r._dashed_h(clipY(marks[0]), gx0, gx1, mcol, 3, 4)

        # خطوط رسم‌شدهٔ کاربر (مختصات درصدی ۰..۱۰۰ از مستطیل پنل)
        # لنگر عمودی قیمت (py) — مثل chart.py: اگر قیمت‌ها معتبر و هم‌طول
        # pts بودند از Y(price) استفاده می‌شود، وگرنه درصد قدیمی
        for ln in (trend_lines or []):
            if not isinstance(ln, dict):
                continue
            lcol = hex_to_rgb(ln.get('color'), None) or fg or t['line']
            py = ln.get('py') or []
            use_py = isinstance(py, (list, tuple)) and len(py) == len(ln.get('pts') or [])
            pts = []
            for ti, pt in enumerate(ln.get('pts') or []):
                try:
                    fx = max(0.0, min(1.0, float(pt[0]) / 100.0))
                    fy = max(0.0, min(1.0, float(pt[1]) / 100.0))
                except (TypeError, ValueError, IndexError):
                    continue
                xx = int(x0 + fx * (x1 - 1 - x0))
                yy = None
                if use_py:
                    try:
                        pv = float(py[ti])
                    except (TypeError, ValueError):
                        pv = 0.0
                    if pv > 0:
                        yy = int(max(y0, min(Y(pv), y1 - 1)))
                if yy is None:
                    yy = int(y0 + fy * (y1 - y0))
                pts.append((xx, yy))
            if len(pts) >= 2:
                self.r._polyline(pts, lcol, 1)
        if alert:
            self.r._text((x0 + pad, y0 + head_h + 13), 'ALERT', 9,
                         ph['edge'], True, 'lm')

        # محور X — نوار تاریخ/زمان پایین پنل (شمسی یا میلادی از کانفیگ)
        if date_h:
            from chart import epoch_to_jalali as _e2j
            span = candles[-1]['t'] - candles[0]['t']
            step = (candles[1]['t'] - candles[0]['t']) if n > 1 else 0
            show_date = span >= 86400 or step >= 3600
            n_lab = max(2, min(4, gw // 60))
            for k in range(n_lab):
                idx = min(n - 1, int((k + 0.5) / n_lab * n))
                tt = int(candles[idx]['t'])
                if show_date:
                    if x_cal == 'gregorian':
                        g = time.gmtime(tt)
                        lab = f'{g.tm_year}/{g.tm_mon:02d}/{g.tm_mday:02d}'
                    else:
                        jy, jm, jd = _e2j(tt)
                        lab = f'{jy}/{jm:02d}/{jd:02d}'
                else:
                    lab = time.strftime('%H:%M', time.localtime(tt))
                lx2 = int(gx0 + gw * (k + 0.5) / n_lab)
                self.r._text((lx2, gy1 + date_h / 2), lab, pax_size,
                             t['grid_txt'], anchor='mm')
            self.r.d.line([(gx0, gy1), (gx1, gy1)], fill=t['sep'])

        # نقطهٔ انتهایی
        lx = int(gx0 + (n - 1) * slot + slot / 2)
        d.ellipse([lx - 2, Y(closes[-1]) - 2, lx + 2, Y(closes[-1]) + 2], fill=ch_col)

        if stale:
            # بالای نوار تاریخ محور X — تا با برچسب‌های تاریخ تداخل نکند
            self.r._text((x0 + pad, y1 - pad - 6 - date_h), 'CACHE', 8, t['down'], anchor='lm')

        self.r._mark(rect)
