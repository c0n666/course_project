import json
import re
from datetime import date, timedelta

import food_db
from conftest import create_user, login
from models import SOURCE_OFF, SOURCE_QUICK, SOURCE_USER, FavoriteProduct, FoodLog, Product, db


def _product(name="Test oats", kcal=380, source="seed", owner=None, barcode=None):
    p = Product(name=name, calories_per_100g=kcal, proteins=13, fats=7, carbs=67,
                source=source, created_by_id=owner, barcode=barcode)
    db.session.add(p)
    db.session.commit()
    return p.id


def _picker_data(html):
    raw = re.search(r'<script type="application/json" id="pickerData">(.*?)</script>', html, re.S).group(1)
    return json.loads(raw)


def _no_network(monkeypatch):
    calls = []
    monkeypatch.setattr(food_db, "fetch_barcode", lambda code: calls.append(code))
    monkeypatch.setattr(food_db, "search_remote", lambda q, limit=20: calls.append(q) or [])
    return calls


# --- picker data --------------------------------------------------------------------

def test_recent_products_remember_last_portion(app, client, monkeypatch):
    _no_network(monkeypatch)
    with app.app_context():
        create_user("r@user.test")
        pid = _product()
    login(client, "r@user.test")
    client.post("/log-food", data={"product_id": pid, "portion_grams": 60, "meal_type": "breakfast"})
    client.post("/log-food", data={"product_id": pid, "portion_grams": 45, "meal_type": "breakfast"})
    data = _picker_data(client.get("/log-food").get_data(as_text=True))
    assert [(p["id"], p["portion"]) for p in data["recent"]] == [(pid, 45.0)]
    assert any(p["id"] == pid for p in data["catalogue"])


def test_search_api_requires_login_and_returns_local_results(app, client, monkeypatch):
    calls = _no_network(monkeypatch)
    assert client.get("/api/products/search?q=oat").status_code in (302, 401)
    with app.app_context():
        create_user("s@user.test")
        _product("Rolled oats")
    login(client, "s@user.test")
    body = client.get("/api/products/search?q=oats&remote=0").get_json()
    assert [p["label"] for p in body["results"]] == ["Rolled oats"]
    assert calls == []


# --- barcode ------------------------------------------------------------------------

def test_barcode_api_found_not_found_and_invalid(app, client, monkeypatch):
    monkeypatch.setattr(food_db, "fetch_barcode", lambda code: {
        "barcode": code, "name": "Skyr", "brand": "Icelandic", "calories_per_100g": 63,
        "proteins": 11, "fats": 0.2, "carbs": 4,
    } if code == "5690527000017" else None)
    with app.app_context():
        create_user("b@user.test")
    login(client, "b@user.test")
    found = client.get("/api/products/barcode/5690527000017")
    assert found.status_code == 200 and found.get_json()["product"]["label"] == "Skyr · Icelandic"
    assert client.get("/api/products/barcode/4006381333931").status_code == 404
    assert client.get("/api/products/barcode/12ab").status_code == 400
    with app.app_context():
        assert Product.query.filter_by(barcode="5690527000017").one().source == SOURCE_OFF


# --- favorites ----------------------------------------------------------------------

def test_toggle_favorite_with_csrf_header(csrf_app):
    with csrf_app.app_context():
        create_user("f@user.test")
        pid = _product()
    client = csrf_app.test_client()
    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    client.post("/login", data={"email": "f@user.test", "password": "secret123", "csrf_token": token})

    no_token = client.post(f"/favorites/{pid}/toggle", json={})  # JSON caller without token
    assert no_token.status_code == 400 and "session expired" in no_token.get_json()["error"]
    headers = {"X-CSRFToken": token}
    assert client.post(f"/favorites/{pid}/toggle", headers=headers).get_json() == {"favorite": True}
    with csrf_app.app_context():
        assert FavoriteProduct.query.count() == 1
    assert client.post(f"/favorites/{pid}/toggle", headers=headers).get_json() == {"favorite": False}
    assert client.post("/favorites/999999/toggle", headers=headers).status_code == 404


# --- custom foods & quick add -------------------------------------------------------

def test_create_food_is_private_and_preselected(app, client, monkeypatch):
    _no_network(monkeypatch)
    with app.app_context():
        create_user("c@user.test")
        create_user("other@user.test")
    login(client, "c@user.test")
    resp = client.post("/products/new", data={"name": "Mom's borscht", "calories_per_100g": "55",
                                              "proteins": "2", "fats": "2.5", "carbs": "6",
                                              "barcode": "4820000000017"})
    assert resp.status_code == 302 and "product=" in resp.headers["Location"]
    with app.app_context():
        food = Product.query.filter_by(name="Mom's borscht").one()
        assert food.source == SOURCE_USER and food.barcode == "4820000000017"
        food_id = food.id
    data = _picker_data(client.get(resp.headers["Location"]).get_data(as_text=True))
    assert data["preselected"]["id"] == food_id
    assert [p["id"] for p in data["mine"]] == [food_id]

    dup = client.post("/products/new", data={"name": "Copy", "calories_per_100g": "10", "barcode": "4820000000017"},
                      follow_redirects=True)
    assert "already exists" in dup.get_data(as_text=True)
    bad = client.post("/products/new", data={"name": "", "calories_per_100g": "10"}, follow_redirects=True)
    assert "Give the food a name" in bad.get_data(as_text=True)

    other = client.application.test_client()
    login(other, "other@user.test")
    assert other.get(f"/log-food?product={food_id}").status_code == 200
    assert _picker_data(other.get(f"/log-food?product={food_id}").get_data(as_text=True))["preselected"] is None


def test_quick_add_logs_hidden_private_entry(app, client, monkeypatch):
    calls = _no_network(monkeypatch)
    with app.app_context():
        create_user("q@user.test")
    login(client, "q@user.test")
    resp = client.post("/log-food/quick", data={"calories": "420", "meal_type": "dinner", "name": "Pho",
                                                "proteins": "20", "next": "/dashboard"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/dashboard")
    with app.app_context():
        log = FoodLog.query.one()
        assert log.meal_type == "dinner" and float(log.portion_grams) == 100
        assert log.product.source == SOURCE_QUICK and round(log.calories) == 420
    assert client.get("/api/products/search?q=pho&remote=0").get_json()["results"] == []
    assert all(p["name"] != "Pho" for p in _picker_data(client.get("/log-food").get_data(as_text=True))["recent"])
    bad = client.post("/log-food/quick", data={"calories": "0", "meal_type": "dinner"}, follow_redirects=True)
    assert "between 1 and 5000" in bad.get_data(as_text=True)
    assert client.post("/log-food/quick", data={"calories": "100", "meal_type": "brunch"}).status_code == 302
    with app.app_context():
        assert FoodLog.query.count() == 1
    assert calls == []


# --- copy meals ---------------------------------------------------------------------

def test_copy_meals_from_previous_day(app, client, monkeypatch):
    _no_network(monkeypatch)
    today = date.today()
    yesterday = today - timedelta(days=1)
    with app.app_context():
        user = create_user("cp@user.test")
        pid = _product()
        for meal, grams in (("breakfast", 80), ("breakfast", 20), ("lunch", 150)):
            db.session.add(FoodLog(user_id=user.id, date=yesterday, meal_type=meal, product_id=pid, portion_grams=grams))
        db.session.commit()
    login(client, "cp@user.test")

    dash = client.get("/dashboard").get_data(as_text=True)
    assert "Copy yesterday · 3 items" in dash

    client.post("/log-food/copy", data={"date": today.isoformat(), "meal_type": "breakfast"})
    with app.app_context():
        copied = FoodLog.query.filter_by(date=today).order_by(FoodLog.id).all()
        assert [(l.meal_type, float(l.portion_grams)) for l in copied] == [("breakfast", 80), ("breakfast", 20)]

    client.post("/log-food/copy", data={"date": today.isoformat(), "meal_type": "all"})
    with app.app_context():
        assert FoodLog.query.filter_by(date=today).count() == 5

    empty = client.post("/log-food/copy", data={"date": (today - timedelta(days=5)).isoformat()}, follow_redirects=True)
    assert "Nothing to copy" in empty.get_data(as_text=True)
    same = client.post("/log-food/copy", data={"date": today.isoformat(), "from_date": today.isoformat()},
                       follow_redirects=True)
    assert "different day" in same.get_data(as_text=True)
    evil = client.post("/log-food/copy", data={"date": today.isoformat(), "next": "//evil.test"})
    assert "evil" not in evil.headers["Location"]
