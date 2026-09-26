"""Food database: local catalogue first, Open Food Facts (OFF) as a cached fallback.

OFF products are upserted into `products` (source="off", keyed by barcode) so food logs keep a
stable local foreign key, work offline afterwards and do not spend OFF's rate limits again
(read: 15 req/min, search: 10 req/min per IP). Network failures never raise — callers get
None / [] and the app keeps working with the local catalogue.

Market rules: products sold in Ukraine are searched first; European results are added only when
Ukraine gives very few. Every word of the query must appear in the name or brand. Russian and
Belarusian products are never returned or cached. Duplicates are collapsed: same barcode, same
name + brand without pack size ("2L", "500 г"), or same brand with the same nutrition per 100 g
(the 0.5 L and 2 L bottle log identically).
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

from models import SOURCE_OFF, SOURCE_QUICK, Product, db

logger = logging.getLogger(__name__)

OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{code}"
OFF_SEARCH_URL = "https://search.openfoodfacts.org/search"
OFF_FIELDS = "code,product_name,product_name_uk,brands,nutriments,countries_tags"
# OFF asks every client to identify itself: AppName/Version (contact).
USER_AGENT = os.environ.get("OFF_USER_AGENT", "").strip() or "Kolos/1.0 (course project)"
TIMEOUT_SECONDS = 8

MIN_REMOTE_QUERY = 3        # characters before we ask OFF
LOCAL_ENOUGH = 8            # skip OFF when the local catalogue already has this many matches
UA_ENOUGH = 5               # skip the European OFF search when Ukraine already gave this many distinct items
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
    hay = " ".join(_words(f"{item.get('name')} {item.get('brand') or ''}"))
    return all(t in hay for t in terms)


def in_nearby_market(item: dict[str, Any]) -> bool:
    return item.get("ukraine") or bool(set(item.get("countries") or []) & NEARBY_MARKETS)


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


def _product_dict(p: Product) -> dict[str, Any]:
    return {"barcode": p.barcode, "name": p.name, "brand": p.brand,
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


def parse_off_product(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Map an OFF product/hit to our fields; None when name or calories are missing."""
    if not raw:
        return None
    name = _clean(raw.get("product_name_uk")) or _clean(raw.get("product_name"))
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

    countries = raw.get("countries_tags") or []
    barcode = normalize_barcode(str(raw.get("code") or ""))
    return {
        "barcode": barcode,
        "name": name[:255],
        "brand": brand[:255] if brand else None,
        "calories_per_100g": round(kcal, 1),
        "proteins": macro("proteins_100g"),
        "fats": macro("fat_100g"),
        "carbs": macro("carbohydrates_100g"),
        "countries": countries,
        "ukraine": UKRAINE_TAG in countries or gs1_prefix(barcode) == UKRAINE_GS1_PREFIX,
    }


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
    params = urllib.parse.urlencode({"q": lucene_query, "page_size": size, "fields": OFF_FIELDS})
    body = _get_json(f"{OFF_SEARCH_URL}?{params}")
    if body is None:
        return None
    return [p for p in (parse_off_product(h) for h in body.get("hits") or []) if p and p["barcode"]]


def search_remote(query: str, limit: int = REMOTE_LIMIT) -> list[dict[str, Any]]:
    """Ukraine first, then nearby (European) markets; all query words required; no RU/BY; deduped."""
    term = re.sub(r"\s+", " ", _LUCENE_SPECIAL.sub(" ", query)).strip().lower()
    terms = _words(term)
    if not terms:
        return []
    cached = _search_cache.get(term)
    if cached and time.monotonic() - cached[0] < SEARCH_CACHE_TTL:
        return cached[1][:limit]

    size = 40  # fetch more than we show: filtering and de-duplication drop a lot
    relevant = lambda item: not is_blocked(item) and matches_all_terms(item, terms)
    ukraine = _search_hits(f'{term} countries_tags:"{UKRAINE_TAG}"', size)
    ukraine_ok = collapse_duplicates([i for i in ukraine or [] if relevant(i)])
    world = None
    if ukraine is None or len(ukraine_ok) < UA_ENOUGH:
        world = _search_hits(term, size)
    if ukraine is None and world is None:
        return []  # do not cache failures

    nearby = [i for i in world or [] if relevant(i) and in_nearby_market(i)]
    # Ukrainian-market items first, then European items that are also sold in Ukraine, then the rest.
    results = collapse_duplicates(ukraine_ok + sorted(nearby, key=lambda p: not p["ukraine"]))[:REMOTE_LIMIT]

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
    for field in ("name", "brand", "calories_per_100g", "proteins", "fats", "carbs"):
        setattr(product, field, data[field])
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


def search_products(query: str, user_id: int, limit: int = 20, remote: bool = True) -> list[Product]:
    """Local matches (shared + own, excluding quick-add entries) first, then cached OFF results.

    Every word must match (so "coca cola" finds "Coca-Cola"); duplicates are collapsed.
    """
    words = _words(query)  # Python lower() folds Cyrillic too
    if not words:
        return []
    q = " ".join(words)
    local_query = Product.visible_to(user_id).filter(Product.source != SOURCE_QUICK)
    for word in words:
        like = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        local_query = local_query.filter(Product.search_terms.like(like, escape="\\"))
    candidates = local_query.order_by(db.func.length(Product.name), Product.name).limit(limit * 3).all()
    local = collapse_duplicates([p for p in candidates if not product_is_blocked(p)], _product_dict)[:limit]
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
    return collapse_duplicates(merged, _product_dict)[:limit]
