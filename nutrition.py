"""BMR/TDEE, macro targets, weekly check-in, water and weight summaries."""

from datetime import date, timedelta

from sqlalchemy.orm import joinedload

from models import FoodLog, Goal, Profile, WaterLog, WeightLog, db

KCAL_PER_KG = 7700
WATER_ML_PER_KG = 33
DEFAULT_WATER_GOAL_ML = 2000

ACTIVITY_MULTIPLIERS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
}

GOAL_LABELS = {
    "weight_loss": "Weight loss",
    "maintenance": "Maintenance",
    "muscle_gain": "Muscle gain",
}


def calculate_age(birth_date: date) -> int:
    today = date.today()
    return today.year - birth_date.year - (
        (today.month, today.day) < (birth_date.month, birth_date.day)
    )


def has_body_metrics(profile: Profile | None) -> bool:
    """True when the profile has everything Mifflin-St Jeor needs (weight, height, birth date)."""
    if profile is None or profile.weight is None or profile.height is None or profile.birth_date is None:
        return False
    return float(profile.weight) > 0 and float(profile.height) > 0


def calculate_bmr(profile: Profile | None) -> float:
    """Mifflin-St Jeor BMR; 0.0 when weight, height or birth date is missing."""
    if not has_body_metrics(profile):
        return 0.0
    weight = float(profile.weight)
    height = float(profile.height)
    age = max(0, calculate_age(profile.birth_date))
    base = 10 * weight + 6.25 * height - 5 * age
    if profile.gender == "female":
        return base - 161
    if profile.gender == "male":
        return base + 5
    # Unknown gender: midpoint of the male/female constants.
    return base - 78


def calculate_tdee(profile: Profile | None) -> float:
    if profile is None:
        return 0.0
    multiplier = ACTIVITY_MULTIPLIERS.get(profile.activity_level or "sedentary", 1.2)
    return calculate_bmr(profile) * multiplier


GOAL_CALORIE_OFFSET = {"weight_loss": -500, "muscle_gain": 300, "maintenance": 0}
MIN_CALORIE_TARGET = 1200


def goal_calorie_target(tdee: float, goal_type: str) -> int:
    return max(MIN_CALORIE_TARGET, round(tdee + GOAL_CALORIE_OFFSET.get(goal_type, 0)))


def calculate_daily_targets(profile: Profile | None, goal: Goal | None) -> dict | None:
    """Return daily calorie and macro targets, or None if the profile lacks body metrics.

    profile.calorie_target_override (set from the weekly check-in) replaces the calorie
    target; macros are split with the same goal percentages either way.
    """
    if not has_body_metrics(profile):
        return None
    tdee = calculate_tdee(profile)
    goal_type = goal.goal_type if goal else "maintenance"

    if goal_type in ("weight_loss", "muscle_gain"):
        protein_pct, fat_pct, carb_pct = 0.30, 0.25, 0.45
    else:
        protein_pct, fat_pct, carb_pct = 0.25, 0.30, 0.45

    calculated = goal_calorie_target(tdee, goal_type)
    override = profile.calorie_target_override
    calories = max(MIN_CALORIE_TARGET, int(override)) if override else calculated

    return {
        "calories": calories,
        "calculated_calories": calculated,
        "is_adaptive": bool(override),
        "proteins": round(calories * protein_pct / 4, 1),
        "fats": round(calories * fat_pct / 9, 1),
        "carbs": round(calories * carb_pct / 4, 1),
        "bmr": round(calculate_bmr(profile), 0),
        "tdee": round(tdee, 0),
        "goal_type": goal_type,
        "goal_label": GOAL_LABELS.get(goal_type, goal_type),
    }


def goal_progress(profile: Profile | None, goal: Goal | None) -> dict | None:
    """Progress metrics for the goal progress widget, or None without a weight or goal."""
    if profile is None or profile.weight is None or goal is None:
        return None
    current = float(profile.weight)
    start = float(goal.start_weight)
    target = float(goal.target_weight)

    if goal.goal_type == "weight_loss":
        total_change = start - target
        achieved = start - current
    elif goal.goal_type == "muscle_gain":
        total_change = target - start
        achieved = current - start
    else:
        total_change = abs(target - start) or 1.0
        achieved = total_change - abs(current - target)

    percent = min(100.0, max(0.0, (achieved / total_change) * 100)) if total_change > 0 else 100.0

    return {
        "current_weight": round(current, 1),
        "start_weight": round(start, 1),
        "target_weight": round(target, 1),
        "percent": round(percent, 1),
        "goal_label": GOAL_LABELS.get(goal.goal_type, goal.goal_type),
    }


def auto_water_goal_ml(profile: Profile | None) -> int:
    """33 ml per kg of body weight, rounded to 50 ml; 2000 ml without a weight."""
    if profile is None or not profile.weight or float(profile.weight) <= 0:
        return DEFAULT_WATER_GOAL_ML
    return int(round(float(profile.weight) * WATER_ML_PER_KG / 50) * 50)


def water_goal_ml(profile: Profile | None) -> int:
    if profile is not None and profile.water_goal_ml:
        return int(profile.water_goal_ml)
    return auto_water_goal_ml(profile)


def water_total_ml(user_id: int, day: date) -> int:
    total = (
        db.session.query(db.func.coalesce(db.func.sum(WaterLog.amount_ml), 0))
        .filter(WaterLog.user_id == user_id, WaterLog.date == day)
        .scalar()
    )
    return int(total)


def logging_streak(user_id: int, today: date | None = None) -> dict:
    """Consecutive days with at least one food log, ending today (or yesterday if today is empty)."""
    today = today or date.today()
    days = {
        d for (d,) in db.session.query(FoodLog.date)
        .filter(FoodLog.user_id == user_id, FoodLog.date <= today)
        .distinct()
    }

    current = 0
    day = today if today in days else today - timedelta(days=1)
    while day in days:
        current += 1
        day -= timedelta(days=1)

    best = run = 0
    previous = None
    for d in sorted(days):
        run = run + 1 if previous and d - previous == timedelta(days=1) else 1
        best = max(best, run)
        previous = d
    return {"current": current, "best": best}


def weight_summary(user_id: int) -> dict | None:
    """Latest weigh-in and the change versus the closest weigh-in at least 7 days earlier."""
    latest = (
        WeightLog.query.filter_by(user_id=user_id).order_by(WeightLog.date.desc()).first()
    )
    if latest is None:
        return None
    baseline = (
        WeightLog.query.filter(
            WeightLog.user_id == user_id,
            WeightLog.date <= latest.date - timedelta(days=7),
        )
        .order_by(WeightLog.date.desc())
        .first()
    )
    weight = float(latest.weight_kg)
    return {
        "weight": round(weight, 1),
        "date": latest.date,
        "change_7d": round(weight - float(baseline.weight_kg), 1) if baseline else None,
    }


CHECKIN_WINDOW_DAYS = 14
CHECKIN_MIN_LOGGED_DAYS = 7
CHECKIN_MIN_WEIGH_IN_SPAN = 7


def weekly_checkin(user_id: int, profile: Profile | None, goal: Goal | None,
                   today: date | None = None) -> dict:
    """MacroFactor-style check-in: real TDEE = average intake − Δweight × 7700 / days.

    Uses the last 14 complete days (today's log is still open). Needs ≥7 logged days
    and two weigh-ins at least 7 days apart; otherwise returns progress towards that.
    """
    today = today or date.today()
    start = today - timedelta(days=CHECKIN_WINDOW_DAYS)
    end = today - timedelta(days=1)

    intake_by_day: dict[date, float] = {}
    logs = (
        FoodLog.query.filter(FoodLog.user_id == user_id, FoodLog.date.between(start, end))
        .options(joinedload(FoodLog.product))
        .all()
    )
    for log in logs:
        intake_by_day[log.date] = intake_by_day.get(log.date, 0.0) + log.calories

    weigh_ins = (
        WeightLog.query.filter(WeightLog.user_id == user_id, WeightLog.date.between(start, today))
        .order_by(WeightLog.date)
        .all()
    )
    span = (weigh_ins[-1].date - weigh_ins[0].date).days if len(weigh_ins) >= 2 else 0
    result = {
        "ready": False,
        "days_logged": len(intake_by_day),
        "weigh_ins": len(weigh_ins),
        "weigh_in_span": span,
        "min_days_logged": CHECKIN_MIN_LOGGED_DAYS,
        "min_weigh_in_span": CHECKIN_MIN_WEIGH_IN_SPAN,
        "window_days": CHECKIN_WINDOW_DAYS,
    }
    if len(intake_by_day) < CHECKIN_MIN_LOGGED_DAYS or span < CHECKIN_MIN_WEIGH_IN_SPAN:
        return result

    avg_intake = sum(intake_by_day.values()) / len(intake_by_day)
    weight_change = float(weigh_ins[-1].weight_kg) - float(weigh_ins[0].weight_kg)
    real_tdee = avg_intake - weight_change * KCAL_PER_KG / span
    # Guard against implausible values from sparse or inaccurate logging.
    real_tdee = min(6000.0, max(MIN_CALORIE_TARGET, real_tdee))
    goal_type = goal.goal_type if goal else "maintenance"

    result.update(
        ready=True,
        avg_intake=round(avg_intake),
        weight_change=round(weight_change, 1),
        real_tdee=round(real_tdee),
        formula_tdee=round(calculate_tdee(profile)) if profile else None,
        suggested_target=goal_calorie_target(real_tdee, goal_type),
        goal_label=GOAL_LABELS.get(goal_type, goal_type),
    )
    return result
