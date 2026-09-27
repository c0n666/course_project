import io
import json
import urllib.error

import pytest

import food_db
from app import _seed_generic_foods
from conftest import create_user, login
from models import SOURCE_OFF, SOURCE_USER, FoodLog, Product, db
from seed_foods import GENERIC_FOODS


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _json_resp(payload):
    return _Resp(json.dumps(payload).encode("utf-8"))


def _off_product(code="3017620422003", name="Nutella", kcal=539, lang="en", **extra):
    nutr = {"energy-kcal_100g": kcal, "proteins_100g": 6.3, "fat_100g": 30.9, "carbohydrates_100g": 57.5}
    return {"code": code, "lang": lang, "product_name": name, "brands": "Nutella, Ferrero", "nutriments": nutr,
            **extra}


@pytest.fixture(autouse=True)
def _clear_search_cache():
    food_db._search_cache.clear()
    yield
    food_db._search_cache.clear()


def _no_network(monkeypatch):
    """Record any OFF call; food_db swallows exceptions, so assert on the list instead."""
    calls = []

    def fake(*_a, **_kw):
        calls.append(1)
        raise urllib.error.URLError("network disabled in test")

    monkeypatch.setattr(food_db.urllib.request, "urlopen", fake)
    return calls


# --- parsing ------------------------------------------------------------------------

def test_normalize_barcode():
    assert food_db.normalize_barcode(" 3017-6204 22003 ") == "3017620422003"
    assert food_db.normalize_barcode("1234") is None
    assert food_db.normalize_barcode(None) is None


def test_parse_off_product_maps_fields_and_brand():
    parsed = food_db.parse_off_product(_off_product())
    assert parsed == {
        "barcode": "3017620422003", "name": "Nutella", "name_uk": None, "brand": "Nutella",
        "calories_per_100g": 539.0, "proteins": 6.3, "fats": 30.9, "carbs": 57.5,
        "countries": [], "ukraine": False,
        "searchable": False,  # no market: found by barcode only
    }
    # search hits return brands as a list
    assert food_db.parse_off_product(_off_product(brands=["Chobani"]))["brand"] == "Chobani"


def test_parse_keeps_english_and_ukrainian_names_and_unescapes_html():
    raw = _off_product(name="Yogurt", product_name_uk="Йогурт &quot;Карпатський&quot;",
                       brands=["ТОВ &quot;Галичина&quot;"], countries_tags=["en:ukraine"])
    parsed = food_db.parse_off_product(raw)
    assert parsed["name"] == "Yogurt"
    assert parsed["name_uk"] == 'Йогурт "Карпатський"'
    assert parsed["brand"] == 'ТОВ "Галичина"'
    assert parsed["ukraine"] is True
    assert parsed["searchable"]


def test_parse_decides_names_and_searchability_by_script_and_market():
    parse = lambda **kw: food_db.parse_off_product(_off_product(**kw))
    # A Ukrainian label entered as product_name_en is not an English name.
    ua = parse(code="4820226161165", name="Йогурт", product_name_en="Йогурт", lang="en")
    assert (ua["name"], ua["name_uk"], ua["searchable"]) == ("Йогурт", None, True)
    # Imported brand sold in Ukraine: the plain-Latin label serves both languages.
    cola = parse(code="5449000000439", name="Coca Cola Original taste", countries_tags=["en:ukraine"])
    assert cola["searchable"] and cola["name_uk"] is None
    # Polish label: neither English nor a name a Ukrainian shopper reads ...
    pl = parse(code="5900000000001", name="Mleko łaciate", lang="pl", countries_tags=["en:ukraine", "en:poland"])
    assert not pl["searchable"]
    # ... unless an English name exists.
    assert parse(code="5900000000001", name="Mleko łaciate", lang="pl", product_name_en="Milk",
                 countries_tags=["en:poland"])["searchable"]
    # English product from an English-speaking market.
    assert parse(code="0894700010137", name="Greek Yogurt", countries_tags=["en:united-states"])["searchable"]
    # Far markets and Cyrillic labels from outside Ukraine are left out.
    assert not parse(code="8801234567890", name="Yogurt", countries_tags=["en:south-korea"])["searchable"]
    assert not parse(code="4870003213136", name="Йогурт", lang="kk", countries_tags=["en:kazakhstan"])["searchable"]


def test_unreadable_scripts_are_skipped_in_names_and_brands():
    assert food_db.parse_off_product(_off_product(name="קוקה קולה")) is None
    assert food_db.parse_off_product(_off_product(name="可口可乐")) is None
    parsed = food_db.parse_off_product(_off_product(name="Coca-Cola Cachere 1.5L", brands=["קוקה קולה", "Coca-Cola"]))
    assert parsed["brand"] == "Coca-Cola"
    assert food_db.parse_off_product(_off_product(name="Yogurt", brands=["Κρι Κρι"]))["brand"] is None
    assert food_db.readable("Йогурт Галичина 2,5%") and food_db.readable("Crème brûlée")


def test_russian_and_belarusian_products_are_blocked():
    assert food_db.is_blocked_barcode("4602248009492")      # 460 Russia
    assert food_db.is_blocked_barcode("4810000000017")      # 481 Belarus
    assert food_db.is_blocked_barcode("46012345")           # EAN-8, Russia
    assert not food_db.is_blocked_barcode("4820222760447")  # 482 Ukraine
    assert not food_db.is_blocked_barcode("012345678905")   # UPC-A (US)
    assert food_db.looks_russian("Сырок глазированный")
    assert not food_db.looks_russian("Сирок глазурований")
    assert not food_db.looks_russian("Йогурт")               # shared Cyrillic only
    assert not food_db.looks_russian("Їжак зі сметаною")
    assert food_db.is_blocked({"barcode": "4820000000017", "name": "X", "countries": ["en:russia"]})
    assert not food_db.is_blocked({"barcode": "3017620422003", "name": "Nutella",
                                   "countries": ["en:russia", "en:ukraine"]})


def test_parse_off_product_kj_fallback_and_rejects_bad_items():
    raw = _off_product()
    raw["nutriments"] = {"energy_100g": 418.4, "proteins_100g": 1}
    assert food_db.parse_off_product(raw)["calories_per_100g"] == 100.0
    assert food_db.parse_off_product(_off_product(name="")) is None
    no_energy = _off_product()
    no_energy["nutriments"] = {"proteins_100g": 5}
    assert food_db.parse_off_product(no_energy) is None
    assert food_db.parse_off_product(_off_product(kcal=5000)) is None


# --- barcode lookup -----------------------------------------------------------------

def test_find_by_barcode_fetches_once_then_uses_cache(app, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        assert req.get_header("User-agent").startswith("Kolos/")
        return _json_resp({"status": 1, "product": _off_product()})

    monkeypatch.setattr(food_db.urllib.request, "urlopen", fake_urlopen)
    with app.app_context():
        user = create_user("scan@user.test")
        first = food_db.find_by_barcode("3017620422003", user.id)
        second = food_db.find_by_barcode("3017620422003", user.id)
        assert first.id == second.id
        assert first.source == SOURCE_OFF and first.brand == "Nutella"
        assert Product.query.filter_by(barcode="3017620422003").count() == 1
    assert len(calls) == 1  # second scan served locally


def test_find_by_barcode_unknown_or_network_error_returns_none(app, monkeypatch):
    monkeypatch.setattr(food_db.urllib.request, "urlopen",
                        lambda *a, **k: _json_resp({"status": 0}))
    with app.app_context():
        user = create_user("scan2@user.test")
        assert food_db.find_by_barcode("0000000000000", user.id) is None

    def boom(*_a, **_kw):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(food_db.urllib.request, "urlopen", boom)
    with app.app_context():
        assert food_db.find_by_barcode("5000000000001", user.id) is None


# --- search -------------------------------------------------------------------------

def test_search_prefers_local_and_skips_network_when_enough(app, monkeypatch):
    calls = _no_network(monkeypatch)
    with app.app_context():
        _seed_generic_foods()
        user = create_user("s@user.test")
        results = food_db.search_products("cooked", user.id)
        assert len(results) >= food_db.LOCAL_ENOUGH
        assert all("cooked" in p.name.lower() for p in results)
        assert [p.name for p in food_db.search_products("ri", user.id)]  # short query: local only
    assert calls == []


def test_search_merges_remote_hits_and_caches(app, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _json_resp({"hits": [
            _off_product(code="0894700010137", name="Nonfat Greek Yogurt", kcal=52.9, brands=["Chobani"],
                         countries_tags=["en:united-states"]),
            _off_product(code="", name="No barcode item"),  # dropped: cannot be cached by barcode
        ]})

    monkeypatch.setattr(food_db.urllib.request, "urlopen", fake_urlopen)
    with app.app_context():
        user = create_user("s2@user.test")
        names = [p.display_name for p in food_db.search_products("chobani", user.id)]
        assert names == ["Nonfat Greek Yogurt · Chobani"]
        food_db.search_products("Chobani", user.id)  # same query, cached (case-insensitive)
        # the product is now local, so an offline search still finds it
        assert Product.query.filter_by(barcode="0894700010137").one().source == SOURCE_OFF
    assert len(calls) == 2  # sold in Ukraine + worldwide on the first search, nothing on the cached one


def test_search_network_failure_returns_local_only(app, monkeypatch):
    def boom(*_a, **_kw):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(food_db.urllib.request, "urlopen", boom)
    with app.app_context():
        user = create_user("s3@user.test")
        assert food_db.search_products("zzzunknown", user.id) == []


# --- privacy ------------------------------------------------------------------------

def test_private_foods_are_invisible_and_unloggable_for_others(app, client, monkeypatch):
    calls = _no_network(monkeypatch)
    with app.app_context():
        owner = create_user("owner@user.test")
        create_user("other@user.test")
        food = Product(name="Grandma's pierogi", calories_per_100g=210, proteins=6, fats=8, carbs=28,
                       source=SOURCE_USER, created_by_id=owner.id)
        db.session.add(food)
        db.session.commit()
        food_id = food.id
        assert [p.id for p in food_db.search_products("pierogi", owner.id, remote=False)] == [food_id]
        other_id = create_user("third@user.test").id
        assert food_db.search_products("pierogi", other_id, remote=False) == []

    login(client, "other@user.test")
    assert "Grandma" not in client.get("/log-food").get_data(as_text=True)
    resp = client.post("/log-food", data={"product_id": food_id, "portion_grams": 100, "meal_type": "lunch"})
    assert resp.status_code == 400
    with app.app_context():
        assert FoodLog.query.count() == 0
    assert calls == []


# --- seeding ------------------------------------------------------------------------

def test_seed_generic_foods_is_idempotent(app):
    with app.app_context():
        before = Product.query.count()
        _seed_generic_foods()
        added = Product.query.count() - before
        _seed_generic_foods()
        assert Product.query.count() - before == added
        assert added >= len(GENERIC_FOODS) - 5  # names already in PRODUCT_SEED are skipped
        assert len({name for name, *_ in GENERIC_FOODS}) == len(GENERIC_FOODS)


# --- market rules in search --------------------------------------------------------

def _fake_search(monkeypatch, ukraine_hits, world_hits=()):
    """Serve OFF searches (Ukraine-market query vs worldwide); returns the (q, langs) pairs asked."""
    asked = []

    def fake_urlopen(req, timeout=None):
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(req.full_url).query)
        q = params["q"][0]
        asked.append((q, params["langs"][0]))
        return _json_resp({"hits": list(ukraine_hits if "en:ukraine" in q else world_hits)})

    monkeypatch.setattr(food_db.urllib.request, "urlopen", fake_urlopen)
    return asked


UA_QUERY = '{} (countries_tags:"en:ukraine" OR code:482*)'
UA_YOGURTS = [
    _off_product(code="4820222760447", name="Йогурт Карпатський", product_name_en="Carpathian yogurt",
                 brands=["Galychyna"], countries_tags=["en:ukraine"]),
    _off_product(code="4820226161165", name="Йогурт", brands=["Danone"]),                   # 482, no tags, no English name
    _off_product(code="5202234141398", name="Greek Yogurt", brands=["Kri Kri"], countries_tags=["en:greece", "en:ukraine"]),
]
WORLD_YOGURTS = [
    _off_product(code="4602248009492", name="Yogurt", brands=["Yarmarka"], countries_tags=["en:poland"]),  # RU prefix
    _off_product(code="4820000000017", name="Yogurt Сырок", brands=["X"], countries_tags=["en:poland"]),     # Russian text
    _off_product(code="0000231312345", name="Yogurt", brands=["Dolche"], countries_tags=["en:poland"], kcal=60),
    _off_product(code="231312345", name="yogurt", brands=["dolche"], countries_tags=["en:poland"], kcal=60),  # same item
    _off_product(code="5202234141399", name="Greek Yogurt 500 g", brands=["Kri Kri"], countries_tags=["en:greece"]),
    _off_product(code="0894700010137", name="Nonfat Greek Yogurt", brands=["Chobani"], countries_tags=["en:united-states"]),
    _off_product(code="8801234567890", name="Yogurt", brands=["Seoul Milk"], countries_tags=["en:south-korea"]),  # far
    _off_product(code="5900000000001", name="Strawberry drink", brands=["Tymbark"], countries_tags=["en:poland"]),  # no term
]


def test_search_remote_ukraine_first_then_worldwide_without_ru_noise_or_duplicates(monkeypatch):
    asked = _fake_search(monkeypatch, UA_YOGURTS, WORLD_YOGURTS)
    names = [p["name"] for p in food_db.search_remote("yogurt")]
    assert asked == [(UA_QUERY.format("yogurt"), "uk,en"), ("yogurt", "uk,en")]
    assert names == ["Carpathian yogurt", "Greek Yogurt", "Yogurt", "Nonfat Greek Yogurt"]


def test_search_remote_matches_ukrainian_names_too(monkeypatch):
    _fake_search(monkeypatch, UA_YOGURTS, WORLD_YOGURTS)
    # Many Ukrainian products have no country tag: the 482 barcode counts as the Ukrainian market.
    assert [(p["name_uk"] or p["name"], p["brand"]) for p in food_db.search_remote("йогурт")] == [
        ("Йогурт Карпатський", "Galychyna"), ("Йогурт", "Danone")]


def test_search_remote_skips_worldwide_when_ukraine_has_enough(monkeypatch):
    kefirs = [_off_product(code=f"48200000000{i:02d}", name=f"Кефір {i}", kcal=40 + i, countries_tags=["en:ukraine"])
              for i in range(food_db.UA_ENOUGH)]
    asked = _fake_search(monkeypatch, kefirs)
    assert len(food_db.search_remote("кефір")) == food_db.UA_ENOUGH
    assert len(asked) == 1


def test_search_remote_strips_query_syntax(monkeypatch):
    asked = _fake_search(monkeypatch, [])
    food_db.search_remote('yogurt:"(x')
    assert asked[0][0] == UA_QUERY.format("yogurt x")


def test_search_remote_collapses_same_brand_same_nutrition(monkeypatch):
    colas = [
        _off_product(code="5449000000439", name="Coca Cola Original taste", brands=["Coca-Cola"], kcal=42,
                     countries_tags=["en:ukraine"]),
        _off_product(code="5449000009067", name="Coca Cola Regular 2L", brands=["Coca-Cola"], kcal=42,
                     countries_tags=["en:ukraine"]),
        _off_product(code="5449000258243", name="Coca-Cola plus Coffee", brands=["Coca-Cola"], kcal=1,
                     countries_tags=["en:ukraine"]),
        _off_product(code="4823063116046", name="Pepsi Zero Mango", brands=["Pepsi-Cola"], kcal=0.5,
                     countries_tags=["en:ukraine"]),
    ]
    _fake_search(monkeypatch, colas)
    assert [p["name"] for p in food_db.search_remote("coca cola")] == ["Coca Cola Original taste",
                                                                       "Coca-Cola plus Coffee"]


def test_search_products_is_shared_between_languages(app, monkeypatch):
    asked = _fake_search(monkeypatch, UA_YOGURTS, WORLD_YOGURTS)
    with app.app_context():
        user = create_user("lang@user.test")
        uk = food_db.search_products("yogurt", user.id, lang="uk")
        en = food_db.search_products("yogurt", user.id, lang="en")
        assert {p.id for p in uk} == {p.id for p in en}
        assert "Йогурт Карпатський" in [p.name_in("uk") for p in uk]
        assert "Carpathian yogurt" in [p.name_in("en") for p in en]
        # Cached products are found by either name, offline too.
        assert "Nonfat Greek Yogurt" in [p.name for p in food_db.search_products("yogurt", user.id, remote=False,
                                                                                lang="uk")]
        assert "Carpathian yogurt" in [p.name for p in food_db.search_products("карпатський", user.id, remote=False,
                                                                              lang="en")]
    assert len(asked) == 2  # the second language reused the cached OFF search


def test_local_search_is_case_insensitive_for_cyrillic_and_knows_ukrainian_names(app, monkeypatch):
    calls = _no_network(monkeypatch)
    with app.app_context():
        _seed_generic_foods()
        user = create_user("ua@user.test")
        food = Product(name="Йогурт Галичина", calories_per_100g=60, source=SOURCE_USER, created_by_id=user.id)
        food.refresh_search_terms()
        db.session.add(food)
        db.session.commit()
        assert [p.name for p in food_db.search_products("йогурт галичина", user.id, remote=False)] == ["Йогурт Галичина"]
        assert "Buckwheat, cooked" in [p.name for p in food_db.search_products("Гречка", user.id, remote=False)]
        assert "Borscht" in [p.name for p in food_db.search_products("борщ", user.id, remote=False)]
        # Ukrainian display names; synonyms and English names find them too.
        uk = lambda q: [p.name_in("uk") for p in food_db.search_products(q, user.id, remote=False, lang="uk")]
        assert uk("гречана каша") == ["Гречка, варена"]
        assert "Куряче філе" in uk("курка")
        assert "Гречка, варена" in uk("buckwheat")
        # LIKE wildcards in user input are literal ("_" would otherwise match any character)
        assert food_db.search_products("_", user.id, remote=False) == []
    assert calls == []


def test_barcode_api_rejects_russian_barcodes(app, client, monkeypatch):
    calls = _no_network(monkeypatch)
    with app.app_context():
        create_user("rb@user.test")
    login(client, "rb@user.test")
    resp = client.get("/api/products/barcode/4602248009492")
    assert resp.status_code == 422 and "Russia" in resp.get_json()["error"]
    assert calls == []
