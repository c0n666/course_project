"""Web side of the Kolos ML models: Nutri-Score grades, diet quality and photo recognition."""
import io
from datetime import date, timedelta

import pytest

import food_db
import ml_inference
from app import diet_quality_7_days, product_payload
from conftest import create_user, login
from models import SOURCE_OFF, FoodLog, Product, db


@pytest.fixture(autouse=True)
def _no_models():
    """Every test starts (and ends) without trained models, like a fresh checkout."""
    ml_inference.register_score_model(None)
    ml_inference.register_photo_model(None)
    yield
    ml_inference.register_score_model(None)
    ml_inference.register_photo_model(None)


def _product(name, kcal=100, grade=None, source=None, **kw):
    p = Product(name=name, calories_per_100g=kcal, proteins=kw.get("p", 5), fats=kw.get("f", 5),
                carbs=kw.get("c", 5), nutri_grade=grade, nutri_source=source)
    db.session.add(p)
    db.session.flush()
    return p


def _log(uid, product, grams, day=None):
    db.session.add(FoodLog(user_id=uid, date=day or date.today(), meal_type="lunch",
                           product_id=product.id, portion_grams=grams))


def _jpeg():
    return io.BytesIO(b"\xff\xd8\xff\xe0fake-jpeg")


# --- grades ------------------------------------------------------------------------

def test_off_grade_is_parsed_and_stored(app):
    nutr = {"energy-kcal_100g": 60, "proteins_100g": 3, "fat_100g": 1, "carbohydrates_100g": 9}
    raw = {"code": "4820000000011", "product_name": "Kefir", "nutriments": nutr, "nutriscore_grade": "B"}
    assert food_db.parse_off_product(raw)["nutri_grade"] == "b"
    assert food_db.parse_off_product({**raw, "nutriscore_grade": "unknown"})["nutri_grade"] is None
    with app.app_context():
        product = food_db.upsert_off_product(food_db.parse_off_product(raw))
        db.session.commit()
        assert (product.source, product.nutri_grade, product.nutri_source) == (SOURCE_OFF, "b", "off")


def test_payload_uses_stored_grade_then_model(app):
    with app.test_request_context():
        rated = _product("Oats", grade="a", source="off")
        plain = _product("Cookie", kcal=480)
        assert (product_payload(rated)["grade"], product_payload(rated)["grade_src"]) == ("a", "off")
        assert product_payload(plain)["grade"] is None  # no model yet

        seen = []
        ml_inference.register_score_model(lambda features: seen.append(features) or "E")
        assert (product_payload(plain)["grade"], product_payload(plain)["grade_src"]) == ("e", "model")
        assert seen[0] == {"kcal": 480.0, "proteins": 5.0, "fats": 5.0, "carbs": 5.0}
        assert product_payload(rated)["grade"] == "a"  # Open Food Facts wins over the model


def test_diet_quality_shares(app):
    with app.test_request_context():
        uid = create_user("q@user.test").id
        good = _product("Apple", kcal=50, grade="a", source="off")
        bad = _product("Chips", kcal=500, grade="e", source="off")
        unrated = _product("Soup", kcal=100)
        _log(uid, good, 200)                                     # 100 kcal A
        _log(uid, bad, 60, date.today() - timedelta(days=1))     # 300 kcal E
        _log(uid, unrated, 100)                                  # 100 kcal unrated
        _log(uid, bad, 100, date.today() - timedelta(days=9))    # outside the week
        db.session.commit()

        dq = diet_quality_7_days(uid)
        assert dq["total_kcal"] == 500
        assert dq["series"]["a"][-1] == 100 and dq["series"]["e"][-2] == 300
        assert dq["share"] == {"a": 20, "b": 0, "c": 0, "d": 0, "e": 60, "unrated": 20}
        assert (dq["rated_pct"], dq["good_pct"], dq["poor_pct"]) == (80, 25, 75)
        assert dq["model_ready"] is False


def test_dashboard_shows_diet_quality_instead_of_forecast(app, client):
    with app.app_context():
        uid = create_user("d@user.test").id
        _log(uid, _product("Apple", kcal=50, grade="a", source="off"), 200)
        db.session.commit()
    login(client, "d@user.test")
    html = client.get("/dashboard").get_data(as_text=True)
    assert "Diet quality" in html and 'id="qualityChart"' in html
    assert "Weight forecast" not in html and "weightChart" not in html
    assert 'class="grade-chip grade-a"' in html  # the logged food carries its grade


# --- photo recognition -------------------------------------------------------------

def test_photo_api_is_unavailable_without_a_model(app, client):
    with app.app_context():
        create_user("p@user.test")
    login(client, "p@user.test")
    res = client.post("/api/food/photo", data={"photo": (_jpeg(), "a.jpg", "image/jpeg")})
    assert res.status_code == 503 and res.get_json()["ready"] is False
    assert "Soon" in client.get("/log-food").get_data(as_text=True)


def test_photo_api_matches_catalogue_foods(app, client):
    with app.app_context():
        create_user("p@user.test")
        _product("Borscht", kcal=60, grade="b", source="off")
        db.session.commit()
    login(client, "p@user.test")
    calls = []
    ml_inference.register_photo_model(
        lambda image, k: calls.append((image, k)) or [("borscht", 0.81), ("dragon fruit stew", 0.12), ("x", 0.01), ("y", 0)])

    res = client.post("/api/food/photo", data={"photo": (_jpeg(), "a.jpg", "image/jpeg")})
    assert res.status_code == 200
    candidates = res.get_json()["candidates"]
    assert calls[0] == (b"\xff\xd8\xff\xe0fake-jpeg", 3)
    assert [c["label"] for c in candidates] == ["borscht", "dragon fruit stew", "x"]  # top 3
    assert candidates[0]["confidence"] == 0.81
    assert candidates[0]["product"]["name"] == "Borscht" and candidates[0]["product"]["grade"] == "b"
    assert candidates[1]["product"] is None


def test_photo_api_rejects_bad_uploads(app, client):
    with app.app_context():
        create_user("p@user.test")
    login(client, "p@user.test")
    ml_inference.register_photo_model(lambda image, k: [])
    assert client.post("/api/food/photo", data={}).status_code == 400
    res = client.post("/api/food/photo", data={"photo": (io.BytesIO(b"GIF89a"), "a.gif", "image/gif")})
    assert res.status_code == 400
    big = io.BytesIO(b"\xff" * (ml_inference.PHOTO_MAX_BYTES + 1))
    assert client.post("/api/food/photo", data={"photo": (big, "a.jpg", "image/jpeg")}).status_code == 413
