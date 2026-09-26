import re

import pytest

import coach_agent
from app import coach_markup
from conftest import create_user, login
from models import CoachMessage, db
from test_coach import FakeHTTP, gcall, gemini, groq

HEADERS = {"Accept": "application/json"}


@pytest.fixture
def athlete(app, client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    with app.app_context():
        user_id = create_user("chat@user.test").id
    login(client, "chat@user.test")
    return user_id


def _post(client, message):
    return client.post("/coach/chat", json={"message": message}, headers=HEADERS)


# --- chat turns -------------------------------------------------------------------------

def test_chat_uses_tools_and_stores_both_turns(app, client, athlete, monkeypatch):
    http = FakeHTTP(
        monkeypatch,
        gemini(gcall("get_daily_totals", {"days": 1})),
        gemini({"text": "Сьогодні лишилось **1200 ккал**.\n- Сир 200 г\n- Банан"}),
    )
    resp = _post(client, "Скільки калорій лишилось?")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["user"]["text"] == "Скільки калорій лишилось?"
    assert body["reply"]["html"] == ("<p>Сьогодні лишилось <strong>1200 ккал</strong>.</p>"
                                     "<ul><li>Сир 200 г</li><li>Банан</li></ul>")
    with app.app_context():
        assert [(m.role, m.user_id) for m in CoachMessage.query.order_by(CoachMessage.id)] == [
            ("user", athlete), ("assistant", athlete)]
    tools = {d["name"] for d in http.requests[0]["payload"]["tools"][0]["functionDeclarations"]}
    assert "get_latest_analysis" in tools and "submit_report" not in tools


def test_earlier_turns_are_sent_as_history(app, client, athlete, monkeypatch):
    http = FakeHTTP(monkeypatch, gemini({"text": "Перша відповідь"}), gemini({"text": "Друга відповідь"}))
    _post(client, "Перше питання")
    _post(client, "Друге питання")
    contents = http.requests[1]["payload"]["contents"]
    assert [(c["role"], c["parts"][0]["text"]) for c in contents] == [
        ("user", "Перше питання"), ("model", "Перша відповідь"), ("user", "Друге питання")]


def test_groq_history_roles(app, client, athlete, monkeypatch):
    monkeypatch.setenv("COACH_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    http = FakeHTTP(monkeypatch, groq(content="A1", finish="stop"), groq(content="A2", finish="stop"))
    _post(client, "Q1")
    _post(client, "Q2")
    roles = [(m["role"], m["content"]) for m in http.requests[1]["payload"]["messages"][1:]]
    assert roles == [("user", "Q1"), ("assistant", "A1"), ("user", "Q2")]


def test_empty_reply_is_nudged_once(app, client, athlete, monkeypatch):
    http = FakeHTTP(monkeypatch, gemini({"text": ""}), gemini({"text": "Готово"}))
    assert _post(client, "Hi").status_code == 200
    assert "answer" in http.requests[1]["payload"]["contents"][-1]["parts"][0]["text"]


# --- errors -----------------------------------------------------------------------------

@pytest.mark.parametrize("message", ["", "   ", "x" * (coach_agent.CHAT_MAX_CHARS + 1)])
def test_invalid_messages_are_rejected(app, client, athlete, monkeypatch, message):
    http = FakeHTTP(monkeypatch)
    assert _post(client, message).status_code == 400
    assert http.requests == []


def test_provider_failure_saves_nothing(app, client, athlete, monkeypatch):
    FakeHTTP(monkeypatch, {"promptFeedback": {"blockReason": "SAFETY"}})
    resp = _post(client, "Hi")
    assert resp.status_code == 502 and "try again" in resp.get_json()["error"]
    with app.app_context():
        assert CoachMessage.query.count() == 0


def test_without_a_key_the_chat_is_disabled(app, client, monkeypatch):
    http = FakeHTTP(monkeypatch)
    with app.app_context():
        create_user("nokey@user.test")
    login(client, "nokey@user.test")
    page = client.get("/coach/chat").get_data(as_text=True)
    assert "not connected yet" in page and re.search(r'<textarea[^>]*disabled', page)
    assert _post(client, "Hi").status_code == 503
    assert http.requests == []


def test_daily_limit(app, client, athlete, monkeypatch):
    monkeypatch.setattr(coach_agent, "CHAT_DAILY_LIMIT", 1)
    FakeHTTP(monkeypatch, gemini({"text": "ok"}))
    assert _post(client, "one").status_code == 200
    resp = _post(client, "two")
    assert resp.status_code == 429 and "limit" in resp.get_json()["error"]


# --- pages & privacy --------------------------------------------------------------------

def test_page_shows_only_own_history_and_clear(app, client, athlete, monkeypatch):
    with app.app_context():
        other = create_user("other@user.test").id
        db.session.add_all([
            CoachMessage(user_id=athlete, role="user", content="My question"),
            CoachMessage(user_id=athlete, role="assistant", content="**Bold** answer"),
            CoachMessage(user_id=other, role="user", content="Someone else's secret"),
        ])
        db.session.commit()
    page = client.get("/coach/chat").get_data(as_text=True)
    assert "My question" in page and "<strong>Bold</strong> answer" in page
    assert "secret" not in page

    client.post("/coach/chat/clear")
    with app.app_context():
        assert [m.content for m in CoachMessage.query.all()] == ["Someone else's secret"]


def test_form_post_without_js_redirects_back(app, client, athlete, monkeypatch):
    FakeHTTP(monkeypatch, gemini({"text": "Відповідь"}))
    resp = client.post("/coach/chat", data={"message": "Питання"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/coach/chat#latest")
    assert "Відповідь" in client.get("/coach/chat").get_data(as_text=True)


def test_prefill_and_trainer_redirect(app, client, athlete):
    page = client.get("/coach/chat?q=Ідея перекусу").get_data(as_text=True)
    assert re.search(r"<textarea[^>]*>Ідея перекусу</textarea>", page)
    trainer = app.test_client()
    with app.app_context():
        create_user("t@trainer.test", role="trainer")
    login(trainer, "t@trainer.test")
    assert trainer.get("/coach/chat").status_code == 302


def test_latest_analysis_tool(app, athlete, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    with app.app_context():
        tools = coach_agent.CoachTools(athlete)
        assert '"analysis": null' in tools.run("get_latest_analysis", {})
        coach_agent.generate_coach_report(athlete)  # no key → rule-based report
        assert '"summary"' in tools.run("get_latest_analysis", {})


# --- markup -----------------------------------------------------------------------------

def test_coach_markup_escapes_and_formats():
    html = str(coach_markup("# Plan\n<img src=x onerror=alert(1)> **ok**\n\n1. one\n2) two\n\nend"))
    assert "<img" not in html and "&lt;img" in html
    assert html == ("<p>Plan<br>&lt;img src=x onerror=alert(1)&gt; <strong>ok</strong></p>"
                    "<ol><li>one</li><li>two</li></ol><p>end</p>")
