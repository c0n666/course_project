from datetime import datetime, timedelta

from conftest import create_user, login
from models import TrainerInvite, User, db


def _make(app):
    with app.app_context():
        trainer = create_user("coach@t.test", role="trainer")
        other = create_user("other@t.test", role="trainer")
        athlete = create_user("ath@a.test")
        return trainer.id, other.id, athlete.id


def _invite_code(app, trainer_id):
    with app.app_context():
        return TrainerInvite.query.filter_by(trainer_id=trainer_id, used_at=None).one().code


def _trainer_of(app, athlete_id):
    with app.app_context():
        return db.session.get(User, athlete_id).trainer_id


def _issue_code(app, client, email, trainer_id):
    login(client, email)
    client.post("/trainer/invite")
    code = _invite_code(app, trainer_id)
    client.get("/logout")
    return code


def test_trainer_creates_code_and_qr(app, client):
    trainer_id, _, _ = _make(app)
    login(client, "coach@t.test")
    assert client.get("/trainer/invite/qr.svg").status_code == 404
    assert client.post("/trainer/invite").status_code == 302
    code = _invite_code(app, trainer_id)
    assert len(code) == 8
    page = client.get("/trainer").get_data(as_text=True)
    assert f"{code[:4]}-{code[4:]}" in page
    qr = client.get("/trainer/invite/qr.svg")
    assert qr.status_code == 200 and qr.mimetype == "image/svg+xml"


def test_new_code_replaces_the_previous_one(app, client):
    trainer_id, _, _ = _make(app)
    login(client, "coach@t.test")
    client.post("/trainer/invite")
    first = _invite_code(app, trainer_id)
    client.post("/trainer/invite")
    assert _invite_code(app, trainer_id) != first


def test_athlete_connects_with_code(app, client):
    trainer_id, _, athlete_id = _make(app)
    code = _issue_code(app, client, "coach@t.test", trainer_id)

    login(client, "ath@a.test")
    # dashes and lowercase are fine
    resp = client.post("/profile/trainer/link", data={"code": f"{code[:4]}-{code[4:]}".lower()})
    assert resp.status_code == 302
    assert _trainer_of(app, athlete_id) == trainer_id
    with app.app_context():
        invite = TrainerInvite.query.filter_by(code=code).one()
        assert invite.used_at is not None and invite.used_by_id == athlete_id
    client.get("/logout")
    login(client, "coach@t.test")
    assert "ath@a.test" in client.get("/trainer").get_data(as_text=True)


def test_code_accepts_scanned_join_url(app, client):
    trainer_id, _, athlete_id = _make(app)
    code = _issue_code(app, client, "coach@t.test", trainer_id)
    login(client, "ath@a.test")
    client.post("/profile/trainer/link", data={"code": f"http://localhost/join/{code}"})
    assert _trainer_of(app, athlete_id) == trainer_id


def test_expired_and_unknown_codes_are_rejected(app, client):
    trainer_id, _, athlete_id = _make(app)
    code = _issue_code(app, client, "coach@t.test", trainer_id)
    with app.app_context():
        TrainerInvite.query.filter_by(code=code).one().expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()
    login(client, "ath@a.test")
    client.post("/profile/trainer/link", data={"code": code})
    client.post("/profile/trainer/link", data={"code": "ZZZZZZZZ"})
    client.post("/profile/trainer/link", data={"code": ""})
    assert _trainer_of(app, athlete_id) is None


def test_code_is_single_use(app, client):
    trainer_id, _, _ = _make(app)
    with app.app_context():
        second = create_user("ath2@a.test").id
    code = _issue_code(app, client, "coach@t.test", trainer_id)
    login(client, "ath@a.test")
    client.post("/profile/trainer/link", data={"code": code})
    client.get("/logout")
    login(client, "ath2@a.test")
    client.post("/profile/trainer/link", data={"code": code})
    assert _trainer_of(app, second) is None


def test_roles_are_enforced(app, client):
    _make(app)
    login(client, "ath@a.test")
    assert client.post("/trainer/invite").status_code == 302
    with app.app_context():
        assert TrainerInvite.query.count() == 0
    client.get("/logout")
    login(client, "coach@t.test")
    assert client.post("/profile/trainer/link", data={"code": "ABCDEFGH"}).status_code == 302
    assert client.post("/profile/trainer/unlink").status_code == 302


def test_link_attempts_are_rate_limited(app, client):
    trainer_id, _, athlete_id = _make(app)
    code = _issue_code(app, client, "coach@t.test", trainer_id)
    login(client, "ath@a.test")
    for _ in range(5):
        client.post("/profile/trainer/link", data={"code": "ZZZZZZZZ"})
    client.post("/profile/trainer/link", data={"code": code})
    assert _trainer_of(app, athlete_id) is None


def test_athlete_with_trainer_cannot_switch_but_can_disconnect(app, client):
    trainer_id, other_id, athlete_id = _make(app)
    with app.app_context():
        db.session.get(User, athlete_id).trainer_id = trainer_id
        db.session.commit()
    code = _issue_code(app, client, "other@t.test", other_id)
    login(client, "ath@a.test")
    client.post("/profile/trainer/link", data={"code": code})
    assert _trainer_of(app, athlete_id) == trainer_id
    client.post("/profile/trainer/unlink")
    assert _trainer_of(app, athlete_id) is None


def test_trainer_removes_only_own_client(app, client):
    trainer_id, _, athlete_id = _make(app)
    with app.app_context():
        db.session.get(User, athlete_id).trainer_id = trainer_id
        db.session.commit()
    login(client, "other@t.test")
    client.post(f"/trainer/client/{athlete_id}/unlink")
    assert _trainer_of(app, athlete_id) == trainer_id
    client.get("/logout")
    login(client, "coach@t.test")
    client.post(f"/trainer/client/{athlete_id}/unlink")
    assert _trainer_of(app, athlete_id) is None


def test_join_link_flow_and_safe_redirect(app, client):
    _make(app)
    resp = client.get("/join/ABCDEFGH")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert "next=" in resp.headers["Location"]
    resp = client.post(
        "/login", data={"email": "ath@a.test", "password": "secret123", "next": "/join/ABCDEFGH"}
    )
    assert resp.headers["Location"].endswith("/join/ABCDEFGH")
    resp = client.get("/join/ABCDEFGH")
    assert resp.headers["Location"].endswith("/profile?code=ABCDEFGH")
    assert 'value="ABCDEFGH"' in client.get("/profile?code=ABCDEFGH").get_data(as_text=True)
    client.get("/logout")
    resp = client.post(
        "/login", data={"email": "ath@a.test", "password": "secret123", "next": "//evil.test"}
    )
    assert "evil.test" not in resp.headers["Location"]


def test_migration_creates_invites_table(app):
    from sqlalchemy import text

    from app import _migrate_profile_goals_schema

    with app.app_context():
        db.session.execute(text("DROP TABLE trainer_invites"))
        db.session.commit()
        _migrate_profile_goals_schema()
        assert db.session.execute(text("SELECT count(*) FROM trainer_invites")).scalar() == 0


def test_profile_previews_the_trainer_behind_a_link(app, client):
    trainer_id, _, _ = _make(app)
    code = _issue_code(app, client, "coach@t.test", trainer_id)
    login(client, "ath@a.test")
    page = client.get(f"/profile?code={code}").get_data(as_text=True)
    assert "coach@t.test" in page
    assert "coach@t.test" not in client.get("/profile?code=ZZZZZZZZ").get_data(as_text=True)
