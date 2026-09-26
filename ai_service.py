"""
Nutrition analytics shared by the AI coach: aggregates FoodLog data into daily averages,
target adherence and micronutrient deficits, plus a deterministic rule-based analysis
used when the AI coach is unavailable (see coach_agent.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import joinedload

from models import FoodLog, Goal, Micronutrient, Product, ProductMicronutrient, Profile
from nutrition import calculate_bmr, calculate_daily_targets, calculate_tdee


DAILY_MICRONUTRIENT_TARGETS: dict[str, float] = {
    "Potassium": 3400,
    "Sodium": 2300,
    "Magnesium": 400,
    "Calcium": 1000,
    "Zinc": 11,
    "Iron": 18,
    "Vitamin C": 90,
    "Vitamin D": 600,
    "Vitamin B12": 2.4,
    "Omega-3": 1.6,
}

IRON_TARGET_MALE = 8.0


@dataclass
class NutritionContext:
    user_id: int
    days: int
    period_start: date
    period_end: date
    profile: Profile | None
    goal: Goal | None
    targets: dict[str, Any]
    bmr: float
    tdee: float
    days_with_logs: int
    daily_averages: dict[str, float]
    micronutrient_daily_avg: list[dict[str, Any]]
    deficits: list[dict[str, Any]] = field(default_factory=list)
    adherence: dict[str, float] = field(default_factory=dict)


def _micronutrient_target(name: str, gender: str | None) -> float:
    if name == "Iron" and gender == "male":
        return IRON_TARGET_MALE
    return DAILY_MICRONUTRIENT_TARGETS.get(name, 0)


def _food_logs_for_period(user_id: int, start: date, end: date) -> list[FoodLog]:
    return (
        FoodLog.query.filter(
            FoodLog.user_id == user_id,
            FoodLog.date >= start,
            FoodLog.date <= end,
        )
        .options(
            joinedload(FoodLog.product).joinedload(Product.micronutrient_links).joinedload(
                ProductMicronutrient.micronutrient
            )
        )
        .order_by(FoodLog.date, FoodLog.id)
        .all()
    )


def collect_nutrition_context(user_id: int, days: int = 7) -> NutritionContext:
    """Load profile, goal, targets, and aggregated intake for the last `days` days."""
    period_end = date.today()
    period_start = period_end - timedelta(days=days - 1)

    profile = Profile.query.filter_by(user_id=user_id).first()
    goal = Goal.query.filter_by(user_id=user_id, status="active").first()
    targets = calculate_daily_targets(profile, goal) or {}
    bmr = calculate_bmr(profile)
    tdee = calculate_tdee(profile)

    logs = _food_logs_for_period(user_id, period_start, period_end)
    days_with_logs = len({log.date for log in logs})

    calories_by_day: dict[date, float] = {}
    proteins_by_day: dict[date, float] = {}
    fats_by_day: dict[date, float] = {}
    carbs_by_day: dict[date, float] = {}
    micro_totals: dict[int, float] = {}

    for log in logs:
        day = log.date
        calories_by_day[day] = calories_by_day.get(day, 0) + log.calories
        proteins_by_day[day] = proteins_by_day.get(day, 0) + log.proteins_g
        fats_by_day[day] = fats_by_day.get(day, 0) + log.fats_g
        carbs_by_day[day] = carbs_by_day.get(day, 0) + log.carbs_g
        grams = log.grams
        for link in log.product.micronutrient_links:
            micro_totals[link.micronutrient_id] = micro_totals.get(link.micronutrient_id, 0) + link.amount_for_portion(
                grams
            )

    divisor = max(days_with_logs, 1)

    daily_averages = {
        "calories": sum(calories_by_day.values()) / divisor,
        "proteins": sum(proteins_by_day.values()) / divisor,
        "fats": sum(fats_by_day.values()) / divisor,
        "carbs": sum(carbs_by_day.values()) / divisor,
    }

    nutrients = Micronutrient.query.order_by(Micronutrient.name).all()
    micronutrient_daily_avg = []
    deficits = []
    gender = profile.gender if profile else None

    for nutrient in nutrients:
        total = micro_totals.get(nutrient.id, 0)
        daily_avg = total / divisor
        target = _micronutrient_target(nutrient.name, gender)
        pct = (daily_avg / target * 100) if target > 0 else 100.0
        entry = {
            "name": nutrient.name,
            "unit": nutrient.unit,
            "daily_avg": round(daily_avg, 2),
            "target_daily": target,
            "percent_of_target": round(pct, 1),
        }
        micronutrient_daily_avg.append(entry)
        if target > 0 and pct < 70:
            deficits.append(
                {
                    "type": "micronutrient",
                    "name": nutrient.name,
                    "percent": pct,
                    "daily_avg": daily_avg,
                    "target": target,
                    "unit": nutrient.unit,
                }
            )

    adherence: dict[str, float] = {}
    if targets:
        for key in ("calories", "proteins", "fats", "carbs"):
            target_val = float(targets.get(key, 0) or 0)
            actual = daily_averages[key]
            adherence[key] = round(actual / target_val * 100, 1) if target_val > 0 else 0.0
            if target_val > 0:
                ratio = actual / target_val
                if key == "calories":
                    over_for_loss = (
                        goal
                        and goal.goal_type == "weight_loss"
                        and ratio > 1.0
                    )
                    if ratio < 0.85 or ratio > 1.15 or over_for_loss:
                        deficits.append(
                            {
                                "type": "macro",
                                "name": "calories",
                                "percent": adherence[key],
                                "direction": "low" if ratio < 0.85 else "high",
                                "weight_loss_surplus": over_for_loss,
                            }
                        )
                elif key == "proteins" and ratio < 0.85:
                    deficits.append(
                        {
                            "type": "macro",
                            "name": "proteins",
                            "percent": adherence[key],
                            "direction": "low",
                        }
                    )
                elif key in ("fats", "carbs") and ratio < 0.75:
                    deficits.append(
                        {
                            "type": "macro",
                            "name": key,
                            "percent": adherence[key],
                            "direction": "low",
                        }
                    )

    return NutritionContext(
        user_id=user_id,
        days=days,
        period_start=period_start,
        period_end=period_end,
        profile=profile,
        goal=goal,
        targets=targets,
        bmr=round(bmr, 0),
        tdee=round(tdee, 0),
        days_with_logs=days_with_logs,
        daily_averages={k: round(v, 1) for k, v in daily_averages.items()},
        micronutrient_daily_avg=micronutrient_daily_avg,
        deficits=deficits,
        adherence=adherence,
    )


def _score_from_context(ctx: NutritionContext) -> float:
    if ctx.days_with_logs == 0:
        return 35.0

    scores: list[float] = []
    for key in ("calories", "proteins", "fats", "carbs"):
        pct = ctx.adherence.get(key, 0)
        if key == "calories":
            deviation = abs(pct - 100)
            scores.append(max(0, 100 - deviation * 1.2))
        else:
            scores.append(min(100, pct * 1.05) if pct <= 110 else max(50, 110 - (pct - 110)))

    micro_scores = [min(100, m["percent_of_target"]) for m in ctx.micronutrient_daily_avg if m["target_daily"] > 0]
    if micro_scores:
        scores.append(sum(micro_scores) / len(micro_scores))

    coverage_bonus = (ctx.days_with_logs / ctx.days) * 10
    return min(100, sum(scores) / len(scores) + coverage_bonus)


def _grade_from_score(score: float) -> str:
    if score >= 92:
        return "A+"
    if score >= 88:
        return "A"
    if score >= 84:
        return "A-"
    if score >= 80:
        return "B+"
    if score >= 76:
        return "B"
    if score >= 72:
        return "B-"
    if score >= 68:
        return "C+"
    if score >= 64:
        return "C"
    if score >= 58:
        return "C-"
    return "D"


def _build_recommendations(ctx: NutritionContext) -> list[str]:
    """Prioritized tips: calories/macros first, micronutrients last."""
    if ctx.days_with_logs == 0:
        return [
            "Логуйте кожен прийом їжі щонайменше 5 днів на тиждень для точного AI-звіту.",
            "Додайте сніданок і перекус із білком (йогурт, яйця, курка) для стабільного балансу БЖВ.",
            "Після тижня логів повторіть аналіз — тренер зможе затвердити рекомендації.",
        ]

    tips: list[str] = []
    goal_type = ctx.goal.goal_type if ctx.goal else None
    cal_pct = ctx.adherence.get("calories", 0)
    prot_pct = ctx.adherence.get("proteins", 0)
    fat_pct = ctx.adherence.get("fats", 0)
    carb_pct = ctx.adherence.get("carbs", 0)

    if goal_type == "weight_loss" and cal_pct > 100:
        tips.append(
            "Обмежте енергетичну щільність раціону: більше овочів і нежирного білка, "
            "менше соусів, випічки та солодких напоїв — калорійність перевищує коридор для схуднення."
        )
    elif cal_pct < 85:
        tips.append(
            "Збільште калорійність основних прийомів або додайте здоровий перекус "
            "(горіхи, рис, йогурт), щоб наблизитися до добової цілі."
        )
    elif cal_pct > 115:
        tips.append(
            "Скоротіть калорійні напої, соуси та вечірні перекуси — споживання перевищує розрахункову ціль."
        )

    if prot_pct < 85:
        tips.append(
            "Додайте 25–35 г білка на прийом (курка, риба, яйця, сир, йогурт) — "
            "зараз білок нижче 85% від цілі."
        )

    if fat_pct < 75:
        tips.append(
            "Включіть корисні жири: авокадо, оливкова олія, горіхи — для гормонального балансу."
        )
    if carb_pct < 75:
        tips.append(
            "Додайте складні вуглеводи (овес, рис, гречка, банан) навколо тренувань для енергії."
        )

    micro_deficits = sorted(
        [d for d in ctx.deficits if d["type"] == "micronutrient"],
        key=lambda d: d.get("percent", 100),
    )
    for deficit in micro_deficits:
        if len(tips) >= 3:
            break
        tips.append(
            f"Збагатіть раціон джерелами {deficit['name']} "
            f"(зараз {deficit['daily_avg']:.1f} {deficit['unit']}/день, ціль {deficit['target']:.0f} {deficit['unit']})."
        )

    defaults = [
        "Тримайте 3 основні прийоми + перекус із білком кожні 3–4 години для стабільного метаболізму.",
        "Пийте 30–35 мл води на кг ваги та логуйте тренування для точнішого TDEE.",
        "Раз на тиждень переглядайте ціль у профілі разом із тренером.",
    ]
    fill_idx = 0
    while len(tips) < 3:
        tips.append(defaults[fill_idx % len(defaults)])
        fill_idx += 1

    return tips[:3]


def rule_based_analysis(ctx: NutritionContext) -> dict[str, Any]:
    """Deterministic analyzer that mimics AI output from real deficits."""
    score = _score_from_context(ctx)
    grade = _grade_from_score(score)
    tips = _build_recommendations(ctx)

    if ctx.days_with_logs == 0:
        summary = (
            "За останні 7 днів немає записів у щоденнику харчування. "
            "Система не може оцінити раціон — додайте прийоми їжі для аналізу."
        )
        return {"ai_grade": "C-", "nutrition_summary": summary, "recommendations": tips}

    cal_pct = ctx.adherence.get("calories", 0)
    prot_pct = ctx.adherence.get("proteins", 0)
    fat_pct = ctx.adherence.get("fats", 0)
    carb_pct = ctx.adherence.get("carbs", 0)
    # The fallback report text is Ukrainian, so it keeps Ukrainian goal names (UI labels are English).
    goal_names_uk = {"weight_loss": "схуднення", "maintenance": "підтримка форми", "muscle_gain": "набір маси"}
    goal_label = goal_names_uk.get(ctx.goal.goal_type, "підтримка") if ctx.goal else "підтримка"

    cal_comment = "калорійність у цільовому коридорі"
    if cal_pct < 85:
        cal_comment = "стійкий дефіцит калорій"
    elif cal_pct > 115 or (ctx.goal and ctx.goal.goal_type == "weight_loss" and cal_pct > 100):
        cal_comment = "перевищення калорійної цілі для схуднення"

    prot_comment = "білок у нормі"
    if prot_pct < 85:
        prot_comment = "недостатній білок"
    elif prot_pct > 120:
        prot_comment = "надлишок білка"

    macro_comment = f"жири {fat_pct:.0f}%, вуглеводи {carb_pct:.0f}% від цілі"
    micro_deficits = [d for d in ctx.deficits if d["type"] == "micronutrient"]
    if micro_deficits:
        worst = min(micro_deficits, key=lambda d: d["percent"])
        micro_comment = f"дефіцит {worst['name']} ({worst['percent']:.0f}% від норми)"
    else:
        micro_comment = "мікронутрієнти без критичних дефіцитів"

    summary = (
        f"За {ctx.days_with_logs} дн. з логами (ціль: {goal_label}): {cal_comment}, {prot_comment}, {macro_comment}. "
        f"Мікронутрієнти: {micro_comment}. "
        f"Середньоденно {ctx.daily_averages['calories']:.0f} ккал / білок {ctx.daily_averages['proteins']:.0f} г "
        f"при цілі {ctx.targets.get('calories', '—')} ккал / {ctx.targets.get('proteins', '—')} г білка. "
        f"Оцінка: {grade} ({score:.0f}/100)."
    )

    return {"ai_grade": grade, "nutrition_summary": summary, "recommendations": tips}
