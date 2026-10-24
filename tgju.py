#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tgju — بازار ایران (طلا، سکه، ارز، نفت و انرژی، کالاهای جهانی)
---------------------------------------------------------------
پورت سبک ماژول «tgju_crawl» با کتابخانهٔ استاندارد پایتون (بدون pandas/lxml/bs4)
برای اجرا روی Raspberry Pi Zero. سه اندپوینتی که ماژول استفاده می‌کند:

  1) https://www.tgju.org                → فهرست نمادها + قیمت لحظه‌ای جدول‌ها
  2) https://www.tgju.org/energy         → نمادهای نفت/انرژی/کالا
  3) https://platform.tgju.org/fa/tvdata/history?symbol=SLUG
                                         → تاریخچهٔ روزانهٔ OHLC هر نماد

نام تابع‌ها با ماژول اصلی یکی است تا همان مستندات کار کند:
    get_df_of_symbols()        فهرست نمادها (DataFrame پانداز اگر نصب باشد)
    get_tgju_data('سکه امامی') تاریخچهٔ روزانه (با تاریخ شمسی و روز هفته)

قرارداد واحد: سایت tgju قیمت داخلی را «ریال» می‌دهد و بقیهٔ پروژه «تومان»
کار می‌کند؛ این ماژول ارزش ریالی را ÷۱۰ می‌کند (RIAL_TO_TOMAN) و نمادهای
دلاری (انس، نفت/انرژی، رمزارز) را دست‌نخورده می‌گذارد. واحد هر آیتم در کلید
unit برمی‌گردد: 'Toman' یا 'USD'.

هیچ تابعی استثنا پرتاب نمی‌کند؛ در خطای شبکه None (یا [] برای فهرست) می‌دهد.
"""

import calendar
import datetime
import json
import re
import time
import urllib.parse
import urllib.request

# ------------------------- اندپوینت‌ها -------------------------
TGJU_BASE = 'https://www.tgju.org'
TGJU_ENERGY_URL = 'https://www.tgju.org/energy'
TGJU_HISTORY_URL = 'https://platform.tgju.org/fa/tvdata/history'

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 CryptoChart-Pi/1.0'

PAGE_TIMEOUT = 25      # صفحهٔ اصلی ~۱.۵ مگابایت است
JSON_TIMEOUT = 12
PAGE_TTL = 120         # کش قیمت‌های فهرست (snapshot)
CATALOG_TTL = 21600    # کش فهرست نمادها (نام/بخش) — ۶ ساعت
HIST_TTL = 120         # کش تاریخچه/قیمت هر نماد
NEG_TTL = 5            # کش خطا — کوتاه تا قطعیِ کوتاه سریع جبران شود

RIAL_TO_TOMAN = 0.1

# ------------------------- نقشهٔ نمادها -------------------------
# نماد «برنامه» (همان چیزی که در کانفیگ/PWA ذخیره شده) → slug سایت tgju.
# اسلاگ‌ها با خود سایت بررسی شده‌اند (profile/<slug>).
ALIASES = {
    'USD':            'PRICE_DOLLAR_RL',
    'IR_GOLD_18K':    'GERAM18',
    'IR_GOLD_24K':    'GERAM24',
    'IR_GOLD_MELTED': 'GOLD_FUTURES',
    'XAUUSD':         'ONS',
    'IR_COIN_EMAMI':  'SEKEE',
    'IR_COIN_BAHAR':  'SEKEB',
    'IR_COIN_HALF':   'NIM',
    'IR_COIN_QUARTER': 'ROB',
    'IR_COIN_1G':     'GERAMI',
    'MESGHAL':        'MESGHAL',
}

# کدهای ارز که سایت با الگوی price_<کد> منتشر می‌کند (USD در ALIASES است)
CURRENCY_CODES = {
    'EUR', 'GBP', 'AED', 'TRY', 'JPY', 'CNY', 'CHF', 'CAD', 'AUD', 'SEK',
    'NOK', 'DKK', 'HKD', 'SGD', 'RUB', 'INR', 'PKR', 'IQD', 'SYP', 'AFN',
    'AZN', 'AMD', 'GEL', 'MYR', 'THB', 'SAR', 'QAR', 'OMR', 'BHD', 'KWD',
}

COIN_SLUGS = {'SEKEB', 'SEKEE', 'SEKEE_DOWN', 'RETAIL_SEKEE', 'RETAIL_SEKEB',
              'RETAIL_NIM', 'RETAIL_ROB', 'RETAIL_GERAMI',
              'NIM', 'ROB', 'GERAMI', 'COIN_BLUBBER', 'SEKEB_BLUBBER',
              'NIM_BLUBBER', 'ROB_BLUBBER', 'GERAMI_BLUBBER'}
GOLD_SLUGS = {
    'GERAM18', 'GERAM24', 'MESGHAL', 'GOLD_MINI_SIZE', 'GOLD_FUTURES',
    'GOLD_MELTED_WHOLESALE', 'GOLD_WORLD_FUTURES', 'GOLD_MELTED_TRANSFER',
    'ONS', 'SILVER_999',
}
CURRENCY_PREFIXES = ('PRICE_', 'BANK_', 'SANA_', 'NIMA_', 'USD_', 'CBI_')

# --- واحد ---------------------------------------------------------------
# فقط همین گروه‌ها روی سایت «ریال» هستند و باید ÷۱۰ شوند (تومان).
# بقیه (نفت، کالا، فلزات جهانی، انس، جفت‌ارزها، شاخص) ریالی نیستند و
# عدد خام می‌مانند — تقسیم اشتباه، مقدار چارت را ۱۰ برابر غلط می‌کرد.
RIAL_EXACT = {'SILVER_999', 'MESGHAL', 'ONS_NO'}
RIAL_PREFIXES = ('PRICE_', 'BANK_', 'SANA_', 'NIMA_', 'USD_', 'CBI_', 'GOLD_',
                 'RETAIL_', 'IME_FUND_', 'GC', 'GO0')
RIAL_SUFFIXES = ('_BLUBBER', '_BLUBBER_RL')
UNITLESS = {'BOURSE'}
UNITLESS_PREFIXES = ('USD-', 'EUR-', 'GBP-', 'AUD-', 'NZD-', 'BRL-', 'CHF-',
                     'CAD-', 'JPY-', 'TRY-', 'RUB-', 'CNY-')

# خانواده‌های صفحهٔ انرژی/کالا (برای بخش‌بندی فهرست)
ENERGY_PREFIXES = ('OIL', 'ENERGY-', 'BRENT', 'GAS', 'PROPANE', 'PETROL',
                   'COKING', 'GIDDINGS', 'OKLAHOMA', 'ARKANSAS', 'WESTERN',
                   'GULF', 'ANS-', 'ANS_', 'LNG-', 'LOUISIANA', 'BASEMETAL-',
                   'COMMODITIES-', 'INDICES-', 'PALLADIUM', 'PLATINUM',
                   'COPPER', 'COBALT', 'SILVER')

# فهرست کوچک و بررسی‌شده برای تشخیص «بازار ایران است یا کریپتو» بدون شبکه
SEED_SLUGS = (set(GOLD_SLUGS) | COIN_SLUGS
              | {'OIL_OPEC', 'OIL_BRENT', 'BRENT', 'ENERGY-BRENT-OIL',
                 'PRICE_DOLLAR_RL', 'SILVER', 'BOURSE', 'GC18'})

# اسلاگ‌هایی که یک‌بار جواب داده‌اند (تشخیص خودآموز نمادهای انرژی/کالا)
_KNOWN_SLUGS = set()
SKIP_SLUGS = {'EDIT'}

# ------------------------- ابزار HTTP و کش -------------------------
_CACHE = {}


class TgjuError(Exception):
    pass


def _cache_get(key):
    hit = _CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    return _MISS


class _Miss(object):
    def __repr__(self):
        return '<miss>'


_MISS = _Miss()


def _cache_put(key, ttl, val):
    now = time.time()
    if len(_CACHE) > 256:
        for k, v in list(_CACHE.items()):
            if v[0] <= now:
                del _CACHE[k]
    _CACHE[key] = (now + (ttl if val is not None else min(ttl, NEG_TTL)), val)
    return val


def _request(url, timeout):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': '*/*',
        'Accept-Language': 'fa-IR,fa;q=0.9,en;q=0.8',
        'Connection': 'close',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw.decode('utf-8', 'replace')


def http_text(url, timeout=PAGE_TIMEOUT):
    """GET → متن؛ خطا/پاسخ خالی → None (بدون استثنا)."""
    try:
        txt = _request(url, timeout)
    except Exception:
        return None
    return txt or None


def http_json(url, timeout=JSON_TIMEOUT):
    """GET → dict؛ خطا/JSON نامعتبر → None."""
    txt = http_text(url, timeout)
    if not txt:
        return None
    try:
        j = json.loads(txt)
    except ValueError:
        return None
    return j if isinstance(j, dict) else None


def clear_cache():
    _CACHE.clear()


# ------------------------- تحلیل HTML -------------------------
_NAV_RE = re.compile(r'<a[^>]*href="[^"]*?profile/([A-Za-z0-9_\-]+)"[^>]*>(.*?)</a>',
                     re.S)
_SLUG_ATTR_RE = re.compile(r'data-market-nameslug="([A-Za-z0-9_\-]+)"')
_ATTR_RE = re.compile(r'([A-Za-z0-9_:\-]+)="([^"]*)"')
_TH_RE = re.compile(r'<th[^>]*>(.*?)</th>', re.S)
_TD_RE = re.compile(r'<td[^>]*>(.*?)</td>', re.S)
_TAG_RE = re.compile(r'<[^>]+>')
_PCT_RE = re.compile(r'\(\s*(-?[\d.]+)\s*%\s*\)')
_NUM_RE = re.compile(r'-?[\d,]+(?:\.\d+)?')

FA_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


def clean_text(html):
    """تگ‌ها → فاصله، فاصله‌های تکراری → یکی، موجودیت‌های رایج جایگزین."""
    t = _TAG_RE.sub(' ', html or '')
    t = (t.replace('&nbsp;', ' ').replace('&amp;', '&')
          .replace('&lt;', '<').replace('&gt;', '>').replace('&#39;', "'"))
    return re.sub(r'\s+', ' ', t).strip()


def num(text):
    """'2,719,050' / '۲٬۷۱۹' → float؛ نامعتبر → 0.0"""
    if text is None:
        return 0.0
    s = str(text).translate(FA_DIGITS).replace('٬', '').replace('،', '')
    m = _NUM_RE.search(s)
    if not m:
        return 0.0
    try:
        return float(m.group(0).replace(',', ''))
    except ValueError:
        return 0.0


def _tag_end(html, start):
    """پایان تگ شروع‌شده در start — با احترام به مقدارهای داخل کوتیشن.

    جست‌وجوی سادهٔ '>' غلط است چون بعضی ردیف‌ها HTML داخل data-title دارند.
    """
    i, n, q = start, len(html), ''
    while i < n:
        c = html[i]
        if q:
            if c == q:
                q = ''
        elif c in ('"', "'"):
            q = c
        elif c == '>':
            return i
        i += 1
    return -1


def parse_nav(html):
    """پیوندهای /profile/<slug> با نام فارسی → [{slug, name}] (خالی‌ها حذف)"""
    out, seen = [], set()
    for m in _NAV_RE.finditer(html or ''):
        slug = m.group(1)
        name = clean_text(m.group(2))
        key = slug.upper()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append({'slug': slug, 'name': name})
    return out


def parse_rows(html):
    """ردیف‌های جدول بازار: [{slug, name, price, changePct, change, low, high}]

    ردیف‌ها ویژگی data-market-nameslug دارند؛ قیمت در data-price (اگر نبود
    اولین سلول) و درصد تغییر در سلول «(0.78%) 123».
    """
    out, seen = {}, set()
    html = html or ''
    for m in _SLUG_ATTR_RE.finditer(html):
        slug = m.group(1)
        key = slug.upper()
        if key in seen:
            continue
        start = html.rfind('<tr', 0, m.start())
        if start < 0:
            continue
        t_end = _tag_end(html, start)
        if t_end < 0:
            continue
        row_end = html.find('</tr>', t_end)
        if row_end < 0:
            continue
        attrs = dict(_ATTR_RE.findall(html[start:t_end + 1]))
        body = html[t_end + 1:row_end]
        th = _TH_RE.search(body)
        name = clean_text(th.group(1)) if th else ''
        cells = [clean_text(c) for c in _TD_RE.findall(body)]
        price = num(attrs.get('data-price'))
        if price <= 0 and cells:
            price = num(cells[0])
        change, pct = 0.0, 0.0
        for c in cells[1:3] + [attrs.get('data-title', '')]:
            pm = _PCT_RE.search(c or '')
            if pm:
                try:
                    pct = float(pm.group(1))
                except ValueError:
                    pct = 0.0
                break
        if len(cells) >= 3:
            change = num(cells[1].split(')')[-1]) if ')' in cells[1] else num(cells[1])
        low = num(cells[2]) if len(cells) > 2 else 0.0
        high = num(cells[3]) if len(cells) > 3 else 0.0
        if price <= 0 and not name:
            continue
        seen.add(key)
        out[key] = {'slug': slug, 'name': name, 'price': price,
                    'changePct': pct, 'change': change, 'low': low, 'high': high}
    return list(out.values())


# ------------------------- فهرست نمادها (سازگار با ماژول) -------------------------
def main_page_html(force=False):
    return _page(TGJU_BASE, 'page|main', force)


def energy_page_html(force=False):
    return _page(TGJU_ENERGY_URL, 'page|energy', force)


def _page(url, key, force=False):
    if not force:
        hit = _cache_get(key)
        if hit is not _MISS:
            return hit
    return _cache_put(key, PAGE_TTL, http_text(url, PAGE_TIMEOUT))


def get_main_symbols(force=False):
    """نمادهای صفحهٔ اصلی (طلا، سکه، ارز) — [{symbol_Fa, symbol_En, SYMBOL}]"""
    return [{'symbol_Fa': it['name'], 'symbol_En': it['slug'],
             'SYMBOL': it['slug'].upper()}
            for it in parse_nav(main_page_html(force))]


def get_energy_symbols(force=False):
    """نمادهای صفحهٔ انرژی (نفت، گاز، کالا) — مثل خروجی ماژول اصلی"""
    rows = parse_rows(energy_page_html(force))
    return [{'symbol_Fa': r['name'], 'symbol_En': r['slug'],
             'SYMBOL': r['slug'].upper()} for r in rows if r['name']]


def _rows_by_slug(html):
    return {r['slug'].upper(): r for r in parse_rows(html)}


def _catalog_entries(force=False):
    """[(slug, name, area, row|None)] — nav صفحهٔ اصلی + ردیف‌های صفحهٔ انرژی.

    قاعدهٔ ماژول اصلی: صفحهٔ اصلی از منوی سایت (نام‌های کامل فارسی) و صفحهٔ
    انرژی از ردیف‌های جدول. نام‌های تکراری (مثل چند «دلار») فقط یک‌بار
    می‌آیند و اسلاگ تکراری هم دوباره اضافه نمی‌شود.
    """
    main_html = main_page_html(force) or ''
    energy_html = energy_page_html(force) or ''
    main_rows = _rows_by_slug(main_html)
    energy_rows = _rows_by_slug(energy_html)

    entries, names, slugs = [], set(), set()

    def add(slug, name, area, row):
        key = slug.upper()
        # رمزارزها از نوبیتکس می‌آیند (نه از tgju) و EDIT نماد نیست
        if not name or name in names or key in slugs or key in SKIP_SLUGS:
            return
        if key.startswith('CRYPTO-'):
            return
        names.add(name)
        slugs.add(key)
        entries.append((slug, name, area, row))

    # منوی صفحهٔ اصلی نام‌های کامل فارسی دارد (طلای 18 عیار، سکه امامی، دلار …)
    for ent in parse_nav(main_html):
        add(ent['slug'], ent['name'], 'main', main_rows.get(ent['slug'].upper()))
    # ردیف‌های جدول: کالا/انرژی صفحهٔ اصلی + همهٔ نمادهای صفحهٔ انرژی
    for key, row in main_rows.items():
        add(row['slug'], row['name'], 'main', row)
    for key, row in energy_rows.items():
        add(row['slug'], row['name'], 'energy', row)
    return entries


def _catalog_now(force=False):
    """{slugUpper: item} — کش CATALOG_TTL"""
    key = 'catalog'
    if not force:
        hit = _cache_get(key)
        if hit is not _MISS:
            return hit or {}
    out = {}
    for slug, name, area, row in _catalog_entries(force):
        up = slug.upper()
        if up in out:
            continue
        mult = scale_of(up)
        out[up] = {
            'symbol': app_symbol(up),
            'slug': up,
            'name': name,
            'name_en': '',
            'section': section_of(up, area),
            'unit': unit_of(up),
            'price': round(float((row or {}).get('price') or 0.0) * mult, 4),
            'changePct': float((row or {}).get('changePct') or 0.0),
            'source': 'tgju',
        }
    return _cache_put(key, CATALOG_TTL, out or None) or {}


def get_df_of_symbols(force=False):
    """فهرست نمادها؛ DataFrame پانداز اگر نصب باشد، وگرنه لیست دیکشنری."""
    rows = [{'symbol_Fa': it['name'], 'symbol_En': it['slug'], 'SYMBOL': it['slug'],
             'price': it['price'], 'section': it['section'], 'unit': it['unit']}
            for it in _catalog_now(force).values()]
    try:
        import pandas as pd          # اختیاری — روی Pi Zero نصب نمی‌شود
        return pd.DataFrame(rows).set_index('symbol_Fa') if rows else pd.DataFrame()
    except Exception:
        return rows


def catalog(force=False):
    """فهرست نمادهای بازار ایران با قیمت لحظه‌ای (برای لیست PWA)"""
    return sorted(_catalog_now(force).values(),
                  key=lambda it: (it['section'] != 'currency', it['name']))


# ------------------------- نقشهٔ نماد ←→ slug -------------------------
def _reverse_aliases():
    return {v: k for k, v in ALIASES.items()}


def app_symbol(slug):
    """slug سایت → نماد برنامه (USD, IR_GOLD_18K, ...)؛ ناشناس → خود slug"""
    up = str(slug or '').upper()
    rev = _reverse_aliases()
    if up in rev:
        return rev[up]
    if up.startswith('PRICE_') and up[6:] in CURRENCY_CODES:
        return up[6:]
    return up


def resolve(symbol):
    """نماد برنامه → slug سایت tgju؛ نامرتبط → None

    پشتیبانی از پیشوند صریح «tgju:» تا هر اسلاگی که در فهرست نیست هم قابل
    استفاده باشد (مثل tgju:oil_brent). اسلاگ‌هایی که یک‌بار داده داده‌اند
    (یا '‌-' دارند) هم به‌عنوان tgju شناخته می‌شوند تا نمادهای انرژی/کالا
    اشتباهی سر از نوبیتکس درنیاورند.
    """
    if not symbol:
        return None
    s = str(symbol).strip()
    if not s:
        return None
    if s.lower().startswith('tgju:'):
        return s[5:].strip().upper() or None
    up = s.upper()
    if up in ALIASES:
        return ALIASES[up]
    if up in CURRENCY_CODES:
        return 'PRICE_' + up
    if up in SEED_SLUGS or up in _KNOWN_SLUGS:
        return up
    if up.startswith(RIAL_PREFIXES + ENERGY_PREFIXES):
        return up
    if '-' in up and not up.endswith(('USDT', 'IRT', 'USDC', 'BUSD')):
        return up                      # اسلاگ‌های tgju خط‌تیره دارند
    hit = _cache_get('catalog')
    if hit is not _MISS and hit and up in hit:
        return up
    return None


def is_tgju_symbol(symbol):
    return resolve(symbol) is not None


def is_rial(slug):
    """ارزش ریالی سایت (طلا/سکه/ارز داخلی و صندوق‌ها) ⇒ باید تومان شود"""
    up = str(slug or '').upper()
    if up in GOLD_SLUGS - {'ONS'} or up in COIN_SLUGS or up in RIAL_EXACT:
        return True
    if up.startswith(RIAL_PREFIXES) or up.endswith(RIAL_SUFFIXES):
        return True
    return False


def is_usd(slug):
    return not is_rial(slug) and unit_of(slug) == 'USD'


def unit_of(slug):
    up = str(slug or '').upper()
    if is_rial(up):
        return 'Toman'
    if up in UNITLESS or up.startswith(UNITLESS_PREFIXES):
        return ''
    return 'USD'


def scale_of(slug):
    return RIAL_TO_TOMAN if is_rial(slug) else 1.0


def section_of(slug, area='main'):
    """currency | gold | coin | energy | other"""
    up = str(slug or '').upper()
    if up in COIN_SLUGS or up.startswith(('GC', 'GO0')):
        return 'coin'
    if up in GOLD_SLUGS or up.startswith(('GOLD_', 'SILVER_', 'IME_FUND_')):
        return 'gold'
    if up.startswith(CURRENCY_PREFIXES) or up.endswith('-ASK'):
        return 'currency'
    if area == 'energy' or up.startswith(ENERGY_PREFIXES):
        return 'energy'
    return 'other'


# ------------------------- تاریخچه و قیمت -------------------------
def _day_candles(j):
    """پاسخ UDF روزانه → کندل‌های یکتا بر اساس روز (ادغام تکراری‌ها)"""
    t = j.get('t') or []
    o = j.get('o') or []
    h = j.get('h') or []
    l = j.get('l') or []
    c = j.get('c') or []
    v = j.get('v') or []

    def at(arr, i):
        try:
            val = arr[i]
        except (IndexError, TypeError):
            return 0.0
        try:
            return float(val or 0)
        except (TypeError, ValueError):
            return 0.0

    days = {}
    for i in range(len(t)):
        try:
            ts = int(t[i])
        except (TypeError, ValueError):
            continue
        close = at(c, i)
        if ts <= 0 or close <= 0:
            continue
        day = ts // 86400 * 86400
        hi, lo = at(h, i) or close, at(l, i) or close
        if hi < lo:
            hi, lo = lo, hi
        cur = days.get(day)
        if cur is None:
            days[day] = {'t': day, 'o': at(o, i) or close,
                         'h': max(hi, close), 'l': min(lo, close),
                         'c': close, 'v': at(v, i)}
        else:
            cur['h'] = max(cur['h'], hi, close)
            cur['l'] = min(cur['l'], lo, close)
            cur['c'] = close
            cur['v'] += at(v, i)
    return [days[k] for k in sorted(days)]


def history(symbol, force=False):
    """کندل روزانهٔ نماد (تومان برای نماد ریالی) — None در خطا، [] بی‌داده"""
    slug = resolve(symbol)
    if not slug:
        return None
    key = 'hist|' + slug
    if not force:
        hit = _cache_get(key)
        if hit is not _MISS:
            return hit
    j = http_json(TGJU_HISTORY_URL + '?symbol=' + urllib.parse.quote(slug),
                  JSON_TIMEOUT)
    if not isinstance(j, dict):
        return _cache_put(key, HIST_TTL, None)
    if (j.get('s') or '') not in ('', 'ok'):
        return _cache_put(key, HIST_TTL, None)
    candles = _day_candles(j)
    if candles:
        _KNOWN_SLUGS.add(slug)
    mult = scale_of(slug)
    if mult != 1.0:
        for cd in candles:
            for f in ('o', 'h', 'l', 'c'):
                cd[f] = round(cd[f] * mult, 4)
    # تاریخی که برس‌آپ با ظهر UTC می‌دهد؛ tgju نیمه‌شب است — یکسان می‌کنیم تا
    # برچسب روز در محور شمسی و باکت‌های هفتگی همه‌جا یکی باشد.
    for cd in candles:
        cd['t'] += 43200
    return _cache_put(key, HIST_TTL, candles)


def quote(symbol):
    """قیمت لحظه‌ای بازار ایران → dict (price, changePct, unit, source) | None

    منبع: آخرین کندل روزانه (همان درخواستی که چارت لازم دارد ⇒ بدون درخواست
    اضافه روی Pi). تغییر نسبت به کندل روز قبل است.
    """
    c = history(symbol)
    if c is None:
        return None
    if not c:
        return {'symbol': str(symbol), 'price': 0.0, 'changePct': 0.0,
                'change_value': 0.0, 'unit': unit_of(resolve(symbol) or ''),
                'name': '', 'source': 'tgju'}
    price = float(c[-1]['c'])
    prev = float(c[-2]['c']) if len(c) > 1 else price
    chg = ((price - prev) / prev * 100.0) if prev else 0.0
    return {'symbol': str(symbol), 'price': price, 'changePct': chg,
            'change_value': price - prev, 'unit': unit_of(resolve(symbol) or ''),
            'name': '', 'source': 'tgju'}


def snapshot(force=False):
    """قیمت لحظه‌ای جدول‌های سایت: {'PRICE_DOLLAR_RL': {...}, ...}"""
    key = 'snap'
    if not force:
        hit = _cache_get(key)
        if hit is not _MISS:
            return hit or {}
    items = {}
    for area, html in (('main', main_page_html()),
                       ('energy', energy_page_html())):
        for row in parse_rows(html or ''):
            up = row['slug'].upper()
            mult = scale_of(up)
            items[up] = dict(row, slug=up, section=section_of(up, area),
                             unit=unit_of(up),
                             price=round(row['price'] * mult, 4),
                             low=round(row['low'] * mult, 4),
                             high=round(row['high'] * mult, 4),
                             symbol=app_symbol(up), source='tgju')
    return _cache_put(key, PAGE_TTL, items or None) or {}


# ------------------------- تاریخ شمسی (سازگار با ماژول) -------------------------
def _jalali(ts):
    """unix → 'YYYY/MM/DD' شمسی — با تابع مشترک پروژه (import تنبل)."""
    try:
        from market import epoch_to_jalali      # بازار ایران و ماژول یکی باشند
        return epoch_to_jalali(int(ts))
    except Exception:
        try:
            import jdatetime
            return str(jdatetime.date.fromgregorian(
                date=datetime.datetime.utcfromtimestamp(int(ts)).date()))
        except Exception:
            return ''


def get_tgju_data(symbol, date_start=None, date_end=None):
    """تاریخچهٔ روزانهٔ نماد — همان قرارداد ماژول اصلی

    خروجی: DataFrame پانداز (اگر نصب باشد) با ستون‌های Date/Weekday/Open/High/
    Low/Close/Symbol و ایندکس J-Date، وگرنه لیست دیکشنری با همان کلیدها.
    date_start/date_end شمسی ('1404/01/01') بازه را فیلتر می‌کند.
    """
    slug = resolve(symbol) or str(symbol or '').strip().upper()
    rows = history(slug) or []
    if not rows:
        try:
            import pandas as pd
            return pd.DataFrame()
        except Exception:
            return []
    lo = _jalali_epoch(date_start) if date_start else 0
    hi = _jalali_epoch(date_end) if date_end else 0
    if hi:
        hi += 86399
    out = []
    for cd in rows:
        if lo and cd['t'] < lo:
            continue
        if hi and cd['t'] > hi:
            continue
        dt = datetime.datetime.utcfromtimestamp(cd['t']).date()
        out.append({
            'J-Date': _jalali(cd['t']),
            'Date': datetime.datetime.combine(dt, datetime.time()),
            'Weekday': calendar.day_name[dt.weekday()],
            'Open': cd['o'], 'High': cd['h'], 'Low': cd['l'], 'Close': cd['c'],
            'Symbol': symbol,
        })
    try:
        import pandas as pd
        df = pd.DataFrame(out)
        return df.set_index('J-Date') if len(df) else df
    except Exception:
        return out


def _jalali_epoch(text):
    try:
        from market import jalali_to_epoch
        return jalali_to_epoch(text)
    except Exception:
        return 0
