import copy
import io
import json
import re
import urllib.error
from datetime import date

import pytest

import coach_agent
import llm_providers
from conftest import create_user, login
from models import SOURCE_USER, CoachReport, FoodLog, Product, db


# --- a scripted stand-in for the providers' HTTP endpoints -------------------------------

class FakeHTTP:
    """Replaces llm_providers._post_json: returns scripted native responses, records requests."""

    def __init__(self, monkeypatch, *responses):
        self.responses = list(responses)
        self.requests = []
        monkeypatch.setattr(llm_providers, "_post_json", self)

    def __call__(self, url, payload, headers):
        self.requests.append({"url": url, "payload": copy.deepcopy(payload), "headers": headers})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def gemini(*parts, finish="STOP"):
    return {"candidates": [{"content": {"role": "model", "parts": list(parts)}, "finishReason": finish}]}


def gcall(name, args=None, **extra):
    return {"functionCall": {"name": name, "args": args or {}}, **extra}


def groq(*calls, content=None, finish="tool_calls"):
    message = {"role": "assistant", "content": content, "reasoning": "internal notes"}
    if calls:
        message["tool_calls"] = [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
            for cid, name, args in calls
        ]
    return {"choices": [{"message": message, "finish_reason": finish}]}


def _report(**overrides):
    base = {
        "summary": "Protein is low.", "grade": "B", "highlights": ["Logged every day"],
        "issues": ["Protein 60% of target"], "changes": [{"title": "Add protein at breakfast", "detail": "30 g."}],
        "foods": [], "dishes": [],
    }
    base.update(overrides)
    return base


def _product(name, kcal=100, p=10, f=5, c=10, owner=None):
    product = Product(name=name, calories_per_100g=kcal, proteins=p, fats=f, carbs=c,
                      source=SOURCE_USER if owner else "seed", created_by_id=owner)
    product.refresh_search_terms()
    db.session.add(product)
    db.session.commit()
    return product.id


@pytest.fixture
def gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")


@pytest.fixture
def groq_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")


# --- provider selection -----------------------------------------------------------------

def test_provider_selection(monkeypatch):
    assert llm_providers.get_provider() is None and not coach_agent.coach_available()
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert llm_providers.get_provider().name == "groq"
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert llm_providers.get_provider().engine == "gemini:gemini-3.5-flash-lite"  # Gemini first
    monkeypatch.setenv("COACH_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    assert llm_providers.get_provider().engine == "groq:llama-3.3-70b-versatile"
    monkeypatch.delenv("GROQ_API_KEY")
    assert llm_providers.get_provider() is None  # the chosen provider has no key


# --- Gemini -----------------------------------------------------------------------------

def test_gemini_agent_runs_tools_and_stores_cleaned_report(app, monkeypatch, gemini_key):
    with app.app_context():
        me = create_user("me@user.test").id
        other = create_user("other@user.test").id
        skyr = _product("Test skyr", kcal=60, p=11, f=0, c=4)
        oats = _product("Test oats", kcal=370, p=13, f=7, c=60)
        private = _product("Private bar", owner=other)
        report = _report(
            foods=[{"product_id": skyr, "portion_grams": 200, "reason": "Protein"},
                   {"product_id": private, "portion_grams": 50, "reason": "not yours"},
                   {"product_id": 999999, "portion_grams": 50, "reason": "made up"}],
            dishes=[{"name": "Skyr oats", "meal_type": "breakfast", "why": "Protein breakfast",
                     "ingredients": [{"product_id": skyr, "name": "skyr", "grams": 200},
                                     {"product_id": oats, "name": "oats", "grams": 50},
                                     {"product_id": None, "name": "cinnamon", "grams": 2}]}],
        )
        http = FakeHTTP(
            monkeypatch,
            gemini(gcall("get_profile_and_targets", thoughtSignature="sig-1"),
                   gcall("search_catalogue", {"query": "test skyr"})),
            gemini({"text": "thinking...", "thought": True}, gcall("submit_report", report)),
        )
        stored = coach_agent.generate_coach_report(me, days=7)

        assert stored.engine == "gemini:gemini-3.5-flash-lite" and stored.grade == "B"
        data = CoachReport.query.one().data
        assert [f["product_id"] for f in data["foods"]] == [skyr]  # other user's / unknown ids dropped
        assert data["foods"][0]["kcal"] == 120
        dish = data["dishes"][0]
        assert dish["kcal"] == round(60 * 2 + 370 * 0.5)  # recomputed from the catalogue
        assert dish["protein_g"] == 28.5
        assert [i["product_id"] for i in dish["ingredients"]] == [skyr, oats, None]

    first, second = http.requests
    assert "gemini-3.5-flash-lite:generateContent" in first["url"]
    assert first["headers"] == {"x-goog-api-key": "test-gemini-key"}
    declarations = first["payload"]["tools"][0]["functionDeclarations"]
    assert {d["name"] for d in declarations} >= {"get_food_log", "search_catalogue", "submit_report"}
    assert all("parametersJsonSchema" in d for d in declarations)
    # The model turn goes back unchanged (thought signature included), then the tool results.
    model_turn, results = second["payload"]["contents"][-2:]
    assert model_turn["role"] == "model" and model_turn["parts"][0]["thoughtSignature"] == "sig-1"
    names = [p["functionResponse"]["name"] for p in results["parts"]]
    assert names == ["get_profile_and_targets", "search_catalogue"]
    found = results["parts"][1]["functionResponse"]["response"]["result"]["results"]
    assert found[0]["name"] == "Test skyr"


def test_tool_errors_are_reported_to_the_model(app, monkeypatch, gemini_key):
    with app.app_context():
        me = create_user("e@user.test").id
        http = FakeHTTP(
            monkeypatch,
            gemini(gcall("delete_everything"), gcall("get_daily_totals", {"days": "lots"})),
            gemini(gcall("submit_report", _report())),
        )
        coach_agent.generate_coach_report(me)
    parts = http.requests[1]["payload"]["contents"][-1]["parts"]
    assert "Unknown tool" in parts[0]["functionResponse"]["response"]["error"]
    assert "result" in parts[1]["functionResponse"]["response"]  # a bad "days" falls back to 7


def test_text_answer_gets_nudged_to_submit(app, monkeypatch, gemini_key):
    with app.app_context():
        me = create_user("t@user.test").id
        http = FakeHTTP(monkeypatch, gemini({"text": "Here is my advice..."}),
                        gemini(gcall("submit_report", _report())))
        assert coach_agent.generate_coach_report(me).engine.startswith("gemini:")
    assert "submit_report" in http.requests[1]["payload"]["contents"][-1]["parts"][0]["text"]


@pytest.mark.parametrize("script", [
    [llm_providers.ProviderError("The free AI quota is used up for now (HTTP 429).")],
    [{"promptFeedback": {"blockReason": "SAFETY"}}],
    [gemini(gcall("submit_report", {"summary": "", "grade": "Z"}))],   # unusable report
    [gemini({"text": "a"}), gemini({"text": "b"}), gemini({"text": "c"})],  # never submits
])
def test_failures_fall_back_to_rule_based_report(app, monkeypatch, gemini_key, script):
    with app.app_context():
        me = create_user("f@user.test").id
        FakeHTTP(monkeypatch, *script)
        report = coach_agent.generate_coach_report(me)
        assert report.engine == "local"
        assert "unavailable" in report.data["notice"]
        assert report.data["changes"]  # rule-based tips


def test_agent_gives_up_after_too_many_turns(app, monkeypatch, gemini_key):
    monkeypatch.setattr(coach_agent, "MAX_TURNS", 2)
    with app.app_context():
        me = create_user("loop@user.test").id
        FakeHTTP(monkeypatch, gemini(gcall("get_profile_and_targets")), gemini(gcall("get_profile_and_targets")))
        assert coach_agent.generate_coach_report(me).engine == "local"


def test_without_a_key_nothing_is_sent(app, monkeypatch):
    http = FakeHTTP(monkeypatch)
    with app.app_context():
        me = create_user("n@user.test").id
        report = coach_agent.generate_coach_report(me)
        assert report.engine == "local" and "notice" not in report.data
    assert http.requests == []


def test_malformed_report_fields_are_dropped(app):
    with app.app_context():
        me = create_user("m@user.test").id
        rice = _product("Test rice", kcal=130)
        cleaned = coach_agent._clean_report({
            "summary": " Ok ", "grade": "A", "highlights": "not a list", "issues": [None, "Low fibre", 5],
            "changes": [{"title": ""}, "text", {"title": "Eat greens"}],
            "foods": [{"product_id": str(rice), "portion_grams": "abc"}, {"product_id": True}, 7],
            "dishes": [{"name": "Rice bowl", "ingredients": [{"product_id": rice, "grams": "150"}, "x"]},
                       {"name": "Empty", "ingredients": []}],
        }, me)
    assert cleaned["summary"] == "Ok" and cleaned["highlights"] == [] and cleaned["issues"] == ["Low fibre"]
    assert cleaned["changes"] == [{"title": "Eat greens", "detail": ""}]
    assert [(f["product_id"], f["portion_grams"]) for f in cleaned["foods"]] == [(rice, 100)]
    assert [(d["name"], d["kcal"], d["meal_type"]) for d in cleaned["dishes"]] == [("Rice bowl", 195, "snack")]


# --- Groq -------------------------------------------------------------------------------

def test_groq_agent_round_trip(app, monkeypatch, groq_key):
    with app.app_context():
        me = create_user("g@user.test").id
        http = FakeHTTP(
            monkeypatch,
            groq(("c1", "get_food_log", '{"days": 3}'), ("c2", "search_catalogue", "{not json")),
            groq(("c3", "submit_report", json.dumps(_report()))),
        )
        assert coach_agent.generate_coach_report(me).engine == "groq:openai/gpt-oss-120b"

    first, second = http.requests
    assert first["url"] == llm_providers.GROQ_URL
    assert first["headers"] == {"Authorization": "Bearer test-groq-key"}
    assert first["payload"]["model"] == "openai/gpt-oss-120b"
    assert first["payload"]["tools"][0]["type"] == "function"
    assistant, tool1, tool2 = second["payload"]["messages"][-3:]
    assert "reasoning" not in assistant and [c["id"] for c in assistant["tool_calls"]] == ["c1", "c2"]
    assert (tool1["role"], tool1["tool_call_id"]) == ("tool", "c1") and "entries" in tool1["content"]
    assert tool2["tool_call_id"] == "c2" and tool2["content"].startswith("Error:")


# --- HTTP layer -------------------------------------------------------------------------

def test_http_errors_become_provider_errors(monkeypatch):
    def rate_limited(*_a, **_k):
        raise urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(llm_providers.urllib.request, "urlopen", rate_limited)
    with pytest.raises(llm_providers.ProviderError, match="quota"):
        llm_providers._post_json("https://x", {}, {})


# --- tools only see the athlete's own data --------------------------------------------

def test_tools_are_bound_to_one_athlete(app):
    today = date.today()
    with app.app_context():
        me = create_user("mine@user.test").id
        other = create_user("theirs@user.test").id
        shared = _product("Shared rice")
        secret = _product("Secret rice", owner=other)
        db.session.add(FoodLog(user_id=other, date=today, meal_type="lunch", product_id=secret, portion_grams=100))
        db.session.add(FoodLog(user_id=me, date=today, meal_type="lunch", product_id=shared, portion_grams=150))
        db.session.commit()

        tools = coach_agent.CoachTools(me, today)
        log = json.loads(tools.run("get_food_log", {"days": 3}))["entries"]
        assert [e["product"] for e in log] == ["Shared rice"]
        found = json.loads(tools.run("search_catalogue", {"query": "rice"}))["results"]
        names = {r["name"] for r in found}
        assert "Shared rice" in names and "Secret rice" not in names
        totals = json.loads(tools.run("get_daily_totals", {"days": 2}))
        assert [d["logged"] for d in totals["days"]] == [False, True]
        assert totals["days"][-1]["calories"] == 150
        profile = json.loads(tools.run("get_profile_and_targets", {}))
        assert profile["daily_targets"]["calories"] > 1200


# --- pages --------------------------------------------------------------------------------

def _stored_report(user_id, product_id):
    data = {
        "summary": "Solid week.", "grade": "A-", "highlights": ["Water on target"], "issues": [],
        "changes": [{"title": "More fish", "detail": "Twice a week."}],
        "foods": [{"product_id": product_id, "name": "Test salmon", "portion_grams": 150, "kcal": 300,
                   "protein_per_100g": 20, "reason": "Omega-3"}],
        "dishes": [{"name": "Salmon bowl", "meal_type": "dinner", "why": "Omega-3", "kcal": 520,
                    "protein_g": 35, "fat_g": 20, "carbs_g": 45,
                    "ingredients": [{"product_id": product_id, "name": "Test salmon", "grams": 150}]}],
    }
    db.session.add(CoachReport(user_id=user_id, days=7, engine="claude-opus-5", grade="A-",
                               summary=data["summary"], payload=json.dumps(data)))
    db.session.commit()


def test_coach_tab_and_trainer_view_render_report(app, client):
    with app.app_context():
        trainer = create_user("coach@trainer.test", role="trainer")
        athlete = create_user("ath@user.test", trainer=trainer)
        salmon = _product("Test salmon", kcal=200, p=20, f=13, c=0)
        _stored_report(athlete.id, salmon)
        athlete_id = athlete.id

    login(client, "ath@user.test")
    html = client.get("/dashboard").get_data(as_text=True)
    assert "Salmon bowl" in html and "More fish" in html and "Open full analysis" in html
    assert re.search(rf'/log-food\?[^"]*product={salmon}', html) and "grams=150" in html

    trainer_client = app.test_client()
    login(trainer_client, "coach@trainer.test")
    page = trainer_client.get(f"/trainer/client/{athlete_id}").get_data(as_text=True)
    assert "Salmon bowl" in page and "/log-food?" not in page


def test_run_analysis_route_uses_basic_mode_without_credentials(app, client):
    with app.app_context():
        create_user("run@user.test")
    login(client, "run@user.test")
    resp = client.post("/dashboard/ai-report")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("#coach")
    with app.app_context():
        assert CoachReport.query.one().engine == "local"
    assert "Basic mode" in client.get("/dashboard").get_data(as_text=True)


def test_log_food_prefills_product_and_grams(app, client):
    with app.app_context():
        create_user("pre@user.test")
        pid = _product("Prefill oats")
    login(client, "pre@user.test")
    html = client.get(f"/log-food?product={pid}&grams=150&meal=dinner").get_data(as_text=True)
    data = json.loads(re.search(r'id="pickerData">(.*?)</script>', html, re.S).group(1))
    assert data["preselected"]["id"] == pid and data["preselected"]["portion"] == 150
