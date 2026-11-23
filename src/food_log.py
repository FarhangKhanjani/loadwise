"""Calorie tracker storage: load the food catalogue, log foods and meals, read history.

The maths and search live in src/nutrition.py; this module only moves data between the
database and those functions.

Food entries are saved directly, without a Confirm step: they are entered by you in the app
or Telegram (never proposed by the agent), and the app shows the kcal before you save.
Mistakes are fixed by deleting the entry.

Load or refresh the catalogue (safe to re-run; logged history is never changed):
  python -m src.food_log --db data/loadwise.db
"""

import argparse
import csv
import json
import uuid
from datetime import date
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.models import Food, FoodLogEntry, MealTemplate
from src.nutrition import (
    SLOTS,
    NutritionError,
    day_summary,
    grams_for,
    item_values,
    meal_lines,
    month_summary,
    search,
    total,
)

DATA = Path(__file__).resolve().parent.parent / "data"


def _split(text: str) -> list[str]:
    return [part.strip() for part in text.split("|") if part.strip()]


def parse_portions(text: str) -> dict[str, float]:
    """'1 large=50|1 medium=44' -> {'1 large': 50.0, '1 medium': 44.0}"""
    portions = {}
    for part in _split(text):
        name, _, grams = part.rpartition("=")
        portions[name.strip()] = float(grams)
    return portions


def load_catalog(db: Session, foods_csv: Path = DATA / "foods.csv", meals_json: Path = DATA / "meals.json") -> tuple[int, int]:
    """Insert or update foods and saved meals by name. Returns (foods, meals) loaded."""
    foods = {f.name: f for f in db.scalars(select(Food))}
    rows = list(csv.DictReader(foods_csv.open(encoding="utf-8")))
    for row in rows:
        food = foods.get(row["name"]) or Food(name=row["name"])
        food.kcal_per_100g = float(row["kcal_per_100g"])
        food.protein_per_100g = float(row["protein_per_100g"])
        food.aliases = _split(row["aliases"])
        food.portions = parse_portions(row["portions"])
        food.source = row["source"]
        db.add(food)
        foods[food.name] = food

    meals = json.loads(meals_json.read_text(encoding="utf-8"))["meals"]
    existing = {m.name: m for m in db.scalars(select(MealTemplate))}
    for spec in meals:
        meal_lines(spec["items"], foods, include_optional=[i["food"] for i in spec["items"]])  # validates
        meal = existing.get(spec["name"]) or MealTemplate(name=spec["name"])
        meal.slot, meal.aliases, meal.items = spec["slot"], spec.get("aliases", []), spec["items"]
        db.add(meal)
    db.commit()
    return len(rows), len(meals)


def _foods(db: Session) -> dict[str, Food]:
    return {f.name: f for f in db.scalars(select(Food))}


def _check_slot(slot: str) -> str:
    if slot not in SLOTS:
        raise NutritionError(f"slot must be one of {', '.join(SLOTS)}")
    return slot


def find_food(db: Session, name: str) -> Food:
    """Exact name first, then the best search match ("eier" -> egg)."""
    foods = _foods(db)
    if name in foods:
        return foods[name]
    matches = search(name, foods.values(), limit=1)
    if not matches:
        raise NutritionError(f"no food matches {name!r}")
    return matches[0]


def find_meal(db: Session, name: str) -> MealTemplate:
    meals = list(db.scalars(select(MealTemplate)))
    exact = [m for m in meals if m.name == name]
    matches = exact or search(name, meals, limit=1)
    if not matches:
        raise NutritionError(f"no saved meal matches {name!r}")
    return matches[0]


def _save(db: Session, day: date, slot: str, lines, meal_name: str | None) -> list[FoodLogEntry]:
    group = uuid.uuid4().hex
    entries = [FoodLogEntry(date=day, slot=slot, food_name=l.food, grams=l.grams, kcal=l.kcal,
                            protein=l.protein, meal_name=meal_name, group_id=group) for l in lines]
    db.add_all(entries)
    db.commit()
    return entries


def log_food(db: Session, day: date, slot: str, food: str, grams: float | None = None,
             portion: str | None = None, count: float = 1) -> FoodLogEntry:
    _check_slot(slot)
    item = find_food(db, food)
    line = item_values(item, grams_for(item, grams, portion, count))
    return _save(db, day, slot, [line], None)[0]


def preview_meal(db: Session, meal: str, include_optional=(), grams_override=None) -> tuple[MealTemplate, list]:
    template = find_meal(db, meal)
    return template, meal_lines(template.items, _foods(db), include_optional, grams_override)


def log_meal(db: Session, day: date, slot: str | None, meal: str, include_optional=(),
             grams_override=None) -> list[FoodLogEntry]:
    template, lines = preview_meal(db, meal, include_optional, grams_override)
    if not lines:
        raise NutritionError("nothing to log: every item of the meal was left out")
    return _save(db, day, _check_slot(slot or template.slot), lines, template.name)


def delete_entries(db: Session, entry_id: int, whole_meal: bool = False) -> int:
    """Delete one entry, or every entry logged together with it (a whole meal)."""
    entry = db.get(FoodLogEntry, entry_id)
    if entry is None:
        raise NutritionError(f"food log entry {entry_id} does not exist")
    if whole_meal and entry.group_id:
        count = db.execute(delete(FoodLogEntry).where(FoodLogEntry.group_id == entry.group_id)).rowcount
    else:
        db.delete(entry)
        count = 1
    db.commit()
    return count


def entries_between(db: Session, start: date, end: date) -> list[FoodLogEntry]:
    return list(db.scalars(select(FoodLogEntry)
                           .where(FoodLogEntry.date >= start, FoodLogEntry.date <= end)
                           .order_by(FoodLogEntry.date, FoodLogEntry.id)))


def day(db: Session, day_: date) -> dict:
    entries = entries_between(db, day_, day_)
    return {"date": day_.isoformat(), **day_summary(entries),
            "entries": [entry_dict(e) for e in entries]}


def month(db: Session, year: int, month_: int, today: date) -> dict:
    start = date(year, month_, 1)
    end = date(year + (month_ == 12), month_ % 12 + 1, 1)
    return month_summary(entries_between(db, start, end), year, month_, today)


def entry_dict(e: FoodLogEntry) -> dict:
    return {"id": e.id, "date": e.date.isoformat(), "slot": e.slot, "food": e.food_name,
            "grams": e.grams, "kcal": e.kcal, "protein": e.protein, "meal": e.meal_name,
            "group_id": e.group_id}


def lines_dict(lines) -> dict:
    kcal, protein = total(lines)
    return {"items": [l.__dict__ for l in lines], "kcal": kcal, "protein": protein}


def main() -> None:  # pragma: no cover
    from src.db import init_db, make_engine, make_session_factory

    parser = argparse.ArgumentParser(description="Load or refresh the food catalogue.")
    parser.add_argument("--db", help="SQLite file (default: DB_PATH from .env)")
    args = parser.parse_args()
    engine = make_engine(args.db)
    init_db(engine)
    with make_session_factory(engine)() as db:
        foods, meals = load_catalog(db)
    print(f"Loaded {foods} foods and {meals} saved meals into {engine.url.database}")


if __name__ == "__main__":
    main()
