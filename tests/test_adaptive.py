from datetime import date, timedelta

from conftest import create_user, login
from models import FoodLog, Goal, Product, Profile, WeightLog, db
from nutrition import calculate_daily_targets, predict_weight_trend, weekly_checkin


def _product_2000():
    """A 2000 kcal/100 g test product, so a 100 g portion is 2000 kcal."""
    p = Product(name="Test 2000", calories_per_100g=2000, proteins=0, fats=0, carbs=0)
    db.session.add(p)
    db.session.flush()
    return p


def _seed_history(user_id, days_logged=10, kcal_per_day=2000, start_kg=80.0, end_kg=79.0, span=10):
    today = date.today()
    product = _product_2000()
    for offset in range(1, days_logged + 1):
        db.session.add(FoodLog(user_id=user_id, date=today - timedelta(days=offset), meal_type="lunch",
                               product_id=product.id, portion_grams=kcal_per_day / 20))
    db.session.add(WeightLog(user_id=user_id, date=today - timedelta(days=span), weight_kg=start_kg))
    db.session.add(WeightLog(user_id=user_id, date=today, weight_kg=end_kg))
    db.session.commit()


def _profile(uid):
    return Profile.query.filter_by(user_id=uid).one()


def _goal(uid):
    return Goal.query.filter_by(user_id=uid, status="active").first()


# --- weekly_checkin ---------------------------------------------------------------

def test_checkin_needs_logged_days_and_weigh_in_span(app):
    with app.app_context():
        uid = create_user("n@user.test").id
        _seed_history(uid, days_logged=6, span=5)
        c = weekly_checkin(uid, _profile(uid), _goal(uid))
        assert c["ready"] is False
        assert (c["days_logged"], c["weigh_ins"], c["weigh_in_span"]) == (6, 2, 5)


def test_checkin_real_tdee_and_suggested_target(app):
    with app.app_context():
        uid = create_user("t@user.test").id  # goal: weight loss
        # 2000 kcal/day while losing 1 kg in 10 days → 2000 + 7700/10 = 2770 kcal.
        _seed_history(uid)
        c = weekly_checkin(uid, _profile(uid), _goal(uid))
        assert c["ready"] is True
        assert (c["avg_intake"], c["weight_change"], c["real_tdee"]) == (2000, -1.0, 2770)
        assert c["suggested_target"] == 2270  # weight loss: −500


def test_checkin_ignores_todays_open_log(app):
    with app.app_context():
        uid = create_user("o@user.test").id
        _seed_history(uid)
        db.session.add(FoodLog(user_id=uid, date=date.today(), meal_type="snack",
                               product_id=Product.query.filter_by(name="Test 2000").one().id,
                               portion_grams=5))
        db.session.commit()
        assert weekly_checkin(uid, _profile(uid), _goal(uid))["avg_intake"] == 2000


# --- targets ------------------------------------------------------------------------

def test_override_replaces_calories_and_rescales_macros(app):
    with app.app_context():
        uid = create_user("m@user.test").id
        profile, goal = _profile(uid), _goal(uid)
        calculated = calculate_daily_targets(profile, goal)
        assert calculated["is_adaptive"] is False
        profile.calorie_target_override = 2000
        t = calculate_daily_targets(profile, goal)
        assert (t["calories"], t["calculated_calories"], t["is_adaptive"]) == (
            2000, calculated["calories"], True)
        assert (t["proteins"], t["fats"], t["carbs"]) == (150.0, 55.6, 225.0)  # 30/25/45 %


def test_apply_and_reset_adaptive_target(app, client):
    with app.app_context():
        uid = create_user("a@user.test").id
    login(client, "a@user.test")
    assert client.post("/targets/adaptive").status_code == 302
    with app.app_context():
        assert _profile(uid).calorie_target_override is None  # not enough data yet
        _seed_history(uid)

    # The form never sends a number; the server recomputes it.
    client.post("/targets/adaptive", data={"calories": "900"})
    with app.app_context():
        assert _profile(uid).calorie_target_override == 2270
    html = client.get("/dashboard").get_data(as_text=True)
    assert "Weekly check-in" in html and "Adaptive target from check-in" in html

    client.post("/targets/adaptive", data={"action": "reset"})
    with app.app_context():
        assert _profile(uid).calorie_target_override is None


def test_changing_goal_type_clears_override(app, client):
    with app.app_context():
        uid = create_user("g@user.test").id
        _profile(uid).calorie_target_override = 2100
        db.session.commit()
    login(client, "g@user.test")
    client.post("/profile", data={"weight": "80", "goal_type": "weight_loss", "target_weight": "74",
                                  "activity_level": "moderate"})
    with app.app_context():
        assert _profile(uid).calorie_target_override == 2100  # same goal type: kept
    client.post("/profile", data={"weight": "80", "goal_type": "muscle_gain", "target_weight": "85",
                                  "activity_level": "moderate"})
    with app.app_context():
        assert _profile(uid).calorie_target_override is None


# --- weight chart -------------------------------------------------------------------

def test_weight_chart_uses_real_weigh_ins(app):
    today = date.today()
    with app.app_context():
        uid = create_user("c@user.test").id
        assert predict_weight_trend(uid)["meta"]["actual_source"] == "estimate"
        db.session.add(WeightLog(user_id=uid, date=today - timedelta(days=3), weight_kg=80.4))
        db.session.add(WeightLog(user_id=uid, date=today, weight_kg=79.8))
        db.session.commit()
        trend = predict_weight_trend(uid)
        assert trend["meta"]["actual_source"] == "weigh_ins"
        assert trend["actual"][:7] == [None, None, None, 80.4, None, None, 79.8]
