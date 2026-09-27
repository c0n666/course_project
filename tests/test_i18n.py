import json
import re
from datetime import date
from pathlib import Path

from babel.messages.pofile import read_po

import coach_agent
from conftest import create_user, login
from i18n import format_local_date
from models import FoodLog, Product, User, db

UK = {"Accept-Language": "uk-UA,uk;q=0.9,en;q=0.5"}
PO = Path(__file__).resolve().parent.parent / "translations" / "uk" / "LC_MESSAGES" / "messages.po"


def _set_lang(client, lang, next_url="/login"):
    return client.post("/language", data={"lang": lang, "next": next_url})


# --- language selection ---------------------------------------------------------------

def test_english_by_default_and_ukrainian_from_browser(client):
    assert "Welcome back" in client.get("/login").get_data(as_text=True)
    page = client.get("/login", headers=UK).get_data(as_text=True)
    assert "З поверненням" in page and '<html lang="uk"' in page


def test_switcher_sets_cookie_and_overrides_browser(client):
    resp = _set_lang(client, "uk", "/register")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/register")
    assert "lang=uk" in resp.headers["Set-Cookie"]
    assert "Створіть акаунт" in client.get("/register").get_data(as_text=True)
    _set_lang(client, "en")
    assert "Create your account" in client.get("/register", headers=UK).get_data(as_text=True)


def test_invalid_language_and_unsafe_next(client):
    resp = client.post("/language", data={"lang": "ru", "next": "//evil.example"})
    assert resp.headers["Location"].endswith("/") and "lang=en" in resp.headers["Set-Cookie"]


def test_choice_is_saved_on_the_account(app, client):
    with app.app_context():
        create_user("lang@user.test")
    login(client, "lang@user.test")
    _set_lang(client, "uk", "/dashboard")
    with app.app_context():
        assert User.query.filter_by(email="lang@user.test").one().language == "uk"
    other_device = app.test_client()  # no cookie, English browser
    login(other_device, "lang@user.test")
    assert "Харчування" in other_device.get("/dashboard").get_data(as_text=True)


# --- translated output ------------------------------------------------------------------

def test_flash_messages_are_translated(client):
    resp = client.post("/login", data={"email": "x@y.z", "password": "wrong"}, headers=UK)
    assert "Неправильна пошта або пароль." in resp.get_data(as_text=True)


def test_dashboard_dates_plurals_and_js_strings(app, client):
    today = date.today()
    with app.app_context():
        user = create_user("uk@user.test")
        product = Product.query.first()
        for offset in range(5):
            db.session.add(FoodLog(user_id=user.id, date=date.fromordinal(today.toordinal() - offset),
                                   meal_type="lunch", product_id=product.id, portion_grams=100))
        db.session.commit()
    login(client, "uk@user.test")
    _set_lang(client, "uk", "/dashboard")
    html = client.get("/dashboard").get_data(as_text=True)
    with app.test_request_context(headers=UK):
        from flask_babel import force_locale
        with force_locale("uk"):
            assert format_local_date(today, "full") in html  # e.g. "субота, 26 вересня"
    assert "5 днів поспіль" in html
    i18n = json.loads(re.search(r'<script type="application/json" id="i18n">(.*?)</script>', html, re.S).group(1))
    assert i18n["Delete"] == "Видалити" and i18n["Found: {name}"] == "Знайдено: {name}"


def test_date_styles():
    d = date(2026, 9, 26)
    assert format_local_date(d, "full") == "Saturday, September 26"  # no request: English
    assert format_local_date(d, "short") == "Sep 26"


# --- the coach follows the language -----------------------------------------------------

def test_coach_prompts_and_basic_report_follow_language(app):
    assert "Write all text for the athlete in Ukrainian" in coach_agent.system_prompt("uk")
    assert "Answer in English" in coach_agent.chat_system_prompt("en")
    with app.app_context():
        uid = create_user("coach@lang.test").id
        report = coach_agent.generate_coach_report(uid, lang="uk")
        assert report.engine == "local"
        assert "записів" in report.summary  # Ukrainian rule-based text
        english = coach_agent.generate_coach_report(uid, lang="en")
        assert "diary entries" in english.summary


# --- catalogue health -------------------------------------------------------------------

def test_every_string_is_translated():
    with PO.open("rb") as f:
        catalog = read_po(f)
    untranslated = [m.id for m in catalog if m.id and (not m.string or (isinstance(m.string, tuple) and not all(m.string)))]
    assert untranslated == []
    assert PO.with_suffix(".mo").exists()
