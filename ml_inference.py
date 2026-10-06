"""Kolos' own ML models, as seen by the web app.

Two models are trained offline (see the training step of the project) and plugged in here:

* food score — predicts a product's Nutri-Score grade (A–E) from its nutrition per 100 g, for
  products Open Food Facts has no grade for (user foods, catalogue foods, unrated OFF items);
* food photo — recognises a dish on a photo and returns the most likely classes.

Until a model is registered, the app falls back gracefully: products show only the grades Open
Food Facts already has, and photo recognition answers "not ready yet" (HTTP 503). A model is any
callable with the signature below, so the training code can register a scikit-learn pipeline,
an ONNX session wrapper or a test double without the web app knowing the difference.
"""
from __future__ import annotations

from typing import Callable, Protocol

GRADES = ("a", "b", "c", "d", "e")
GRADE_SOURCE_OFF = "off"      # grade published by Open Food Facts
GRADE_SOURCE_MODEL = "model"  # grade predicted by the Kolos food-score model

PHOTO_TOP_K = 3
PHOTO_MAX_BYTES = 8 * 1024 * 1024
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}


class _Nutrition(Protocol):
    calories_per_100g: float
    proteins: float
    fats: float
    carbs: float


ScoreModel = Callable[[dict[str, float]], str | None]
"""features per 100 g → grade "a".."e" (or None when the model is unsure)."""

PhotoModel = Callable[[bytes, int], list[tuple[str, float]]]
"""(image bytes, top k) → [(class label, probability), …] sorted by probability."""

_score_model: ScoreModel | None = None
_photo_model: PhotoModel | None = None


def register_score_model(model: ScoreModel | None) -> None:
    global _score_model
    _score_model = model


def register_photo_model(model: PhotoModel | None) -> None:
    global _photo_model
    _photo_model = model


def score_model_ready() -> bool:
    return _score_model is not None


def photo_model_ready() -> bool:
    return _photo_model is not None


def normalize_grade(value: object) -> str | None:
    grade = str(value or "").strip().lower()
    return grade if grade in GRADES else None


def nutrition_features(product: _Nutrition) -> dict[str, float]:
    """Model input: nutrition per 100 g (the training step may add more fields)."""
    return {
        "kcal": float(product.calories_per_100g or 0),
        "proteins": float(product.proteins or 0),
        "fats": float(product.fats or 0),
        "carbs": float(product.carbs or 0),
    }


def predict_grade(product: _Nutrition) -> str | None:
    if _score_model is None:
        return None
    return normalize_grade(_score_model(nutrition_features(product)))


def recognize_photo(image: bytes, top_k: int = PHOTO_TOP_K) -> list[dict]:
    """Top-k dish classes for a photo; raises RuntimeError when no photo model is registered."""
    if _photo_model is None:
        raise RuntimeError("photo model is not available")
    return [
        {"label": label, "confidence": round(float(prob), 4)}
        for label, prob in _photo_model(image, top_k)[:top_k]
    ]


def product_grade(product) -> tuple[str | None, str | None]:
    """(grade, source) for a Product: the stored Open Food Facts grade, else the model's prediction."""
    if product.nutri_grade:
        return product.nutri_grade, product.nutri_source or GRADE_SOURCE_OFF
    grade = predict_grade(product)
    return (grade, GRADE_SOURCE_MODEL) if grade else (None, None)
