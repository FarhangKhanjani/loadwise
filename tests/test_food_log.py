"""The calorie tracker against a real database and the real catalogue in data/."""

import csv
from datetime import date

import pytest
from sqlalchemy import func, select

from src import food_log
from src.food_log import DATA, load_catalog, parse_portions
from src.models import Food, FoodLogEntry, MealTemplate
from src.nutrition import NutritionError

DAY = date(2026, 11, 20)


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


# ---------- the catalogue ----------

def test_every_food_has_values_and_a_source():
    rows = list(csv.DictReader((DATA / "foods.csv").open(encoding="utf-8")))
    assert len(rows) >= 20
    for row in rows:
        assert float(row["kcal_per_100g"]) >= 0 and float(row["protein_per_100g"]) >= 0
        assert row["source"].startswith(("USDA FoodData Central:", "package label:")), row["name"]


def test_catalog_loads_and_reloading_is_safe(catalog_db):
    assert count(catalog_db, Food) >= 20 and count(catalog_db, MealTemplate) == 5
    load_catalog(catalog_db)  # second run updates in place
    assert count(catalog_db, Food) >= 20 and count(catalog_db, MealTemplate) == 5
    egg = catalog_db.scalar(select(Food).where(Food.name == "egg"))
    assert egg.portions["1 large"] == 50 and "eier" in egg.aliases


def test_parse_portions():
    assert parse_portions("1 large=50| 1/2 medium = 61.5") == {"1 large": 50.0, "1/2 medium": 61.5}
    assert parse_portions("") == {}


# ---------- logging ----------

def test_log_food_by_grams_portion_and_alias(catalog_db):
    oats = food_log.log_food(catalog_db, DAY, "breakfast", "oats", grams=120)
    assert (oats.kcal, oats.protein) == (454.8, 15.8)
    eggs = food_log.log_food(catalog_db, DAY, "snack", "eier", portion="1 large", count=2)  # German alias
    assert (eggs.food_name, eggs.grams, eggs.kcal) == ("egg", 100, 143.0)


@pytest.mark.parametrize("kwargs, message", [
    ({"slot": "brunch", "food": "oats", "grams": 100}, "slot"),
    ({"slot": "lunch", "food": "chocolate cake", "grams": 100}, "no food"),
    ({"slot": "lunch", "food": "egg", "portion": "1 jumbo"}, "no portion"),
])
def test_log_food_errors(catalog_db, kwargs, message):
    with pytest.raises(NutritionError, match=message):
        food_log.log_food(catalog_db, DAY, **kwargs)
    assert count(catalog_db, FoodLogEntry) == 0


def test_oatmeal_bowl_default_total(catalog_db):
    entries = food_log.log_meal(catalog_db, DAY, None, "oatmeal bowl")
    # oats 120 g (454.8) + milk 100 ml = 103 g (62.8) + 1 medium banana 118 g (105.0)
    assert [e.food_name for e in entries] == ["oats", "milk", "banana"]
    assert round(sum(e.kcal for e in entries), 1) == 622.6
    assert round(sum(e.protein for e in entries), 1) == 20.3
    assert {e.slot for e in entries} == {"breakfast"}  # the meal's own slot
    assert len({e.group_id for e in entries}) == 1 and entries[0].meal_name == "oatmeal bowl"


def test_nimrou_uses_the_billa_label_and_usda_portions(catalog_db):
    _, lines = food_log.preview_meal(catalog_db, "nimroo")  # alias
    by_food = {l.food: l for l in lines}
    assert by_food["protein toast"].grams == 104 and by_food["protein toast"].kcal == 296.4  # 4 x 26 g
    assert by_food["egg"].grams == 100 and by_food["khiarshour"].grams == 111  # 2 large, 3 small
    assert food_log.lines_dict(lines)["kcal"] == 506.9


def test_meal_options_when_logging(catalog_db):
    entries = food_log.log_meal(catalog_db, DAY, "snack", "oatmeal bowl",
                                include_optional=["blueberries"], grams_override={"oats": 60, "milk": 0})
    assert [(e.food_name, e.grams) for e in entries] == [("oats", 60), ("banana", 118), ("blueberries", 100)]
    assert entries[0].slot == "snack"
    with pytest.raises(NutritionError, match="nothing to log"):
        food_log.log_meal(catalog_db, DAY, None, "meat plate",
                          grams_override={"beef": 0, "mixed vegetables": 0, "fries": 0})


def test_history_keeps_the_values_from_logging_time(catalog_db):
    entry = food_log.log_food(catalog_db, DAY, "snack", "banana", grams=100)
    banana = catalog_db.scalar(select(Food).where(Food.name == "banana"))
    banana.kcal_per_100g = 200  # e.g. the catalogue is corrected later
    catalog_db.commit()
    assert catalog_db.get(FoodLogEntry, entry.id).kcal == 89.0


def test_delete_one_entry_or_the_whole_meal(catalog_db):
    entries = food_log.log_meal(catalog_db, DAY, None, "pasta")
    assert food_log.delete_entries(catalog_db, entries[0].id) == 1
    assert food_log.delete_entries(catalog_db, entries[1].id, whole_meal=True) == len(entries) - 1
    assert count(catalog_db, FoodLogEntry) == 0
    with pytest.raises(NutritionError):
        food_log.delete_entries(catalog_db, 9999)


# ---------- history ----------

def test_day_and_month(catalog_db):
    food_log.log_meal(catalog_db, DAY, None, "oatmeal bowl")
    food_log.log_food(catalog_db, date(2026, 11, 18), "lunch", "pasta", grams=100)
    food_log.log_food(catalog_db, date(2026, 10, 31), "lunch", "pasta", grams=100)  # other month

    today = food_log.day(catalog_db, DAY)
    assert today["kcal"] == 623 and today["by_slot"]["breakfast"]["kcal"] == 623
    assert len(today["entries"]) == 3

    november = food_log.month(catalog_db, 2026, 11, today=DAY)
    assert november["days_logged"] == 2 and november["days_in_range"] == 20
    assert november["total_kcal"] == 623 + 371
    assert november["avg_kcal_per_logged_day"] == round((623 + 371) / 2)

    december_of_last_year = food_log.month(catalog_db, 2025, 12, today=DAY)  # year boundary
    assert december_of_last_year["days_in_range"] == 31 and december_of_last_year["days_logged"] == 0


def test_preview_food_saves_nothing(catalog_db):
    line = food_log.preview_food(catalog_db, "eier", portion="1 large", count=2)
    assert (line.food, line.grams, line.kcal) == ("egg", 100.0, 143.0)
    assert catalog_db.scalar(select(func.count()).select_from(FoodLogEntry)) == 0
