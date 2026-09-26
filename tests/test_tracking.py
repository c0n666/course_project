from datetime import date, timedelta

from conftest import create_user, login
from models import FoodLog, Product, Profile, User, WaterLog, WeightLog, db
from nutrition import logging_streak, water_goal_ml, weight_summary


def _user_id(email):
    return User.query.filter_by(email=email).one().id


def _log_food(user_id, day):
    product = Product.query.first()
    db.session.add(FoodLog(user_id=user_id, date=day, meal_type="lunch",
                           product_id=product.id, portion_grams=100))


# --- water --------------------------------------------------------------------------

def test_water_goal_formula_and_override():
    assert water_goal_ml(None) == 2000
    assert water_goal_ml(Profile(weight=80)) == 2650  # 80 × 33 = 2640 → nearest 50
    assert water_goal_ml(Profile(weight=80, water_goal_ml=3000)) == 3000


def test_water_add_json_and_redirect_and_limits(app, client):
    with app.app_context():
        create_user("w@user.test")
    login(client, "w@user.test")
    today = date.today().isoformat()

    body = client.post("/water", json={"amount_ml": 250, "date": today},
                       headers={"Accept": "application/json"}).get_json()
    assert body == {"total": 250, "goal": 2650, "pct": 9.4, "can_undo": True}

    resp = client.post("/water", data={"amount_ml": "500", "date": today})
    assert resp.status_code == 302 and f"date={today}" in resp.headers["Location"]

    for bad in (0, 49, 2001, "abc"):
        r = client.post("/water", json={"amount_ml": bad}, headers={"Accept": "application/json"})
        assert r.status_code == 400
    future = (date.today() + timedelta(days=1)).isoformat()
    assert client.post("/water", json={"amount_ml": 250, "date": future},
                       headers={"Accept": "application/json"}).status_code == 400
    with app.app_context():
        assert sorted(w.amount_ml for w in WaterLog.query.all()) == [250, 500]


def test_water_undo_removes_only_own_latest_entry(app, client):
    today = date.today()
    with app.app_context():
        create_user("u@user.test")
        other = create_user("o@user.test")
        db.session.add(WaterLog(user_id=other.id, date=today, amount_ml=750))
        db.session.commit()
    login(client, "u@user.test")
    client.post("/water", data={"amount_ml": 250})
    client.post("/water", data={"amount_ml": 500})

    body = client.post("/water/undo", json={"date": today.isoformat()},
                       headers={"Accept": "application/json"}).get_json()
    assert body["total"] == 250 and body["can_undo"] is True
    client.post("/water/undo", data={})
    r = client.post("/water/undo", json={}, headers={"Accept": "application/json"})
    assert r.status_code == 400
    with app.app_context():
        assert [w.amount_ml for w in WaterLog.query.all()] == [750]  # the other user's entry


# --- weight -------------------------------------------------------------------------

def test_weight_upserts_by_date_and_updates_profile_for_latest(app, client):
    today = date.today()
    with app.app_context():
        create_user("kg@user.test")
    login(client, "kg@user.test")

    client.post("/weight", data={"weight_kg": "79.4", "date": today.isoformat()})
    client.post("/weight", data={"weight_kg": "79.0", "date": today.isoformat()})
    client.post("/weight", data={"weight_kg": "81.2", "date": (today - timedelta(days=8)).isoformat()})
    with app.app_context():
        uid = _user_id("kg@user.test")
        assert WeightLog.query.filter_by(user_id=uid).count() == 2
        # The older weigh-in must not overwrite the current weight.
        assert float(Profile.query.filter_by(user_id=uid).one().weight) == 79.0
        assert weight_summary(uid) == {"weight": 79.0, "date": today, "change_7d": -2.2}


def test_weight_rejects_future_date_and_bad_values(app, client):
    with app.app_context():
        create_user("bad@user.test")
    login(client, "bad@user.test")
    future = (date.today() + timedelta(days=1)).isoformat()
    client.post("/weight", data={"weight_kg": "70", "date": future})
    client.post("/weight", data={"weight_kg": "12"})
    client.post("/weight", data={"weight_kg": "nan"})
    with app.app_context():
        assert WeightLog.query.count() == 0


def test_profile_weight_change_creates_weigh_in_and_sets_water_goal(app, client):
    with app.app_context():
        create_user("p@user.test")
    login(client, "p@user.test")
    form = {"weight": "80", "goal_type": "weight_loss", "target_weight": "75",
            "activity_level": "moderate", "water_goal_ml": ""}
    client.post("/profile", data=form)  # unchanged weight → no weigh-in
    with app.app_context():
        assert WeightLog.query.count() == 0

    client.post("/profile", data={**form, "weight": "78.5", "water_goal_ml": "3100"})
    with app.app_context():
        log = WeightLog.query.one()
        assert (log.date, float(log.weight_kg)) == (date.today(), 78.5)
        assert Profile.query.filter_by(user_id=log.user_id).one().water_goal_ml == 3100

    client.post("/profile", data={**form, "weight": "78.5", "water_goal_ml": "99999"})
    client.post("/profile", data={**form, "weight": "78.5"})  # blank → back to automatic
    with app.app_context():
        assert Profile.query.filter_by(user_id=log.user_id).one().water_goal_ml is None


# --- streak -------------------------------------------------------------------------

def test_logging_streak_current_and_best(app):
    today = date.today()
    with app.app_context():
        uid = create_user("s@user.test").id
        assert logging_streak(uid, today) == {"current": 0, "best": 0}
        # Best run of 4 days ending 10 days ago, then a gap, then yesterday + the day before.
        for offset in (10, 11, 12, 13, 1, 2):
            _log_food(uid, today - timedelta(days=offset))
        _log_food(uid, today - timedelta(days=1))  # two logs on one day count once
        db.session.commit()
        # Nothing logged today yet: the streak still counts up to yesterday.
        assert logging_streak(uid, today) == {"current": 2, "best": 4}
        _log_food(uid, today)
        db.session.commit()
        assert logging_streak(uid, today) == {"current": 3, "best": 4}
        assert logging_streak(uid, today + timedelta(days=2))["current"] == 0


# --- pages --------------------------------------------------------------------------

def test_dashboard_renders_water_weight_and_streak(app, client):
    today = date.today()
    with app.app_context():
        user = create_user("d@user.test")
        _log_food(user.id, today)
        db.session.add(WaterLog(user_id=user.id, date=today, amount_ml=1250))
        db.session.add(WeightLog(user_id=user.id, date=today, weight_kg=79.3))
        db.session.commit()
    login(client, "d@user.test")
    html = client.get("/dashboard").get_data(as_text=True)
    assert "1-day streak" in html
    assert "1,250" in html and "2,650" in html
    assert "79.3" in html and 'id="weightSheet"' in html


def test_trainer_sees_client_habits_only_for_own_client(app):
    today = date.today()
    with app.app_context():
        trainer = create_user("t@user.test", role="trainer")
        other_trainer = create_user("t2@user.test", role="trainer")
        athlete = create_user("a@user.test", trainer=trainer)
        db.session.add(WaterLog(user_id=athlete.id, date=today, amount_ml=900))
        db.session.add(WeightLog(user_id=athlete.id, date=today, weight_kg=77.7))
        db.session.commit()
        athlete_id = athlete.id
        del other_trainer
    own = app.test_client()
    login(own, "t@user.test")
    html = own.get(f"/trainer/client/{athlete_id}").get_data(as_text=True)
    assert "Water today" in html and "900" in html and "77.7" in html

    stranger = app.test_client()
    login(stranger, "t2@user.test")
    resp = stranger.get(f"/trainer/client/{athlete_id}")
    assert resp.status_code == 302 and "77.7" not in resp.get_data(as_text=True)
