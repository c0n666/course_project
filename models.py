import json
from datetime import date, datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import ForeignKey, Numeric, Text, UniqueConstraint, event, or_
from sqlalchemy.orm import Mapped, mapped_column, relationship

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(db.String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(db.String(255), nullable=False)
    role: Mapped[str] = mapped_column(db.String(20), nullable=False, default="user")
    trainer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    trainer: Mapped["User | None"] = relationship(
        "User",
        remote_side=[id],
        back_populates="clients",
        foreign_keys=[trainer_id],
    )
    clients: Mapped[list["User"]] = relationship(
        "User",
        back_populates="trainer",
        foreign_keys=[trainer_id],
    )
    profile: Mapped["Profile | None"] = relationship(back_populates="user", uselist=False)
    goals: Mapped[list["Goal"]] = relationship(back_populates="user")
    food_logs: Mapped[list["FoodLog"]] = relationship(back_populates="user")
    workouts: Mapped[list["Workout"]] = relationship(back_populates="user")
    reports: Mapped[list["Report"]] = relationship(back_populates="user")

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class Profile(db.Model):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    gender: Mapped[str | None] = mapped_column(db.String(20))
    birth_date: Mapped[date | None] = mapped_column(db.Date)
    height: Mapped[float | None] = mapped_column(Numeric(5, 2))
    weight: Mapped[float | None] = mapped_column(Numeric(5, 2))
    activity_level: Mapped[str | None] = mapped_column(db.String(20))
    medical_notes: Mapped[str | None] = mapped_column(Text)
    # None = use the default (weight-based) water goal / calculated calorie target.
    water_goal_ml: Mapped[int | None] = mapped_column(nullable=True)
    calorie_target_override: Mapped[int | None] = mapped_column(nullable=True)

    user: Mapped["User"] = relationship(back_populates="profile")


class Goal(db.Model):
    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    goal_type: Mapped[str] = mapped_column(db.String(30), nullable=False)
    target_weight: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    start_weight: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    start_date: Mapped[date] = mapped_column(db.Date, nullable=False, default=date.today)
    status: Mapped[str] = mapped_column(db.String(20), nullable=False, default="active")

    user: Mapped["User"] = relationship(back_populates="goals")


class Micronutrient(db.Model):
    __tablename__ = "micronutrients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(db.String(100), unique=True, nullable=False)
    unit: Mapped[str] = mapped_column(db.String(20), nullable=False)

    product_links: Mapped[list["ProductMicronutrient"]] = relationship(back_populates="micronutrient")


# Product.source values
SOURCE_SEED = "seed"    # built-in catalogue
SOURCE_OFF = "off"      # cached from Open Food Facts
SOURCE_USER = "user"    # custom food created by an athlete (private)
SOURCE_QUICK = "quick"  # "quick add" calories entry (private, hidden from lists)
PRIVATE_SOURCES = (SOURCE_USER, SOURCE_QUICK)


class Product(db.Model):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(db.String(255), nullable=False)
    calories_per_100g: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    proteins: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    fats: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    carbs: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    barcode: Mapped[str | None] = mapped_column(db.String(32), unique=True, index=True, nullable=True)
    brand: Mapped[str | None] = mapped_column(db.String(255), nullable=True)
    source: Mapped[str] = mapped_column(db.String(10), nullable=False, default=SOURCE_SEED)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # Lower-cased "name brand aliases" for search: SQLite's LOWER()/LIKE only fold ASCII,
    # so Cyrillic queries ("йогурт" vs "Йогурт") must match a Python-lowered copy.
    search_terms: Mapped[str | None] = mapped_column(db.String(600), nullable=True)

    food_logs: Mapped[list["FoodLog"]] = relationship(back_populates="product")
    micronutrient_links: Mapped[list["ProductMicronutrient"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
    )

    @classmethod
    def visible_to(cls, user_id: int):
        """Query of products a user may see/log: shared catalogue + their own private foods."""
        return cls.query.filter(or_(cls.created_by_id.is_(None), cls.created_by_id == user_id))

    def is_visible_to(self, user_id: int) -> bool:
        return self.created_by_id is None or self.created_by_id == user_id

    def refresh_search_terms(self, aliases: str | None = None) -> None:
        parts = (self.name, self.brand, aliases)
        self.search_terms = " ".join(p for p in parts if p).lower()[:600]

    @property
    def display_name(self) -> str:
        return f"{self.name} · {self.brand}" if self.brand else self.name

    def _per_portion(self, per_100g: float, grams: float) -> float:
        return float(per_100g) * grams / 100.0

    def calories_for_portion(self, grams: float) -> float:
        return self._per_portion(self.calories_per_100g, grams)

    def protein_for_portion(self, grams: float) -> float:
        return self._per_portion(self.proteins, grams)

    def fat_for_portion(self, grams: float) -> float:
        return self._per_portion(self.fats, grams)

    def carb_for_portion(self, grams: float) -> float:
        return self._per_portion(self.carbs, grams)


@event.listens_for(Product, "before_insert")
def _default_search_terms(_mapper, _connection, product: Product) -> None:
    """Every product is searchable, whichever code path created it."""
    if not product.search_terms:
        product.refresh_search_terms()


class ProductMicronutrient(db.Model):
    __tablename__ = "product_micronutrients"
    __table_args__ = (
        UniqueConstraint("product_id", "micronutrient_id", name="uq_product_micronutrient"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    micronutrient_id: Mapped[int] = mapped_column(
        ForeignKey("micronutrients.id"), nullable=False, index=True
    )
    amount_per_100g: Mapped[float] = mapped_column(db.Float, nullable=False, default=0)

    product: Mapped["Product"] = relationship(back_populates="micronutrient_links")
    micronutrient: Mapped["Micronutrient"] = relationship(back_populates="product_links")

    def amount_for_portion(self, grams: float) -> float:
        return float(self.amount_per_100g) * grams / 100.0


class FoodLog(db.Model):
    __tablename__ = "food_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    date: Mapped[date] = mapped_column(db.Date, nullable=False, default=date.today, index=True)
    meal_type: Mapped[str] = mapped_column(db.String(50), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    portion_grams: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)

    user: Mapped["User"] = relationship(back_populates="food_logs")
    product: Mapped["Product"] = relationship(back_populates="food_logs")

    @property
    def grams(self) -> float:
        return float(self.portion_grams)

    @property
    def calories(self) -> float:
        return self.product.calories_for_portion(self.grams)

    @property
    def proteins_g(self) -> float:
        return self.product.protein_for_portion(self.grams)

    @property
    def fats_g(self) -> float:
        return self.product.fat_for_portion(self.grams)

    @property
    def carbs_g(self) -> float:
        return self.product.carb_for_portion(self.grams)


class Workout(db.Model):
    __tablename__ = "workouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(db.String(100), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(nullable=False)
    calories_burned: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="workouts")


class Report(db.Model):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    nutrition_summary: Mapped[str | None] = mapped_column(Text)
    ai_grade: Mapped[str | None] = mapped_column(db.String(10))
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="reports")
    recommendations: Mapped[list["Recommendation"]] = relationship(back_populates="report")


class Recommendation(db.Model):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(db.String(20), nullable=False, default="pending")

    report: Mapped["Report"] = relationship(back_populates="recommendations")


class FavoriteProduct(db.Model):
    __tablename__ = "favorite_products"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_favorite_product"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=datetime.utcnow)

    product: Mapped["Product"] = relationship()


class WaterLog(db.Model):
    __tablename__ = "water_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    date: Mapped[date] = mapped_column(db.Date, nullable=False, default=date.today, index=True)
    amount_ml: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=datetime.utcnow)


class WeightLog(db.Model):
    __tablename__ = "weight_logs"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_weight_log_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    date: Mapped[date] = mapped_column(db.Date, nullable=False, default=date.today)
    weight_kg: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)


class CoachReport(db.Model):
    """One AI coach analysis; `payload` holds the structured JSON the coach returned."""

    __tablename__ = "coach_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=datetime.utcnow, index=True)
    days: Mapped[int] = mapped_column(nullable=False, default=7)
    engine: Mapped[str] = mapped_column(db.String(40), nullable=False)  # model id or "local"
    grade: Mapped[str | None] = mapped_column(db.String(4))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    @property
    def data(self) -> dict:
        try:
            return json.loads(self.payload or "{}")
        except ValueError:
            return {}
