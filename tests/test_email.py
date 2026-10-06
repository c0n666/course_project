import re
from datetime import datetime, timedelta

from conftest import create_user, login
from models import TrainerInvite, User, db


def _outbox(app):
    return app.config.setdefault("MAIL_OUTBOX", [])


def _link(mail, path):
    match = re.search(rf"http://localhost(/{path}/[^\s\"<]+)", mail.text)
    assert match, mail.text
    return match.group(1)


# --- password reset -------------------------------------------------------------------

def test_reset_password_by_email(app, client):
    with app.app_context():
        create_user("a@a.test")
    resp = client.post("/password/forgot", data={"email": "A@a.test"})
    assert resp.status_code == 200 and "a@a.test" in resp.get_data(as_text=True)
    (mail,) = _outbox(app)
    assert mail.to == "a@a.test" and "<a href=" in mail.html
    link = _link(mail, "password/reset")

    assert client.get(link).status_code == 200
    assert client.post(link, data={"new_password": "short"}).status_code == 400
    resp = client.post(link, data={"new_password": "brandnew123"})
    assert resp.status_code == 302
    assert client.get("/dashboard").status_code == 200  # signed in
    client.get("/logout")
    assert login(client, "a@a.test", "brandnew123").status_code == 302
    # single use: the link stops working once the password changed
    assert client.get(link).status_code == 400


def test_reset_does_not_reveal_unknown_accounts(app, client):
    resp = client.post("/password/forgot", data={"email": "nobody@a.test"})
    assert resp.status_code == 200
    assert "nobody@a.test" in resp.get_data(as_text=True)
    assert _outbox(app) == []


def test_reset_link_expires(app, client, monkeypatch):
    with app.app_context():
        create_user("a@a.test")
    client.post("/password/forgot", data={"email": "a@a.test"})
    link = _link(_outbox(app)[0], "password/reset")
    import app as app_module
    monkeypatch.setattr(app_module, "RESET_TOKEN_MAX_AGE", -1)
    assert client.get(link).status_code == 400
    assert client.get("/password/reset/garbage").status_code == 400


def test_reset_requests_are_rate_limited(app, client):
    with app.app_context():
        create_user("a@a.test")
    for _ in range(5):
        client.post("/password/forgot", data={"email": "a@a.test"})
    assert len(_outbox(app)) == 3


def test_reset_email_uses_the_users_language(app, client):
    with app.app_context():
        user = create_user("a@a.test")
        user.language = "uk"
        db.session.commit()
    client.post("/password/forgot", data={"email": "a@a.test"})
    assert _outbox(app)[0].subject == "Відновлення пароля Kolos"


# --- email confirmation ---------------------------------------------------------------

def test_sign_up_sends_a_confirmation_link(app, client):
    client.post("/register", data={
        "email": "t@t.test", "password": "secret123", "role": "trainer", "consent": "1",
    })
    with app.app_context():
        assert User.query.filter_by(email="t@t.test").one().email_verified_at is None
    assert 'id="confirm-email-title"' in client.get("/trainer").get_data(as_text=True)
    # unconfirmed trainers cannot invite athletes yet
    client.post("/trainer/invite")
    with app.app_context():
        assert TrainerInvite.query.count() == 0

    (mail,) = _outbox(app)
    resp = client.get(_link(mail, "email/confirm"))
    assert resp.status_code == 302
    with app.app_context():
        assert User.query.filter_by(email="t@t.test").one().email_verified_at is not None
    assert 'id="confirm-email-title"' not in client.get("/trainer").get_data(as_text=True)
    client.post("/trainer/invite")
    with app.app_context():
        assert TrainerInvite.query.count() == 1


def test_confirmation_link_is_bound_to_the_address(app, client):
    with app.app_context():
        create_user("a@a.test", verified=False)
    login(client, "a@a.test")
    client.post("/email/confirm/resend")
    link = _link(_outbox(app)[0], "email/confirm")
    with app.app_context():
        User.query.filter_by(email="a@a.test").one().email = "b@a.test"
        db.session.commit()
    client.get(link)
    with app.app_context():
        assert User.query.filter_by(email="b@a.test").one().email_verified_at is None


def test_resend_is_rate_limited_and_skipped_when_confirmed(app, client):
    with app.app_context():
        create_user("a@a.test", verified=False)
        create_user("ok@a.test")
    login(client, "a@a.test")
    for _ in range(5):
        client.post("/email/confirm/resend")
    assert len(_outbox(app)) == 3
    client.get("/logout")
    login(client, "ok@a.test")
    client.post("/email/confirm/resend")
    assert len(_outbox(app)) == 3


def test_reset_link_also_confirms_the_email(app, client):
    with app.app_context():
        create_user("a@a.test", verified=False)
    client.post("/password/forgot", data={"email": "a@a.test"})
    client.post(_link(_outbox(app)[0], "password/reset"), data={"new_password": "brandnew123"})
    with app.app_context():
        assert User.query.filter_by(email="a@a.test").one().email_verified_at is not None


def test_migration_treats_existing_accounts_as_confirmed(app):
    from sqlalchemy import text

    from app import _migrate_profile_goals_schema

    with app.app_context():
        create_user("old@a.test", verified=False)
        db.session.execute(text("ALTER TABLE users DROP COLUMN email_verified_at"))
        db.session.commit()
        _migrate_profile_goals_schema()
        assert db.session.execute(
            text("SELECT email_verified_at FROM users WHERE email='old@a.test'")
        ).scalar() is not None


def test_console_mail_when_smtp_is_not_configured(app, monkeypatch, capsys):
    import mailer

    monkeypatch.delenv("MAIL_SERVER", raising=False)
    app.config["TESTING"] = False
    try:
        with app.test_request_context():
            assert mailer.send(mailer.Mail("x@a.test", "Hi", "body text", "<p>body</p>"))
    finally:
        app.config["TESTING"] = True
    assert "body text" in capsys.readouterr().out
