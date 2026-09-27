"""Localisation: English source strings, Ukrainian translations in translations/uk/LC_MESSAGES.

Language choice, in order: the signed-in user's saved language, the `lang` cookie (set by the
switcher, also before sign-in), then the browser's Accept-Language; English if nothing matches.

Update the catalogue after changing strings (these options keep the diffs small; messages.pot is a
temporary template and is not committed):
    pybabel extract -F babel.cfg -k lazy_gettext --no-location -o messages.pot .
    pybabel update -w 100 -i messages.pot -d translations
    (translate the new entries in translations/uk/LC_MESSAGES/messages.po)
    pybabel compile -d translations
"""

from __future__ import annotations

from flask import has_request_context, request
from flask_babel import get_locale, gettext
from flask_login import current_user

LANGUAGES = {"en": "English", "uk": "Українська"}
DEFAULT_LANGUAGE = "en"
LANG_COOKIE = "lang"
LANG_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# Names the AI coach understands when told which language to answer in.
MODEL_LANGUAGE_NAMES = {"en": "English", "uk": "Ukrainian"}


def N_(text: str) -> str:
    """Mark a string for extraction without translating it here (translated where it is shown)."""
    return text


# Values the templates show via _(value): meal types, goals, activity levels, sexes.
DYNAMIC_LABELS = [
    N_("Breakfast"), N_("Lunch"), N_("Dinner"), N_("Snack"), N_("Other"),
    N_("Weight loss"), N_("Maintenance"), N_("Muscle gain"),
    N_("Sedentary"), N_("Moderate"), N_("Active"),
    N_("Male"), N_("Female"),
    N_("Zernia"),  # the AI coach's name: Зернятко in Ukrainian
]

# Strings used by static/js/*.js (English source; {name} placeholders are filled in by App.t).
JS_STRINGS = [
    N_("You're offline. Reconnect to save your changes."),
    N_("You're offline. Reconnect to talk to the coach."),
    N_("Offline — showing saved data"),
    N_("Could not save. Please try again."),
    N_("The coach could not answer. Please try again."),
    N_("Dismiss notification"),
    N_("Delete"),
    N_("{kcal} kcal · P {p} · F {f} · C {c}"),
    N_("last {g} g"),
    N_("per 100 g"),
    N_("Add {name} to favorites"),
    N_("Remove {name} from favorites"),
    N_("Foods you log will show up here with your usual portion. Search or scan to get started."),
    N_("Tap the star next to a food to keep it here."),
    N_("Foods you create appear here. Use “Create food” for home recipes."),
    N_("Could not update favorites."),
    N_("No matches in your foods yet…"),
    N_("No matches. You are offline, so online search is unavailable."),
    N_("Searching Open Food Facts…"),
    N_("Nothing found. Try another name, scan the barcode, or create the food."),
    N_("Online search is unavailable right now."),
    N_("Choose a food first."),
    N_("Looking up {code}…"),
    N_("Found: {name}"),
    N_("Barcode {code}"),
    N_("Could not look up this barcode."),
    N_("Could not reach the food database."),
    N_("You are offline — try again when connected."),
    N_("Camera needs a secure (HTTPS) connection. Type the number below instead."),
    N_("Starting camera…"),
    N_("Point the camera at a barcode."),
    N_("Camera access was denied. Type the barcode number below instead."),
    N_("Camera is not available. Type the barcode number below instead."),
    N_("A barcode has 8 to 14 digits."),
    N_("Nutri-Score {g}"),
    N_("Nutri-Score {g} (predicted by Kolos)"),
    N_("Not in the food list yet. Try search."),
    N_("Confidence {n}%"),
    N_("Selected: {name}"),
    N_("Recognising…"),
    N_("Could not recognise this photo."),
    N_("No food found on this photo. Try another angle."),
]


def normalize_language(value: str | None) -> str | None:
    value = (value or "").strip().lower()[:2]
    return value if value in LANGUAGES else None


def select_locale() -> str:
    """Flask-Babel locale selector."""
    if not has_request_context():
        return DEFAULT_LANGUAGE
    if current_user and current_user.is_authenticated:
        saved = normalize_language(getattr(current_user, "language", None))
        if saved:
            return saved
    cookie = normalize_language(request.cookies.get(LANG_COOKIE))
    if cookie:
        return cookie
    return request.accept_languages.best_match(list(LANGUAGES)) or DEFAULT_LANGUAGE


def coach_display_name() -> str:
    """The AI coach's name in the current language (Zernia / Зернятко)."""
    return gettext("Zernia")


def current_language() -> str:
    locale = get_locale() if has_request_context() else None
    return normalize_language(str(locale)) if locale else DEFAULT_LANGUAGE


def js_translations() -> dict[str, str]:
    """Translations for the JS strings in the current locale (only the ones that differ)."""
    out = {}
    for text in JS_STRINGS:
        translated = gettext(text)
        if translated != text:
            out[text] = translated
    return out


def format_local_date(value, style: str = "short") -> str:
    """Dates in the current language (Jinja filter `ldate`). Styles:
    full "Saturday, September 26" · long "September 26, 2026" · short "Sep 26" · weekday "Saturday" ·
    dow "Sat" · month "September 2026" · numeric "9/26/2026" · datetime "Sep 26, 11:31" · chart "26 Sat".
    """
    from babel.dates import format_date, format_skeleton, format_time

    if value is None:
        return ""
    loc = current_language()
    if style == "full":
        return f"{format_date(value, 'EEEE', locale=loc)}, {format_skeleton('MMMMd', value, locale=loc)}"
    if style == "long":
        return format_date(value, "long", locale=loc)
    if style == "weekday":
        return format_date(value, "EEEE", locale=loc)
    if style == "dow":
        return format_date(value, "EEE", locale=loc)
    if style == "month":
        return format_skeleton("yMMMM", value, locale=loc)
    if style == "numeric":
        return format_skeleton("yMd", value, locale=loc)
    if style == "datetime":
        return f"{format_skeleton('MMMd', value, locale=loc)}, {format_time(value, 'HH:mm', locale=loc)}"
    if style == "chart":
        return format_skeleton("Ed", value, locale=loc)
    return format_skeleton("MMMd", value, locale=loc)
