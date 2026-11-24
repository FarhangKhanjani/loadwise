"""Calorie and protein tracking: pure calculations, no database, no LLM.

All numbers are per 100 g in the food catalogue (USDA FoodData Central or a package label),
so an item's value is simply grams / 100 x value. Amounts are given either in grams or as
a named portion with a count ("1 large" egg x 2), using the portion weights from USDA.

Search is forgiving on purpose: case, German umlauts and small typos don't matter, and
every food has aliases (German names, short forms).
"""

import calendar
import unicodedata
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import Iterable, Protocol

SLOTS = ("breakfast", "lunch", "dinner", "snack")
FUZZY_MIN = 0.75  # similarity needed for a typo match ("bananna" -> "banana")


class FoodLike(Protocol):
    name: str
    kcal_per_100g: float
    protein_per_100g: float
    aliases: list
    portions: dict


class EntryLike(Protocol):
    date: date
    slot: str
    kcal: float
    protein: float


class NutritionError(ValueError):
    """A problem the user can fix: unknown food, unknown portion, bad amount."""


@dataclass(frozen=True)
class Line:
    food: str
    grams: float
    kcal: float
    protein: float


def suggested_slot(hour: int) -> str:
    """The slot the app preselects for the local time: before 11 breakfast, before 16 lunch,
    before 18 snack, then dinner. Only a default; the user can always pick another."""
    if hour < 11:
        return "breakfast"
    if hour < 16:
        return "lunch"
    if hour < 18:
        return "snack"
    return "dinner"


# ---------- amounts ----------

def item_values(food: FoodLike, grams: float) -> Line:
    if not 0 < grams <= 5000:
        raise NutritionError(f"amount for {food.name} must be between 0 and 5000 g, got {grams}")
    return Line(food.name, round(grams, 1),
                round(food.kcal_per_100g * grams / 100, 1),
                round(food.protein_per_100g * grams / 100, 1))


def grams_for(food: FoodLike, grams: float | None = None, portion: str | None = None,
              count: float = 1) -> float:
    """Grams from either an explicit weight or a named portion times a count."""
    if grams is not None:
        return float(grams)
    if portion is None:
        raise NutritionError(f"give grams or a portion for {food.name}")
    if portion not in food.portions:
        known = ", ".join(food.portions) or "none, use grams"
        raise NutritionError(f"{food.name} has no portion {portion!r} (portions: {known})")
    if count <= 0:
        raise NutritionError("count must be positive")
    return food.portions[portion] * count


def meal_lines(items: list[dict], foods: dict[str, FoodLike], include_optional: Iterable[str] = (),
               grams_override: dict[str, float] | None = None) -> list[Line]:
    """The lines of a saved meal. Optional items are left out unless named in include_optional;
    grams_override replaces the default amount of a food (set it to 0 to leave a food out)."""
    include_optional, grams_override = set(include_optional), grams_override or {}
    lines = []
    for item in items:
        name = item["food"]
        if item.get("optional") and name not in include_optional and name not in grams_override:
            continue
        if name not in foods:
            raise NutritionError(f"meal uses unknown food {name!r}")
        food = foods[name]
        if name in grams_override:
            if grams_override[name] == 0:
                continue
            grams = float(grams_override[name])
        else:
            grams = grams_for(food, item.get("grams"), item.get("portion"), item.get("count", 1))
        lines.append(item_values(food, grams))
    return lines


def total(lines: Iterable[Line]) -> tuple[float, float]:
    lines = list(lines)
    return round(sum(l.kcal for l in lines), 1), round(sum(l.protein for l in lines), 1)


# ---------- search ----------

def normalize(text: str) -> str:
    """'Gemüse ' -> 'gemuse', 'Eiweißtoast' -> 'eiweisstoast': case, accents and ß don't matter."""
    text = text.strip().lower().replace("ß", "ss")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _score(query: str, names: list[str]) -> float:
    """1.0 exact, 0.9 prefix, 0.8 contained, else typo similarity (0 if too different)."""
    best = 0.0
    for name in names:
        if query == name:
            return 1.0
        if name.startswith(query):
            best = max(best, 0.9)
        elif query in name:
            best = max(best, 0.8)
        else:
            similarity = max([SequenceMatcher(None, query, name).ratio()]
                             + [SequenceMatcher(None, query, word).ratio() for word in name.split()])
            if similarity >= FUZZY_MIN:
                best = max(best, similarity * 0.79)  # always ranked below real substring matches
    return best


def search(query: str, entries: Iterable, limit: int = 10) -> list:
    """Entries (foods or meals, anything with .name and .aliases) best match first."""
    q = normalize(query)
    if not q:
        return []
    scored = []
    for entry in entries:
        score = _score(q, [normalize(entry.name)] + [normalize(a) for a in entry.aliases])
        if score > 0:
            scored.append((score, entry.name, entry))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [entry for _, _, entry in scored[:limit]]


# ---------- history ----------

def day_summary(entries: Iterable[EntryLike]) -> dict:
    entries = list(entries)
    by_slot = {slot: {"kcal": 0.0, "protein": 0.0} for slot in SLOTS}
    for e in entries:
        by_slot[e.slot]["kcal"] += e.kcal
        by_slot[e.slot]["protein"] += e.protein
    return {
        "kcal": round(sum(e.kcal for e in entries)),
        "protein": round(sum(e.protein for e in entries)),
        "items": len(entries),
        "by_slot": {slot: {k: round(v) for k, v in values.items()} for slot, values in by_slot.items()},
    }


def month_summary(entries: Iterable[EntryLike], year: int, month: int, today: date) -> dict:
    """Totals per day for one month. The average counts only days with something logged:
    an empty day is far more likely a forgotten log than a day without food."""
    per_day: dict[date, list] = {}
    for e in entries:
        if (e.date.year, e.date.month) == (year, month):
            per_day.setdefault(e.date, []).append(e)
    last_day = calendar.monthrange(year, month)[1]
    if (year, month) == (today.year, today.month):
        last_day = today.day
    days = [date(year, month, d) for d in range(1, last_day + 1)]
    daily = [{"date": d.isoformat(),
              "kcal": round(sum(e.kcal for e in per_day.get(d, []))),
              "protein": round(sum(e.protein for e in per_day.get(d, []))),
              "logged": d in per_day} for d in days]
    logged = [d for d in daily if d["logged"]]
    total_kcal = sum(d["kcal"] for d in logged)
    return {
        "month": f"{year:04d}-{month:02d}",
        "days": daily,
        "days_logged": len(logged),
        "days_in_range": len(days),
        "total_kcal": total_kcal,
        "total_protein": sum(d["protein"] for d in logged),
        "avg_kcal_per_logged_day": round(total_kcal / len(logged)) if logged else None,
        "avg_protein_per_logged_day": round(sum(d["protein"] for d in logged) / len(logged)) if logged else None,
    }
