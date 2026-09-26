import re
from datetime import date

from conftest import create_user, login
from models import CoachReport, FoodLog, Product, Recommendation, Report, Workout, db


def _setup_trainers(app):
    with app.app_context():
        trainer_a = create_user("a@trainer.test", role="trainer")
        trainer_b = create_user("b@trainer.test", role="trainer")
        client_b = create_user("client@b.test", trainer=trainer_b)
        report = Report(user_id=client_b.id, nutrition_summary="x", ai_grade="B")
        db.session.add(report)
        db.session.flush()
        rec = Recommendation(report_id=report.id, content="original", status="pending")
        db.session.add(rec)
        db.session.commit()
        return client_b.id, rec.id


# --- trainer access control -------------------------------------------------

def test_trainer_cannot_view_other_trainers_client(app, client):
    client_id, _ = _setup_trainers(app)
    login(client, "a@trainer.test")
    resp = client.get(f"/trainer/client/{client_id}")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/trainer")
    page = client.get("/trainer").get_data(as_text=True)
    assert "client@b.test" not in page


def test_trainer_cannot_recommend_to_other_trainers_client(app, client):
    client_id, _ = _setup_trainers(app)
    login(client, "a@trainer.test")
    resp = client.post(f"/trainer/client/{client_id}/recommend", data={"content": "spam"})
    assert resp.status_code == 302
    with app.app_context():
        assert Recommendation.query.filter_by(content="spam").count() == 0


def test_trainer_cannot_approve_other_trainers_recommendation(app, client):
    _, rec_id = _setup_trainers(app)
    login(client, "a@trainer.test")
    client.post(f"/trainer/recommendation/approve/{rec_id}", data={"content": "hijacked"})
    with app.app_context():
        rec = db.session.get(Recommendation, rec_id)
        assert rec.status == "pending"
        assert rec.content == "original"


def test_own_trainer_can_view_and_approve(app, client):
    client_id, rec_id = _setup_trainers(app)
    login(client, "b@trainer.test")
    resp = client.get(f"/trainer/client/{client_id}")
    assert resp.status_code == 200
    assert "client@b.test" in resp.get_data(as_text=True)
    client.post(f"/trainer/recommendation/approve/{rec_id}", data={"content": "ok"})
    with app.app_context():
        assert db.session.get(Recommendation, rec_id).status == "approved"


def test_athlete_cannot_open_trainer_pages(app, client):
    client_id, _ = _setup_trainers(app)
    login(client, "client@b.test")
    assert client.get("/trainer").status_code == 302
    assert client.get(f"/trainer/client/{client_id}").status_code == 302


def test_register_rejects_non_trainer_as_trainer(app, client):
    with app.app_context():
        other = create_user("other@user.test")
        other_id = other.id
    resp = client.post("/register", data=_register_form("x@y.test", trainer_id=str(other_id)))
    assert resp.status_code == 200
    with app.app_context():
        from models import User
        assert User.query.filter_by(email="x@y.test").first() is None


# --- smoke flow ---------------------------------------------------------------

def _register_form(email, **extra):
    data = {
        "email": email,
        "password": "secret123",
        "confirm_password": "secret123",
        "role": "user",
        "gender": "female",
        "birth_date": "1998-05-20",
        "height": "168",
        "weight": "62",
        "activity_level": "light",
        "goal_type": "maintenance",
        "target_weight": "60",
        "trainer_id": "",
    }
    data.update(extra)
    return data


def test_smoke_register_login_log_food_dashboard(app, client):
    assert client.get("/register").status_code == 200
    resp = client.post("/register", data=_register_form("new@user.test"))
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")

    resp = login(client, "new@user.test")
    assert resp.status_code == 302

    with app.app_context():
        product_id = Product.query.first().id

    assert client.get("/log-food").status_code == 200
    resp = client.post(
        "/log-food",
        data={"product_id": str(product_id), "portion_grams": "150", "meal_type": "lunch",
              "date": date.today().isoformat()},
    )
    assert resp.status_code == 302

    resp = client.post("/log-workout", data={"type": "Run", "duration_minutes": "30",
                                             "calories_burned": "250"})
    assert resp.status_code == 302

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Run" in body
    assert client.get("/profile").status_code == 200

    resp = client.post("/dashboard/ai-report")
    assert resp.status_code == 302
    with app.app_context():
        assert FoodLog.query.count() == 1
        assert Workout.query.count() == 1
        assert CoachReport.query.filter_by(engine="local").count() == 1


def test_workout_on_past_date_shows_on_that_day(app, client):
    with app.app_context():
        create_user("w@user.test")
    login(client, "w@user.test")
    client.post("/log-workout", data={"type": "Swim", "duration_minutes": "20", "date": "2024-03-10"})
    assert "Swim" in client.get("/dashboard?date=2024-03-10").get_data(as_text=True)


def test_log_food_validation(app, client):
    with app.app_context():
        create_user("v@user.test")
        product_id = Product.query.first().id
    login(client, "v@user.test")
    bad_inputs = [
        {"product_id": str(product_id), "portion_grams": "0"},
        {"product_id": str(product_id), "portion_grams": "-5"},
        {"product_id": str(product_id), "portion_grams": "nan"},
        {"product_id": str(product_id), "portion_grams": "abc"},
        {"product_id": "abc", "portion_grams": "100"},
        {"product_id": "99999", "portion_grams": "100"},
        {"product_id": str(product_id), "portion_grams": "100", "date": "2024-13-45"},
        {"product_id": str(product_id), "portion_grams": "100", "meal_type": "<script>"},
    ]
    for data in bad_inputs:
        data.setdefault("meal_type", "lunch")
        resp = client.post("/log-food", data=data)
        assert resp.status_code == 400, data
    with app.app_context():
        assert FoodLog.query.count() == 0


def test_delete_food_log_rejects_offsite_next(app, client):
    with app.app_context():
        user = create_user("d@user.test")
        entry = FoodLog(user_id=user.id, date=date.today(), meal_type="lunch",
                        product_id=Product.query.first().id, portion_grams=100)
        db.session.add(entry)
        db.session.commit()
        entry_id = entry.id
    login(client, "d@user.test")
    resp = client.post(f"/log-food/delete/{entry_id}", data={"next": "/\\evil.example"})
    assert "evil" not in resp.headers["Location"]


def test_athlete_cannot_delete_other_users_food_log(app, client):
    with app.app_context():
        owner = create_user("owner@user.test")
        create_user("intruder@user.test")
        entry = FoodLog(user_id=owner.id, date=date.today(), meal_type="lunch",
                        product_id=Product.query.first().id, portion_grams=100)
        db.session.add(entry)
        db.session.commit()
        entry_id = entry.id
    login(client, "intruder@user.test")
    client.post(f"/log-food/delete/{entry_id}")
    with app.app_context():
        assert db.session.get(FoodLog, entry_id) is not None


# --- CSRF -------------------------------------------------------------------------

def test_csrf_blocks_post_without_token(csrf_app):
    with csrf_app.app_context():
        create_user("c@user.test")
    client = csrf_app.test_client()
    resp = login(client, "c@user.test")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/")
    # not logged in: dashboard still redirects to login
    assert "/login" in client.get("/dashboard").headers["Location"]

    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    resp = client.post("/login", data={"email": "c@user.test", "password": "secret123",
                                       "csrf_token": token})
    assert resp.status_code == 302
    assert client.get("/dashboard").status_code == 200


def test_service_worker_served_from_root_with_full_scope(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert resp.mimetype == "application/javascript"
    assert resp.headers["Service-Worker-Allowed"] == "/"
    assert "no-cache" in resp.headers["Cache-Control"]
    assert b"addEventListener('fetch'" in resp.data
    resp.close()
