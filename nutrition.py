"""BMR/TDEE, macro targets, and weight trend forecasting."""

import math
from datetime import date, timedelta

from sqlalchemy.orm import joinedload

from models import FoodLog, Goal, Profile, WaterLog, WeightLog, Workout, db

KCAL_PER_KG = 7700
WATER_ML_PER_KG = 33
DEFAULT_WATER_GOAL_ML = 2000
HISTORY_DAYS = 7
KCAL_TDEE_DROP_PER_KG = 24

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


def calculate_daily_targets(profile: Profile | None, goal: Goal | None) -> dict | None:
    """Return daily calorie and macro targets, or None if the profile lacks body metrics."""
    if not has_body_metrics(profile):
        return None
    tdee = calculate_tdee(profile)
    goal_type = goal.goal_type if goal else "maintenance"

    if goal_type == "weight_loss":
        calories = tdee - 500
        protein_pct, fat_pct, carb_pct = 0.30, 0.25, 0.45
    elif goal_type == "muscle_gain":
        calories = tdee + 300
        protein_pct, fat_pct, carb_pct = 0.30, 0.25, 0.45
    else:
        calories = tdee
        protein_pct, fat_pct, carb_pct = 0.25, 0.30, 0.45

    calories = max(1200, round(calories))

    return {
        "calories": calories,
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


def _daily_food_calories(user_id: int, day: date) -> float:
    logs = (
        FoodLog.query.filter_by(user_id=user_id, date=day)
        .options(joinedload(FoodLog.product))
        .all()
    )
    return sum(log.calories for log in logs)


def _daily_workout_calories(user_id: int, day: date) -> float:
    rows = (
        Workout.query.filter(
            Workout.user_id == user_id,
            db.func.date(Workout.created_at) == day,
        ).all()
    )
    return sum(float(w.calories_burned) for w in rows)


def _weekly_energy_averages(user_id: int, profile: Profile, end_day: date | None = None) -> dict:
    """Average daily intake and expenditure (TDEE + workouts) over the last 7 days."""
    end_day = end_day or date.today()
    start_day = end_day - timedelta(days=HISTORY_DAYS - 1)

    intake_total = 0.0
    expenditure_total = 0.0
    tdee = calculate_tdee(profile)

    for offset in range(HISTORY_DAYS):
        day = start_day + timedelta(days=offset)
        intake_total += _daily_food_calories(user_id, day)
        expenditure_total += tdee + _daily_workout_calories(user_id, day)

    return {
        "avg_intake": intake_total / HISTORY_DAYS,
        "avg_expenditure": expenditure_total / HISTORY_DAYS,
        "avg_balance": (intake_total - expenditure_total) / HISTORY_DAYS,
        "tdee": tdee,
    }


def _estimate_weight_on_date(
    current_weight: float,
    current_day: date,
    target_day: date,
    user_id: int,
    profile: Profile,
) -> float:
    """Back-calculate weight on target_day from current weight using logged energy balance."""
    if target_day >= current_day:
        return current_weight

    net_kcal = 0.0
    day = target_day + timedelta(days=1)
    tdee = calculate_tdee(profile)
    while day <= current_day:
        intake = _daily_food_calories(user_id, day)
        spent = tdee + _daily_workout_calories(user_id, day)
        net_kcal += intake - spent
        day += timedelta(days=1)

    return current_weight - (net_kcal / KCAL_PER_KG)


def _apply_target_cap(weight: float, goal: Goal | None) -> float:
    if not goal:
        return weight
    target = float(goal.target_weight)
    if goal.goal_type == "weight_loss":
        return max(target, weight)
    if goal.goal_type == "muscle_gain":
        return min(target, weight)
    return weight


def _avg_daily_workout_calories(user_id: int, end_day: date) -> float:
    start_day = end_day - timedelta(days=HISTORY_DAYS - 1)
    total = 0.0
    for offset in range(HISTORY_DAYS):
        day = start_day + timedelta(days=offset)
        total += _daily_workout_calories(user_id, day)
    return total / HISTORY_DAYS


def _adaptive_tdee(base_tdee: float, weight: float, start_weight: float, goal: Goal | None) -> float:
    """TDEE adapts ~24 kcal per kg lost (or gained) from the starting weight."""
    if not goal:
        return base_tdee
    if goal.goal_type == "weight_loss":
        kg_lost = max(0.0, start_weight - weight)
        return base_tdee - KCAL_TDEE_DROP_PER_KG * kg_lost
    if goal.goal_type == "muscle_gain":
        kg_gained = max(0.0, weight - start_weight)
        return base_tdee + KCAL_TDEE_DROP_PER_KG * kg_gained
    return base_tdee


def _proximity_dampening(weight: float, start_weight: float, target: float, goal_type: str) -> float:
    """Exponential slowdown as weight approaches the goal (smooth curve near target)."""
    if goal_type == "weight_loss" and weight > target:
        span = max(start_weight - target, 0.5)
        remaining = weight - target
        return max(0.12, 1 - math.exp(-remaining / (span * 0.35)))
    if goal_type == "muscle_gain" and weight < target:
        span = max(target - start_weight, 0.5)
        remaining = target - weight
        return max(0.12, 1 - math.exp(-remaining / (span * 0.35)))
    return 1.0


def _simulate_weight_forecast(
    start_weight: float,
    goal: Goal | None,
    avg_intake: float,
    base_tdee: float,
    avg_workout: float,
    days_forecast: int,
) -> list[float]:
    """Day-by-day nonlinear forecast with metabolic adaptation and goal proximity damping."""
    weight = start_weight
    points: list[float] = []

    for _ in range(days_forecast):
        tdee = _adaptive_tdee(base_tdee, weight, start_weight, goal)
        balance = avg_intake - tdee - avg_workout
        delta_kg = balance / KCAL_PER_KG

        if goal:
            target = float(goal.target_weight)
            delta_kg *= _proximity_dampening(weight, start_weight, target, goal.goal_type)

        weight += delta_kg
        weight = _apply_target_cap(weight, goal)
        points.append(round(weight, 2))

    return points


def predict_weight_trend(user_id: int, days_forecast: int = 30) -> dict:
    """
    Forecast body weight from average weekly energy balance (7700 kcal ≈ 1 kg).
    Returns Chart.js-ready labels and series for actual (estimated) vs forecast lines.
    """
    profile = Profile.query.filter_by(user_id=user_id).first()
    if not has_body_metrics(profile):
        return {
            "labels": [],
            "actual": [],
            "forecast": [],
            "target_weight": None,
            "meta": {},
        }

    goal = Goal.query.filter_by(user_id=user_id, status="active").first()
    today = date.today()
    current_weight = float(profile.weight)
    energy = _weekly_energy_averages(user_id, profile, today)
    base_tdee = calculate_tdee(profile)
    avg_workout = _avg_daily_workout_calories(user_id, today)
    forecast_weights = _simulate_weight_forecast(
        current_weight,
        goal,
        energy["avg_intake"],
        base_tdee,
        avg_workout,
        days_forecast,
    )
    initial_delta = (energy["avg_balance"] / KCAL_PER_KG) if energy["avg_balance"] else 0
    final_delta = 0.0
    if len(forecast_weights) >= 2:
        final_delta = (forecast_weights[-1] - forecast_weights[-2])

    labels: list[str] = []
    actual: list[float | None] = []
    forecast: list[float | None] = []

    history_start = today - timedelta(days=HISTORY_DAYS - 1)
    for offset in range(HISTORY_DAYS):
        day = history_start + timedelta(days=offset)
        labels.append(day.strftime("%d.%m"))
        w = _estimate_weight_on_date(current_weight, today, day, user_id, profile)
        actual.append(round(w, 2))
        forecast.append(None)

    for step, projected in enumerate(forecast_weights, start=1):
        day = today + timedelta(days=step)
        labels.append(day.strftime("%d.%m"))
        actual.append(None)
        forecast.append(projected)

    forecast[HISTORY_DAYS - 1] = round(current_weight, 2)

    return {
        "labels": labels,
        "actual": actual,
        "forecast": forecast,
        "target_weight": round(float(goal.target_weight), 1) if goal else None,
        "meta": {
            "avg_intake_kcal": round(energy["avg_intake"], 0),
            "avg_expenditure_kcal": round(energy["avg_expenditure"], 0),
            "avg_balance_kcal": round(energy["avg_balance"], 0),
            "daily_delta_kg": round(initial_delta, 4),
            "daily_delta_kg_day30": round(final_delta, 4),
            "current_weight": round(current_weight, 1),
            "model": "adaptive_tdee",
        },
    }
