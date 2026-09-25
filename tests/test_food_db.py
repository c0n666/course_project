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


def _off_product(code="3017620422003", name="Nutella", kcal=539, **extra):
    nutr = {"energy-kcal_100g": kcal, "proteins_100g": 6.3, "fat_100g": 30.9, "carbohydrates_100g": 57.5}
    return {"code": code, "product_name": name, "brands": "Nutella, Ferrero", "nutriments": nutr, **extra}


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
        "barcode": "3017620422003", "name": "Nutella", "brand": "Nutella",
        "calories_per_100g": 539.0, "proteins": 6.3, "fats": 30.9, "carbs": 57.5,
    }
    # search hits return brands as a list
    assert food_db.parse_off_product(_off_product(brands=["Chobani"]))["brand"] == "Chobani"


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
        assert req.get_header("User-agent").startswith("NutritionWorkout/")
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
            _off_product(code="0894700010137", name="Nonfat Greek Yogurt", kcal=52.9, brands=["Chobani"]),
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
    assert len(calls) == 1


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
