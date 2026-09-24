from datetime import date

import pytest

from models import Goal, Profile
from nutrition import (
    calculate_bmr,
    calculate_daily_targets,
    calculate_tdee,
    goal_progress,
)


def _profile(**kw):
    today = date.today()
    data = dict(
        gender="male",
        birth_date=date(today.year - 30, 1, 1),  # exactly 30 years old today
        height=180,
        weight=80,
        activity_level="moderate",
    )
    data.update(kw)
    return Profile(**data)


def test_bmr_male_and_female():
    # Mifflin-St Jeor: 10*80 + 6.25*180 - 5*30 (+5 male / -161 female)
    assert calculate_bmr(_profile()) == pytest.approx(1780)
    assert calculate_bmr(_profile(gender="female")) == pytest.approx(1614)


def test_tdee_uses_activity_multiplier():
    assert calculate_tdee(_profile()) == pytest.approx(1780 * 1.55)
    assert calculate_tdee(_profile(activity_level=None)) == pytest.approx(1780 * 1.2)


def test_daily_targets_maintenance():
    targets = calculate_daily_targets(_profile(), None)
    calories = round(1780 * 1.55)
    assert targets["calories"] == calories
    assert targets["goal_type"] == "maintenance"
    assert targets["proteins"] == round(calories * 0.25 / 4, 1)
    assert targets["fats"] == round(calories * 0.30 / 9, 1)
    assert targets["carbs"] == round(calories * 0.45 / 4, 1)


def test_daily_targets_weight_loss_and_gain():
    tdee = 1780 * 1.55
    loss = calculate_daily_targets(_profile(), Goal(goal_type="weight_loss", target_weight=75, start_weight=80))
    gain = calculate_daily_targets(_profile(), Goal(goal_type="muscle_gain", target_weight=85, start_weight=80))
    assert loss["calories"] == round(tdee - 500)
    assert gain["calories"] == round(tdee + 300)


def test_daily_targets_floor_1200():
    tiny = _profile(gender="female", height=100, weight=30, activity_level="sedentary")
    goal = Goal(goal_type="weight_loss", target_weight=28, start_weight=30)
    assert calculate_daily_targets(tiny, goal)["calories"] == 1200


@pytest.mark.parametrize("field", ["height", "weight", "birth_date"])
def test_missing_body_metrics_do_not_crash(field):
    profile = _profile(**{field: None})
    assert calculate_bmr(profile) == 0.0
    assert calculate_tdee(profile) == 0.0
    assert calculate_daily_targets(profile, None) is None


def test_missing_gender_uses_neutral_constant():
    assert calculate_bmr(_profile(gender=None)) == pytest.approx(1780 - 5 - 78)
    assert calculate_daily_targets(_profile(gender=None), None) is not None


def test_none_profile():
    assert calculate_bmr(None) == 0.0
    assert calculate_tdee(None) == 0.0
    assert calculate_daily_targets(None, None) is None


def test_goal_progress_values():
    goal = Goal(goal_type="weight_loss", target_weight=70, start_weight=80)
    progress = goal_progress(_profile(weight=75), goal)
    assert progress["percent"] == 50.0
    assert progress["current_weight"] == 75.0


def test_goal_progress_none_handling_and_zero_division():
    goal = Goal(goal_type="weight_loss", target_weight=80, start_weight=80)
    assert goal_progress(_profile(weight=None), goal) is None
    assert goal_progress(_profile(), None) is None
    # start == target must not divide by zero
    assert goal_progress(_profile(), goal)["percent"] == 100.0
    maint = Goal(goal_type="maintenance", target_weight=80, start_weight=80)
    assert goal_progress(_profile(), maint)["percent"] == 100.0
