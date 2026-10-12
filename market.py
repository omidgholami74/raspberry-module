#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketFeed — تمام دادهٔ بازار مستقیماً روی خود ماژول
------------------------------------------------------
* ارز دیجیتال  → نوبیتکس (udf/history) + قیمت لحظه‌ای از همان UDF
* بازار ایران   → BrsApi (اسنپ‌شات Gold_Currency.php + Pro Gold_Currency_Pro.php)
* سرور فقط کانفیگ می‌دهد (و کلید BrsApi را داخل کانفیگ می‌فرستد)؛ هیچ دادهٔ بازاری
  از سرور خوانده نمی‌شود.
* همهٔ متدها در صورت خطا/قطعی، None برمی‌گردانند → فراخواننده تصمیم می‌گیرد.

قرارداد خروجی feed():  (candles|None, source)
    None            → هیچ منبعی جواب نداد
    []              → نماد بدون تاریخچه (کارت قیمت)
    [ ... ]         → کندل‌ها
"""

import json
import time
import urllib.parse
import urllib.request

# ------------------------- منابع -------------------------
NOB_URL = 'https://apiv2.nobitex.ir/market/udf/history'
BRS_SNAP_URL = 'https://Api.BrsApi.ir/Market/Gold_Currency.php?key='
BRS_PRO_URL = 'https://api.brsapi.ir/Market/Gold_Currency_Pro.php'
BRS_DEFAULT_KEY = 'Bh3LYInFHUrNlvWHLikv2sTiUYGwajqe'

NOB_TIMEOUT = 10
BRS_TIMEOUT = 12
NOB_TTL = 30          # کش کندل کریپتو — تقریباً زنده
QUOTE_TTL = 45        # کش قیمت لحظه‌ای
SNAP_TTL = 60         # اسنپ‌شات بازار ایران
SNAP_MAXAGE = 3600    # در قطعی منبع، کش کهنه معتبر است
TICKS_TTL = 30        # تیک ۲۴h
DAILY_TTL = 21600     # کندل روزانه (۶ ساعت)
DAILY_DAYS = 2000     # عمق تاریخچهٔ روزانه (~۵.۵ سال)
RANGE_MAX = 3000      # سقف کندل خروجی (= بودجهٔ صفحه‌بندی نوبیتکس)
LIMIT_MAX = 2000      # بیشترین تعداد کندل درخواستی کاربر

INTERVALS = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600, '8h': 28800,
    '12h': 43200, '1d': 86400, '3d': 259200, '1w': 604800,
}

# باکت (ثانیه) → رزولوشن UDF نوبیتکس.
# فقط مقادیر مستندشده: 1,5,15,30,60,180,240,360,720,D,2D,3D — «W» پشتیبانی نمی‌شود
NOB_RESS = [(60, '1'), (300, '5'), (900, '15'), (1800, '30'), (3600, '60'),
            (10800, '180'), (14400, '240'), (21600, '360'), (43200, '720'),
            (86400, 'D'), (259200, '3D')]
NOB_PAGE_MAX = 500     # سقف کندل هر درخواست
NOB_MAX_PAGES = 6      # حداکثر ۶ صفحه در هر فید
NOB_RANGE_MAX = NOB_PAGE_MAX * NOB_MAX_PAGES   # بودجهٔ کندل برای بازهٔ تاریخی

FA_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


# ------------------------- تاریخ شمسی -------------------------
def tdiv(a: int, b: int) -> int:
    """تقسیم صحیح رو به صفر (هم‌قرارداد tdiv در PHP و jalaali-js)."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _jalali_to_jdn(jy: int, jm: int, jd: int) -> int:
    breaks = (-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210,
              1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178)
    gy = jy + 621
    leap_j = -14
    jp = breaks[0]
    jump = 0
    for i in range(1, len(breaks)):
        jm2 = breaks[i]
        jump = jm2 - jp
        if jy < jm2:
            break
        leap_j += tdiv(jump, 33) * 8 + tdiv(jump % 33, 4)
        jp = jm2
    n = jy - jp
    leap_j += tdiv(n, 33) * 8 + tdiv((n % 33) + 3, 4)
    if (jump % 33) == 4 and jump - n == 4:
        leap_j += 1
    leap_g = tdiv(gy, 4) - tdiv((tdiv(gy, 100) + 1) * 3, 4) - 150
    march = 20 + leap_j - leap_g
    q = gy + tdiv(3 - 8, 6) + 100100
    jdn = (tdiv(q * 1461, 4) + tdiv(153 * ((3 + 9) % 12) + 2, 5)
           + march - 34840408 - tdiv(tdiv(q, 100) * 3, 4) + 752)
    return jdn + (jm - 1) * 31 - tdiv(jm, 7) * (jm - 7) + jd - 1


JDN_1970 = 2440588


def jalali_to_epoch(d: str) -> int:
    """'۱۴۰۴/۰۱/۰۱' → unix epoch (ظهر UTC — دور از مرز روز)؛ نامعتبر → 0."""
    s = (d or '').strip().translate(FA_DIGITS).replace('-', '/')
    parts = s.split('/')
    if len(parts) != 3:
        return 0
    try:
        jy, jm, jd = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return 0
    if jm < 1 or jm > 12 or jd < 1 or jd > 31 or jy < 1200 or jy > 1700:
        return 0
    return (_jalali_to_jdn(jy, jm, jd) - JDN_1970) * 86400 + 43200


def _gregorian_to_jalali(gy: int, gm: int, gd: int):
    gdm = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
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


def epoch_to_jalali(ts: int) -> str:
    """unix epoch → 'Y/m/d' شمسی (هم‌پورت با بک‌اند و chart.py)."""
    days = tdiv(int(ts), 86400) + JDN_1970
    j = 4 * days + 139361631
    j += tdiv(tdiv(4 * days + 183187720, 146097) * 3, 4) * 4 - 3908
    i = tdiv(j % 1461, 4) * 5 + 308
    gd = tdiv(i % 153, 5) + 1
    gm = tdiv(i, 153) % 12 + 1
    gy = tdiv(j, 1461) - 100100 + tdiv(8 - gm, 6)
    jy, jm, jd = _gregorian_to_jalali(gy, gm, gd)
    return f'{jy:04d}/{jm:02d}/{jd:02d}'


# ------------------------- ابزار -------------------------
def _candidates(symbol: str):
    """نام بازارهای نوبیتکس: BTC → [BTCUSDT, BTCIRT]"""
    sym = symbol.upper()
    bases = [sym]
    for suf in ('USDT', 'FDUSD', 'BUSD', 'USDC', 'TUSD'):
        if sym.endswith(suf) and sym[:-len(suf)]:
            bases.append(sym[:-len(suf)])
    bases = list(dict.fromkeys(bases))
    out = [b + 'USDT' for b in bases if not b.endswith(('USDT', 'IRT'))]
    out += [b + 'IRT' for b in bases if not b.endswith('IRT')]
    return list(dict.fromkeys(out))


def _quote_only(symbol: str) -> bool:
    """نمادهای تومانی بدون تاریخچه (مثل USDT_IRT) → فقط کارت قیمت."""
    return symbol.upper().endswith('_IRT')


def _brs_symbol(s: str) -> str:
    """BTCUSDT → BTC (هم‌قرارداد brs_symbol در بک‌اند)."""
    for suf in ('USDT', 'FDUSD', 'BUSD', 'USDC', 'TUSD'):
        if s.endswith(suf) and s[:-len(suf)]:
            return s[:-len(suf)]
    return s


def aggregate(candles, bucket):
    """درهم‌ساختن کندل‌های ریزتر در باکت بزرگ‌تر (۳روزی/هفتگی از روزانه)."""
    out, b = [], None
    for c in candles:
        bt = int(c['t']) // bucket * bucket
        if b is None or bt != b['t']:
            if b is not None:
                out.append(b)
            b = dict(c, t=bt)
        else:
            b['h'] = max(b['h'], c['h'])
            b['l'] = min(b['l'], c['l'])
            b['c'] = c['c']
            b['v'] = b.get('v', 0) + c.get('v', 0)
    if b is not None:
        out.append(b)
    return out


class MarketFeed:
    """تمام دادهٔ بازار؛ همهٔ متدها در صورت خطا None (بدون استثنا) برمی‌گردانند."""

    def __init__(self, key=BRS_DEFAULT_KEY, timeout=NOB_TIMEOUT):
        self.key = key or BRS_DEFAULT_KEY
        self.timeout = timeout
        self.last_error = ''       # آخرین خطا — برای لاگ و نمایش روی ماژول
        self._cache = {}

    # ---------------- زیرساخت ----------------
    def set_key(self, key):
        if key and key != self.key:
            self.key = key
            self._cache.clear()

    def _http_json(self, url, timeout):
        req = urllib.request.Request(url, headers={
            # الگوی توصیه‌شدهٔ نوبیتکس برای شناسایی بات
            'User-Agent': 'TraderBot/cryptochart-pi-1.0',
            'Accept': 'application/json',
            'Connection': 'close',
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))

    def _get(self, key, ttl, fn):
        now = time.time()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        try:
            val = fn()
        except Exception as e:
            self.last_error = f'{type(e).__name__}: {e}'[:160]
            val = None
        if len(self._cache) > 512:      # پاک‌سازی کلیدهای کهنه
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        self._cache[key] = (now + ttl, val)
        return val

    # ---------------- BrsApi: بازار ایران ----------------
    def snapshot(self):
        """{symbol: item} از اسنپ‌شات قیمت؛ در قطعی، کش کهنه تا SNAP_MAXAGE."""
        def _fetch():
            j = self._http_json(BRS_SNAP_URL + urllib.parse.quote(self.key),
                                BRS_TIMEOUT)
            items = {}
            for sec in ('gold', 'currency', 'cryptocurrency'):
                for row in (j.get(sec) or []):
                    sym = str(row.get('symbol') or '')
                    if not sym or row.get('price') is None:
                        continue
                    items[sym] = {
                        'symbol': sym, 'section': sec,
                        'name': str(row.get('name') or sym),
                        'price': float(row.get('price') or 0),
                        'change_value': float(row.get('change_value') or 0),
                        'changePct': float(row.get('change_percent') or 0),
                        'unit': str(row.get('unit') or ''),
                        'time_unix': int(row.get('time_unix') or 0),
                    }
            return items or None

        return self._get('snap', SNAP_TTL, _fetch) or {}

    def _pro(self, query):
        q = dict(query)
        q['key'] = self.key
        return self._http_json(BRS_PRO_URL + '?' + urllib.parse.urlencode(q),
                               BRS_TIMEOUT)

    def brs_daily(self, sym):
        """کندل روزانهٔ OHLC چندساله (history=2) — کش ۶ ساعته."""
        def _fetch():
            start = epoch_to_jalali(int(time.time()) - DAILY_DAYS * 86400)
            j = self._pro({'history': '2', 'symbol': sym, 'date_start': start})
            out = []
            for r in (j.get('history_daily') or []):
                t = jalali_to_epoch(str(r.get('date') or ''))
                c = float(r.get('close') or 0)
                if not t or c <= 0:
                    continue
                h, l = float(r.get('high') or 0), float(r.get('low') or 0)
                if h < l:
                    h, l = l, h
                out.append({'t': t, 'o': float(r.get('open') or 0),
                            'h': h, 'l': l, 'c': c,
                            'v': float(r.get('volume') or 0)})
            out.sort(key=lambda c: c['t'])
            return out or None

        return self._get(f'daily|{sym}', DAILY_TTL, _fetch)

    def brs_ticks(self, sym):
        """تیک دقیقه‌ای ۲۴h (history=1) — کش ۳۰ ثانیه."""
        def _fetch():
            j = self._pro({'history': '1', 'symbol': sym})
            rows = sorted((j.get('history_24h') or []),
                          key=lambda r: int(r.get('time_unix') or 0))
            return rows or None

        return self._get(f'ticks|{sym}', TICKS_TTL, _fetch)

    @staticmethod
    def _ticks_to_candles(rows, bucket):
        candles, b = [], None
        for r in rows:
            t = int(r.get('time_unix') or 0)
            p = float(r.get('price') or 0)
            if not t or p <= 0:
                continue
            bt = t // bucket * bucket
            if b is None or bt != b['t']:
                if b is not None:
                    candles.append(b)
                b = {'t': bt, 'o': p, 'h': p, 'l': p, 'c': p, 'v': 0.0}
            else:
                b['h'] = max(b['h'], p)
                b['l'] = min(b['l'], p)
                b['c'] = p
        if b is not None:
            candles.append(b)
        return candles

    # ---------------- Nobitex: ارز دیجیتال ----------------
    def _udf(self, market, res, frm, to, countback=0, page=0):
        """
        یک درخواست UDF → dict آرایه‌های t/o/h/l/c/v؛ خطا/بی‌داده → None.
        page>0 صفحهٔ کندل‌های قدیمی‌تر را می‌گیرد (تایم‌فریم عمیق).
        """
        q = {'symbol': market, 'resolution': res, 'to': int(to)}
        if page > 0:
            q['page'] = page
        elif countback:
            q['countback'] = int(countback)
        else:
            q['from'] = int(frm)
        url = NOB_URL + '?' + urllib.parse.urlencode(q)
        j = self._http_json(url, self.timeout)
        if not isinstance(j, dict):
            return None
        if j.get('s') == 'no_data':
            return {'t': [], 'o': [], 'h': [], 'l': [], 'c': [], 'v': []}
        if j.get('s') != 'ok':
            return None
        return {k: list(j.get(k) or []) for k in ('t', 'o', 'h', 'l', 'c', 'v')}

    @staticmethod
    def _resolve(bucket):
        """نردبان رزولوشن: دقیق، وگرنه نزدیک‌ترین بزرگ‌ترِ پشتیبانی‌شده."""
        for s, r in NOB_RESS:
            if s >= bucket:
                return r, s
        return NOB_RESS[-1][1], NOB_RESS[-1][0]

    def _nobitex_candles(self, symbol, bucket, limit, from_ts, to_ts):
        """
        کندل نوبیتکس با عمق دلخواه:
          * بی‌بازه → یک درخواست با countback=limit (تازه‌ترین N کندل)
          * بازهٔ تاریخی → رزولوشن متناسب + صفحه‌بندی (page) برای کندل‌های قدیمی‌تر
        """
        to = int(to_ts or time.time())

        # 1W پشتیبانی UDF ندارد → از روزانهٔ نوبیتکس می‌سازیم (aggregate).
        # مه: باکت هفتگی با floor(روز/604800) کلید می‌خورد؛ با دستکاری می‌یابیم:
        # بازهٔ درخواستی ۲ روز بزرگ‌تر (چون باکت هفتگی از وسط هفته شروع می‌شود)
        # تا aggregate() همان باکت‌های سوییچ (بدون open گم‌شده) را بسازد (مثل بک‌اند).
        if bucket == 604800:
            res, res_sec = 'D', 86400
            if from_ts:
                from_ts = (int(from_ts) // 604800) * 604800
            else:
                from_ts = (to // 604800 - max(1, limit) - 1) * 604800

        if bucket != 604800:
            res, res_sec = self._resolve(bucket)

        if from_ts:
            # نردبان رزولوشن بر اساس طول بازه: تا جایی که در بودجهٔ صفحه جا شود
            span = max(1, to - int(from_ts))
            if bucket != 604800:
                # نردبان رزولوشن فقط برای تایم‌فریم‌های عادی؛ 1W همیشه از روزانه می‌آید
                idx = [s for s, _ in NOB_RESS].index(res_sec)
                while span // res_sec > NOB_RANGE_MAX and idx + 1 < len(NOB_RESS):
                    idx += 1
                    res_sec = NOB_RESS[idx][0]
                    res = NOB_RESS[idx][1]
            need = max(1, int(span // res_sec))
            pages = min(NOB_MAX_PAGES, max(1, -(-need // NOB_PAGE_MAX)))
            frm = int(from_ts)
        else:
            # بدون بازه: تا جای ممکن رزولوشن انتخابی حفظ می‌شود و فقط وقتی
            # limit از بودجهٔ صفحه‌بندی بگذرد، تایم‌فریم درشت‌تر می‌شود.
            # نتیجه: هرچه تعداد کندل بیشتر، بازهٔ زمانی نمایش‌داده‌شده بیشتر.
            need_pages = max(1, -(-limit // NOB_PAGE_MAX))
            if need_pages > NOB_MAX_PAGES:
                budget = NOB_PAGE_MAX * NOB_MAX_PAGES
                for s, r in NOB_RESS:
                    if s >= res_sec and limit * s <= budget:
                        res, res_sec = r, s
                        break
                need_pages = max(1, -(-limit // NOB_PAGE_MAX))
            pages = min(NOB_MAX_PAGES, need_pages)
            frm = max(0, to - NOB_PAGE_MAX * res_sec * pages)

        for mkt in _candidates(symbol):
            collected = {}
            for page in range(1, pages + 1):
                cb = 0 if page > 1 or from_ts else limit
                u = self._get(f'nob|{mkt}|{res}|{frm}|{to}|{cb}|{page}', NOB_TTL,
                              lambda _m=mkt, _p=page, _cb=cb:
                              self._udf(_m, res, frm, to, _cb, page))
                if not u or not u['t']:
                    if page == 1:
                        break          # این بازار اصلاً داده ندارد → بازار بعدی
                    break                  # صفحهٔ بعدی داده ندارد → همان‌جا تمام
                for i, t in enumerate(u['t']):
                    t = int(t)
                    c = float(u['c'][i] or 0) if i < len(u['c']) else 0.0
                    if not t or c <= 0 or t in collected:
                        continue
                    collected[t] = {
                        't': t,
                        'o': float(u['o'][i]) if i < len(u['o']) and u['o'][i] else c,
                        'h': float(u['h'][i]) if i < len(u['h']) and u['h'][i] else c,
                        'l': float(u['l'][i]) if i < len(u['l']) and u['l'][i] else c,
                        'c': c,
                        'v': float(u['v'][i]) if i < len(u['v']) and u['v'][i] else 0.0,
                    }
                if len(u['t']) < 100:            # صفحهٔ آخر
                    break
            if not collected:
                continue
            candles = sorted(collected.values(), key=lambda x: x['t'])
            if res_sec < bucket:
                candles = aggregate(candles, bucket)
            if from_ts or to_ts:
                candles = [c for c in candles
                           if (not from_ts or c['t'] >= from_ts)
                           and (not to_ts or c['t'] <= to_ts)]
            elif len(candles) > limit:
                candles = candles[-limit:]
            if len(candles) > RANGE_MAX:
                candles = candles[-RANGE_MAX:]
            if candles:
                return candles
        return None

    # ---------------- دسته‌بندی نماد ----------------
    def is_crypto(self, symbol):
        """True اگر رمزارز باشد (نوبیتکس)؛ False برای بازار ایران (برس‌آپ)."""
        if not symbol:
            return False
        if _quote_only(symbol):
            return False
        snap = self.snapshot()
        for key in (symbol, _brs_symbol(symbol)):
            it = snap.get(key)
            if it:
                return it.get('section') == 'cryptocurrency'
        # اسنپ‌شات در دسترس نیست → حدس بر اساس پسوند/پیشوند
        up = symbol.upper()
        return not (up.endswith('_IRT') or up.startswith('IR_')
                    or up.startswith('USD') or up.startswith('EUR')
                    or up.startswith('AED') or up.startswith('TRY'))

    # ---------------- قیمت لحظه‌ای ----------------
    def quote(self, symbol):
        """
        قیمت لحظه‌ای از خود ماژول:
          کریپتو → آخرین کندل ساعتی نوبیتکس (تغییر ۲۴ساعته)
          ایران   → اسنپ‌شات BrsApi (قیمت/تغییر/واحد/نام)
        None در صورت شکست.
        """
        if not symbol:
            return None
        if self.is_crypto(symbol):
            return self._quote_nobitex(symbol)
        return self._quote_brs(symbol)

    def _quote_nobitex(self, symbol):
        to = int(time.time())
        frm = to - 86400

        def _fetch():
            for mkt in _candidates(symbol):
                u = self._http_json(NOB_URL + '?' + urllib.parse.urlencode({
                    'symbol': mkt, 'resolution': '60', 'from': frm, 'to': to,
                }), self.timeout)
                if not isinstance(u, dict) or u.get('s') != 'ok' or not u.get('c'):
                    continue
                closes = [float(x) for x in u['c'] if x]
                opens = [float(x) for x in u.get('o') or u['c'] if x]
                if not closes:
                    continue
                price = closes[-1]
                base = opens[0] if opens else closes[0]
                chg = ((price - base) / base * 100.0) if base else 0.0
                return {'symbol': symbol, 'price': price, 'changePct': chg,
                        'change_value': 0.0, 'unit': '', 'name': '',
                        'source': 'nobitex'}
            return None

        return self._get(f'q|{symbol}', QUOTE_TTL, _fetch)

    def _quote_brs(self, symbol):
        snap = self.snapshot()
        if not snap:
            return None
        it = snap.get(symbol) or snap.get(_brs_symbol(symbol))
        if not it:
            return None
        return {'symbol': it['symbol'], 'price': it['price'],
                'changePct': it['changePct'], 'change_value': it['change_value'],
                'unit': it['unit'], 'name': it['name'], 'source': 'brsapi'}

    # ---------------- کندل ----------------
    def feed(self, symbol, interval, limit, from_ts=0, to_ts=0):
        """
        کندل‌های نماد — مستقیم از منبع درست:
          کریپتو → نوبیتکس  |  بازار ایران → BrsApi (تیک ۲۴ساعته یا روزانه)
        خروجی: (candles|None, source)
        """
        if not symbol:
            return None, ''
        limit = max(20, min(int(limit or 90), LIMIT_MAX))
        bucket = INTERVALS.get(interval, 900)

        if _quote_only(symbol):
            return [], 'quote-only'

        if self.is_crypto(symbol):
            key = f'feed|{symbol}|{interval}|{limit}|{from_ts}|{to_ts}'
            c = self._get(key, NOB_TTL,
                          lambda: self._nobitex_candles(symbol, bucket, limit,
                                                        from_ts, to_ts))
            return c, ('nobitex' if c else '')

        # ---- بازار ایران ----
        key = f'feed|{symbol}|{interval}|{limit}|{from_ts}|{to_ts}'
        now = int(time.time())

        def _fetch():
            # بازه‌ای که قدیمی‌تر از پنجرهٔ ۲۴ساعتهٔ تیک است → باید روزانه باشد
            if bucket >= 86400 or (from_ts and from_ts < now - 26 * 3600):
                all_c = self.brs_daily(symbol) or []
                if bucket > 86400:
                    all_c = aggregate(all_c, bucket)
                return all_c or None
            rows = self.brs_ticks(symbol)
            cands = self._ticks_to_candles(rows, bucket) if rows else []
            if not cands:
                all_c = self.brs_daily(symbol) or []   # چارت هیچ‌وقت خالی نشود
                if bucket > 86400:
                    all_c = aggregate(all_c, bucket)
                return all_c or None
            return cands

        c = self._get(key, NOB_TTL, _fetch)
        if c is None:
            return None, ''
        if from_ts or to_ts:
            c = [x for x in c if (not from_ts or x['t'] >= from_ts)
                 and (not to_ts or x['t'] <= to_ts)]
        if len(c) > RANGE_MAX:
            c = c[-RANGE_MAX:]
        if not (from_ts or to_ts) and len(c) > limit:
            c = c[-limit:]
        return c, ('brsapi' if c else '')