import os
import sys
from datetime import date

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("SECRET_KEY", "test-secret-key")
# Tests never call a real AI provider: without keys the coach runs in basic mode.
for _var in ("COACH_PROVIDER", "GEMINI_API_KEY", "GROQ_API_KEY"):
    os.environ.pop(_var, None)

from app import _seed_micronutrients, _seed_products_and_links, create_app  # noqa: E402
from models import Goal, Profile, User, db  # noqa: E402


def _make_app(tmp_path, **overrides):
    config = {
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
        "WTF_CSRF_ENABLED": False,
    }
    config.update(overrides)
    app = create_app(config)
    with app.app_context():
        db.create_all()
        _seed_micronutrients()
        _seed_products_and_links()
    return app


@pytest.fixture
def app(tmp_path):
    app = _make_app(tmp_path)
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def csrf_app(tmp_path):
    app = _make_app(tmp_path, WTF_CSRF_ENABLED=True)
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def create_user(email, role="user", trainer=None, password="secret123"):
    user = User(
        email=email,
        password_hash=generate_password_hash(password),
        role=role,
        trainer_id=trainer.id if trainer else None,
    )
    db.session.add(user)
    db.session.flush()
    if role == "user":
        db.session.add(
            Profile(
                user_id=user.id,
                gender="male",
                birth_date=date(1995, 1, 1),
                height=180,
                weight=80,
                activity_level="moderate",
            )
        )
        db.session.add(
            Goal(user_id=user.id, goal_type="weight_loss", target_weight=75, start_weight=80)
        )
    else:
        db.session.add(Profile(user_id=user.id))
    db.session.commit()
    return user


def login(client, email, password="secret123"):
    return client.post("/login", data={"email": email, "password": password})
