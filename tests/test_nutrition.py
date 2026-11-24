"""Calorie maths, portions, saved meals, search and monthly summaries (pure functions)."""

from dataclasses import dataclass, field
from datetime import date

import pytest

from src.nutrition import (
    NutritionError,
    day_summary,
    grams_for,
    item_values,
    meal_lines,
    month_summary,
    normalize,
    search,
    suggested_slot,
    total,
)


@dataclass
class F:
    name: str
    kcal_per_100g: float
    protein_per_100g: float = 0.0
    aliases: list = field(default_factory=list)
    portions: dict = field(default_factory=dict)


@dataclass
class E:
    date: date
    slot: str
    kcal: float
    protein: float = 0.0


OATS = F("oats", 379, 13.15, ["haferflocken"])
EGG = F("egg", 143, 12.56, ["ei", "eier"], {"1 large": 50})
BANANA = F("banana", 89, 1.09, ["banane"], {"1 medium": 118})
VEG = F("mixed vegetables", 72, 3.33, ["veggies", "gemüse"])
FOODS = {f.name: f for f in (OATS, EGG, BANANA, VEG)}


# ---------- amounts ----------

def test_item_values_scale_per_100_g():
    line = item_values(OATS, 120)
    assert (line.kcal, line.protein) == (454.8, 15.8)  # 379 x 1.2, 13.15 x 1.2


@pytest.mark.parametrize("grams", [0, -5, 5001])
def test_item_values_reject_impossible_amounts(grams):
    with pytest.raises(NutritionError):
        item_values(OATS, grams)


def test_grams_from_portion_and_count():
    assert grams_for(EGG, portion="1 large", count=3) == 150
    assert grams_for(EGG, grams=60) == 60  # explicit grams win


@pytest.mark.parametrize("kwargs, message", [
    ({}, "grams or a portion"),
    ({"portion": "1 jumbo"}, "no portion"),
    ({"portion": "1 large", "count": 0}, "count"),
])
def test_grams_errors(kwargs, message):
    with pytest.raises(NutritionError, match=message):
        grams_for(EGG, **kwargs)


# ---------- saved meals ----------

BOWL = [
    {"food": "oats", "grams": 120},
    {"food": "banana", "portion": "1 medium", "count": 1},
    {"food": "egg", "grams": 50, "optional": True},
]


def test_meal_leaves_out_optional_items_by_default():
    lines = meal_lines(BOWL, FOODS)
    assert [l.food for l in lines] == ["oats", "banana"]
    assert total(lines) == (559.8, 17.1)  # 454.8 + 105.0 kcal, 15.8 + 1.3 g protein


def test_meal_with_optional_item_and_changed_amount():
    lines = meal_lines(BOWL, FOODS, include_optional=["egg"], grams_override={"oats": 60})
    assert [(l.food, l.grams) for l in lines] == [("oats", 60), ("banana", 118), ("egg", 50)]


def test_meal_override_zero_removes_an_item_and_unknown_food_fails():
    assert [l.food for l in meal_lines(BOWL, FOODS, grams_override={"banana": 0})] == ["oats"]
    with pytest.raises(NutritionError, match="unknown food"):
        meal_lines([{"food": "pizza", "grams": 100}], FOODS)


# ---------- search ----------

@pytest.mark.parametrize("query, first", [
    ("oats", "oats"),
    ("Haferflocken", "oats"),   # German alias, any case
    ("eier", "egg"),
    ("gemuse", "mixed vegetables"),  # umlaut typed without dots
    ("GEMÜSE", "mixed vegetables"),
    ("bananna", "banana"),      # typo
    ("veg", "mixed vegetables"),  # prefix
])
def test_search_finds_the_right_food(query, first):
    assert search(query, FOODS.values())[0].name == first


def test_search_ranks_exact_above_partial_and_ignores_nonsense():
    foods = [F("egg", 1), F("eggplant", 1)]
    assert [f.name for f in search("egg", foods)] == ["egg", "eggplant"]
    assert search("chocolate", FOODS.values()) == []
    assert search("   ", FOODS.values()) == []


def test_normalize():
    assert normalize(" Eiweißtoast ") == "eiweisstoast"


# ---------- history ----------

def test_day_summary_by_slot():
    summary = day_summary([E(date(2026, 11, 1), "breakfast", 600.4, 20.2),
                           E(date(2026, 11, 1), "lunch", 900.3, 50.1)])
    assert summary["kcal"] == 1501 and summary["protein"] == 70
    assert summary["by_slot"]["breakfast"] == {"kcal": 600, "protein": 20}
    assert summary["by_slot"]["dinner"] == {"kcal": 0, "protein": 0}


def test_month_summary_averages_only_logged_days():
    entries = [E(date(2026, 11, 1), "lunch", 2000), E(date(2026, 11, 3), "lunch", 1000),
               E(date(2026, 10, 31), "lunch", 5000)]  # other month: ignored
    summary = month_summary(entries, 2026, 11, today=date(2026, 11, 5))
    assert summary["days_in_range"] == 5  # current month: only up to today
    assert summary["days_logged"] == 2
    assert summary["total_kcal"] == 3000
    assert summary["avg_kcal_per_logged_day"] == 1500  # not 3000 / 5
    assert [d["kcal"] for d in summary["days"]] == [2000, 0, 1000, 0, 0]


def test_month_summary_for_a_past_month_and_an_empty_month():
    past = month_summary([], 2026, 2, today=date(2026, 11, 5))
    assert past["days_in_range"] == 28 and past["avg_kcal_per_logged_day"] is None


@pytest.mark.parametrize("hour, slot", [
    (0, "breakfast"), (10, "breakfast"), (11, "lunch"), (15, "lunch"),
    (16, "snack"), (17, "snack"), (18, "dinner"), (23, "dinner"),
])
def test_suggested_slot_follows_the_time_of_day(hour, slot):
    assert suggested_slot(hour) == slot
