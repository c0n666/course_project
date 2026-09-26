import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from functools import wraps

import click
from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFError, CSRFProtect
from markupsafe import Markup, escape
from sqlalchemy import event, func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import joinedload
from sqlalchemy.pool import NullPool
from werkzeug.security import check_password_hash, generate_password_hash

from models import (
    SOURCE_QUICK,
    SOURCE_SEED,
    SOURCE_USER,
    CoachMessage,
    FavoriteProduct,
    FoodLog,
    Goal,
    Micronutrient,
    Product,
    ProductMicronutrient,
    Profile,
    Recommendation,
    Report,
    User,
    WaterLog,
    WeightLog,
    Workout,
    db,
)
import food_db
from coach_agent import (
    CHAT_MAX_CHARS,
    ChatLimitError,
    CoachError,
    chat_history,
    coach_available,
    coach_chat,
    generate_coach_report,
    latest_coach_report,
)
from llm_providers import ProviderError
from seed_foods import GENERIC_FOODS, UK_SEARCH_NAMES
from nutrition import (
    auto_water_goal_ml,
    calculate_daily_targets,
    goal_progress,
    logging_streak,
    predict_weight_trend,
    water_goal_ml,
    water_total_ml,
    weekly_checkin,
    weight_summary,
)

logger = logging.getLogger(__name__)
csrf = CSRFProtect()

MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")


def _set_sqlite_pragmas(dbapi_connection, _connection_record):
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _configure_sqlite(app: Flask) -> None:
    """Reduce 'database is locked' errors: WAL journal, busy timeout, connection timeout."""
    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if not uri.startswith("sqlite"):
        return

    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {
            "check_same_thread": False,
            "timeout": 30,
        },
        "poolclass": NullPool,
    }


def _register_sqlite_pragmas(app: Flask) -> None:
    """Attach the PRAGMA hook to this app's engine only (not globally to every Engine)."""
    if not app.config.get("SQLALCHEMY_DATABASE_URI", "").startswith("sqlite"):
        return
    with app.app_context():
        engine = db.engine
        if not event.contains(engine, "connect", _set_sqlite_pragmas):
            event.listen(engine, "connect", _set_sqlite_pragmas)


def db_commit_with_retry(retries: int = 5, delay: float = 0.15) -> None:
    """Commit with short retries when SQLite reports a transient lock."""
    for attempt in range(retries):
        try:
            db.session.commit()
            return
        except OperationalError as exc:
            db.session.rollback()
            if "locked" not in str(exc).lower() or attempt >= retries - 1:
                raise
            time.sleep(delay * (attempt + 1))


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    secret_key = os.environ.get("SECRET_KEY", "").strip()
    if not secret_key:
        logger.warning(
            "SECRET_KEY is not set; using a random per-process key. Sessions will not "
            "survive a restart. Set the SECRET_KEY environment variable in production."
        )
        secret_key = secrets.token_hex(32)
    app.config["SECRET_KEY"] = secret_key
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///nutrition_db.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    if test_config:
        app.config.update(test_config)

    _configure_sqlite(app)
    db.init_app(app)
    _register_sqlite_pragmas(app)
    csrf.init_app(app)

    @app.errorhandler(CSRFError)
    def _handle_csrf_error(_exc):
        message = "Your session expired or the request was invalid. Refresh the page and try again."
        if request.is_json or request.accept_mimetypes.best == "application/json":
            return jsonify(error=message), 400  # fetch() callers need JSON, not a redirect
        flash(message, "error")
        return redirect(url_for("index"))

    @app.teardown_appcontext
    def _close_db_session(_exc=None):
        db.session.remove()

    with app.app_context():
        _enable_wal_on_existing_db()

    app.add_template_filter(coach_markup, "coach_markup")

    login_manager = LoginManager(app)
    login_manager.login_view = "login"
    login_manager.login_message_category = "info"

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        try:
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None

    register_routes(app)
    return app


def _enable_wal_on_existing_db() -> None:
    """Switch the DB file to WAL once when possible; skip silently if another process holds the file."""
    for attempt in range(3):
        try:
            with db.engine.connect() as conn:
                mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
                if mode and str(mode).lower() != "wal":
                    conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                conn.exec_driver_sql("PRAGMA busy_timeout=30000")
                conn.commit()
            return
        except OperationalError:
            if attempt >= 2:
                return
            time.sleep(0.25 * (attempt + 1))


def role_required(*roles: str):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                flash("You do not have permission to access this page.", "error")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def food_logs_for_day(user_id: int, day: date | None = None) -> list[FoodLog]:
    day = day or date.today()
    return (
        FoodLog.query.filter_by(user_id=user_id, date=day)
        .options(
            joinedload(FoodLog.product).joinedload(Product.micronutrient_links).joinedload(
                ProductMicronutrient.micronutrient
            )
        )
        .order_by(FoodLog.id.desc())
        .all()
    )


def product_payload(product: Product, favorite_ids=frozenset(), portion: float | None = None) -> dict:
    """JSON-friendly product for the Log Food picker (values per 100 g)."""
    return {
        "id": product.id,
        "name": product.name,
        "brand": product.brand,
        "label": product.display_name,
        "kcal": float(product.calories_per_100g),
        "p": float(product.proteins),
        "f": float(product.fats),
        "c": float(product.carbs),
        "source": product.source,
        "terms": product.search_terms or product.name.lower(),
        "favorite": product.id in favorite_ids,
        "portion": portion,
    }


def favorite_ids_for(user_id: int) -> set[int]:
    return {pid for (pid,) in db.session.query(FavoriteProduct.product_id).filter_by(user_id=user_id)}


def recent_products_for(user_id: int, limit: int = 12) -> list[tuple[Product, float]]:
    """Most recently logged distinct products with the portion used last time."""
    last_ids = (
        db.session.query(func.max(FoodLog.id))
        .filter(FoodLog.user_id == user_id)
        .group_by(FoodLog.product_id)
        .order_by(func.max(FoodLog.id).desc())
        .limit(limit * 2)  # headroom for quick-add entries filtered out below
        .all()
    )
    logs = (
        FoodLog.query.options(joinedload(FoodLog.product))
        .filter(FoodLog.id.in_([i for (i,) in last_ids]))
        .order_by(FoodLog.id.desc())
        .all()
    )
    return [(log.product, float(log.portion_grams)) for log in logs if log.product.source != SOURCE_QUICK][:limit]


def meal_counts_for_day(user_id: int, day: date) -> dict[str, dict]:
    """{meal_type: {"count", "kcal"}} for one day — used to offer copying a previous day."""
    out: dict[str, dict] = {}
    for log in food_logs_for_day(user_id, day):
        item = out.setdefault(log.meal_type, {"count": 0, "kcal": 0.0})
        item["count"] += 1
        item["kcal"] += log.calories
    return out


def water_state(user_id: int, day: date, profile: Profile | None) -> dict:
    """Water card data: total, goal, ring percent and whether there is an entry to undo."""
    total = water_total_ml(user_id, day)
    goal = water_goal_ml(profile)
    return {
        "total": total,
        "goal": goal,
        "pct": round(min(100.0, total / goal * 100), 1) if goal else 0.0,
        "can_undo": WaterLog.query.filter_by(user_id=user_id, date=day).first() is not None,
    }


def record_weight(user_id: int, day: date, weight: float) -> None:
    """Upsert the weigh-in for a day; the newest weigh-in also becomes profile.weight."""
    entry = WeightLog.query.filter_by(user_id=user_id, date=day).first()
    if entry:
        entry.weight_kg = weight
    else:
        db.session.add(WeightLog(user_id=user_id, date=day, weight_kg=weight))
    newer = WeightLog.query.filter(WeightLog.user_id == user_id, WeightLog.date > day).first()
    if newer is None:
        profile = Profile.query.filter_by(user_id=user_id).first()
        if profile is None:
            profile = Profile(user_id=user_id)
            db.session.add(profile)
        profile.weight = weight


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_BULLET = re.compile(r"^\s*(?:[-*•])\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def coach_markup(text: str) -> Markup:
    """Render the coach's plain-text reply as safe HTML: paragraphs, "- " / "1." lists and **bold**.
    Everything is escaped first, so model output can never inject markup."""
    def inline(line: str) -> str:
        return _BOLD.sub(r"<strong>\1</strong>", str(escape(line.strip().lstrip("#").strip())))

    html, list_tag, paragraph = [], None, []

    def flush_paragraph():
        if paragraph:
            html.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            html.append(f"</{list_tag}>")
            list_tag = None

    for line in (text or "").splitlines():
        bullet, numbered = _BULLET.match(line), _NUMBERED.match(line)
        if bullet or numbered:
            flush_paragraph()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close_list()
                html.append(f"<{tag}>")
                list_tag = tag
            html.append(f"<li>{inline((bullet or numbered).group(1))}</li>")
        elif line.strip():
            close_list()
            paragraph.append(inline(line))
        else:
            close_list()
            flush_paragraph()
    close_list()
    flush_paragraph()
    return Markup("".join(html))


def chat_message_payload(message) -> dict:
    return {
        "id": message.id,
        "role": message.role,
        "html": str(coach_markup(message.content)) if message.role == "assistant" else None,
        "text": message.content if message.role == "user" else None,
    }


def wants_json() -> bool:
    return request.is_json or request.accept_mimetypes.best == "application/json"


def daily_nutrition_summary(user_id: int, day: date | None = None) -> dict:
    logs = food_logs_for_day(user_id, day)
    return {
        "calories": sum(log.calories for log in logs),
        "proteins": sum(log.proteins_g for log in logs),
        "fats": sum(log.fats_g for log in logs),
        "carbs": sum(log.carbs_g for log in logs),
    }


def daily_micronutrient_totals(user_id: int, day: date | None = None) -> list[dict]:
    """Total consumed per micronutrient for the given day."""
    day = day or date.today()
    nutrients = Micronutrient.query.order_by(Micronutrient.name).all()
    totals = {n.id: 0.0 for n in nutrients}

    for log in food_logs_for_day(user_id, day):
        grams = log.grams
        for link in log.product.micronutrient_links:
            totals[link.micronutrient_id] += link.amount_for_portion(grams)

    return [
        {
            "name": n.name,
            "unit": n.unit,
            "total": round(totals[n.id], 2),
        }
        for n in nutrients
    ]


def daily_calories(user_id: int, day: date | None = None) -> float:
    return daily_nutrition_summary(user_id, day)["calories"]


def calorie_trend_7_days(user_id: int, end_day: date | None = None) -> dict:
    end_day = end_day or date.today()
    labels, values = [], []
    for offset in range(6, -1, -1):
        day = end_day - timedelta(days=offset)
        labels.append(day.strftime("%a %d"))
        values.append(round(daily_calories(user_id, day), 1))
    return {"labels": labels, "values": values}


def pending_recommendations_for_user(user_id: int) -> list[Recommendation]:
    return (
        Recommendation.query.join(Report)
        .filter(Report.user_id == user_id, Recommendation.status == "pending")
        .order_by(Recommendation.id.desc())
        .all()
    )


def get_pending_recommendation_for_athlete(rec_id: int, user_id: int) -> Recommendation | None:
    return (
        Recommendation.query.join(Report)
        .filter(
            Recommendation.id == rec_id,
            Report.user_id == user_id,
            Recommendation.status == "pending",
        )
        .first()
    )


def get_pending_recommendation_for_trainer(rec_id: int, trainer_id: int) -> Recommendation | None:
    rec = (
        Recommendation.query.join(Report)
        .filter(Recommendation.id == rec_id, Recommendation.status == "pending")
        .first()
    )
    if not rec:
        return None
    client = get_trainer_client(trainer_id, rec.report.user_id)
    return rec if client else None


def get_trainer_client(trainer_id: int, client_id: int) -> User | None:
    return User.query.filter_by(
        id=client_id, trainer_id=trainer_id, role="user"
    ).first()


def get_active_goal(user_id: int) -> Goal | None:
    return Goal.query.filter_by(user_id=user_id, status="active").first()


def profile_complete(profile: Profile | None) -> bool:
    if not profile:
        return False
    return all(
        [
            profile.gender,
            profile.birth_date,
            profile.height,
            profile.weight,
            profile.activity_level,
        ]
    )


def parse_selected_date(date_str: str | None) -> date:
    return parse_date_strict(date_str) or date.today()


def parse_date_strict(date_str: str | None) -> date | None:
    """Parse YYYY-MM-DD; None if missing or malformed."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_number(
    value: str | None, min_val: float, max_val: float, *, integer: bool = False
) -> float | int | None:
    """Parse a finite number within [min_val, max_val]; None if invalid (rejects nan/inf)."""
    if value is None:
        return None
    try:
        num = int(value) if integer else float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num < min_val or num > max_val:
        return None
    return num


def is_safe_local_path(target: str) -> bool:
    return target.startswith("/") and not target.startswith("//") and "\\" not in target


def date_nav_context(endpoint: str, selected: date) -> dict:
    prev_day = selected - timedelta(days=1)
    next_day = selected + timedelta(days=1)
    return {
        "selected_date": selected,
        "prev_url": url_for(endpoint, date=prev_day.isoformat()),
        "next_url": url_for(endpoint, date=next_day.isoformat()),
        "today_url": url_for(endpoint),
        "base_url": url_for(endpoint),
    }


def _migrate_profile_goals_schema() -> None:
    """Add new columns/tables to existing SQLite databases."""
    from sqlalchemy import text

    profile_cols = {
        row[1] for row in db.session.execute(text("PRAGMA table_info(profiles)")).fetchall()
    }
    if "activity_level" not in profile_cols:
        db.session.execute(
            text("ALTER TABLE profiles ADD COLUMN activity_level VARCHAR(20)")
        )

    tables = {
        row[0]
        for row in db.session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    if "goals" not in tables:
        Goal.__table__.create(db.engine, checkfirst=True)

    db.session.commit()


def register_routes(app: Flask) -> None:
    @app.route("/")
    def index():
        if current_user.is_authenticated:
            if current_user.role == "trainer":
                return redirect(url_for("trainer_dashboard"))
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/sw.js")
    def service_worker():
        # Served from the site root (not /static/) so the worker's scope covers every page.
        response = send_from_directory(app.static_folder, "js/sw.js", mimetype="application/javascript")
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("index"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = User.query.filter_by(email=email).first()

            if user and check_password_hash(user.password_hash, password):
                login_user(user)
                flash("Welcome back!", "success")
                return redirect(url_for("index"))

            flash("Invalid email or password.", "error")

        return render_template("login.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("index"))

        trainers = User.query.filter_by(role="trainer").order_by(User.email).all()

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            role = request.form.get("role", "user")
            trainer_id = request.form.get("trainer_id")

            errors = []
            if not email or not password:
                errors.append("Email and password are required.")
            elif "@" not in email or len(email) > 255:
                errors.append("Enter a valid email address.")
            if password and len(password) < 6:
                errors.append("Password must be at least 6 characters.")
            if password != confirm:
                errors.append("Passwords do not match.")
            if User.query.filter_by(email=email).first():
                errors.append("An account with this email already exists.")
            if role not in ("user", "trainer"):
                errors.append("Invalid role selected.")

            gender = birth_date = activity_level = goal_type = None
            height = weight = target_weight = None

            if role == "user":
                gender = request.form.get("gender", "").strip()
                birth_date_str = request.form.get("birth_date", "")
                activity_level = request.form.get("activity_level", "").strip()
                goal_type = request.form.get("goal_type", "").strip()
                height = parse_number(request.form.get("height"), 100, 250)
                weight = parse_number(request.form.get("weight"), 30, 300)
                target_weight = parse_number(request.form.get("target_weight"), 30, 300)
                if height is None or weight is None or target_weight is None:
                    errors.append("Enter valid height, weight, and target weight.")
                if gender not in ("male", "female"):
                    errors.append("Select a valid gender.")
                if activity_level not in ("sedentary", "light", "moderate", "active"):
                    errors.append("Select a valid activity level.")
                if goal_type not in ("weight_loss", "maintenance", "muscle_gain"):
                    errors.append("Select a valid goal type.")
                birth_date = parse_date_strict(birth_date_str)
                if birth_date is None or not (date(1900, 1, 1) <= birth_date < date.today()):
                    errors.append("Enter a valid birth date.")

                if trainer_id:
                    trainer = (
                        db.session.get(User, int(trainer_id)) if trainer_id.isdigit() else None
                    )
                    if not trainer or trainer.role != "trainer":
                        errors.append("The selected trainer was not found.")

            if errors:
                for msg in errors:
                    flash(msg, "error")
            else:
                try:
                    user = User(
                        email=email,
                        password_hash=generate_password_hash(password),
                        role=role,
                    )
                    if role == "user" and trainer_id:
                        user.trainer_id = trainer.id

                    db.session.add(user)
                    db.session.flush()

                    if role == "user":
                        profile = Profile(
                            user_id=user.id,
                            gender=gender,
                            birth_date=birth_date,
                            height=height,
                            weight=weight,
                            activity_level=activity_level,
                        )
                        db.session.add(profile)
                        db.session.add(
                            Goal(
                                user_id=user.id,
                                goal_type=goal_type,
                                target_weight=target_weight,
                                start_weight=weight,
                                start_date=date.today(),
                                status="active",
                            )
                        )
                    else:
                        db.session.add(Profile(user_id=user.id))

                    db.session.commit()
                    flash("Account created. Please sign in.", "success")
                    return redirect(url_for("login"))
                except Exception:
                    db.session.rollback()
                    flash("Registration failed. Please try again.", "error")

        return render_template("register.html", trainers=trainers)

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("You have been signed out.", "info")
        return redirect(url_for("login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        if current_user.role == "trainer":
            return redirect(url_for("trainer_dashboard"))

        today = date.today()
        selected_date = parse_selected_date(request.args.get("date"))
        summary = daily_nutrition_summary(current_user.id, selected_date)
        today_workouts = (
            Workout.query.filter(
                Workout.user_id == current_user.id,
                db.func.date(Workout.created_at) == selected_date,
            )
            .order_by(Workout.created_at.desc())
            .all()
        )
        calories_burned = sum(float(w.calories_burned) for w in today_workouts)
        food_logs = food_logs_for_day(current_user.id, selected_date)
        profile = Profile.query.filter_by(user_id=current_user.id).first()
        active_goal = get_active_goal(current_user.id)
        recommendations = pending_recommendations_for_user(current_user.id)
        trend = calorie_trend_7_days(current_user.id, today)
        weight_trend = (
            predict_weight_trend(current_user.id, days_forecast=30)
            if profile_complete(profile)
            else {"labels": [], "actual": [], "forecast": [], "target_weight": None, "meta": {}}
        )

        calories_in = summary["calories"]
        targets = (
            calculate_daily_targets(profile, active_goal)
            if profile_complete(profile)
            else None
        )
        progress = (
            goal_progress(profile, active_goal)
            if profile_complete(profile) and active_goal
            else None
        )

        chart_macros = {
            "labels": ["Protein", "Fat", "Carbs"],
            "values": [
                round(summary["proteins"], 1),
                round(summary["fats"], 1),
                round(summary["carbs"], 1),
            ],
        }

        return render_template(
            "dashboard.html",
            coach_report=latest_coach_report(current_user.id),
            coach_ai=coach_available(),
            calories_in=round(calories_in, 1),
            calories_burned=round(calories_burned, 1),
            net_calories=round(calories_in - calories_burned, 1),
            proteins_g=round(summary["proteins"], 1),
            fats_g=round(summary["fats"], 1),
            carbs_g=round(summary["carbs"], 1),
            targets=targets,
            goal_progress=progress,
            micronutrient_totals=daily_micronutrient_totals(current_user.id, selected_date),
            food_logs=food_logs,
            workouts=today_workouts,
            profile=profile,
            active_goal=active_goal,
            recommendations=recommendations,
            today=today,
            selected_date=selected_date,
            date_nav=date_nav_context("dashboard", selected_date),
            chart_macros_json=json.dumps(chart_macros),
            chart_calories_json=json.dumps(trend),
            chart_weight_json=json.dumps(weight_trend),
            weight_meta=weight_trend.get("meta", {}),
            copy_from_meals=meal_counts_for_day(current_user.id, selected_date - timedelta(days=1)),
            water=water_state(current_user.id, selected_date, profile),
            streak=logging_streak(current_user.id, today),
            weight_log=weight_summary(current_user.id),
            checkin=(
                weekly_checkin(current_user.id, profile, active_goal)
                if profile_complete(profile)
                else None
            ),
        )

    @app.route("/coach/chat", methods=["GET", "POST"])
    @login_required
    def coach_chat_page():
        if current_user.role == "trainer":
            return redirect(url_for("trainer_dashboard"))
        if request.method == "GET":
            return render_template(
                "coach_chat.html",
                messages=chat_history(current_user.id),
                coach_ai=coach_available(),
                max_chars=CHAT_MAX_CHARS,
                prefill=request.args.get("q", "")[:CHAT_MAX_CHARS],
            )

        data = request.get_json(silent=True) or request.form
        message = str(data.get("message", "")).strip()
        error, status = None, 200
        if not message or len(message) > CHAT_MAX_CHARS:
            error, status = f"Write a message of up to {CHAT_MAX_CHARS} characters.", 400
        elif not coach_available():
            error, status = "The AI coach is not connected yet.", 503
        else:
            try:
                asked, answered = coach_chat(current_user.id, message)
            except ChatLimitError as exc:
                error, status = str(exc), 429
            except (CoachError, ProviderError) as exc:
                app.logger.warning("coach chat failed: %s", exc)
                db.session.rollback()
                error, status = "The coach couldn't answer right now. Please try again in a minute.", 502

        if wants_json():
            if error:
                return jsonify(error=error), status
            return jsonify(user=chat_message_payload(asked), reply=chat_message_payload(answered))
        if error:
            flash(error, "error")
        return redirect(url_for("coach_chat_page") + "#latest")

    @app.route("/coach/chat/clear", methods=["POST"])
    @login_required
    def clear_coach_chat():
        CoachMessage.query.filter_by(user_id=current_user.id).delete()
        db_commit_with_retry()
        flash("Chat cleared.", "success")
        return redirect(url_for("coach_chat_page"))

    @app.route("/targets/adaptive", methods=["POST"])
    @login_required
    def apply_adaptive_target():
        back = url_for("dashboard") + "#insights"
        profile = Profile.query.filter_by(user_id=current_user.id).first()
        if request.form.get("action") == "reset":
            if profile and profile.calorie_target_override:
                profile.calorie_target_override = None
                db_commit_with_retry()
                flash("Back to the calculated calorie target.", "success")
            return redirect(back)

        if not profile_complete(profile):
            flash("Complete your profile first.", "error")
            return redirect(url_for("profile_page"))
        # Recomputed on the server: the form only says "apply", never the number.
        checkin = weekly_checkin(current_user.id, profile, get_active_goal(current_user.id))
        if not checkin["ready"]:
            flash("Not enough data yet for a check-in.", "error")
            return redirect(back)
        profile.calorie_target_override = checkin["suggested_target"]
        try:
            db_commit_with_retry()
            flash(f"New daily target: {checkin['suggested_target']} kcal.", "success")
        except OperationalError:
            flash("Could not save the target. Please try again.", "error")
        return redirect(back)

    @app.route("/dashboard/ai-report", methods=["POST"])
    @login_required
    def dashboard_ai_report():
        if current_user.role == "trainer":
            return redirect(url_for("trainer_dashboard"))

        profile = Profile.query.filter_by(user_id=current_user.id).first()
        if not profile_complete(profile):
            flash("Complete your profile (weight, height, activity) to run the AI analysis.", "error")
            return redirect(url_for("profile_page"))

        try:
            report = generate_coach_report(current_user.id, days=7)
            if report.engine == "local":
                flash("Basic analysis ready (the AI coach is not connected).", "info")
            else:
                flash("Your coach analysis is ready.", "success")
        except OperationalError:
            db.session.rollback()
            flash("Could not save the analysis. Please try again.", "error")

        selected = parse_selected_date(
            request.args.get("date") or request.form.get("date")
        )
        return redirect(url_for("dashboard", date=selected.isoformat()) + "#coach")

    @app.route(
        "/recommendation/delete/<int:rec_id>",
        methods=["POST"],
        endpoint="delete_recommendation",
    )
    @login_required
    def delete_recommendation(rec_id: int):
        if current_user.role == "trainer":
            return redirect(url_for("trainer_dashboard"))

        rec = get_pending_recommendation_for_athlete(rec_id, current_user.id)
        if not rec:
            flash("Recommendation not found or it cannot be deleted.", "error")
            return redirect(url_for("dashboard"))

        try:
            db.session.delete(rec)
            db_commit_with_retry()
            flash("Recommendation deleted.", "success")
        except OperationalError:
            db.session.rollback()
            flash("Could not delete. Please try again.", "error")

        selected = parse_selected_date(
            request.args.get("date") or request.form.get("date")
        )
        return redirect(url_for("dashboard", date=selected.isoformat()))

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def profile_page():
        if current_user.role == "trainer":
            flash("Trainers use a simplified profile. Contact admin to update.", "info")
            return redirect(url_for("trainer_dashboard"))

        profile = Profile.query.filter_by(user_id=current_user.id).first()
        active_goal = get_active_goal(current_user.id)
        targets = (
            calculate_daily_targets(profile, active_goal)
            if profile_complete(profile)
            else None
        )

        if request.method == "POST":
            new_weight = parse_number(request.form.get("weight"), 30, 300)
            if new_weight is None:
                flash("Enter a valid current weight.", "error")
                return redirect(url_for("profile_page"))

            water_goal = None
            raw_water_goal = request.form.get("water_goal_ml", "").strip()
            if raw_water_goal:
                water_goal = parse_number(raw_water_goal, 500, 6000, integer=True)
                if water_goal is None:
                    flash("Water goal must be a whole number between 500 and 6000 ml.", "error")
                    return redirect(url_for("profile_page"))

            if profile is None:
                profile = Profile(user_id=current_user.id)
                db.session.add(profile)
            old_weight = float(profile.weight) if profile.weight is not None else None
            profile.weight = new_weight
            if old_weight != new_weight:
                record_weight(current_user.id, date.today(), new_weight)
            if "water_goal_ml" in request.form:
                profile.water_goal_ml = water_goal
            new_goal_type = request.form.get("goal_type", "").strip()
            new_target = request.form.get("target_weight", "")

            if new_goal_type in ("weight_loss", "maintenance", "muscle_gain"):
                target_val = parse_number(new_target, 30, 300)
                if target_val is None:
                    db.session.rollback()
                    flash("Enter a valid target weight when changing goal.", "error")
                    return redirect(url_for("profile_page"))

                if active_goal and active_goal.goal_type == new_goal_type:
                    active_goal.target_weight = target_val
                else:
                    # A check-in target was tuned for the old goal.
                    profile.calorie_target_override = None
                    if active_goal:
                        active_goal.status = "completed"
                    db.session.add(
                        Goal(
                            user_id=current_user.id,
                            goal_type=new_goal_type,
                            target_weight=target_val,
                            start_weight=new_weight,
                            start_date=date.today(),
                            status="active",
                        )
                    )

            activity = request.form.get("activity_level", "").strip()
            if activity in ("sedentary", "light", "moderate", "active"):
                profile.activity_level = activity

            try:
                db_commit_with_retry()
                flash("Profile updated. Daily targets recalculated.", "success")
            except OperationalError:
                flash("Could not save your profile. Please try again.", "error")
            return redirect(url_for("profile_page"))

        progress = (
            goal_progress(profile, active_goal)
            if profile_complete(profile) and active_goal
            else None
        )

        return render_template(
            "profile.html",
            profile=profile,
            auto_water_goal=auto_water_goal_ml(profile),
            active_goal=active_goal,
            targets=targets,
            goal_progress=progress,
        )

    def _log_food_context(selected_date: date, preselect_id: int | None = None,
                          preselect_grams: float | None = None) -> dict:
        uid = current_user.id
        # Shared catalogue + the athlete's own foods; quick-add entries are not browsable.
        products = (
            Product.visible_to(uid)
            .filter(Product.source != SOURCE_QUICK)
            .order_by(Product.name)
            .all()
        )
        favorite_ids = favorite_ids_for(uid)
        recent = recent_products_for(uid)
        favorites = [p for p in products if p.id in favorite_ids]
        my_products = [p for p in products if p.source == SOURCE_USER and p.created_by_id == uid]
        preselected = db.session.get(Product, preselect_id) if preselect_id else None
        if preselected and (not preselected.is_visible_to(uid) or preselected.source == SOURCE_QUICK):
            preselected = None
        # Instant client-side search covers the built-in and personal catalogue;
        # cached Open Food Facts items are found through /api/products/search.
        local_catalogue = [p for p in products if p.source != "off" or p.id in favorite_ids]
        return {
            "products": products,
            "today": date.today(),
            "selected_date": selected_date,
            "date_nav": date_nav_context("log_food", selected_date),
            "food_logs": food_logs_for_day(uid, selected_date),
            "recent_products": [product_payload(p, favorite_ids, portion) for p, portion in recent],
            "favorite_products": [product_payload(p, favorite_ids) for p in favorites],
            "my_products": [product_payload(p, favorite_ids) for p in my_products],
            "catalogue": [product_payload(p, favorite_ids) for p in local_catalogue],
            "preselected": product_payload(preselected, favorite_ids, preselect_grams) if preselected else None,
            "copy_from_date": selected_date - timedelta(days=1),
            "copy_from_meals": meal_counts_for_day(uid, selected_date - timedelta(days=1)),
        }

    @app.route("/log-food", methods=["GET", "POST"])
    @login_required
    def log_food():
        selected_date = parse_selected_date(
            request.form.get("date") if request.method == "POST" else request.args.get("date")
        )

        if request.method == "POST":
            product_id = request.form.get("product_id", "").strip()
            portion_val = parse_number(request.form.get("portion_grams"), 0, 5000)
            meal_type = request.form.get("meal_type", "lunch")

            error = None
            if request.form.get("date") and parse_date_strict(request.form.get("date")) is None:
                error = "Invalid date."
            elif portion_val is None or portion_val <= 0:
                error = "Enter a valid portion in grams."
            elif meal_type not in MEAL_TYPES:
                error = "Choose a valid meal type."
            product = db.session.get(Product, int(product_id)) if product_id.isdigit() else None
            if product and not product.is_visible_to(current_user.id):
                product = None  # another user's private food
            if error is None and not product:
                error = "Please select a product."

            if error:
                flash(error, "error")
                return render_template("log_food.html", **_log_food_context(selected_date)), 400

            entry = FoodLog(
                user_id=current_user.id,
                date=selected_date,
                meal_type=meal_type,
                product_id=product.id,
                portion_grams=portion_val,
            )
            db.session.add(entry)
            try:
                db_commit_with_retry()
                flash(f"Logged {product.name} ({portion_val} g).", "success")
            except OperationalError:
                flash("Could not save the entry. Please try again.", "error")
            return redirect(url_for("log_food", date=selected_date.isoformat()))

        # ?product=<id>&grams=<g>&meal=<type> pre-fills the form (used by coach suggestions).
        preselect = request.args.get("product", "")
        return render_template(
            "log_food.html",
            **_log_food_context(
                selected_date,
                int(preselect) if preselect.isdigit() else None,
                parse_number(request.args.get("grams"), 1, 2000),
            ),
        )

    # ------------------------------------------------------------------ food database API

    @app.route("/api/products/search")
    @login_required
    def api_product_search():
        query = request.args.get("q", "").strip()[:80]
        remote = request.args.get("remote", "1") != "0"
        results = food_db.search_products(query, current_user.id, limit=25, remote=remote)
        favorite_ids = favorite_ids_for(current_user.id)
        return jsonify(results=[product_payload(p, favorite_ids) for p in results])

    @app.route("/api/products/barcode/<code>")
    @login_required
    def api_product_barcode(code: str):
        if not food_db.normalize_barcode(code):
            return jsonify(error="That doesn't look like a barcode."), 400
        if food_db.is_blocked_barcode(code):
            return jsonify(error="Products from Russia and Belarus are not supported."), 422
        product = food_db.find_by_barcode(code, current_user.id)
        if not product:
            return jsonify(error="Product not found."), 404
        return jsonify(product=product_payload(product, favorite_ids_for(current_user.id)))

    @app.route("/favorites/<int:product_id>/toggle", methods=["POST"])
    @login_required
    def toggle_favorite(product_id: int):
        product = db.session.get(Product, product_id)
        if not product or not product.is_visible_to(current_user.id) or product.source == SOURCE_QUICK:
            return jsonify(error="Product not found."), 404
        fav = FavoriteProduct.query.filter_by(user_id=current_user.id, product_id=product_id).first()
        if fav:
            db.session.delete(fav)
        else:
            db.session.add(FavoriteProduct(user_id=current_user.id, product_id=product_id))
        db_commit_with_retry()
        return jsonify(favorite=fav is None)

    @app.route("/products/new", methods=["POST"])
    @login_required
    def create_product():
        selected = parse_selected_date(request.form.get("date"))
        back = url_for("log_food", date=selected.isoformat())
        name = request.form.get("name", "").strip()[:255]
        brand = request.form.get("brand", "").strip()[:255] or None
        kcal = parse_number(request.form.get("calories_per_100g"), 0, 950)
        macros = [parse_number(request.form.get(f) or "0", 0, 100) for f in ("proteins", "fats", "carbs")]
        raw_barcode = request.form.get("barcode", "").strip()
        barcode = food_db.normalize_barcode(raw_barcode)

        error = None
        if not name:
            error = "Give the food a name."
        elif kcal is None:
            error = "Enter calories per 100 g (0–950)."
        elif any(m is None for m in macros):
            error = "Protein, fat and carbs must be between 0 and 100 g."
        elif raw_barcode and not barcode:
            error = "That doesn't look like a barcode."
        elif barcode and Product.query.filter_by(barcode=barcode).first():
            error = "A product with this barcode already exists."
        if error:
            flash(error, "error")
            return redirect(back)

        product = Product(
            name=name, brand=brand, barcode=barcode,
            calories_per_100g=kcal, proteins=macros[0], fats=macros[1], carbs=macros[2],
            source=SOURCE_USER, created_by_id=current_user.id,
        )
        product.refresh_search_terms()
        db.session.add(product)
        db_commit_with_retry()
        flash(f"Saved \u201c{name}\u201d to My foods.", "success")
        return redirect(url_for("log_food", date=selected.isoformat(), product=product.id))

    @app.route("/log-food/quick", methods=["POST"])
    @login_required
    def quick_add():
        selected = parse_selected_date(request.form.get("date"))
        next_url = request.form.get("next", "").strip()
        back = next_url if is_safe_local_path(next_url) else url_for("log_food", date=selected.isoformat())
        kcal = parse_number(request.form.get("calories"), 1, 5000)
        meal_type = request.form.get("meal_type", "")
        macros = [parse_number(request.form.get(f) or "0", 0, 500) for f in ("proteins", "fats", "carbs")]
        if kcal is None:
            flash("Enter calories between 1 and 5000.", "error")
            return redirect(back)
        if meal_type not in MEAL_TYPES:
            flash("Choose a valid meal type.", "error")
            return redirect(back)
        if any(m is None for m in macros):
            flash("Macros must be between 0 and 500 g.", "error")
            return redirect(back)

        # A private one-off product logged as a 100 g portion keeps FoodLog unchanged.
        name = request.form.get("name", "").strip()[:80] or "Quick add"
        product = Product(
            name=name, calories_per_100g=kcal, proteins=macros[0], fats=macros[1], carbs=macros[2],
            source=SOURCE_QUICK, created_by_id=current_user.id,
        )
        product.refresh_search_terms()
        db.session.add(product)
        db.session.flush()
        db.session.add(FoodLog(user_id=current_user.id, date=selected, meal_type=meal_type,
                               product_id=product.id, portion_grams=100))
        try:
            db_commit_with_retry()
            flash(f"Added {kcal:.0f} kcal to {meal_type}.", "success")
        except OperationalError:
            flash("Could not save the entry. Please try again.", "error")
        return redirect(back)

    @app.route("/log-food/copy", methods=["POST"])
    @login_required
    def copy_meals():
        to_date = parse_selected_date(request.form.get("date"))
        from_date = parse_date_strict(request.form.get("from_date")) or (to_date - timedelta(days=1))
        meal_type = request.form.get("meal_type", "all")
        next_url = request.form.get("next", "").strip()
        back = next_url if is_safe_local_path(next_url) else url_for("log_food", date=to_date.isoformat())
        if meal_type != "all" and meal_type not in MEAL_TYPES:
            flash("Choose a valid meal type.", "error")
            return redirect(back)
        if from_date == to_date:
            flash("Pick a different day to copy from.", "error")
            return redirect(back)

        query = FoodLog.query.filter_by(user_id=current_user.id, date=from_date)
        if meal_type != "all":
            query = query.filter_by(meal_type=meal_type)
        source_logs = query.order_by(FoodLog.id).all()
        if not source_logs:
            flash("Nothing to copy from that day.", "info")
            return redirect(back)
        for log in source_logs:
            db.session.add(FoodLog(user_id=current_user.id, date=to_date, meal_type=log.meal_type,
                                   product_id=log.product_id, portion_grams=log.portion_grams))
        try:
            db_commit_with_retry()
            what = "meals" if meal_type == "all" else meal_type
            noun = "item" if len(source_logs) == 1 else "items"
            flash(f"Copied {len(source_logs)} {noun} ({what}) from {from_date.strftime('%b %d')}.", "success")
        except OperationalError:
            flash("Could not copy. Please try again.", "error")
        return redirect(back)

    def _water_response(selected: date, message: str | None = None, error: str | None = None):
        if wants_json():
            if error:
                return jsonify(error=error), 400
            profile = Profile.query.filter_by(user_id=current_user.id).first()
            return jsonify(water_state(current_user.id, selected, profile))
        if error or message:
            flash(error or message, "error" if error else "success")
        return redirect(url_for("dashboard", date=selected.isoformat()))

    @app.route("/water", methods=["POST"])
    @login_required
    def add_water():
        data = request.get_json(silent=True) or request.form
        selected = parse_selected_date(data.get("date"))
        if selected > date.today():
            return _water_response(date.today(), error="You can't log water for a future day.")
        amount = parse_number(data.get("amount_ml"), 50, 2000, integer=True)
        if amount is None:
            return _water_response(selected, error="Enter between 50 and 2000 ml.")
        db.session.add(WaterLog(user_id=current_user.id, date=selected, amount_ml=amount))
        try:
            db_commit_with_retry()
        except OperationalError:
            return _water_response(selected, error="Could not save. Please try again.")
        return _water_response(selected, message=f"Added {amount} ml of water.")

    @app.route("/water/undo", methods=["POST"])
    @login_required
    def undo_water():
        data = request.get_json(silent=True) or request.form
        selected = parse_selected_date(data.get("date"))
        entry = (
            WaterLog.query.filter_by(user_id=current_user.id, date=selected)
            .order_by(WaterLog.id.desc())
            .first()
        )
        if not entry:
            return _water_response(selected, error="Nothing to undo for this day.")
        amount = entry.amount_ml
        db.session.delete(entry)
        try:
            db_commit_with_retry()
        except OperationalError:
            return _water_response(selected, error="Could not undo. Please try again.")
        return _water_response(selected, message=f"Removed {amount} ml.")

    @app.route("/weight", methods=["POST"])
    @login_required
    def log_weight():
        next_url = request.form.get("next", "").strip()
        back = next_url if is_safe_local_path(next_url) else url_for("dashboard")
        weight = parse_number(request.form.get("weight_kg"), 30, 300)
        day = parse_date_strict(request.form.get("date")) or date.today()
        if weight is None:
            flash("Enter a weight between 30 and 300 kg.", "error")
            return redirect(back)
        if day > date.today():
            flash("You can't log weight for a future day.", "error")
            return redirect(back)
        record_weight(current_user.id, day, weight)
        try:
            db_commit_with_retry()
            flash(f"Logged {weight:.1f} kg for {day.strftime('%b %d')}.", "success")
        except OperationalError:
            flash("Could not save your weight. Please try again.", "error")
        return redirect(back)

    @app.route("/log-food/delete/<int:entry_id>", methods=["POST"])
    @login_required
    def delete_food_log(entry_id: int):
        entry = (
            FoodLog.query.filter_by(id=entry_id, user_id=current_user.id).first()
        )
        if not entry:
            flash("Entry not found or access denied.", "error")
            return redirect(url_for("dashboard"))

        log_date = entry.date.isoformat()
        next_url = request.form.get("next", "").strip()

        try:
            FoodLog.query.filter_by(id=entry_id, user_id=current_user.id).delete(
                synchronize_session=False
            )
            db_commit_with_retry()
            flash("Entry deleted.", "success")
        except OperationalError:
            db.session.rollback()
            flash(
                "The database is temporarily busy. Close DB Browser/SQLite Studio, "
                "restart the server and try again.",
                "error",
            )

        if is_safe_local_path(next_url):
            return redirect(next_url)
        return redirect(url_for("dashboard", date=log_date))

    @app.route("/log-workout", methods=["POST"])
    @login_required
    def log_workout():
        workout_type = request.form.get("type", "").strip()[:100]
        duration_val = parse_number(request.form.get("duration_minutes"), 1, 1440, integer=True)
        calories = request.form.get("calories_burned")
        calories_val = parse_number(calories, 0, 20000) if calories else 0.0
        selected = parse_selected_date(request.form.get("date") or request.args.get("date"))

        if duration_val is None or calories_val is None:
            flash("Enter valid workout duration.", "error")
            return redirect(url_for("dashboard", date=selected.isoformat()))

        if not workout_type:
            flash("Workout type is required.", "error")
            return redirect(url_for("dashboard", date=selected.isoformat()))

        workout = Workout(
            user_id=current_user.id,
            type=workout_type,
            duration_minutes=duration_val,
            calories_burned=calories_val,
            # Dashboard filters workouts by date(created_at), so store it on the chosen day.
            created_at=datetime.combine(selected, datetime.now().time()),
        )
        db.session.add(workout)
        try:
            db_commit_with_retry()
            flash("Workout logged.", "success")
        except OperationalError:
            flash("Could not save the workout. Please try again.", "error")
        return redirect(url_for("dashboard", date=selected.isoformat()))

    @app.route("/trainer")
    @login_required
    @role_required("trainer", "admin")
    def trainer_dashboard():
        clients = (
            User.query.filter_by(trainer_id=current_user.id, role="user")
            .order_by(User.email)
            .all()
        )
        client_stats = []
        today = date.today()

        for client in clients:
            cals = daily_calories(client.id, today)
            workout_count = (
                Workout.query.filter(
                    Workout.user_id == client.id,
                    db.func.date(Workout.created_at) == today,
                ).count()
            )
            profile = Profile.query.filter_by(user_id=client.id).first()
            client_stats.append(
                {
                    "user": client,
                    "profile": profile,
                    "calories_today": round(cals, 1),
                    "workouts_today": workout_count,
                }
            )

        return render_template(
            "trainer.html",
            clients=client_stats,
            client_count=len(client_stats),
            today=today,
        )

    @app.route("/trainer/client/<int:client_id>")
    @login_required
    @role_required("trainer", "admin")
    def trainer_client(client_id: int):
        client = get_trainer_client(current_user.id, client_id)
        if not client:
            flash("Client not found or not assigned to you.", "error")
            return redirect(url_for("trainer_dashboard"))

        today = date.today()
        food_logs = (
            FoodLog.query.filter_by(user_id=client.id, date=today)
            .options(
                joinedload(FoodLog.product).joinedload(Product.micronutrient_links).joinedload(
                    ProductMicronutrient.micronutrient
                )
            )
            .order_by(FoodLog.meal_type, FoodLog.id)
            .all()
        )
        summary = daily_nutrition_summary(client.id, today)
        micronutrient_totals = daily_micronutrient_totals(client.id, today)
        profile = Profile.query.filter_by(user_id=client.id).first()
        pending_recs = (
            Recommendation.query.join(Report)
            .filter(Report.user_id == client.id, Recommendation.status == "pending")
            .order_by(Recommendation.id.desc())
            .all()
        )
        recent_recs = (
            Recommendation.query.join(Report)
            .filter(Report.user_id == client.id, Recommendation.status != "pending")
            .order_by(Recommendation.id.desc())
            .limit(5)
            .all()
        )

        return render_template(
            "trainer_client.html",
            client=client,
            profile=profile,
            food_logs=food_logs,
            summary=summary,
            micronutrient_totals=micronutrient_totals,
            today=today,
            pending_recommendations=pending_recs,
            recent_recommendations=recent_recs,
            coach_report=latest_coach_report(client.id),
            water=water_state(client.id, today, profile),
            streak=logging_streak(client.id, today),
            weight_log=weight_summary(client.id),
        )

    @app.route("/trainer/client/<int:client_id>/recommend", methods=["POST"])
    @login_required
    @role_required("trainer", "admin")
    def send_recommendation(client_id: int):
        client = get_trainer_client(current_user.id, client_id)
        if not client:
            flash("Client not found or not assigned to you.", "error")
            return redirect(url_for("trainer_dashboard"))

        content = request.form.get("content", "").strip()
        if not content:
            flash("Recommendation message cannot be empty.", "error")
            return redirect(url_for("trainer_client", client_id=client_id))

        report = Report(
            user_id=client.id,
            nutrition_summary="Trainer recommendation",
            ai_grade=None,
        )
        db.session.add(report)
        db.session.flush()

        recommendation = Recommendation(
            report_id=report.id,
            content=content,
            status="pending",
        )
        db.session.add(recommendation)
        db.session.commit()

        flash(f"Recommendation sent to {client.email}.", "success")
        return redirect(url_for("trainer_client", client_id=client_id))

    @app.route(
        "/trainer/recommendation/approve/<int:rec_id>",
        methods=["POST"],
        endpoint="approve_recommendation",
    )
    @login_required
    @role_required("trainer", "admin")
    def approve_recommendation(rec_id: int):
        rec = get_pending_recommendation_for_trainer(rec_id, current_user.id)
        if not rec:
            flash("Recommendation not found or already processed.", "error")
            return redirect(url_for("trainer_dashboard"))

        client_id = rec.report.user_id
        content = request.form.get("content", "").strip()
        if not content:
            flash("Recommendation text cannot be empty.", "error")
            return redirect(url_for("trainer_client", client_id=client_id))

        rec.content = content
        rec.status = "approved"
        try:
            db_commit_with_retry()
            flash("Recommendation approved and sent to the athlete.", "success")
        except OperationalError:
            db.session.rollback()
            flash("Could not save. Please try again.", "error")

        return redirect(url_for("trainer_client", client_id=client_id))

    @app.cli.command("generate-ai-report")
    @click.argument("user_id", type=int)
    @click.option("--days", default=7, type=click.IntRange(min=1, max=28), help="Number of days to analyze")
    def cli_generate_ai_report(user_id: int, days: int):
        """Run the AI coach analysis for a user (CLI)."""
        user = db.session.get(User, user_id)
        if not user:
            print(f"User {user_id} not found.")
            return
        report = generate_coach_report(user_id, days=days)
        print(f"engine: {report.engine}")
        print(json.dumps(report.data, indent=2, ensure_ascii=False))

    @app.cli.command("init-db")
    def init_db():
        """Create nutrition_db.db, all tables, and seed demo data."""
        import models  # noqa: F401 — register all models before create_all

        db.create_all()
        _migrate_profile_goals_schema()
        db_path = os.path.join(app.root_path, "nutrition_db.db")
        print(f"SQLite database: {db_path}")

        _seed_micronutrients()
        _seed_products_and_links()
        _seed_generic_foods()

        if User.query.count() == 0:
            trainer = User(
                email="trainer@demo.local",
                password_hash=generate_password_hash("trainer123"),
                role="trainer",
            )
            db.session.add(trainer)
            db.session.flush()
            db.session.add(Profile(user_id=trainer.id))

            athlete = User(
                email="athlete@demo.local",
                password_hash=generate_password_hash("athlete123"),
                role="user",
                trainer_id=trainer.id,
            )
            db.session.add(athlete)
            db.session.flush()
            db.session.add(
                Profile(
                    user_id=athlete.id,
                    gender="male",
                    birth_date=date(2000, 6, 15),
                    height=180,
                    weight=75,
                    activity_level="moderate",
                )
            )
            db.session.add(
                Goal(
                    user_id=athlete.id,
                    goal_type="muscle_gain",
                    target_weight=80,
                    start_weight=75,
                    start_date=date.today(),
                    status="active",
                )
            )
            db.session.commit()
            print("Demo accounts: trainer@demo.local / trainer123, athlete@demo.local / athlete123")

        _upgrade_demo_athlete_profile()
        print("Database initialized.")


def _upgrade_demo_athlete_profile() -> None:
    """Ensure demo athlete has full profile + active goal on existing DBs."""
    athlete = User.query.filter_by(email="athlete@demo.local").first()
    if not athlete:
        return
    profile = Profile.query.filter_by(user_id=athlete.id).first()
    if profile:
        if not profile.birth_date:
            profile.birth_date = date(2000, 6, 15)
        if not profile.activity_level:
            profile.activity_level = "moderate"
    if not get_active_goal(athlete.id) and profile and profile.weight:
        db.session.add(
            Goal(
                user_id=athlete.id,
                goal_type="muscle_gain",
                target_weight=80,
                start_weight=float(profile.weight),
                start_date=date.today(),
                status="active",
            )
        )
    db.session.commit()


MICRONUTRIENT_CATALOG = [
    ("Potassium", "mg"),
    ("Sodium", "mg"),
    ("Magnesium", "mg"),
    ("Calcium", "mg"),
    ("Zinc", "mg"),
    ("Iron", "mg"),
    ("Vitamin C", "mg"),
    ("Vitamin D", "IU"),
    ("Vitamin B12", "mcg"),
    ("Omega-3", "g"),
]

PRODUCT_SEED = {
    "Chicken breast": {
        "calories_per_100g": 165,
        "proteins": 31,
        "fats": 3.6,
        "carbs": 0,
        "micronutrients": {
            "Potassium": 256,
            "Sodium": 74,
            "Magnesium": 28,
            "Calcium": 15,
            "Zinc": 1.0,
            "Iron": 1.0,
            "Vitamin C": 0,
            "Vitamin D": 4,
            "Vitamin B12": 0.3,
            "Omega-3": 0.03,
        },
    },
    "Brown rice": {
        "calories_per_100g": 111,
        "proteins": 2.6,
        "fats": 0.9,
        "carbs": 23,
        "micronutrients": {
            "Potassium": 86,
            "Sodium": 5,
            "Magnesium": 43,
            "Calcium": 10,
            "Zinc": 1.2,
            "Iron": 0.6,
            "Vitamin C": 0,
            "Vitamin D": 0,
            "Vitamin B12": 0,
            "Omega-3": 0.01,
        },
    },
    "Greek yogurt": {
        "calories_per_100g": 97,
        "proteins": 9,
        "fats": 5,
        "carbs": 3.6,
        "micronutrients": {
            "Potassium": 141,
            "Sodium": 36,
            "Magnesium": 11,
            "Calcium": 110,
            "Zinc": 0.5,
            "Iron": 0.1,
            "Vitamin C": 0,
            "Vitamin D": 0,
            "Vitamin B12": 0.5,
            "Omega-3": 0,
        },
    },
    "Banana": {
        "calories_per_100g": 89,
        "proteins": 1.1,
        "fats": 0.3,
        "carbs": 23,
        "micronutrients": {
            "Potassium": 358,
            "Sodium": 1,
            "Magnesium": 27,
            "Calcium": 5,
            "Zinc": 0.2,
            "Iron": 0.3,
            "Vitamin C": 8.7,
            "Vitamin D": 0,
            "Vitamin B12": 0,
            "Omega-3": 0,
        },
    },
    "Oatmeal": {
        "calories_per_100g": 68,
        "proteins": 2.4,
        "fats": 1.4,
        "carbs": 12,
        "micronutrients": {
            "Potassium": 61,
            "Sodium": 2,
            "Magnesium": 177,
            "Calcium": 54,
            "Zinc": 2.6,
            "Iron": 4.7,
            "Vitamin C": 0,
            "Vitamin D": 0,
            "Vitamin B12": 0,
            "Omega-3": 0.11,
        },
    },
}


def _seed_micronutrients() -> None:
    if Micronutrient.query.count() > 0:
        return
    for name, unit in MICRONUTRIENT_CATALOG:
        db.session.add(Micronutrient(name=name, unit=unit))
    db.session.commit()
    print(f"Seeded {len(MICRONUTRIENT_CATALOG)} micronutrients.")


def _seed_products_and_links() -> None:
    nutrient_map = {m.name: m for m in Micronutrient.query.all()}
    if not nutrient_map:
        _seed_micronutrients()
        nutrient_map = {m.name: m for m in Micronutrient.query.all()}

    created_products = False
    for name, data in PRODUCT_SEED.items():
        product = Product.query.filter_by(name=name).first()
        if not product:
            product = Product(
                name=name,
                calories_per_100g=data["calories_per_100g"],
                proteins=data["proteins"],
                fats=data["fats"],
                carbs=data["carbs"],
            )
            db.session.add(product)
            db.session.flush()
            created_products = True
        else:
            product.calories_per_100g = data["calories_per_100g"]
            product.proteins = data["proteins"]
            product.fats = data["fats"]
            product.carbs = data["carbs"]

        for nut_name, amount in data["micronutrients"].items():
            micro = nutrient_map[nut_name]
            link = ProductMicronutrient.query.filter_by(
                product_id=product.id, micronutrient_id=micro.id
            ).first()
            if link:
                link.amount_per_100g = amount
            else:
                db.session.add(
                    ProductMicronutrient(
                        product_id=product.id,
                        micronutrient_id=micro.id,
                        amount_per_100g=amount,
                    )
                )

    db.session.commit()
    if created_products:
        print("Seeded sample products.")
    print("Synced product micronutrient links.")


def _seed_generic_foods() -> None:
    """Add the built-in generic food catalogue (idempotent, keyed by name)."""
    existing = {name for (name,) in db.session.query(Product.name).filter(Product.created_by_id.is_(None))}
    added = 0
    for name, kcal, protein, fat, carbs in GENERIC_FOODS:
        if name in existing:
            continue
        db.session.add(
            Product(name=name, calories_per_100g=kcal, proteins=protein, fats=fat, carbs=carbs, source=SOURCE_SEED)
        )
        added += 1
    db.session.flush()
    # Search terms (with Ukrainian names) for every built-in product, incl. PRODUCT_SEED ones.
    for product in Product.query.filter(Product.created_by_id.is_(None), Product.source == SOURCE_SEED):
        product.refresh_search_terms(UK_SEARCH_NAMES.get(product.name))
    db.session.commit()
    if added:
        print(f"Seeded {added} generic foods.")


app = create_app()

if __name__ == "__main__":
    # use_reloader=False avoids a second process locking SQLite during development.
    # The Werkzeug debugger allows code execution, so enable it only via FLASK_DEBUG=1 on localhost.
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(debug=debug, host="127.0.0.1" if debug else "0.0.0.0", port=5000, use_reloader=False)
