#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CryptoChart Renderer — چارت حرفه‌ای روی فریم‌بافر لینوکس
----------------------------------------------------------
ویژهٔ Raspberry Pi Zero (Raspberry Pi OS Lite بدون دسکتاپ):
  * تنها وابستگی: Pillow  (بدون X، بدون pygame، بدون مرورگر)
  * رندر مستقیم روی /dev/fb0 با پشتیبانی 16/24/32bpp
  * فلاش ناحیه‌ای (dirty-rect) → کمترین انتقال حافظه و مصرف CPU
"""

import math
import mmap
import os
import struct
import sys
import time

from PIL import Image, ImageDraw, ImageFont

from mini_chart import MiniChart

# ---------------- ثابت‌های فریم‌بافر ----------------
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602

VAR_FMT = '=' + '40I'                      # fb_var_screeninfo (160 بایت، ARM 32bit)
FIX_FMT = '=16sLIIIHHH2xIIIII'             # fb_fix_screeninfo (تا line_length)


# ---------------- تم‌ها ----------------
THEMES = {
    'dark': dict(
        bg=(13, 16, 22), panel=(19, 23, 31), grid=(37, 43, 55), grid_txt=(120, 130, 148),
        text=(210, 218, 230), dim=(120, 130, 148),
        up=(0, 200, 120), down=(245, 82, 100), up_dim=(0, 120, 75), down_dim=(140, 50, 62),
        accent=(255, 193, 7), line=(64, 156, 255), ema=(255, 152, 0),
        bb=(155, 120, 255), sep=(35, 40, 52), tag_bg=(28, 34, 46),
    ),
    'midnight': dict(
        bg=(6, 10, 26), panel=(10, 15, 36), grid=(26, 34, 66), grid_txt=(96, 110, 160),
        text=(200, 210, 240), dim=(96, 110, 160),
        up=(0, 230, 160), down=(255, 90, 120), up_dim=(0, 130, 95), down_dim=(150, 55, 75),
        accent=(255, 214, 0), line=(80, 170, 255), ema=(255, 170, 60),
        bb=(170, 140, 255), sep=(24, 30, 58), tag_bg=(16, 22, 48),
    ),
    'ocean': dict(
        bg=(4, 22, 30), panel=(7, 30, 40), grid=(20, 56, 70), grid_txt=(90, 150, 165),
        text=(200, 235, 240), dim=(90, 150, 165),
        up=(30, 220, 170), down=(255, 100, 110), up_dim=(20, 130, 100), down_dim=(150, 60, 68),
        accent=(255, 200, 60), line=(60, 190, 230), ema=(255, 160, 70),
        bb=(140, 150, 255), sep=(18, 46, 58), tag_bg=(12, 40, 52),
    ),
    'light': dict(
        bg=(246, 247, 250), panel=(255, 255, 255), grid=(222, 226, 234), grid_txt=(120, 128, 142),
        text=(28, 32, 40), dim=(120, 128, 142),
        up=(6, 166, 100), down=(226, 58, 74), up_dim=(140, 210, 180), down_dim=(235, 170, 178),
        accent=(183, 120, 0), line=(0, 110, 220), ema=(230, 126, 34),
        bb=(130, 100, 220), sep=(228, 231, 238), tag_bg=(235, 238, 244),
    ),
    'matrix': dict(
        bg=(0, 8, 2), panel=(0, 14, 4), grid=(0, 46, 16), grid_txt=(30, 130, 60),
        text=(60, 240, 110), dim=(30, 130, 60),
        up=(0, 255, 90), down=(255, 60, 60), up_dim=(0, 150, 55), down_dim=(150, 40, 40),
        accent=(180, 255, 40), line=(0, 200, 255), ema=(200, 255, 60),
        bb=(0, 180, 120), sep=(0, 40, 14), tag_bg=(0, 26, 8),
    ),
}

# ---------------- آلارم: چشمک قرمز ↔ سبز ----------------
# دو فاز رنگی؛ فاز اول قرمز و فاز دوم سبز. بازهٔ قیمتی فقط رنگ و بنر
# می‌آورد و هیچ تغییری در مقیاس محور Y نمی‌دهد.
ALERT_PHASE = (
    dict(bg=(38, 6, 10), panel=(56, 9, 14), grid=(104, 22, 30), grid_txt=(228, 130, 144),
         text=(255, 233, 237), dim=(228, 130, 144), edge=(255, 66, 94),
         up=(255, 66, 94), down=(255, 150, 165), up_dim=(140, 18, 32), down_dim=(96, 40, 50),
         line=(255, 120, 140), ema=(255, 190, 200), bb=(214, 40, 68),
         accent=(255, 66, 94), sep=(96, 20, 28), tag_bg=(70, 11, 18),
         banner=(212, 24, 44), banner_fg=(255, 255, 255)),
    dict(bg=(3, 24, 13), panel=(6, 46, 23), grid=(18, 96, 48), grid_txt=(122, 220, 166),
         text=(228, 255, 238), dim=(122, 220, 166), edge=(52, 240, 142),
         up=(52, 240, 142), down=(180, 225, 200), up_dim=(12, 130, 74), down_dim=(70, 110, 88),
         line=(130, 240, 185), ema=(200, 255, 220), bb=(40, 200, 120),
         accent=(52, 240, 142), sep=(18, 88, 44), tag_bg=(8, 54, 28),
         banner=(0, 156, 92), banner_fg=(4, 22, 12)),
)
ALERT_THEME_NAMES = ('alert_red', 'alert_green')


def alert_phase(blink=False):
    """فاز جاری آلارم: False → قرمز، True → سبز."""
    return ALERT_PHASE[1 if blink else 0]


def _alert_theme(ph):
    """تم آلارم از تم پایه ساخته می‌شود تا فقط رنگ‌ها عوض شوند."""
    return dict(THEMES['dark'], **{
        k: ph[k] for k in ('bg', 'panel', 'grid', 'grid_txt', 'text', 'dim',
                           'up', 'down', 'up_dim', 'down_dim', 'line', 'ema',
                           'bb', 'accent', 'sep', 'tag_bg')})


THEMES['alert_red'] = _alert_theme(ALERT_PHASE[0])
THEMES['alert_green'] = _alert_theme(ALERT_PHASE[1])


FONT_CANDIDATES = [
    # فونت فارسی (نصب با install.sh) — برای نام‌های فارسی طلا/ارز
    '/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Regular.ttf',
    '/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Bold.ttf',
    '/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf',
    '/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf',
    # لاتین
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
    # ویندوز — فقط برای پیش‌نمایش توسعه (dev_screenshot.py)
    'C:/Windows/Fonts/arialbd.ttf',
    'C:/Windows/Fonts/arial.ttf',
]

# فونت پشتیبان برای متن فارسی — انتخاب اولین فونتی که گلیف فارسی دارد
import glob as _glob
_FA_FALLBACKS = [
    '/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Regular.ttf',
    'C:/Windows/Fonts/tahoma.ttf',
    '/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf',
]

def _find_fa_font():
    for p in _FA_FALLBACKS:
        if os.path.exists(p):
            return p
    return None

_FA_FONT = _find_fa_font()

def _is_fa_text(s):
    """تشخیص متن فارسی/عربی برای انتخاب فونت مناسب."""
    return any('\u0600' <= ch <= '\u06ff' or '\u200c' in s for ch in (s or ''))


# ---------------- قالب‌بندی اعداد ----------------
def fmt_price(p: float) -> str:
    a = abs(p)
    if a >= 10000: return f'{p:,.0f}'
    if a >= 100:   return f'{p:,.2f}'
    if a >= 1:     return f'{p:.4f}'
    if a >= 0.01:  return f'{p:.5f}'
    return f'{p:.8f}'.rstrip('0').rstrip('.') or '0'


def fmt_axis(v: float) -> str:
    a = abs(v)
    if a >= 1e6:  return f'{v / 1e6:.2f}M'
    if a >= 1e4:  return f'{v / 1e3:.1f}k'
    if a >= 100:  return f'{v:.1f}'
    if a >= 1:    return f'{v:.3f}'
    if a >= 0.01: return f'{v:.4f}'
    return f'{v:.6f}'


def fmt_compact(v: float) -> str:
    a = abs(v)
    for div, suf in ((1e9, 'B'), (1e6, 'M'), (1e3, 'k')):
        if a >= div:
            s = f'{v / div:.1f}'.rstrip('0').rstrip('.')
            return s + suf
    return f'{v:.0f}'


def fmt_change(ch: float) -> str:
    return ('+' if ch >= 0 else '') + f'{ch:.2f}%'


def hex_to_rgb(h, default=None):
    """'#ff8800' → (255, 136, 0)؛ نامعتبر → default."""
    s = str(h or '').strip().lstrip('#')
    if len(s) != 6:
        return default
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


def scale(v, pct):
    """ضریب اندازهٔ فونت برحسب درصد (۱۰۰٪ = بدون تغییر)."""
    try:
        return max(0.5, min(2.5, (pct if pct else 100) / 100.0))
    except (TypeError, ValueError):
        return 1.0


# ---------------- سه Moving Average (MA) — جایگزین EMA/بولینگر ----------------
# سه MA با دوره‌های جدا که کاربر از وب‌اپ وارد می‌کند؛ رنگ‌ها متمایزند.
MA_SPECS = (('ma1', 20), ('ma2', 50), ('ma3', 200))          # (id, دورهٔ پیش‌فرض)
MA_PLOTS = (('ma1', 'accent', 2), ('ma2', 'ema', 1), ('ma3', 'bb', 1))  # (id، رنگ، ضخامت)


def norm_ind(ind):
    """اندیکاتورها به قالب جدید (سه MA) — کانفیگ قدیمی ma/ema/bbands مهاجرت می‌کند."""
    out = dict(ind or {})
    if 'ma1' not in out:
        out['ma1'] = out.get('ma', False)
    for k in ('ma', 'ema', 'bbands'):
        out.pop(k, None)
    for mid, _d in MA_SPECS:
        out[mid] = bool(out.get(mid))
    return out


def norm_iparams(ip):
    """پارامترهای اندیکاتور به قالب جدید — دورهٔ هر MA محدود به ۲..۴۰۰."""
    out = dict(ip or {})
    if not isinstance(out.get('ma1'), dict) and isinstance(out.get('ma'), dict):
        out['ma1'] = out['ma']
    for k in ('ma', 'ema', 'bbands'):
        out.pop(k, None)
    for mid, dflt in MA_SPECS:
        ent = out.get(mid) if isinstance(out.get(mid), dict) else {}
        try:
            out[mid] = {'period': max(2, min(400, int(ent.get('period') or dflt)))}
        except (TypeError, ValueError):
            out[mid] = {'period': dflt}
    return out


def _norm_pane_ind(pi):
    """اندیکاتورهای پنل چندچارت → سه MA (فرمت قدیمی ma → ma1)."""
    out = {}
    src = pi if isinstance(pi, dict) else {}
    for mid, dflt in MA_SPECS:
        ent = src.get(mid)
        if not isinstance(ent, dict) and mid == 'ma1':
            ent = src.get('ma')
        if isinstance(ent, dict) and ent.get('on'):
            try:
                per = max(2, int(ent.get('period') or dflt))
            except (TypeError, ValueError):
                per = dflt
            out[mid] = {'on': True, 'period': per}
    return out


def trend_lines_for(cfg, sym):
    """خطوط رسم‌شدهٔ یک نماد — مقاوم در برابر فرمت قدیمی/خالی.
    وقتی هیچ خطی رسم نشده backend آرایهٔ خالی می‌فرستد؛ .get روی list
    می‌ترکید (AttributeError: 'list' object has no attribute 'get')."""
    tl = (cfg or {}).get('trendLines') if isinstance(cfg, dict) else None
    if isinstance(tl, dict):
        lines = tl.get(sym)
    elif isinstance(tl, list):
        lines = tl
    else:
        lines = None
    if not isinstance(lines, list):
        return []
    return [ln for ln in lines if isinstance(ln, dict)]


# ---------------- تاریخ شمسی (پورت jalaali-js — هم‌قرارداد با بک‌اند) ----------------
def tdiv(a: int, b: int) -> int:
    """تقسیم صحیح با گرد کردن به سمت صفر (سازگار با tdiv بک‌اند)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _gregorian_to_jalali(gy: int, gm: int, gd: int):
    gdm = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    jy = 0 if gy <= 1600 else 979
    y = gy - (621 if gy <= 1600 else 1600)
    y2 = y + 1 if gm > 2 else y
    days = (365 * y + tdiv(y2 + 3, 4) - tdiv(y2 + 99, 100)
            + tdiv(y2 + 399, 400) - 80 + gd + gdm[gm - 1])
    jy += tdiv(days, 12053) * 33
    days %= 12053
    jy += tdiv(days, 1461) * 4
    days %= 1461
    if days > 365:
        jy += tdiv(days - 1, 365)
        days = (days - 1) % 365
    jm = 1 + tdiv(days, 31) if days < 186 else 7 + tdiv(days - 186, 30)
    jd = 1 + (days % 31 if days < 186 else (days - 186) % 30)
    return jy, jm, jd


def epoch_to_jalali(ts: int):
    """unix epoch → (سال، ماه، روز شمسی) — پورت d2g از jalaali-js"""
    days = tdiv(int(ts), 86400) + 2440588          # JDN_1970
    j = 4 * days + 139361631
    j += tdiv(tdiv(4 * days + 183187720, 146097) * 3, 4) * 4 - 3908
    i = tdiv(j % 1461, 4) * 5 + 308
    gd = tdiv(i % 153, 5) + 1
    gm = tdiv(i, 153) % 12 + 1
    gy = tdiv(j, 1461) - 100100 + tdiv(8 - gm, 6)
    return _gregorian_to_jalali(gy, gm, gd)


# ---------------- اندیکاتورها ----------------
def sma_series(vals, n=20):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema_series(vals, n=12):
    out = [None] * len(vals)
    if not vals:
        return out
    k = 2.0 / (n + 1)
    e = None
    for i, v in enumerate(vals):
        e = v if e is None else (v * k + e * (1 - k))
        if i >= n - 1:
            out[i] = e
    return out


def bollinger_series(vals, n=20, mult=2.0):
    mid = sma_series(vals, n)
    upper = [None] * len(vals)
    lower = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        win = vals[i - n + 1:i + 1]
        m = mid[i]
        var = sum((x - m) ** 2 for x in win) / n
        sd = math.sqrt(var)
        upper[i] = m + mult * sd
        lower[i] = m - mult * sd
    return upper, mid, lower


def rsi_series(closes, n=14):
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0.0)
        l += max(-d, 0.0)
    ag, al = g / n, l / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def macd_series(closes, fast=12, slow=26, sig=9):
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    line = [None] * len(closes)
    for i in range(len(closes)):
        if ef[i] is not None and es[i] is not None:
            line[i] = ef[i] - es[i]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    signal = [None] * len(closes)
    if first is not None:
        vals = line[first:]
        se = ema_series(vals, sig)
        for j, v in enumerate(se):
            if v is not None:
                signal[first + j] = v
    return line, signal


def heikin_ashi(candles):
    out = []
    for i, c in enumerate(candles):
        hc = (c['o'] + c['h'] + c['l'] + c['c']) / 4.0
        ho = (candles[i - 1]['o'] + candles[i - 1]['c']) / 2.0 if i else (c['o'] + c['c']) / 2.0
        out.append({'t': c['t'], 'o': ho, 'c': hc,
                    'h': max(c['h'], ho, hc), 'l': min(c['l'], ho, hc), 'v': c['v']})
    return out


# ---------------- فونت‌ها ----------------
class Fonts:
    def __init__(self):
        self.reg = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
        self.bold = next((p for p in FONT_CANDIDATES if 'Bold' in p and os.path.exists(p)), self.reg)
        self._cache = {}
        # Raqm برای اتصال درست حروف فارسی (PIL در دبیان/ویندوز معمولاً دارد)
        try:
            self._layout = ImageFont.Layout.RAQM
        except AttributeError:
            self._layout = None

    def get(self, size, bold=False, fa=False):
        """fa=True → فونتی که گلیف فارسی دارد (برای نام‌های طلا/ارز ایران)."""
        key = (size, bold, fa)
        if key not in self._cache:
            if fa and _FA_FONT:
                path = _FA_FONT
            else:
                path = self.bold if bold else self.reg
            try:
                if path:
                    kwargs = {}
                    if self._layout is not None:
                        kwargs['layout_engine'] = self._layout
                    self._cache[key] = ImageFont.truetype(path, size, **kwargs)
                else:
                    self._cache[key] = ImageFont.load_default()
            except OSError:
                self._cache[key] = ImageFont.load_default()
        return self._cache[key]


# ---------------- صفحه (فریم‌بافر خام) ----------------
class Screen:
    """باز کردن /dev/fb0، تشخیص رزولوشن/فرمت و کپی ناحیه‌ای پیکسل‌ها."""

    def __init__(self, dev='/dev/fb0'):
        import fcntl  # فقط در لینوکس موجود است
        self.fd = os.open(dev, os.O_RDWR)
        self._mmap = None
        self.rotate = False

        var = struct.unpack(VAR_FMT, fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, b'\0' * 160))
        raw_fix = fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, b'\0' * 64)
        fix = struct.unpack(FIX_FMT, raw_fix[:struct.calcsize(FIX_FMT)])

        self.W, self.H = var[0], var[1]
        self.bpp = var[6]
        self.red_off = var[8]
        self.line_length = fix[9]
        self.stride = self.line_length or (self.W * self.bpp // 8)

        size = max(self.stride * self.H, self.W * self.H * self.bpp // 8)
        self._mmap = mmap.mmap(self.fd, size, mmap.MAP_SHARED,
                               mmap.PROT_READ | mmap.PROT_WRITE)

    def pack(self, img_rgb):
        """تبدیل RGB پیلو به آرایهٔ بایت سازگار با فرمت فریم‌بافر (در C، سریع)."""
        if self.bpp == 32:
            raw = 'BGRA' if self.red_off >= 16 else 'RGBA'
            return img_rgb.convert('RGBA').tobytes('raw', raw), 4
        if self.bpp == 24:
            raw = 'BGR' if self.red_off >= 16 else 'RGB'
            return img_rgb.tobytes('raw', raw), 3
        if self.bpp == 16:
            return img_rgb.convert('RGB').tobytes('raw', 'BGR;16'), 2
        raise RuntimeError(f'unsupported framebuffer bpp={self.bpp}')

    def flush(self, img_rgb, rect=None):
        """کپی ناحیهٔ dirty به فریم‌بافر — ردیف به ردیف با برش حافظه."""
        W, H = self.W, self.H
        if self.rotate:
            img_rgb = img_rgb.transpose(Image.ROTATE_90)
            W, H = H, W
        if self.rotate:
            rect = None  # در حالت چرخش، فلاش کامل انجام می‌شود
        x0, y0, x1, y1 = rect or (0, 0, W, H)
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(W, int(x1)), min(H, int(y1))
        if x1 <= x0 or y1 <= y0:
            return
        crop = img_rgb.crop((x0, y0, x1, y1))
        buf, pix = self.pack(crop)
        row = (x1 - x0) * pix
        fb = self._mmap
        for j in range(y1 - y0):
            off = (y0 + j) * self.stride + x0 * pix
            fb[off:off + row] = buf[j * row:(j + 1) * row]

    def close(self):
        try:
            if self._mmap:
                self._mmap.close()
            os.close(self.fd)
        except OSError:
            pass


# ---------------- رندرر چارت ----------------
class ChartRenderer:
    """
    رندر کامل چارت روی یک بافر پیلو؛ سپس فقط ناحیهٔ تغییرکرده به fb کپی می‌شود.
    صفحهٔ منطقی همیشه افقی است؛ rotate برای LCDهای عمودی است.
    """

    def __init__(self, screen: Screen, rotate=False):
        self.sc = screen
        self.sc.rotate = bool(rotate)
        # صفحهٔ منطقی: اگر rotate، عرض/ارتفاع جابه‌جا می‌شوند
        self.W, self.H = (screen.H, screen.W) if self.sc.rotate else (screen.W, screen.H)
        self.img = Image.new('RGB', (self.W, self.H), (0, 0, 0))
        self.d = ImageDraw.Draw(self.img)
        self.fonts = Fonts()
        self.theme = THEMES['dark']
        self._theme_name = ''
        self._dirty = None
        self._axis_w = 56
        self._price_scale = 1.0   # ضریب اندازهٔ فونت قیمت (از کانفیگ priceFont)
        self._alert_active = False  # آلارم بازهٔ قیمتی فعال است؟
        self._blink = True          # فاز چشمک آلارم (اولین فاز: قرمز)
        self._last_price_y = None   # ارتفاع خط قیمت لحظه‌ای (برای حالت callout)

    # ---------- ابزار داخلی ----------
    def _mark(self, rect):
        if self._dirty is None:
            self._dirty = list(rect)
        else:
            d = self._dirty
            d[0] = min(d[0], rect[0]); d[1] = min(d[1], rect[1])
            d[2] = max(d[2], rect[2]); d[3] = max(d[3], rect[3])

    def _text(self, xy, s, size=12, color=None, bold=False, anchor='la'):
        # متن فارسی → فونت دارای گلیف فارسی
        font = self.fonts.get(size, bold, fa=_is_fa_text(s))
        self.d.text(xy, s, font=font, fill=color or self.theme['text'], anchor=anchor)

    def _dashed_h(self, y, x0, x1, color, dash=4, gap=4, width=1):
        x = x0
        while x < x1:
            self.d.line([(x, y), (min(x + dash, x1), y)], fill=color, width=width)
            x += dash + gap

    def _dashed_v(self, x, y0, y1, color, dash=4, gap=4):
        y = y0
        while y < y1:
            self.d.line([(x, y), (x, min(y + dash, y1))], fill=color)
            y += dash + gap

    def _polyline(self, pts, color, width=1):
        if len(pts) >= 2:
            self.d.line(pts, fill=color, width=width, joint='curve')

    # ---------- چیدمان ----------
    def _layout(self, ind, cfg=None):
        H, W = self.H, self.W
        cfg = cfg or {}
        # هدر حذف شد — نماد/تایم‌فریم/قیمت روی خودِ چارت رسم می‌شوند تا نمودار
        # کل فضای صفحه را بگیرد. هدر فقط وقتی نگه داشته می‌شود که سبک قیمت
        # «header» باشد (قیمت بزرگ همان‌جا نمایش داده می‌شود). در حالت
        # چندچارت هدر هرگز لازم نیست — هر پنل قیمت خودش را دارد.
        show_header = (cfg.get('layout', 'single') == 'single'
                       and cfg.get('priceStyle', 'chart') == 'header')
        header_h = (max(30, min(int(H * 0.15 * max(1.0, self._price_scale) ** 0.6), 120))
                    if show_header else 0)
        footer_h = 16 if H < 320 else 20
        rem = H - header_h - footer_h - 4
        vol_h = rsi_h = macd_h = 0
        if ind.get('rsi'):   rsi_h = max(34, min(int(rem * 0.20), 90))
        if ind.get('macd'):  macd_h = max(36, min(int(rem * 0.22), 100))
        vol_h = max(30, min(int(rem * 0.15), 80)) if ind.get('_vol') else 0
        price_h = rem - vol_h - rsi_h - macd_h
        if price_h < 80:  # محافظت در صفحه‌های خیلی کوچک
            price_h = 80
            vol_h = max(0, vol_h // 2); rsi_h = max(0, rsi_h // 2); macd_h = max(0, macd_h // 2)
        return dict(header=header_h, footer=footer_h, price=price_h,
                    vol=vol_h, rsi=rsi_h, macd=macd_h, axis=self._axis_w)

    # ---------- هدر ----------
    def _draw_header(self, y0, h, cfg, price, change, n_candles, server_ok):
        t = self.theme
        W = self.W
        self.d.rectangle([0, y0, W, y0 + h], fill=t['panel'])
        self.d.line([(0, y0 + h), (W, y0 + h)], fill=t['sep'])
        cy = y0 + h // 2
        self._price_style = cfg.get('priceStyle', 'chart')

        # نماد و تایم‌فریم — اندازهٔ فونت از fontScales (٪).
        # حالت تکی: این دو روی خودِ چارت رسم می‌شوند (_draw_chart_title)؛
        # هدر فقط قیمت لحظه‌ای (در سبک header) و نقطهٔ وضعیت را نشان می‌دهد.
        fs = getattr(self, '_fs', {}) or {}
        sym_size = max(10, int(15 * scale(None, fs.get('symbol', 100))))
        tf_size = max(8, int(11 * scale(None, fs.get('timeframe', 100))))
        sym_w = 0
        bw = 0
        if cfg.get('layout', 'single') != 'single':
            sym_txt = str(cfg.get('displayName') or cfg.get('symbol') or '')
            self._text((8, cy), sym_txt, sym_size, t['text'], True, 'lm')
            sym_w = (self.d.textlength(sym_txt, font=self.fonts.get(sym_size, True)) if sym_txt else 0) + 16
            iv = cfg.get('interval', '15m').upper()
            tag_h = max(17, tf_size + 7)
            bw = int(self.d.textlength(iv, font=self.fonts.get(tf_size, True))) + 12
            self.d.rectangle([sym_w, cy - tag_h // 2, sym_w + bw, cy + tag_h // 2], fill=t['tag_bg'], outline=t['sep'])
            self._text((sym_w + 6, cy), iv, tf_size, t['accent'], True, 'lm')

        # قیمت لحظه‌ای — خیلی بزرگ، گوشهٔ بالا-راست (خودتنظیم تا جایی که جا شود)
        if price and price > 0 and cfg.get('priceStyle', 'chart') == 'header':
            pc = t['up'] if change >= 0 else t['down']
            ps = fmt_price(price)
            chs = fmt_change(change)
            right_edge = W - 18                 # قبل از نقطهٔ وضعیت اتصال
            left_used = sym_w + bw + 12         # انتهای نماد + بج تایم‌فریم
            avail = max(60, right_edge - left_used)
            sc = self._price_scale
            size = max(16, min(int(h * 0.72 * sc), int(h * 0.80), int(52 * sc)))
            while size > 14:
                f = self.fonts.get(size, True)
                pw = self.d.textlength(ps, font=f)
                csize = max(10, int(size * 0.42))
                cw = self.d.textlength(chs, font=self.fonts.get(csize, True))
                if pw + cw + 20 <= avail:
                    break
                size -= 2
            f = self.fonts.get(size, True)
            pw = self.d.textlength(ps, font=f)
            csize = max(10, int(size * 0.42))
            cw = self.d.textlength(chs, font=self.fonts.get(csize, True))
            self._text((right_edge - pw, cy), ps, size, pc, True, 'lm')
            self._text((right_edge - pw - cw - 14, cy), chs, csize, pc, True, 'lm')

        # وضعیت اتصال
        dot_c = t['up'] if server_ok else t['down']
        self.d.ellipse([W - 12, cy - 3, W - 6, cy + 3], fill=dot_c)

    # ---------- تیتر روی چارت (حالت تکی): نماد + تایم‌فریم ----------
    def _draw_chart_title(self, rect, cfg):
        """نماد و تایم‌فریم گوشهٔ بالا-چپِ خود چارت (به‌جای هدر).
        اندازه با fontScales.title از وب‌اپ قابل تنظیم است."""
        t = self.theme
        x0, y0, x1, _y1 = rect
        fs = getattr(self, '_fs', {}) or {}
        sym_size = max(9, int(15 * scale(None, fs.get('title', 100))))
        tf_size = max(8, int(11 * scale(None, fs.get('title', 100))))
        sym_txt = str(cfg.get('displayName') or cfg.get('symbol') or '')
        cy = y0 + max(12, sym_size // 2 + 5)
        self._text((8, cy), sym_txt, sym_size, t['text'], True, 'lm')
        sym_w = (self.d.textlength(sym_txt, font=self.fonts.get(sym_size, True)) if sym_txt else 0) + 16
        iv = cfg.get('interval', '15m').upper()
        tag_h = max(15, tf_size + 6)
        bw = int(self.d.textlength(iv, font=self.fonts.get(tf_size, True))) + 12
        self.d.rectangle([sym_w, cy - tag_h // 2, sym_w + bw, cy + tag_h // 2],
                         fill=t['tag_bg'], outline=t['sep'])
        self._text((sym_w + 6, cy), iv, tf_size, t['accent'], True, 'lm')
        self._mark((x0, y0, min(x1, int(sym_w + bw + 10)), cy + tag_h // 2 + 2))

    # ---------- چارت قیمت ----------
    def _draw_price(self, rect, candles, ind_on, chart_type, candle_mode, last_price, prev_close,
                    mark_from=0.0, mark_to=0.0, x_cal='jalali', ind_params=None,
                    marks_list=None, trend_lines=None):
        t = self.theme
        x0, y0, x1, y1 = rect
        axw = self._axis_w
        px0, px1 = x0, x1 - axw              # ناحیهٔ رسم قیمت
        self.d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=t['bg'])

        if len(candles) < 2:
            self._text(((x0 + x1) // 2, (y0 + y1) // 2), 'Waiting for data...', 13, t['dim'], anchor='mm')
            self._mark(rect)
            return

        closes = [c['c'] for c in candles]
        lo = min(c['l'] for c in candles)
        hi = max(c['h'] for c in candles)

        ip = ind_params or {}
        ser = {}
        # سه Moving Average با دوره‌های وارد‌شده توسط کاربر (جایگزین EMA/بولینگر).
        # نکته: MAها اسکیل محور Y را تغییر نمی‌دهند؛ بیرون از بازه بریده می‌شوند.
        _ma_dflt = dict(MA_SPECS)
        for mid, _ckey, _lnw in MA_PLOTS:
            if ind_on.get(mid):
                per = max(2, int((ip.get(mid) or {}).get('period') or _ma_dflt[mid]))
                ser[mid] = sma_series(closes, per)

        # قیمت هدف / بازهٔ قیمتی (markFrom/markTo) — عمداً وارد اسکیل نمی‌شود تا
        # انتخاب بازه هیچ تغییری در اعداد محور Y ندهد؛ فقط کشیده/بریده می‌شود.
        marks = [float(m) for m in (mark_from, mark_to) if m and m > 0]

        # قیمت لحظه‌ای همیشه داخل اسکیل باشد (حتی اگر بیرون از کندل‌ها باشد)
        if last_price and last_price > 0:
            lo = min(lo, last_price)
            hi = max(hi, last_price)

        pad = (hi - lo) * 0.05 or 1e-9
        lo, hi = lo - pad, hi + pad
        # نوار تاریخ محور X پایین پنل قیمت (برای صفحه‌های خیلی کوچک حذف می‌شود)
        date_h = 14 if (y1 - y0) >= 70 else 0
        y_bot = y1 - date_h
        ph = y_bot - y0

        def Y(v):
            return y_bot - (v - lo) / (hi - lo) * ph

        n = len(candles)
        slot = (px1 - px0) / n
        bw = max(1, min(int(slot * 0.62), 22))
        cx0 = px0 + slot / 2

        # شبکهٔ افقی + برچسب قیمت — اندازهٔ فونت محور Y از fontScales.axis (٪)
        fs = getattr(self, '_fs', {}) or {}
        ax_size = max(7, int(10 * scale(None, fs.get('axis', 100))))
        f_lab = self.fonts.get(ax_size)
        for i in range(1, 5):
            gy = y0 + ph * i / 5
            self._dashed_h(int(gy), px0, px1, t['grid'])
            self._text((x1 - axw + 6, gy), fmt_axis(hi - (hi - lo) * i / 5), ax_size, t['grid_txt'], anchor='lm')

        # ابزار بازهٔ قیمتی — فیل پشت کندل‌ها کشیده می‌شود تا نمودار زیر آن
        # کاملاً معلوم باشد (شفاف‌تر؛ فقط یک تینت ملایم پس‌زمینه)
        def _mix(c1, c2, k):
            return tuple(int(c1[i] * (1 - k) + c2[i] * k) for i in range(3))

        def _clip(v):
            return int(max(y0, min(v, y_bot)))

        acc = t['accent']
        range_alpha = 0.13
        fills = []
        for m in (marks_list or []):
            mf = float(m.get('from') or 0)
            mt = float(m.get('to') or 0)
            if not mf and not mt:
                continue
            mc = hex_to_rgb(m.get('color'), None) or acc
            vals_m = sorted(v for v in (mf, mt) if v > 0)
            if len(vals_m) == 2 and vals_m[0] != vals_m[1]:
                fills.append((_clip(Y(vals_m[-1])), _clip(Y(vals_m[0])), mc))
        if not fills and marks and len(marks) == 2 and marks[0] != marks[1]:
            fills.append((_clip(Y(max(marks))), _clip(Y(min(marks))), acc))
        for ya_f, yb_f, col_f in fills:
            self.d.rectangle([px0, ya_f, px1 - 1, max(yb_f, ya_f)],
                             fill=_mix(t['bg'], col_f, range_alpha))

        # نوع چارت — حالت کندل: solid/hollow/border روی نوع candles اعمال می‌شود
        body_mode = 'solid'
        if chart_type == 'hollow':
            body_mode = 'hollow'
        elif chart_type == 'candles':
            body_mode = candle_mode if candle_mode in ('solid', 'hollow', 'border') else 'solid'
        candle_like = chart_type in ('candles', 'heikin', 'hollow', 'bars')
        if candle_like:
            for i, c in enumerate(candles):
                x = int(cx0 + i * slot)
                up = c['c'] >= c['o']
                col = t['up'] if up else t['down']
                yh, yl = int(Y(c['h'])), int(Y(c['l']))
                yo, yc = int(Y(c['o'])), int(Y(c['c']))
                self.d.line([(x, yh), (x, yl)], fill=col, width=1)  # فتیله
                top, bot = min(yo, yc), max(yo, yc)
                if chart_type == 'bars':
                    self.d.line([(x - bw // 2, yo), (x, yo)], fill=col)
                    self.d.line([(x, yc), (x + bw // 2 + 1, yc)], fill=col)
                elif body_mode == 'hollow' and up:
                    self.d.rectangle([x - bw // 2, top, x + bw // 2, bot], outline=col)
                elif body_mode == 'border':
                    if bot - top < 1:
                        bot = top + 1
                    self.d.rectangle([x - bw // 2, top, x + bw // 2, bot], fill=col, outline=t['dim'])
                else:
                    if bot - top < 1:
                        bot = top + 1
                    self.d.rectangle([x - bw // 2, top, x + bw // 2, bot], fill=col)
        elif chart_type == 'line':
            pts = [(int(cx0 + i * slot), Y(c)) for i, c in enumerate(closes)]
            self._polyline(pts, t['line'], 2)
        elif chart_type == 'area':
            pts = [(int(cx0 + i * slot), Y(c)) for i, c in enumerate(closes)]
            poly = [(px0, y_bot)] + pts + [(px1 - 1, y_bot)]
            base = t['line']
            for yy in range(y0, y_bot):  # گرادیان عمودی سبک
                k = (yy - y0) / max(1, ph)
                col = tuple(int(base[c2] * (0.38 - 0.30 * k) + t['bg'][c2] * (0.62 + 0.30 * k)) for c2 in range(3))
                self.d.line([(px0, yy), (px1 - 1, yy)], fill=col)
            self.d.polygon(poly, fill=None)
            self._polyline(pts, base, 2)

        # خط/بازهٔ قیمت هدف (رنگی) — اسکیل را تغییر نمی‌دهد، فقط در ناحیهٔ رسم بریده می‌شود
        # (فیل بازه بالاتر، پشت کندل‌ها کشیده شد — اینجا فقط خط‌چین و تگ)
        drawn_any = False
        # بازه‌های چندگانهٔ کاربر با رنگ اختصاصی (marksList)
        for m in (marks_list or []):
            mf = float(m.get('from') or 0)
            mt = float(m.get('to') or 0)
            if not mf and not mt:
                continue
            mc = hex_to_rgb(m.get('color'), None) or acc
            vals_m = sorted(v for v in (mf, mt) if v > 0)
            if len(vals_m) == 2 and vals_m[0] != vals_m[1]:
                ya, yb = _clip(Y(vals_m[-1])), _clip(Y(vals_m[0]))
                for yy in (ya, yb):
                    self._dashed_h(yy, px0, px1, mc, 3, 4)
            else:
                yy = _clip(Y(vals_m[0]))
                self._dashed_h(yy, px0, px1, mc, 4, 4)
                tag = fmt_price(vals_m[0])
                tw = int(self.d.textlength(tag, font=f_lab)) + 8
                if tw <= axw:
                    hh = max(16, ax_size + 6)
                    self.d.rectangle([x1 - axw + 2, yy - hh // 2, x1 - 2, yy + hh // 2], fill=mc)
                    self._text((x1 - axw + 6, yy), tag, ax_size,
                               (25, 25, 25) if self._theme_name == 'light' else (30, 30, 30), anchor='lm')
            drawn_any = True
        # بازهٔ کلاسیک markFrom/markTo — فقط اگر بازهٔ چندگانه‌ای رسم نشده باشد
        if not drawn_any and marks:
            if len(marks) == 2 and marks[0] != marks[1]:
                ya, yb = _clip(Y(max(marks))), _clip(Y(min(marks)))
                for yy in (ya, yb):
                    self._dashed_h(yy, px0, px1, acc, 3, 4)
            else:
                yy = _clip(Y(marks[0]))
                self._dashed_h(yy, px0, px1, acc, 4, 4)
                tag = fmt_price(marks[0])
                tw = int(self.d.textlength(tag, font=f_lab)) + 8
                if tw <= axw:
                    hh = max(16, ax_size + 6)
                    self.d.rectangle([x1 - axw + 2, yy - hh // 2, x1 - 2, yy + hh // 2], fill=acc)
                    self._text((x1 - axw + 6, yy), tag, ax_size,
                               (25, 25, 25) if self._theme_name == 'light' else (30, 30, 30), anchor='lm')

        # خطوط رسم‌شدهٔ کاربر (trendLines) — مختصات درصدی ۰..۱۰۰ از ناحیهٔ رسم
        # لنگر عمودی قیمت (py): اگر وب قیمت واقعی هر نقطه را فرستاده باشد و
        # هم‌طول pts باشد، Y همان قیمت کشیده می‌شود — جای خط دیگر به اسکیل
        # لحظهٔ رندر وابسته نیست؛ وگرنه درصد قدیمی (fallback دادهٔ قدیمی)
        for ln in (trend_lines or []):
            lcol = hex_to_rgb(ln.get('color'), None) or t['line']
            py = ln.get('py') or []
            use_py = isinstance(py, (list, tuple)) and len(py) == len(ln.get('pts') or [])
            pts = []
            for ti, pt in enumerate(ln.get('pts') or []):
                try:
                    fx = float(pt[0]) / 100.0
                    fy = float(pt[1]) / 100.0
                except (TypeError, ValueError, IndexError):
                    continue
                fx = max(0.0, min(1.0, fx))
                fy = max(0.0, min(1.0, fy))
                yy = None
                if use_py:
                    try:
                        pv = float(py[ti])
                    except (TypeError, ValueError):
                        pv = 0.0
                    if pv > 0:
                        yy = int(max(y0, min(Y(pv), y_bot)))
                if yy is None:
                    yy = int(y0 + fy * (y_bot - y0))
                pts.append((int(px0 + fx * (px1 - 1 - px0)), yy))
            if len(pts) >= 2:
                self._polyline(pts, lcol, 2)
                if len(pts) == 2:
                    self.d.ellipse([pts[0][0] - 2, pts[0][1] - 2, pts[0][0] + 2, pts[0][1] + 2], fill=lcol)
                    self.d.ellipse([pts[1][0] - 2, pts[1][1] - 2, pts[1][0] + 2, pts[1][1] + 2], fill=lcol)

        # اندیکاتورهای رویِ چارت — سه Moving Average با رنگ‌های متمایز
        for mid, ckey, lnw in MA_PLOTS:
            if ind_on.get(mid):
                pts = [(int(cx0 + i * slot), Y(v)) for i, v in enumerate(ser[mid]) if v is not None]
                self._polyline(pts, t[ckey], lnw)

        # خط قیمت لحظه‌ای + تگ  (همیشه رسم می‌شود؛ قیمت هرگز از چارت گم نمی‌شود)
        if last_price and last_price > 0:
            ly = int(max(y0 + 9, min(Y(last_price), y_bot - 9)))
            col = t['up'] if last_price - (prev_close or last_price) >= 0 else t['down']
            self._dashed_h(ly, px0, px1, col, 2, 5)
            tag = fmt_price(last_price)
            ts = 11
            ft = self.fonts.get(ts, True)
            while ts > 7 and self.d.textlength(tag, font=ft) > axw - 12:
                ts -= 1
                ft = self.fonts.get(ts, True)
            tcol = (255, 255, 255) if self._theme_name != 'light' else (255, 255, 255)
            self.d.rectangle([x1 - axw + 1, ly - 8, x1 - 2, ly + 8], fill=col)
            self._text((x1 - axw + 5, ly), tag, ts, tcol, True, 'lm')
            self._last_price_y = ly

        # نوار تاریخ محور X — شمسی یا میلادی (از کانفیگ xAxisCal)
        if date_h and candles:
            span = candles[-1]['t'] - candles[0]['t']
            step = (candles[1]['t'] - candles[0]['t']) if len(candles) > 1 else 0
            # تایم‌فریم ساعتی و بالاتر (یا بازه‌ی ≥۱ روز) → تاریخ؛ ریزتر → ساعت
            show_date = span >= 86400 or step >= 3600
            n = max(2, min(6, int((px1 - px0) // 95)))
            self.d.rectangle([px0, y_bot, px1 - 1, y1 - 1], fill=t['bg'])
            self.d.line([(px0, y_bot), (px1 - 1, y_bot)], fill=t['sep'])
            fs = getattr(self, '_fs', {}) or {}
            f_ax = self.fonts.get(max(7, int(9 * scale(None, fs.get('date', 100)))))
            for k in range(n):
                idx = min(len(candles) - 1, int((k + 0.5) / n * len(candles)))
                tt = int(candles[idx]['t'])
                if show_date:
                    if x_cal == 'gregorian':
                        g = time.gmtime(tt)
                        lab = f'{g.tm_year}/{g.tm_mon:02d}/{g.tm_mday:02d}'
                    else:
                        jy, jm, jd = epoch_to_jalali(tt)
                        lab = f'{jy}/{jm:02d}/{jd:02d}'
                else:
                    lab = time.strftime('%H:%M', time.localtime(tt))
                lx = int(px0 + (px1 - px0) * (k + 0.5) / n)
                self.d.text((lx, y_bot + date_h / 2), lab, font=f_ax,
                            fill=t['grid_txt'], anchor='mm')

        self.d.rectangle([x0, y0, x1 - 1, y1 - 1], outline=t['sep'])
        self.d.line([(px1, y0), (px1, y1 - 1)], fill=t['sep'])
        self._mark(rect)

    # ---------- پنل ولوم ----------
    def _draw_volume(self, rect, candles):
        t = self.theme
        x0, y0, x1, y1 = rect
        axw = self._axis_w
        px1 = x1 - axw
        self.d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=t['panel'])
        if not candles:
            self._mark(rect); return
        mx = max(c['v'] for c in candles) or 1.0
        n = len(candles)
        slot = (px1 - x0) / n
        bw = max(1, min(int(slot * 0.62), 22))
        for i, c in enumerate(candles):
            x = int(x0 + slot / 2 + i * slot)
            h = int(c['v'] / mx * (y1 - y0 - 4))
            if h <= 0:
                continue  # نماد بدون حجم (بازار ایران) — میله رسم نمی‌شود
            col = t['up_dim'] if c['c'] >= c['o'] else t['down_dim']
            self.d.rectangle([x - bw // 2, y1 - h, x + bw // 2, y1 - 1], fill=col)
        self._text((x0 + 4, y0 + 2), 'VOL', 9, t['grid_txt'])
        self.d.line([(x0, y0), (x1 - 1, y0)], fill=t['sep'])
        self.d.line([(px1, y0), (px1, y1 - 1)], fill=t['sep'])
        self._mark(rect)

    # ---------- پنل RSI ----------
    def _draw_rsi(self, rect, candles, params=None):
        t = self.theme
        x0, y0, x1, y1 = rect
        axw = self._axis_w
        px1 = x1 - axw
        self.d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=t['panel'])
        ph = y1 - y0

        def Y(v):
            return y1 - v / 100.0 * (ph - 4) - 2

        def mix(c1, c2, k):
            return tuple(int(c1[j] * (1 - k) + c2[j] * k) for j in range(3))

        rp = max(2, int((params or {}).get('period') or 14))
        vals = rsi_series([c['c'] for c in candles], rp)
        # سایهٔ ملایم نواحی اشباع خرید/فروش
        self.d.rectangle([x0, y0, px1 - 1, int(Y(70))], fill=mix(t['panel'], t['down'], 0.10))
        self.d.rectangle([x0, int(Y(30)), px1 - 1, y1 - 1], fill=mix(t['panel'], t['up'], 0.10))
        for lv in (30, 50, 70):
            self._dashed_h(int(Y(lv)), x0, px1, t['grid'] if lv == 50 else t['sep'])
        pts = [(int(x0 + (px1 - x0) * (i + 0.5) / len(vals)), Y(v)) for i, v in enumerate(vals) if v is not None]
        if pts:
            self._polyline(pts, (170, 120, 255), 2)
            last = [v for v in vals if v is not None]
            if last:
                self._text((x1 - axw + 6, (y0 + y1) // 2), f'{last[-1]:.1f}', 10, t['grid_txt'], anchor='lm')
        self._text((x0 + 4, y0 + 2), f'RSI {rp}', 9, t['grid_txt'])
        self.d.line([(x0, y0), (x1 - 1, y0)], fill=t['sep'])
        self.d.line([(px1, y0), (px1, y1 - 1)], fill=t['sep'])
        self._mark(rect)

    # ---------- پنل MACD ----------
    def _draw_macd(self, rect, candles, params=None):
        t = self.theme
        x0, y0, x1, y1 = rect
        axw = self._axis_w
        px1 = x1 - axw
        self.d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=t['panel'])
        p = params or {}
        fast = max(2, int(p.get('fast') or 12))
        slow = max(fast + 1, int(p.get('slow') or 26))
        sig = max(2, int(p.get('signal') or 9))
        line, signal = macd_series([c['c'] for c in candles], fast, slow, sig)
        vals = [v for v in line + signal if v is not None]
        if not vals:
            self._mark(rect); return
        hi, lo = max(vals), min(vals)
        rng = (hi - lo) or 1e-9
        mid = y0 + int((hi / rng) * (y1 - y0))
        n = len(candles)
        slot = (px1 - x0) / n
        bw = max(1, min(int(slot * 0.5), 14))
        for i in range(n):
            if line[i] is None:
                continue
            x = int(x0 + slot / 2 + i * slot)
            yy = y0 + int(((hi - line[i]) / rng) * (y1 - y0))
            col = t['up_dim'] if line[i] >= 0 else t['down_dim']
            self.d.rectangle([x - bw // 2, min(yy, mid), x + bw // 2, max(yy, mid)], fill=col)
        for series, col, w in ((line, t['line'], 1), (signal, t['ema'], 1)):
            pts = [(int(x0 + slot / 2 + i * slot), y0 + int(((hi - v) / rng) * (y1 - y0)))
                   for i, v in enumerate(series) if v is not None]
            self._polyline(pts, col, w)
        self.d.line([(x0, mid), (px1, mid)], fill=t['grid'])
        self._text((x0 + 4, y0 + 2), f'MACD {fast}/{slow}/{sig}', 9, t['grid_txt'])
        self.d.line([(x0, y0), (x1 - 1, y0)], fill=t['sep'])
        self.d.line([(px1, y0), (px1, y1 - 1)], fill=t['sep'])
        self._mark(rect)

    # ---------- کارت قیمت لحظه‌ای (بازار ایران) ----------
    def _draw_quote_card(self, rect, cfg, price, chg, price_info):
        """نمایش بزرگ قیمت برای نمادهای بدون کندل تاریخی (طلا، سکه، ارز)."""
        t = self.theme
        x0, y0, x1, y1 = rect
        self.d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=t['bg'])
        cx = (x0 + x1) // 2

        col = t['up'] if chg >= 0 else t['down']

        # نماد
        self._text((cx, y0 + int((y1 - y0) * 0.12)),
                   cfg.get('displayName') or cfg.get('symbol', ''), 20, t['text'], True, 'mm')

        # قیمت با فونت خیلی بزرگ (قابل تنظیم با priceFont)
        ps = fmt_price(price)
        fsize = int((44 if self.W >= 800 else 30) * self._price_scale)
        while fsize > 14 and self.d.textlength(ps, font=self.fonts.get(fsize, True)) > (x1 - x0 - 16):
            fsize -= 2
        self._text((cx, int((y0 + y1) * 0.42)), ps, fsize, col, True, 'mm')

        # واحد پول (اگر باشد)
        unit = price_info.get('unit', '')
        if unit:
            self._text((cx, int((y0 + y1) * 0.55)), unit, 14, t['dim'], anchor='mm')

        # تغییر
        ch_val = price_info.get('change_value', 0)
        line = f'{chg:+.2f}%'
        if ch_val:
            line += f'   ({ch_val:+,.0f})'
        self._text((cx, int((y0 + y1) * 0.66)), line, 17, col, True, 'mm')

        # پیکان جهت
        arrow_y = int((y0 + y1) * 0.80)
        if chg >= 0:
            self.d.polygon([(cx, arrow_y - 16), (cx - 14, arrow_y + 8), (cx + 14, arrow_y + 8)], fill=t['up'])
        else:
            self.d.polygon([(cx, arrow_y + 16), (cx - 14, arrow_y - 8), (cx + 14, arrow_y - 8)], fill=t['down'])

        self._mark(rect)

    # ---------- قیمت لحظه‌ای گوشهٔ بالا-راستِ خودِ چارت ----------
    def _draw_price_corner(self, rect, price, chg, style='corner'):
        """
        قیمت به‌صورت یک عدد در گوشهٔ بالا-راستِ ناحیهٔ چارت (روی خود چارت).
        style: chart (پیش‌فرض: خیلی درشت مثل هدر، روی چارت) | corner | badge | callout
        """
        t = self.theme
        x0, y0, x1, _y1 = rect
        axw = self._axis_w
        px1 = x1 - axw
        col = t['up'] if chg >= 0 else t['down']
        pad = 6
        rx = px1 - pad
        ps = fmt_price(price)
        chs = fmt_change(chg)
        scale = self._price_scale

        # اندازهٔ فونت: تا جا شود در نوار بالای چارت
        band = max(18, int((_y1 - y0) * 0.17))
        if style == 'chart':
            # همان اندازهٔ درشت هدر، ولی روی خودِ چارت (حالت پیش‌فرض)
            size = max(16, min(int(band * 1.15 * scale), int(52 * scale)))
        else:
            size = max(14, min(int(band * 0.72 * scale), int(46 * scale)))
        while size > 12 and self.d.textlength(ps, font=self.fonts.get(size, True)) > (px1 - x0) * 0.70:
            size -= 2
        f = self.fonts.get(size, True)
        w = self.d.textlength(ps, font=f)
        csize = max(9, int(size * 0.46))
        fc = self.fonts.get(csize, True)
        cw = self.d.textlength(chs, font=fc)

        if style == 'badge':
            bw = int(w + cw + 22)
            bh = int(size * 0.82) + 10
            self.d.rectangle([rx - bw, y0 + pad, rx, y0 + pad + bh], fill=col)
            self._text((rx - 7, y0 + pad + bh / 2), ps, size,
                       (0, 0, 0) if chg >= 0 else (255, 255, 255), True, 'rm')
            self._text((rx - 10 - w, y0 + pad + bh / 2), chs, csize,
                       (0, 0, 0) if chg >= 0 else (255, 255, 255), True, 'rm')
        elif style == 'chart':
            # مثل هدر: عدد خیلی درشت، درصد تغییر در سمت چپِ عدد
            self._text((rx, y0 + pad + size * 0.62), ps, size, col, True, 'rm')
            self._text((rx - w - 12, y0 + pad + size * 0.62), chs, csize, col, True, 'rm')
        elif style == 'callout':
            self._text((rx, y0 + pad + size * 0.5), ps, size, col, True, 'rm')
            self._text((rx - w - 10, y0 + pad + size * 0.5), chs, csize, col, True, 'rm')
            ly = self._last_price_y
            if ly is not None and ly > y0 + pad + size + 6:
                self.d.line([(rx - w, y0 + pad + size * 0.6), (rx, ly)], fill=col, width=1)
        else:   # corner
            self._text((rx, y0 + pad + size * 0.55), ps, size, col, True, 'rm')
            self._text((rx - w - 10, y0 + pad + size * 0.55), chs, csize, col, True, 'rm')

        self._mark((int(rx - w - cw - 30), y0, int(rx + 2), y0 + pad + int(size * 1.5)))

    # ---------- بنر آلارم (قیمت داخل بازهٔ هدف) ----------
    def _draw_alert(self, rect, price, lo_m, hi_m):
        """بنر چشمکی قرمز↔سبز بالای پنل قیمت — وضعیت آلارم را از هر فاصله‌ای می‌گوید."""
        x0, y0, x1, _y1 = rect
        h = 17
        ph = alert_phase(self._blink)
        bg, fg = ph['banner'], ph['banner_fg']
        self.d.rectangle([x0, y0, x1 - 1, y0 + h - 1], fill=bg, outline=fg)
        msg = f'ALERT  {fmt_price(lo_m)} — {fmt_price(hi_m)}'
        ts = 12
        while ts > 8 and self.d.textlength(msg, font=self.fonts.get(ts, True)) > (x1 - x0 - 20):
            ts -= 1
        self._text(((x0 + x1) // 2, y0 + h // 2), msg, ts, fg, True, 'mm')
        self._mark((x0, y0, x1, y0 + h))

    # ---------- فوتر ----------
    def _draw_footer(self, y0, h, cfg, n, server_ok, ts):
        t = self.theme
        self.d.rectangle([0, y0, self.W, y0 + h], fill=t['panel'])
        mode = {'spot': 'SPOT', 'futures': 'FUTURES', 'margin': 'MARGIN'}.get(cfg.get('feedMode'), '')
        left = f'{n} candles · {mode}'
        self._text((6, y0 + h // 2), left, 10, t['dim'], anchor='lm')
        clock = time_stamp(ts)
        rt = f'LIVE {clock}' if server_ok else f'CACHE {clock}'
        w = self.d.textlength(rt, font=self.fonts.get(10))
        self._text((self.W - 6 - w, y0 + h // 2), rt, 10, t['up'] if server_ok else t['down'], anchor='lm')

    # ---------- رندر چندچارت ----------
    def _draw_multi(self, y0, h, cfg, panes):
        """چیدمان دوتایی (افقی) یا چهارتایی (۲×۲) از مینی‌چارت‌ها."""
        layout = cfg.get('layout', 'single')
        gap = 3
        W = self.W
        # پاک‌سازی کامل ناحیهٔ چندچارت — وگرنه آثار فریم قبلی (مثلاً نمودار
        # تکیِ قبل از تعویض حالت، یا ناحیهٔ حاشیهٔ پنل‌های قبلی) روی LCD به‌عنوان
        # بک‌گراند باقی می‌ماند؛ چون فلاش فقط ناحیهٔ dirty را به‌روز می‌کند.
        self.d.rectangle([0, y0, W - 1, y0 + h - 1], fill=self.theme['bg'])
        mc = MiniChart(self)

        if layout == 'dual':
            cells = [
                (0, y0, W // 2 - gap, y0 + h),
                (W // 2 + gap, y0, W, y0 + h),
            ]
        else:  # quad
            half_h = (h - gap) // 2
            cells = [
                (0, y0, W // 2 - gap, y0 + half_h),
                (W // 2 + gap, y0, W, y0 + half_h),
                (0, y0 + half_h + gap, W // 2 - gap, y0 + h),
                (W // 2 + gap, y0 + half_h + gap, W, y0 + h),
            ]

        fs = getattr(self, '_fs', {}) or {}
        ipn = norm_iparams(cfg.get('indParams'))   # دوره‌های سه MA برای هر پنل
        for idx, cell in enumerate(cells):
            if idx >= len(panes):
                break
            p = panes[idx]
            key = p['symbol'] + ':' + p['interval']
            feed = (self._pane_feeds or {}).get(key) or {}
            mf = float(feed.get('markFrom') or p.get('markFrom') or 0)
            mt = float(feed.get('markTo') or p.get('markTo') or 0)
            pv = float((feed.get('price') or {}).get('price') or 0.0)
            # آلارم مستقل هر پنل: قیمت خودش در بازهٔ خودش
            mvals = sorted(v for v in (mf, mt) if v > 0)
            p_alert = bool(mvals and pv > 0 and mvals[0] <= pv <= mvals[-1]
                           and cfg.get('alertRange', True))
            mc.draw(cell, feed.get('candles') or [], p['symbol'], p['interval'],
                    p.get('chartType', 'candles'),
                    pv,
                    (feed.get('price') or {}).get('changePct', 0.0),
                    bool(feed.get('stale')), alert=p_alert,
                    mark_from=mf, mark_to=mt,
                    mark_color=feed.get('markColor') or '',
                    marks_list=feed.get('marksList') or [],
                    trend_lines=trend_lines_for(feed, p['symbol']),
                    pane_ind=_norm_pane_ind(feed.get('paneIndicators')),
                    ind_params=ipn,
                    fonts=fs,
                    price_scale=getattr(self, '_price_scale', 1.0),
                    x_cal=cfg.get('xAxisCal') or 'jalali')

    # ---------- رندر کامل ----------
    def render(self, candles, cfg, price_info, server_ok=True, ts=None, panes=None):
        import time as _t
        ts = ts or int(_t.time())

        self._pane_feeds = panes or {}
        layout = cfg.get('layout', 'single')

        # ضریب فونت قیمت — قبل از _layout تا هدر هم متناسب بلند شود
        self._price_scale = max(0.6, min(3.0, (cfg.get('priceFont') or 100) / 100.0))
        # اندازهٔ نسبی فونت‌های تاریخ/نام/تایم‌فریم/محور/تیتر (٪)
        fsc = cfg.get('fontScales') or {}
        self._fs = {
            'date': max(60, min(250, int(fsc.get('date') or 100))),
            'symbol': max(60, min(250, int(fsc.get('symbol') or 100))),
            'timeframe': max(60, min(250, int(fsc.get('timeframe') or 100))),
            'axis': max(60, min(250, int(fsc.get('axis') or 100))),
            'title': max(60, min(250, int(fsc.get('title') or 100))),
            'pane': max(60, min(250, int(fsc.get('pane') or 100))),
        }

        price = float(price_info.get('price') or 0.0)
        chg = float(price_info.get('changePct') or 0.0)

        # ---- آلارم: قیمت لحظه‌ای داخل بازهٔ هدف ----
        mvals = sorted(v for v in (float(cfg.get('markFrom') or 0),
                                   float(cfg.get('markTo') or 0)) if v > 0)
        alert = bool(mvals and price > 0
                     and mvals[0] <= price <= mvals[-1]
                     and cfg.get('alertRange', True))
        if layout != 'single':
            # چندچارت: تم/بنر سراسری آلارم ممنوع — فقط پنل همان نماد چشمک می‌زند
            alert = False
        self._blink = not self._blink
        if alert != self._alert_active:
            self._alert_active = alert
            if alert:                     # زنگ آلارم (در ژورنال systemd هم ثبت می‌شود)
                try:
                    sys.stdout.write('\a')
                    sys.stdout.flush()
                except Exception:
                    pass

        # هنگام آلارم: تم چشمکی قرمز ↔ سبز
        theme_name = (ALERT_THEME_NAMES[1 if self._blink else 0] if alert
                      else cfg.get('theme', 'dark'))
        t = THEMES.get(theme_name, THEMES['dark'])
        if theme_name != self._theme_name:
            self.theme = t
            self._theme_name = theme_name
            self.d.rectangle([0, 0, self.W, self.H], fill=t['bg'])

        # محور قیمت به‌اندازهٔ بلندترین برچسب (تا تگ قیمت جا شود)
        pw = self.d.textlength(fmt_price(price), font=self.fonts.get(11, True)) if price > 0 else 0
        self._axis_w = int(max(56, min(140, pw + 18)))

        ind = norm_ind(cfg.get('indicators'))
        ipn = norm_iparams(cfg.get('indParams'))
        ind['_vol'] = bool(cfg.get('showVolume', True)) and layout == 'single'
        # نمادهای بدون دادهٔ حجم (بازار ایران) → پنل ولوم مخفی شود
        if ind['_vol'] and candles and all(c.get('v', 0) == 0 for c in candles):
            ind['_vol'] = False
        L = self._layout(ind, cfg)

        y = 0
        if L['header'] > 0:
            self._draw_header(y, L['header'], cfg, price, chg, 0, server_ok)
            y += L['header'] + 2

        # بازار ایرانی (طلا/سکه/ارز): کندل تاریخی نداریم → کارت بزرگ قیمت لحظه‌ای
        if not candles and price > 0:
            self._draw_quote_card((0, y, self.W, self.H - L['footer']),
                                  cfg, price, chg, price_info)
            if alert:
                self._draw_alert((0, y, self.W, y + 17), price, mvals[0], mvals[-1])
            self._draw_footer(self.H - L['footer'], L['footer'], cfg, 0, server_ok, ts)
            rect = tuple(self._dirty) if self._dirty else (0, 0, self.W, self.H)
            self.sc.flush(self.img, rect)
            self._dirty = None
            self._pane_feeds = {}
            return

        if layout != 'single':
            # چندچارت: ناحیهٔ باقی‌مانده بین پنل‌ها تقسیم می‌شود
            bottom = self.H - L['footer']
            self._draw_multi(y, bottom - y, cfg, cfg.get('panes') or [])
        else:
            data = candles if cfg.get('chartType') != 'heikin' else heikin_ashi(candles)

            prev_close = candles[-2]['c'] if len(candles) >= 2 else None
            price_rect = (0, y, self.W, y + L['price'])
            sym_main = str(cfg.get('symbol') or '')
            _marks = cfg.get('marks')
            mrec = (_marks.get(sym_main) or {}) if isinstance(_marks, dict) else {}
            if not isinstance(mrec, dict):
                mrec = {}
            self._draw_price(price_rect, data, ind,
                             cfg.get('chartType', 'candles'), cfg.get('candleMode', 'solid'),
                             price, prev_close,
                             cfg.get('markFrom') or 0, cfg.get('markTo') or 0,
                             cfg.get('xAxisCal') or 'jalali',
                             ind_params=ipn,
                             marks_list=mrec.get('list') or [],
                             trend_lines=trend_lines_for(cfg, sym_main))
            # نماد + تایم‌فریم روی خود چارت (حالت تکی) — به‌جای هدر
            self._draw_chart_title(price_rect, cfg)
            if alert:
                self._draw_alert(price_rect, price, mvals[0], mvals[-1])
            if price > 0 and cfg.get('priceStyle', 'chart') != 'header':
                self._draw_price_corner(price_rect, price, chg,
                                        cfg.get('priceStyle', 'chart'))
            y += L['price']
            if L['vol']:
                self._draw_volume((0, y, self.W, y + L['vol']), data)
                y += L['vol']
            if L['rsi']:
                self._draw_rsi((0, y, self.W, y + L['rsi']), candles,
                               ipn.get('rsi'))
                y += L['rsi']
            if L['macd']:
                self._draw_macd((0, y, self.W, y + L['macd']), candles,
                                ipn.get('macd'))
                y += L['macd']

        self._draw_footer(self.H - L['footer'], L['footer'], cfg,
                          len(candles), server_ok, ts)

        rect = tuple(self._dirty) if self._dirty else (0, 0, self.W, self.H)
        self.sc.flush(self.img, rect)
        self._dirty = None
        self._pane_feeds = {}

    # ---------- صفحه‌های وضعیت ----------
    def boot_splash(self):
        t = THEMES['dark']
        self.theme = t
        self.d.rectangle([0, 0, self.W, self.H], fill=t['bg'])
        cx, cy = self.W // 2, self.H // 2
        self._text((cx, cy - 26), 'CryptoChart', 26, t['text'], True, 'mm')
        self._text((cx, cy + 6), 'Raspberry Pi Zero · Framebuffer Renderer', 12, t['dim'], anchor='mm')
        for i in range(24):
            self.d.rectangle([cx - 120 + i * 10, cy + 26, cx - 114 + i * 10, cy + 32],
                             fill=t['accent'] if i % 3 == 0 else t['grid'])
        self.sc.flush(self.img)
        self._dirty = None

    def offline(self, retry_in=10):
        t = THEMES['dark']
        self.theme = t
        self._theme_name = ''
        self.d.rectangle([0, 0, self.W, self.H], fill=t['bg'])
        cx, cy = self.W // 2, self.H // 2
        self.d.ellipse([cx - 14, cy - 58, cx + 14, cy - 30], outline=t['down'], width=2)
        self.d.line([(cx - 6, cy - 50), (cx - 1, cy - 40)], fill=t['down'], width=2)
        self.d.ellipse([cx - 2, cy - 38, cx + 2, cy - 34], fill=t['down'])
        self._text((cx, cy - 6), 'OFFLINE', 22, t['down'], True, 'mm')
        self._text((cx, cy + 22), f'Retrying in {retry_in}s ...', 12, t['dim'], anchor='mm')
        self.sc.flush(self.img)
        self._dirty = None

    def config_error(self, code, hint, url='', retry_in=10):
        """
        خطای پیکربندی (آدرس اشتباه/دسترسی رد شد) — آدرس را روی صفحه نشان می‌دهد
        تا بدون SSH هم علت دیده شود.
        """
        t = THEMES['dark']
        self.theme = t
        self._theme_name = ''
        self.d.rectangle([0, 0, self.W, self.H], fill=t['bg'])
        cx, cy = self.W // 2, self.H // 2
        bw = min(self.W - 16, 300)
        self.d.rectangle([cx - bw // 2, cy - 46, cx + bw // 2, cy + 46],
                         outline=t['down'], width=2)
        self._text((cx, cy - 30), f'HTTP {code}', 20, t['down'], True, 'mm')
        size = 10
        while size > 6 and self.d.textlength(hint, font=self.fonts.get(size)) > bw - 16:
            size -= 1
        self._text((cx, cy - 8), hint[:70], size, t['text'], anchor='mm')
        # آدرس مورد انتظار — مهم‌ترین اطلاعات این صفحه
        us = 11
        shown = url
        while us > 6 and self.d.textlength(shown, font=self.fonts.get(us, True)) > bw - 12:
            us -= 1
            shown = shown[:-4] + '…' if len(shown) > 4 else shown
        self._text((cx, cy + 12), shown, us, t['accent'], True, 'mm')
        self._text((cx, cy + 34), f'Retrying in {retry_in}s ...', 9, t['dim'], anchor='mm')
        self.sc.flush(self.img)
        self._dirty = None

    def error(self, msg, retry_in=10):
        """خطای منطقی/رندر (نه شبکه) — عمداً متفاوت از OFFLINE."""
        t = THEMES['dark']
        self.theme = t
        self._theme_name = ''
        self.d.rectangle([0, 0, self.W, self.H], fill=t['bg'])
        cx, cy = self.W // 2, self.H // 2
        self.d.rectangle([cx - 90, cy - 40, cx + 90, cy + 40], outline=t['accent'], width=2)
        self._text((cx, cy - 20), 'ERROR', 20, t['accent'], True, 'mm')
        size = 10
        while size > 6 and self.d.textlength(msg, font=self.fonts.get(size)) > self.W - 30:
            size -= 1
        self._text((cx, cy + 4), msg[:60], size, t['text'], anchor='mm')
        self._text((cx, cy + 24), f'Retrying in {retry_in}s ...', 10, t['dim'], anchor='mm')
        self.sc.flush(self.img)
        self._dirty = None


def time_stamp(ts):
    return time.strftime('%H:%M:%S', time.localtime(ts))
