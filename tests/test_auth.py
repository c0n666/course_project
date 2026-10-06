from conftest import create_user, login
from models import Goal, User, db


def _athlete_form(email="new@a.test", **extra):
    data = {
        "email": email,
        "password": "secret123",
        "role": "user",
        "gender": "female",
        "birth_date": "1998-05-20",
        "height": "168",
        "weight": "62",
        "activity_level": "light",
        "goal_type": "weight_loss",
        "target_weight": "58",
        "consent": "1",
    }
    data.update(extra)
    return data


# --- registration ----------------------------------------------------------------

def test_register_signs_in_and_records_consent(app, client):
    resp = client.post("/register", data=_athlete_form())
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/")
    assert client.get("/dashboard").status_code == 200
    with app.app_context():
        assert User.query.filter_by(email="new@a.test").one().consented_at is not None


def test_register_trainer_needs_only_step_one(app, client):
    resp = client.post("/register", data={
        "email": "t@t.test", "password": "secret123", "role": "trainer", "consent": "1",
    })
    assert resp.status_code == 302
    assert client.get("/trainer").status_code == 200


def test_register_error_keeps_values_and_opens_the_right_step(app, client):
    resp = client.post("/register", data=_athlete_form(height="20"))
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert 'value="new@a.test"' in page and 'value="62"' in page and 'value="58"' in page
    assert 'data-start-step="2"' in page
    assert "Height must be between 100 and 250 cm." in page
    with app.app_context():
        assert User.query.filter_by(email="new@a.test").first() is None


def test_register_existing_email_links_to_sign_in(app, client):
    with app.app_context():
        create_user("taken@a.test")
    page = client.post("/register", data=_athlete_form("taken@a.test")).get_data(as_text=True)
    assert "An account with this email already exists." in page
    assert 'href="/login"' in page


def test_register_requires_consent_and_long_password(app, client):
    form = _athlete_form(password="short1")
    form.pop("consent")
    page = client.post("/register", data=form).get_data(as_text=True)
    assert "Password must be at least 8 characters." in page
    assert "Please agree to the processing of your data." in page


def test_register_target_must_follow_the_goal(app, client):
    page = client.post("/register", data=_athlete_form(target_weight="70")).get_data(as_text=True)
    assert "To lose weight, the target must be below your current weight." in page
    page = client.post(
        "/register", data=_athlete_form(goal_type="muscle_gain", target_weight="60")
    ).get_data(as_text=True)
    assert "To gain muscle, the target must be above your current weight." in page
    resp = client.post("/register", data=_athlete_form(goal_type="maintenance", target_weight="62"))
    assert resp.status_code == 302


def test_register_has_no_preselected_body_answers(client):
    page = client.get("/register").get_data(as_text=True)
    for name in ("gender", "activity_level", "goal_type"):
        assert f'name="{name}" value="' in page
        assert not __import__("re").search(rf'name="{name}" value="[^"]+" class="[^"]*" checked', page)


def test_register_from_trainer_link_returns_to_join(app, client):
    page = client.get("/register?next=/join/ABCDEFGH").get_data(as_text=True)
    assert 'name="next" value="/join/ABCDEFGH"' in page
    resp = client.post("/register", data=_athlete_form(next="/join/ABCDEFGH"))
    assert resp.headers["Location"].endswith("/join/ABCDEFGH")


def test_targets_preview(client):
    resp = client.get("/api/targets-preview?gender=female&birth_date=1998-05-20&height=168"
                      "&weight=62&activity_level=light&goal_type=weight_loss")
    data = resp.get_json()
    assert data["ok"] and 1200 <= data["calories"] <= 2500 and data["proteins"] > 0
    assert client.get("/api/targets-preview?height=168").get_json() == {"ok": False}


# --- sign in ------------------------------------------------------------------------

def test_failed_sign_in_keeps_email_and_shows_error_by_the_field(app, client):
    with app.app_context():
        create_user("a@a.test")
    resp = client.post("/login", data={"email": "a@a.test", "password": "wrong"})
    assert resp.status_code == 401
    page = resp.get_data(as_text=True)
    assert 'value="a@a.test"' in page and 'id="loginError"' in page


def test_sign_in_is_remembered(app, client):
    with app.app_context():
        create_user("a@a.test")
    resp = login(client, "a@a.test")
    assert "remember_token" in " ".join(resp.headers.getlist("Set-Cookie"))


def test_sign_in_is_rate_limited(app, client):
    with app.app_context():
        create_user("a@a.test")
    for _ in range(5):
        client.post("/login", data={"email": "a@a.test", "password": "wrong"})
    resp = login(client, "a@a.test")
    assert resp.status_code == 401
    assert "Too many attempts" in resp.get_data(as_text=True)


def test_sign_in_page_explains_a_trainer_invite(client):
    page = client.get("/login?next=/join/ABCDEFGH").get_data(as_text=True)
    assert "connect to your trainer" in page
    assert "/register?next=/join/ABCDEFGH" in page.replace("%2F", "/")


# --- password ---------------------------------------------------------------------------

def test_change_password(app, client):
    with app.app_context():
        create_user("a@a.test")
    login(client, "a@a.test")
    resp = client.post("/account/password", data={"current_password": "nope", "new_password": "newpass123"})
    assert resp.status_code == 400
    resp = client.post("/account/password", data={"current_password": "secret123", "new_password": "newpass123"})
    assert resp.status_code == 302
    client.get("/logout")
    assert login(client, "a@a.test", "newpass123").status_code == 302


def test_admin_resets_a_password_from_the_cli(app, client):
    with app.app_context():
        create_user("a@a.test")
    result = app.test_cli_runner().invoke(args=["reset-password", "A@a.test"])
    assert result.exit_code == 0
    temporary = result.output.strip().rsplit(" ", 1)[-1]
    assert login(client, "a@a.test", temporary).status_code == 302


def test_profile_goal_change_checks_direction(app, client):
    with app.app_context():
        user_id = create_user("a@a.test").id  # weight 80
    login(client, "a@a.test")
    client.post("/profile", data={"weight": "80", "goal_type": "muscle_gain", "target_weight": "75"})
    with app.app_context():
        assert Goal.query.filter_by(user_id=user_id, status="active").one().goal_type == "weight_loss"


def test_migration_adds_consent_column(app):
    from sqlalchemy import text

    from app import _migrate_profile_goals_schema

    with app.app_context():
        db.session.execute(text("ALTER TABLE users DROP COLUMN consented_at"))
        db.session.commit()
        _migrate_profile_goals_schema()
        cols = {row[1] for row in db.session.execute(text("PRAGMA table_info(users)"))}
        assert "consented_at" in cols
