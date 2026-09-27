"""Food database: local catalogue first, Open Food Facts (OFF) as a cached fallback.

OFF products are upserted into `products` (source="off", keyed by barcode) so food logs keep a
stable local foreign key, work offline afterwards and do not spend OFF's rate limits again
(read: 15 req/min, search: 10 req/min per IP). Network failures never raise — callers get
None / [] and the app keeps working with the local catalogue.

One search for both interface languages; only the shown name follows the language
(Product.name_in). Products sold in Ukraine (Ukraine market tag or a 482 barcode, incl. imported
ones) come first; worldwide products with an English name from Europe and English-speaking
markets are added only when Ukraine gives very few. A query matches English and Ukrainian names.

Every word of the query must appear in the name or brand. Russian and Belarusian products are
never returned or cached. Duplicates are collapsed: same barcode, same name + brand without pack
size ("2L", "500 г"), or same brand with the same nutrition per 100 g (the 0.5 L and 2 L bottle
log identically).
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from i18n import current_language
from ml_inference import GRADE_SOURCE_OFF, normalize_grade
from models import SOURCE_OFF, SOURCE_QUICK, Product, db

logger = logging.getLogger(__name__)

OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{code}"
OFF_SEARCH_URL = "https://search.openfoodfacts.org/search"
OFF_FIELDS = "code,lang,product_name,product_name_uk,product_name_en,brands,nutriments,countries_tags,nutriscore_grade"
# OFF asks every client to identify itself: AppName/Version (contact).
USER_AGENT = os.environ.get("OFF_USER_AGENT", "").strip() or "Kolos/1.0 (course project)"
TIMEOUT_SECONDS = 8

MIN_REMOTE_QUERY = 3        # characters before we ask OFF
LOCAL_ENOUGH = 8            # skip OFF when the local catalogue already has this many matches
UA_ENOUGH = 5               # skip the worldwide search when products sold in Ukraine gave this many
REMOTE_LIMIT = 10           # at most this many Open Food Facts items per search
SEARCH_CACHE_TTL = 600      # seconds
_search_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_SEARCH_CACHE_MAX = 200

UKRAINE_TAG = "en:ukraine"
BLOCKED_COUNTRIES = {"en:russia", "en:belarus"}
# GS1 company prefixes: 460–469 Russia, 481 Belarus.
BLOCKED_GS1_PREFIXES = tuple(str(n) for n in range(460, 470)) + ("481",)
UKRAINE_GS1_PREFIX = "482"  # many Ukrainian products in OFF have no country tag at all
_RU_ONLY_LETTERS = set("ыэёъЫЭЁЪ")
_UA_ONLY_LETTERS = set("іїєґІЇЄҐ")
# OFF search matches the query against these languages' name fields (the default is "en" only,
# and many Ukrainian labels were entered as product_name_en).
SEARCH_LANGS = "uk,en"
_LUCENE_SPECIAL = re.compile(r'[\\+\-!():^\[\]"{}~*?|&/]')
_PACK_SIZE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:ml|cl|l|kg|g|oz|мл|л|кг|г)\b", re.IGNORECASE)
# Markets Ukrainian shops import from; worldwide results outside them are mostly noise
# (other regions' variants of the same product, per-serving values entered as per-100 g, …).
NEARBY_MARKETS = {UKRAINE_TAG} | {f"en:{c}" for c in (
    "austria belgium bulgaria croatia cyprus czech-republic denmark estonia finland france germany greece "
    "hungary ireland italy latvia lithuania luxembourg malta netherlands poland portugal romania slovakia "
    "slovenia spain sweden united-kingdom switzerland norway iceland moldova georgia turkey serbia "
    "montenegro bosnia-and-herzegovina north-macedonia albania european-union"
).split()}
ENGLISH_MARKETS = NEARBY_MARKETS | {f"en:{c}" for c in "united-states canada australia new-zealand".split()}


# --------------------------------------------------------------------------- rules

def normalize_barcode(code: str | None) -> str | None:
    """EAN-8 / UPC-A / EAN-13 / GTIN-14 digits only; None if it cannot be a barcode."""
    digits = re.sub(r"\D", "", code or "")
    return digits if 8 <= len(digits) <= 14 else None


def gs1_prefix(code: str | None) -> str | None:
    barcode = normalize_barcode(code)
    if not barcode:
        return None
    if len(barcode) == 8:
        return barcode[:3]
    if len(barcode) == 14:
        return barcode[1:4]  # first digit is the packaging indicator
    return barcode.zfill(13)[:3]  # UPC-A (12 digits) is EAN-13 with a leading 0


def is_blocked_barcode(code: str | None) -> bool:
    prefix = gs1_prefix(code)
    return bool(prefix) and prefix.startswith(BLOCKED_GS1_PREFIXES)


def looks_russian(text: str | None) -> bool:
    """Russian-only letters (ы э ё ъ) and no Ukrainian-only ones (і ї є ґ)."""
    chars = set(text or "")
    return bool(chars & _RU_ONLY_LETTERS) and not chars & _UA_ONLY_LETTERS


def is_blocked(item: dict[str, Any]) -> bool:
    """Parsed OFF item from Russia/Belarus (barcode, market or Russian-language label)."""
    countries = set(item.get("countries") or [])
    if countries and countries <= BLOCKED_COUNTRIES:
        return True
    return is_blocked_barcode(item.get("barcode")) or looks_russian(f"{item.get('name')} {item.get('brand') or ''}")


def product_is_blocked(product: Product) -> bool:
    """Same rule for products already stored locally (country list is not stored)."""
    return product.source == SOURCE_OFF and (
        is_blocked_barcode(product.barcode) or looks_russian(f"{product.name} {product.brand or ''}")
    )


def _words(text: str | None) -> list[str]:
    return re.sub(r"\W+", " ", (text or "").lower()).split()


def dedupe_key(item: dict[str, Any]) -> str:
    """Name + brand without pack size: "Coca Cola Regular 2L" == "Coca Cola Regular"."""
    name = _PACK_SIZE.sub(" ", item.get("name") or "")
    return " ".join(_words(f"{name} {item.get('brand') or ''}"))


def nutrition_key(item: dict[str, Any]) -> tuple | None:
    """Same brand + same values per 100 g → the same food for logging purposes."""
    brand = " ".join(_words(item.get("brand")))
    if not brand:
        return None  # unbranded items with equal macros can still be different foods
    return (brand,) + tuple(round(float(item.get(k) or 0)) for k in ("calories_per_100g", "proteins", "fats", "carbs"))


def matches_all_terms(item: dict[str, Any], terms: list[str]) -> bool:
    hay = " ".join(_words(f"{item.get('name')} {item.get('name_uk') or ''} {item.get('brand') or ''}"))
    return all(t in hay for t in terms)


def in_english_market(item: dict[str, Any]) -> bool:
    return item.get("ukraine") or bool(set(item.get("countries") or []) & ENGLISH_MARKETS)


def collapse_duplicates(items: list[Any], as_dict=lambda x: x) -> list[Any]:
    """Keep the first of each barcode / name+brand / brand+nutrition group (input is priority-ordered)."""
    seen: set = set()
    out = []
    for item in items:
        d = as_dict(item)
        keys = {("b", d.get("barcode")), ("n", dedupe_key(d)), ("f", nutrition_key(d))}
        keys = {k for k in keys if k[1]}
        if keys & seen:
            continue
        seen |= keys
        out.append(item)
    return out


def _product_dict(p: Product, lang: str = "en") -> dict[str, Any]:
    return {"barcode": p.barcode, "name": p.name_in(lang), "brand": p.brand,
            "calories_per_100g": p.calories_per_100g, "proteins": p.proteins, "fats": p.fats, "carbs": p.carbs}


# --------------------------------------------------------------------------- parsing

def _number(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def readable(text: str | None) -> bool:
    """Only Latin / Cyrillic letters (digits and punctuation allowed) — what a Ukrainian user can read."""
    letters = [ch for ch in text or "" if ch.isalpha()]
    return bool(letters) and all(unicodedata.name(ch, "").startswith(("LATIN", "CYRILLIC")) for ch in letters)


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(text or ""))).strip()


def _letters(text: str) -> list[str]:
    return [ch for ch in text if ch.isalpha()]


def latin_only(text: str) -> bool:
    return readable(text) and not any(unicodedata.name(ch, "").startswith("CYRILLIC") for ch in _letters(text))


def plain_latin(text: str) -> bool:
    """Latin letters without diacritics: brand names, not Polish/German/French labels."""
    return bool(_letters(text)) and all("a" <= ch.lower() <= "z" for ch in _letters(text))


def ukrainian_name(raw: dict[str, Any], ukraine: bool) -> str | None:
    """The label a Ukrainian shopper sees: Ukrainian Cyrillic, or a plain-Latin brand name if sold in Ukraine."""
    for key in ("product_name_uk", "product_name", "product_name_en"):
        name = _clean(raw.get(key))
        if not name or not readable(name) or looks_russian(name):
            continue
        if not latin_only(name) or (ukraine and plain_latin(name)):
            return name
    return None


def english_name(raw: dict[str, Any]) -> str | None:
    """product_name_en in Latin script (it often holds a Cyrillic label), else an English main name."""
    name = _clean(raw.get("product_name_en"))
    if name and latin_only(name):
        return name
    name = _clean(raw.get("product_name"))
    return name if raw.get("lang") == "en" and name and latin_only(name) else None


def parse_off_product(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Map an OFF product/hit to our fields; None when name or calories are missing.

    `name` is the English name when there is one, else the Ukrainian or original label;
    `name_uk` is set only when the Ukrainian label differs from `name`.
    """
    if not raw:
        return None
    countries = raw.get("countries_tags") or []
    barcode = normalize_barcode(str(raw.get("code") or ""))
    ukraine = UKRAINE_TAG in countries or gs1_prefix(barcode) == UKRAINE_GS1_PREFIX
    name_en = english_name(raw)
    name_uk = ukrainian_name(raw, ukraine)
    name = name_en or name_uk or _clean(raw.get("product_name"))
    if name and not readable(name):
        return None  # Hebrew, Chinese, Greek… labels are no use to Ukrainian users
    nutr = raw.get("nutriments") or {}
    kcal = _number(nutr.get("energy-kcal_100g"))
    if kcal is None:
        kj = _number(nutr.get("energy_100g"))
        kcal = kj / 4.184 if kj is not None else None
    if not name or kcal is None or kcal > 950:  # >950 kcal/100 g is not physically plausible
        return None

    brands = raw.get("brands")
    brand_list = brands if isinstance(brands, list) else (brands or "").split(",")
    # First brand written in Latin/Cyrillic: ["קוקה קולה", "Coca-Cola"] → "Coca-Cola".
    brand = next((b for b in map(_clean, brand_list) if readable(b)), None)

    def macro(key: str) -> float:
        v = _number(nutr.get(key))
        return round(min(v, 100.0), 2) if v is not None else 0.0

    item = {
        "barcode": barcode,
        "name": name[:255],
        "name_uk": name_uk[:255] if name_uk and name_uk != name else None,
        "brand": brand[:255] if brand else None,
        "calories_per_100g": round(kcal, 1),
        "proteins": macro("proteins_100g"),
        "fats": macro("fat_100g"),
        "carbs": macro("carbohydrates_100g"),
        "nutri_grade": normalize_grade(raw.get("nutriscore_grade")),  # "unknown"/"not-applicable" → None
        "countries": countries,
        "ukraine": ukraine,
    }
    # Worth listing in search: a Ukrainian-market product a Ukrainian shopper can read, or a
    # product with an English name from a nearby / English-speaking market.
    item["searchable"] = (ukraine and name_uk is not None) or (name_en is not None and in_english_market(item))
    return item


# --------------------------------------------------------------------------- network

def _get_json(url: str) -> dict[str, Any] | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            logger.warning("Open Food Facts returned HTTP %s", exc.code)
    except Exception as exc:  # network errors, timeouts, malformed JSON
        logger.warning("Open Food Facts request failed (%s)", type(exc).__name__)
    return None


def fetch_barcode(code: str) -> dict[str, Any] | None:
    barcode = normalize_barcode(code)
    if not barcode or is_blocked_barcode(barcode):
        return None
    url = OFF_PRODUCT_URL.format(code=barcode) + "?" + urllib.parse.urlencode({"fields": OFF_FIELDS})
    body = _get_json(url)
    if not body or body.get("status") != 1:
        return None
    parsed = parse_off_product(body.get("product"))
    if parsed and not parsed["barcode"]:
        parsed["barcode"] = barcode
    return None if (not parsed or is_blocked(parsed)) else parsed


def _search_hits(lucene_query: str, size: int) -> list[dict[str, Any]] | None:
    params = urllib.parse.urlencode({"q": lucene_query, "langs": SEARCH_LANGS, "page_size": size,
                                     "fields": OFF_FIELDS})
    body = _get_json(f"{OFF_SEARCH_URL}?{params}")
    if body is None:
        return None
    return [p for p in (parse_off_product(h) for h in body.get("hits") or []) if p and p["barcode"]]


def search_remote(query: str, limit: int = REMOTE_LIMIT) -> list[dict[str, Any]]:
    """Sold in Ukraine first, then worldwide English-named items; all query words required; no RU/BY; deduped."""
    term = re.sub(r"\s+", " ", _LUCENE_SPECIAL.sub(" ", query)).strip().lower()
    terms = _words(term)
    if not terms:
        return []
    cached = _search_cache.get(term)
    if cached and time.monotonic() - cached[0] < SEARCH_CACHE_TTL:
        return cached[1][:limit]

    searches = [
        # Many Ukrainian products have no country tag, only a 482 barcode.
        f'{term} (countries_tags:"{UKRAINE_TAG}" OR code:{UKRAINE_GS1_PREFIX}*)',
        term,  # worldwide, only when Ukraine gives very few
    ]
    relevant, answered = [], False
    for lucene in searches:
        hits = _search_hits(lucene, 40)  # more than we show: filtering and de-duplication drop a lot
        answered |= hits is not None
        relevant += [i for i in hits or [] if i["searchable"] and not is_blocked(i) and matches_all_terms(i, terms)]
        if len(collapse_duplicates(relevant)) >= UA_ENOUGH:
            break
    if not answered:
        return []  # do not cache failures

    relevant.sort(key=lambda i: not i["ukraine"])  # stable: sold in Ukraine first, then OFF's ranking
    results = collapse_duplicates(relevant)[:REMOTE_LIMIT]

    if len(_search_cache) >= _SEARCH_CACHE_MAX:
        _search_cache.pop(next(iter(_search_cache)))
    _search_cache[term] = (time.monotonic(), results)
    return results[:limit]


# --------------------------------------------------------------------------- persistence

def upsert_off_product(data: dict[str, Any]) -> Product:
    """Insert or refresh an OFF product by barcode (caller commits)."""
    product = Product.query.filter_by(barcode=data["barcode"]).first()
    if product is None:
        product = Product(barcode=data["barcode"], source=SOURCE_OFF)
        db.session.add(product)
    elif product.source != SOURCE_OFF:
        return product  # never overwrite catalogue or user foods that share a barcode
    for field in ("name", "name_uk", "brand", "calories_per_100g", "proteins", "fats", "carbs"):
        setattr(product, field, data[field])
    if data.get("nutri_grade"):
        product.nutri_grade, product.nutri_source = data["nutri_grade"], GRADE_SOURCE_OFF
    product.refresh_search_terms()
    return product


def find_by_barcode(code: str, user_id: int, remote: bool = True) -> Product | None:
    """Local product for this barcode, else fetch from OFF and cache it."""
    barcode = normalize_barcode(code)
    if not barcode or is_blocked_barcode(barcode):
        return None
    product = Product.query.filter_by(barcode=barcode).first()
    if product and product.is_visible_to(user_id) and not product_is_blocked(product):
        return product
    if product or not remote:
        return None
    data = fetch_barcode(barcode)
    if not data:
        return None
    product = upsert_off_product(data)
    db.session.commit()
    return product


def search_products(query: str, user_id: int, limit: int = 20, remote: bool = True,
                    lang: str | None = None) -> list[Product]:
    """Local matches (shared + own, excluding quick-add entries) first, then cached OFF results.

    The same products in both languages; `lang` (default: the interface language) only orders and
    de-duplicates by the name shown. Every word must match (so "coca cola" finds "Coca-Cola").
    """
    lang = lang or current_language()
    words = _words(query)  # Python lower() folds Cyrillic too
    if not words:
        return []
    q = " ".join(words)
    local_query = Product.visible_to(user_id).filter(Product.source != SOURCE_QUICK)
    for word in words:
        like = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        local_query = local_query.filter(Product.search_terms.like(like, escape="\\"))
    shown_name = db.func.coalesce(Product.name_uk, Product.name) if lang == "uk" else Product.name
    candidates = local_query.order_by(db.func.length(shown_name), shown_name).limit(limit * 3).all()
    as_dict = lambda p: _product_dict(p, lang)
    local = collapse_duplicates([p for p in candidates if not product_is_blocked(p)], as_dict)[:limit]
    if not remote or len(local) >= LOCAL_ENOUGH or len(q) < MIN_REMOTE_QUERY:
        return local

    remote_hits = search_remote(q)
    if not remote_hits:
        return local
    merged = list(local)
    for data in remote_hits:
        product = upsert_off_product(data)
        db.session.flush()
        if product.is_visible_to(user_id):
            merged.append(product)
    db.session.commit()
    return collapse_duplicates(merged, as_dict)[:limit]
