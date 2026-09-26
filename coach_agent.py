"""AI nutrition coach: an LLM agent that reads one athlete's data through tools and returns
a structured analysis (summary, findings, concrete changes, foods and dishes to log).

It runs on a free provider from llm_providers.py (Gemini or Groq). Without an API key — or
if the provider fails — the rule-based analyzer from ai_service produces the report instead,
so the feature always works.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Callable

from sqlalchemy.orm import joinedload

import food_db
from ai_service import collect_nutrition_context, rule_based_analysis
from llm_providers import Provider, ProviderError, get_provider
from models import CoachMessage, CoachReport, FoodLog, Goal, Product, Profile, WaterLog, WeightLog, db
from nutrition import (
    GOAL_LABELS,
    calculate_age,
    calculate_daily_targets,
    logging_streak,
    water_goal_ml,
    weekly_checkin,
    weight_summary,
)

logger = logging.getLogger(__name__)

APP_NAME = "Kolos"
COACH_NAME = "Zernia"        # shown in the English UI
COACH_NAME_UK = "Зернятко"   # how the coach calls itself in Ukrainian
COACH_LANGUAGE = os.environ.get("COACH_LANGUAGE", "").strip() or "Ukrainian"
MAX_TURNS = 10
MAX_NUDGES = 2
MAX_DAYS = 28
CHAT_HISTORY = 20        # earlier chat turns sent back to the model
CHAT_MAX_CHARS = 1000
CHAT_DAILY_LIMIT = 40    # athlete messages per day, to stay inside the free provider quota
MEAL_TYPES = ["breakfast", "lunch", "dinner", "snack"]
GRADES = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D", "F"]

PERSONA = (f"Your name is {COACH_NAME_UK} ({COACH_NAME}), the AI nutrition coach inside {APP_NAME}, a "
           "nutrition and workout tracking app. Like a grain that grows into a harvest, you believe in small "
           "steady steps: warm, encouraging and down to earth, never preachy.")

SYSTEM_PROMPT = f"""{PERSONA}

You analyze one athlete's logged data and give specific, practical advice on what to change in their diet.

How to work:
- Look at the data with the tools before drawing conclusions. Start with get_profile_and_targets and \
get_daily_totals, then dig into whatever stands out: the food log for eating patterns, micronutrients \
for gaps, weight history for the trend.
- Ground every finding in numbers from the tools, e.g. "protein averaged 92 g against a 150 g target \
on 5 of 7 days".
- Suggest foods and dishes the athlete can log in the app: find them with search_catalogue and use the \
returned product_id. Prefer common whole foods and give portions in grams. Build each dish from \
catalogue products with gram amounts that close the gaps you found; an ingredient that is not in the \
catalogue (spices, water) gets product_id null.
- Respect the goal and the calorie target. Never suggest going under 1200 kcal a day or extreme diets. \
You are not a doctor: if the data suggests a health problem (very low intake, very fast weight change), \
recommend seeing a professional instead of diagnosing.
- If fewer than 3 days are logged, say so and make the logging habit one of the changes.
- Always include 3-6 foods and 2-3 dishes, even when data is sparse: base them on the goal and the \
daily targets. Look the ingredients up with search_catalogue first.
- Keep it short and scannable: a 2-3 sentence summary, findings of one sentence each, and changes with \
a short actionable title plus one or two sentences of detail.

Write all text for the athlete in {COACH_LANGUAGE}. Product names may stay as they are in the catalogue."""


CHAT_SYSTEM_PROMPT = f"""{PERSONA}

You are chatting with one athlete about their diet. Answer their question directly and practically.

- Use the tools to look at the athlete's real data whenever the answer depends on it (what they ate, \
targets, remaining calories today, weight trend, their latest analysis). Quote concrete numbers.
- When you suggest foods or meals, prefer products from the catalogue (search_catalogue) and give \
portions in grams with approximate calories and protein.
- Keep answers short: a few sentences or a short list. Plain text; you may use "- " bullets and **bold**.
- Stay on nutrition, training and healthy habits; politely decline unrelated requests. You are not a \
doctor: for symptoms or medical conditions, recommend seeing a professional.

Answer in the language the athlete writes in; if unsure, use {COACH_LANGUAGE}."""


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


REPORT_SCHEMA = _obj({
    "summary": {"type": "string", "description": "2-3 sentences: the overall picture."},
    "grade": {"type": "string", "enum": GRADES},
    "highlights": {"type": "array", "items": {"type": "string"}, "description": "What is going well."},
    "issues": {"type": "array", "items": {"type": "string"}, "description": "Problems, with numbers."},
    "changes": {
        "type": "array",
        "description": "3-5 concrete changes, most important first.",
        "items": _obj({"title": {"type": "string"}, "detail": {"type": "string"}}),
    },
    "foods": {
        "type": "array",
        "description": "Up to 6 catalogue foods worth adding.",
        "items": _obj({
            "product_id": {"type": "integer"},
            "portion_grams": {"type": "number"},
            "reason": {"type": "string"},
        }),
    },
    "dishes": {
        "type": "array",
        "description": "2-3 dishes built from catalogue products.",
        "items": _obj({
            "name": {"type": "string"},
            "meal_type": {"type": "string", "enum": MEAL_TYPES},
            "why": {"type": "string"},
            "ingredients": {
                "type": "array",
                "items": _obj({
                    "product_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "name": {"type": "string"},
                    "grams": {"type": "number"},
                }),
            },
        }),
    },
})

_DAYS = {"type": "integer", "description": f"How many days back from today, 1-{MAX_DAYS}."}

TOOL_DEFINITIONS = [
    {
        "name": "get_profile_and_targets",
        "description": "Athlete profile (sex, age, height, weight, activity), active goal, daily calorie "
                       "and macro targets, water goal, logging streak and the weekly check-in (real TDEE "
                       "from intake and weigh-ins, when there is enough data).",
        "input_schema": _obj({}),
    },
    {
        "name": "get_daily_totals",
        "description": "Per-day totals for the last N days: calories, protein, fat, carbs, number of "
                       "entries per meal type and water. Days without any food entry are listed with "
                       "logged=false. Also returns the daily targets for comparison.",
        "input_schema": _obj({"days": _DAYS}),
    },
    {
        "name": "get_food_log",
        "description": "Every food entry for the last N days (date, meal, product, grams, calories, "
                       "macros). Use it to find eating patterns and the foods the athlete relies on.",
        "input_schema": _obj({"days": _DAYS}),
    },
    {
        "name": "get_micronutrients",
        "description": "Average daily micronutrient intake over the last N logged days against daily "
                       "targets, plus the detected macro and micronutrient deficits.",
        "input_schema": _obj({"days": _DAYS}),
    },
    {
        "name": "get_weight_history",
        "description": "Weigh-ins for the last N days, the latest weight, the change against a week "
                       "earlier and the goal weight.",
        "input_schema": _obj({"days": _DAYS}),
    },
    {
        "name": "search_catalogue",
        "description": "Search the app's food catalogue (English or Ukrainian names). Returns products "
                       "with product_id and nutrition per 100 g. Use it to find foods and dish "
                       "ingredients to recommend; only recommend product_ids returned here.",
        "input_schema": _obj({
            "query": {"type": "string", "description": "One food, e.g. 'cottage cheese' or 'гречка'."},
        }),
    },
]
LATEST_ANALYSIS_TOOL = {
    "name": "get_latest_analysis",
    "description": "The athlete's most recent coach analysis (summary, issues, suggested changes, "
                   "foods and dishes), if they have run one.",
    "input_schema": _obj({}),
}

SUBMIT_TOOL = {
    "name": "submit_report",
    "description": "Deliver the final analysis to the athlete. Call it once, after looking at the data.",
    "input_schema": REPORT_SCHEMA,
}


class CoachError(Exception):
    """The AI coach could not produce a report (provider error or unusable output)."""


def coach_available() -> bool:
    """True when a free AI provider (Gemini or Groq) has an API key configured."""
    return get_provider() is not None


def _clamp_days(value: Any, default: int = 7) -> int:
    try:
        return max(1, min(MAX_DAYS, int(value)))
    except (TypeError, ValueError):
        return default


def _num(value: Any, digits: int = 1) -> float | None:
    return round(float(value), digits) if value is not None else None


def _product_nutrition(p: Product) -> dict[str, Any]:
    return {
        "product_id": p.id,
        "name": p.name if not p.brand else f"{p.name} ({p.brand})",
        "kcal_per_100g": _num(p.calories_per_100g, 0),
        "protein_per_100g": _num(p.proteins),
        "fat_per_100g": _num(p.fats),
        "carbs_per_100g": _num(p.carbs),
    }


class CoachTools:
    """The coach's read-only tools, bound to one athlete; the model never chooses whose data it reads."""

    def __init__(self, user_id: int, today: date | None = None):
        self.user_id = user_id
        self.today = today or date.today()
        self.handlers: dict[str, Callable[..., Any]] = {
            "get_profile_and_targets": self.get_profile_and_targets,
            "get_daily_totals": self.get_daily_totals,
            "get_food_log": self.get_food_log,
            "get_micronutrients": self.get_micronutrients,
            "get_weight_history": self.get_weight_history,
            "search_catalogue": self.search_catalogue,
            "get_latest_analysis": self.get_latest_analysis,
        }

    def run(self, name: str, args: dict[str, Any]) -> str:
        handler = self.handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool {name!r}.")
        return json.dumps(handler(**args), ensure_ascii=False, default=str)

    # --- helpers ---------------------------------------------------------------------

    def _profile(self) -> Profile | None:
        return Profile.query.filter_by(user_id=self.user_id).first()

    def _goal(self) -> Goal | None:
        return Goal.query.filter_by(user_id=self.user_id, status="active").first()

    def _logs(self, days: int) -> list[FoodLog]:
        start = self.today - timedelta(days=days - 1)
        return (
            FoodLog.query.filter(FoodLog.user_id == self.user_id, FoodLog.date.between(start, self.today))
            .options(joinedload(FoodLog.product))
            .order_by(FoodLog.date, FoodLog.id)
            .all()
        )

    # --- tools -----------------------------------------------------------------------

    def get_profile_and_targets(self) -> dict[str, Any]:
        profile, goal = self._profile(), self._goal()
        targets = calculate_daily_targets(profile, goal)
        checkin = weekly_checkin(self.user_id, profile, goal, self.today) if targets else None
        return {
            "today": self.today.isoformat(),
            "profile": None if profile is None else {
                "sex": profile.gender,
                "age": calculate_age(profile.birth_date) if profile.birth_date else None,
                "height_cm": _num(profile.height),
                "weight_kg": _num(profile.weight),
                "activity_level": profile.activity_level,
            },
            "goal": None if goal is None else {
                "type": GOAL_LABELS.get(goal.goal_type, goal.goal_type),
                "start_weight_kg": _num(goal.start_weight),
                "target_weight_kg": _num(goal.target_weight),
                "started": goal.start_date,
            },
            "daily_targets": None if targets is None else {
                "calories": targets["calories"],
                "protein_g": targets["proteins"],
                "fat_g": targets["fats"],
                "carbs_g": targets["carbs"],
                "tdee_formula": targets["tdee"],
                "adaptive_target_applied": targets["is_adaptive"],
            },
            "water_goal_ml": water_goal_ml(profile),
            "logging_streak_days": logging_streak(self.user_id, self.today),
            "weekly_checkin": checkin,
        }

    def get_daily_totals(self, days: int = 7) -> dict[str, Any]:
        days = _clamp_days(days)
        by_day: dict[date, dict[str, Any]] = {}
        for log in self._logs(days):
            d = by_day.setdefault(log.date, {"calories": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0,
                                             "entries_by_meal": {}})
            d["calories"] += log.calories
            d["protein_g"] += log.proteins_g
            d["fat_g"] += log.fats_g
            d["carbs_g"] += log.carbs_g
            d["entries_by_meal"][log.meal_type] = d["entries_by_meal"].get(log.meal_type, 0) + 1

        water = dict(
            db.session.query(WaterLog.date, db.func.sum(WaterLog.amount_ml))
            .filter(WaterLog.user_id == self.user_id,
                    WaterLog.date.between(self.today - timedelta(days=days - 1), self.today))
            .group_by(WaterLog.date)
        )
        rows = []
        for offset in range(days - 1, -1, -1):
            day = self.today - timedelta(days=offset)
            totals = by_day.get(day)
            row: dict[str, Any] = {"date": day.isoformat(), "logged": totals is not None,
                                   "water_ml": int(water.get(day) or 0)}
            if totals:
                row.update({k: round(v, 1) if isinstance(v, float) else v for k, v in totals.items()})
            rows.append(row)
        targets = calculate_daily_targets(self._profile(), self._goal())
        return {
            "days": rows,
            "note": "The last day is today and may still be in progress.",
            "targets": None if not targets else {
                "calories": targets["calories"], "protein_g": targets["proteins"],
                "fat_g": targets["fats"], "carbs_g": targets["carbs"],
            },
        }

    def get_food_log(self, days: int = 7) -> dict[str, Any]:
        days = _clamp_days(days)
        entries = [
            {
                "date": log.date.isoformat(),
                "meal": log.meal_type,
                "product_id": log.product_id,
                "product": log.product.name,
                "grams": _num(log.portion_grams, 0),
                "calories": round(log.calories),
                "protein_g": round(log.proteins_g, 1),
                "fat_g": round(log.fats_g, 1),
                "carbs_g": round(log.carbs_g, 1),
            }
            for log in self._logs(days)
        ]
        return {"entries": entries[-300:], "truncated": len(entries) > 300}

    def get_micronutrients(self, days: int = 7) -> dict[str, Any]:
        ctx = collect_nutrition_context(self.user_id, days=_clamp_days(days))
        return {
            "days_with_logs": ctx.days_with_logs,
            "daily_average": ctx.micronutrient_daily_avg,
            "macro_adherence_percent": ctx.adherence,
            "deficits": ctx.deficits,
        }

    def get_weight_history(self, days: int = 28) -> dict[str, Any]:
        days = _clamp_days(days, MAX_DAYS)
        weigh_ins = (
            WeightLog.query.filter(WeightLog.user_id == self.user_id,
                                   WeightLog.date >= self.today - timedelta(days=days - 1))
            .order_by(WeightLog.date)
            .all()
        )
        goal, profile = self._goal(), self._profile()
        return {
            "weigh_ins": [{"date": w.date.isoformat(), "kg": _num(w.weight_kg)} for w in weigh_ins],
            "latest": weight_summary(self.user_id),
            "profile_weight_kg": _num(profile.weight) if profile else None,
            "goal_weight_kg": _num(goal.target_weight) if goal else None,
        }

    def search_catalogue(self, query: str) -> dict[str, Any]:
        products = food_db.search_products(query, self.user_id, limit=8, remote=False)
        return {"results": [_product_nutrition(p) for p in products]}

    def get_latest_analysis(self) -> dict[str, Any]:
        report = latest_coach_report(self.user_id)
        if report is None:
            return {"analysis": None, "note": "The athlete has not run an analysis yet."}
        return {"created": report.created_at.isoformat(timespec="minutes"), "days": report.days,
                "analysis": report.data}


# --- the agent loop ---------------------------------------------------------------------

def _answer_tool_calls(convo, tools: CoachTools, calls) -> None:
    results = []
    for call in calls:
        if call.error:
            results.append((call, f"Error: {call.error}", True))
            continue
        try:
            results.append((call, tools.run(call.name, call.args), False))
        except Exception as exc:  # a tool failure is reported to the model, not raised
            logger.warning("coach tool %s failed: %s", call.name, exc)
            db.session.rollback()
            results.append((call, f"Error: {exc}", True))
    convo.add_tool_results(results)


def run_agent(provider: Provider, tools: CoachTools, task: str) -> dict[str, Any]:
    """Drive the tool-use loop until the model calls submit_report; returns its arguments."""
    convo = provider.start(SYSTEM_PROMPT, task, TOOL_DEFINITIONS + [SUBMIT_TOOL])
    nudges = 0
    for _ in range(MAX_TURNS):
        reply = convo.send()
        submitted = next((c for c in reply.tool_calls if c.name == SUBMIT_TOOL["name"]), None)
        if submitted is not None and submitted.error is None:
            return submitted.args
        if reply.tool_calls:
            _answer_tool_calls(convo, tools, reply.tool_calls)
            continue
        if nudges >= MAX_NUDGES:
            break
        nudges += 1
        convo.add_user("Deliver the analysis now by calling submit_report with the full report.")
    raise CoachError("The coach did not deliver a report.")


def run_chat(provider: Provider, tools: CoachTools, history: list[dict[str, str]], message: str) -> str:
    """One chat turn: let the model use the tools, then return its text answer."""
    convo = provider.start(CHAT_SYSTEM_PROMPT, message, TOOL_DEFINITIONS + [LATEST_ANALYSIS_TOOL], history)
    nudged = False
    for _ in range(MAX_TURNS):
        reply = convo.send()
        if reply.tool_calls:
            _answer_tool_calls(convo, tools, reply.tool_calls)
            continue
        if reply.text.strip():
            return reply.text.strip()
        if nudged:
            break
        nudged = True
        convo.add_user("Please answer my last message.")
    raise CoachError("The coach did not answer.")


def _items(value: Any, limit: int) -> list:
    return [v for v in value if v][:limit] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_id(value: Any) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _clean_report(raw: dict[str, Any], user_id: int) -> dict[str, Any]:
    """Validate the model's report: keep only catalogue products the athlete can see, recompute dish
    nutrition from the catalogue and drop anything malformed (free models don't always follow the schema)."""
    if not isinstance(raw, dict) or not _text(raw.get("summary")):
        raise CoachError("The coach's report was empty.")
    raw_foods = [f for f in _items(raw.get("foods"), 6) if isinstance(f, dict)]
    raw_dishes = [d for d in _items(raw.get("dishes"), 4) if isinstance(d, dict)]
    ids = {_as_id(f.get("product_id")) for f in raw_foods}
    ids |= {_as_id(i.get("product_id")) for d in raw_dishes
            for i in _items(d.get("ingredients"), 12) if isinstance(i, dict)}
    ids.discard(None)
    products = {p.id: p for p in Product.query.filter(Product.id.in_(ids)).all() if p.is_visible_to(user_id)}

    foods = []
    for item in raw_foods:
        product = products.get(_as_id(item.get("product_id")))
        if product:
            grams = max(1.0, min(1000.0, _as_float(item.get("portion_grams"), 100) or 100))
            foods.append({**_product_nutrition(product), "portion_grams": round(grams),
                          "kcal": round(float(product.calories_per_100g) * grams / 100),
                          "reason": _text(item.get("reason"))})

    dishes = []
    for dish in raw_dishes:
        ingredients, totals = [], {"kcal": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
        for ing in _items(dish.get("ingredients"), 12):
            if not isinstance(ing, dict):
                continue
            grams = max(0.0, min(2000.0, _as_float(ing.get("grams"))))
            product = products.get(_as_id(ing.get("product_id")))
            if product:
                factor = grams / 100
                totals["kcal"] += float(product.calories_per_100g) * factor
                totals["protein_g"] += float(product.proteins or 0) * factor
                totals["fat_g"] += float(product.fats or 0) * factor
                totals["carbs_g"] += float(product.carbs or 0) * factor
            name = product.name if product else _text(ing.get("name"))
            if name:
                ingredients.append({"product_id": product.id if product else None, "name": name,
                                    "grams": round(grams)})
        if _text(dish.get("name")) and ingredients:
            dishes.append({
                "name": _text(dish.get("name")),
                "meal_type": dish.get("meal_type") if dish.get("meal_type") in MEAL_TYPES else "snack",
                "why": _text(dish.get("why")),
                "ingredients": ingredients,
                **{k: round(v) if k == "kcal" else round(v, 1) for k, v in totals.items()},
            })

    changes = [
        {"title": _text(c.get("title")), "detail": _text(c.get("detail"))}
        for c in _items(raw.get("changes"), 6) if isinstance(c, dict) and _text(c.get("title"))
    ]
    return {
        "summary": _text(raw.get("summary")),
        "grade": raw.get("grade") if raw.get("grade") in GRADES else None,
        "highlights": [_text(x) for x in _items(raw.get("highlights"), 5) if _text(x)],
        "issues": [_text(x) for x in _items(raw.get("issues"), 6) if _text(x)],
        "changes": changes,
        "foods": foods,
        "dishes": dishes,
    }


def _local_report(user_id: int, days: int) -> dict[str, Any]:
    analysis = rule_based_analysis(collect_nutrition_context(user_id, days=days))
    return {
        "summary": analysis["nutrition_summary"],
        "grade": analysis["ai_grade"],
        "highlights": [],
        "issues": [],
        "changes": [{"title": tip, "detail": ""} for tip in analysis["recommendations"]],
        "foods": [],
        "dishes": [],
    }


def generate_coach_report(user_id: int, days: int = 7, provider: Provider | None = None,
                          today: date | None = None) -> CoachReport:
    """Run the coach for the last `days` days and store the report (rule-based without a provider)."""
    days = _clamp_days(days)
    provider = provider or get_provider()
    engine, data, error = "local", None, None
    if provider is not None:
        try:
            task = (f"Analyze my nutrition for the last {days} days (today is {(today or date.today()).isoformat()}) "
                    f"and tell me what to change: findings, concrete changes, foods and dishes to add.")
            data = _clean_report(run_agent(provider, CoachTools(user_id, today), task), user_id)
            engine = provider.engine
        except (ProviderError, CoachError) as exc:
            logger.warning("coach report failed, using the rule-based analysis: %s", exc)
            error = str(exc)
    if data is None:
        data = _local_report(user_id, days)
    if error:
        data["notice"] = f"{COACH_NAME} is unavailable right now, so this is a basic automatic analysis."

    report = CoachReport(user_id=user_id, days=days, engine=engine[:40], grade=data["grade"],
                         summary=data["summary"] or "—", payload=json.dumps(data, ensure_ascii=False))
    db.session.add(report)
    db.session.commit()
    return report


def latest_coach_report(user_id: int) -> CoachReport | None:
    return (
        CoachReport.query.filter_by(user_id=user_id)
        .order_by(CoachReport.created_at.desc(), CoachReport.id.desc())
        .first()
    )


# --- chat ---------------------------------------------------------------------------------

class ChatLimitError(CoachError):
    """The athlete reached today's message limit."""


def chat_history(user_id: int, limit: int = 50) -> list[CoachMessage]:
    rows = (
        CoachMessage.query.filter_by(user_id=user_id)
        .order_by(CoachMessage.id.desc())
        .limit(limit)
        .all()
    )
    return rows[::-1]


def messages_today(user_id: int) -> int:
    today_start = datetime.combine(date.today(), datetime.min.time())
    return CoachMessage.query.filter(
        CoachMessage.user_id == user_id,
        CoachMessage.role == "user",
        CoachMessage.created_at >= today_start,
    ).count()


def coach_chat(user_id: int, message: str, provider: Provider | None = None) -> tuple[CoachMessage, CoachMessage]:
    """Answer one athlete message and store both turns. Raises CoachError / ProviderError."""
    provider = provider or get_provider()
    if provider is None:
        raise CoachError("The AI coach is not connected.")
    if messages_today(user_id) >= CHAT_DAILY_LIMIT:
        raise ChatLimitError(f"You've reached today's limit of {CHAT_DAILY_LIMIT} messages. Try again tomorrow.")
    history = [{"role": m.role, "content": m.content} for m in chat_history(user_id, CHAT_HISTORY)]
    answer = run_chat(provider, CoachTools(user_id), history, message)
    asked = CoachMessage(user_id=user_id, role="user", content=message)
    answered = CoachMessage(user_id=user_id, role="assistant", content=answer)
    db.session.add_all([asked, answered])
    db.session.commit()
    return asked, answered
