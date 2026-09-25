"""Food database: local catalogue first, Open Food Facts (OFF) as a cached fallback.

OFF products are upserted into `products` (source="off", keyed by barcode) so food logs keep a
stable local foreign key, work offline afterwards and do not spend OFF's rate limits again
(read: 15 req/min, search: 10 req/min per IP). Network failures never raise — callers get
None / [] and the app keeps working with the local catalogue.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from sqlalchemy import func, or_

from models import SOURCE_OFF, SOURCE_QUICK, Product, db

logger = logging.getLogger(__name__)

OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{code}"
OFF_SEARCH_URL = "https://search.openfoodfacts.org/search"
OFF_FIELDS = "code,product_name,brands,nutriments"
# OFF asks every client to identify itself: AppName/Version (contact).
USER_AGENT = os.environ.get("OFF_USER_AGENT", "").strip() or "NutritionWorkout/1.0 (course project)"
TIMEOUT_SECONDS = 8

MIN_REMOTE_QUERY = 3        # characters before we ask OFF
LOCAL_ENOUGH = 8            # skip OFF when the local catalogue already has this many matches
SEARCH_CACHE_TTL = 600      # seconds
_search_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_SEARCH_CACHE_MAX = 200


# --------------------------------------------------------------------------- parsing

def normalize_barcode(code: str | None) -> str | None:
    """EAN-8 / UPC-A / EAN-13 / GTIN-14 digits only; None if it cannot be a barcode."""
    digits = re.sub(r"\D", "", code or "")
    return digits if 8 <= len(digits) <= 14 else None


def _number(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def parse_off_product(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Map an OFF product/hit to our fields; None when name or calories are missing."""
    if not raw:
        return None
    name = (raw.get("product_name") or "").strip()
    nutr = raw.get("nutriments") or {}
    kcal = _number(nutr.get("energy-kcal_100g"))
    if kcal is None:
        kj = _number(nutr.get("energy_100g"))
        kcal = kj / 4.184 if kj is not None else None
    if not name or kcal is None or kcal > 950:  # >950 kcal/100 g is not physically plausible
        return None

    brands = raw.get("brands")
    if isinstance(brands, list):
        brand = brands[0] if brands else None
    else:
        brand = (brands or "").split(",")[0] or None

    def macro(key: str) -> float:
        v = _number(nutr.get(key))
        return round(min(v, 100.0), 2) if v is not None else 0.0

    return {
        "barcode": normalize_barcode(str(raw.get("code") or "")),
        "name": name[:255],
        "brand": brand.strip()[:255] if brand else None,
        "calories_per_100g": round(kcal, 1),
        "proteins": macro("proteins_100g"),
        "fats": macro("fat_100g"),
        "carbs": macro("carbohydrates_100g"),
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
    if not barcode:
        return None
    url = OFF_PRODUCT_URL.format(code=barcode) + "?" + urllib.parse.urlencode({"fields": OFF_FIELDS})
    body = _get_json(url)
    if not body or body.get("status") != 1:
        return None
    parsed = parse_off_product(body.get("product"))
    if parsed and not parsed["barcode"]:
        parsed["barcode"] = barcode
    return parsed


def search_remote(query: str, limit: int = 20) -> list[dict[str, Any]]:
    key = query.strip().lower()
    cached = _search_cache.get(key)
    if cached and time.monotonic() - cached[0] < SEARCH_CACHE_TTL:
        return cached[1][:limit]
    params = urllib.parse.urlencode({"q": key, "page_size": max(limit, 20), "fields": OFF_FIELDS})
    body = _get_json(f"{OFF_SEARCH_URL}?{params}")
    if body is None:
        return []  # do not cache failures
    results = [p for p in (parse_off_product(h) for h in body.get("hits") or []) if p and p["barcode"]]
    if len(_search_cache) >= _SEARCH_CACHE_MAX:
        _search_cache.pop(next(iter(_search_cache)))
    _search_cache[key] = (time.monotonic(), results)
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
    return product


def find_by_barcode(code: str, user_id: int, remote: bool = True) -> Product | None:
    """Local product for this barcode, else fetch from OFF and cache it."""
    barcode = normalize_barcode(code)
    if not barcode:
        return None
    product = Product.query.filter_by(barcode=barcode).first()
    if product and product.is_visible_to(user_id):
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
    """Local matches (shared + own, excluding quick-add entries) first, then cached OFF results."""
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q.lower()}%"
    local = (
        Product.visible_to(user_id)
        .filter(Product.source != SOURCE_QUICK)
        .filter(or_(func.lower(Product.name).like(like), func.lower(Product.brand).like(like)))
        .order_by(func.length(Product.name), Product.name)
        .limit(limit)
        .all()
    )
    if not remote or len(local) >= LOCAL_ENOUGH or len(q) < MIN_REMOTE_QUERY:
        return local

    seen = {p.id for p in local}
    merged = list(local)
    remote_hits = search_remote(q, limit)
    if remote_hits:
        for data in remote_hits:
            product = upsert_off_product(data)
            db.session.flush()
            if product.id not in seen and product.is_visible_to(user_id):
                seen.add(product.id)
                merged.append(product)
        db.session.commit()
    return merged[:limit]
