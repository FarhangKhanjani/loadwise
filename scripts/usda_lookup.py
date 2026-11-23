"""Look up foods in USDA FoodData Central and build data/foods.csv from data/food_sources.csv.

Every number in foods.csv is traceable: a USDA FoodData Central ID (fdc_id), a package
label, or an average of named USDA IDs. Nothing is typed in from memory.

  python scripts/usda_lookup.py search "bananas raw"     # find the fdc_id for a new food
  python scripts/usda_lookup.py build                    # regenerate data/foods.csv

Needs USDA_API_KEY in .env (free: https://api.data.gov/signup/). The key is sent only to
api.nal.usda.gov and never printed.

food_sources.csv columns:
  name       English name, unique
  source     "usda:<fdc_id>", "usda_avg:<id>+<id>+..." or "label:<description>"
  kcal, protein   per 100 g; only for "label:" rows (copied from the package)
  aliases    other names separated by "|" (German names, short forms)
  portions   "<name>=<grams>" separated by "|", e.g. "1 egg=50|1 slice=26"
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

DATA = Path(__file__).resolve().parent.parent / "data"
SOURCES = DATA / "food_sources.csv"
FOODS = DATA / "foods.csv"
API = "https://api.nal.usda.gov/fdc/v1"
# Energy is "Energy" (kcal) for most SR Legacy foods; a few only carry the Atwater variants.
ENERGY_NAMES = ("Energy", "Energy (Atwater General Factors)", "Energy (Atwater Specific Factors)")


def _key() -> str:
    load_dotenv()
    key = os.getenv("USDA_API_KEY")
    if not key:
        sys.exit("Set USDA_API_KEY in .env (free key: https://api.data.gov/signup/).")
    return key


def _get(path: str, **params) -> object:
    url = f"{API}/{path}?{urllib.parse.urlencode({**params, 'api_key': _key()})}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 3:
                raise SystemExit(f"USDA API error {e.code} for {path}") from None
            time.sleep(10 * (attempt + 1))


def nutrients(food: dict) -> tuple[float | None, float | None]:
    """(kcal, protein) per 100 g from a FoodData Central food record."""
    kcal = protein = None
    for item in food.get("foodNutrients", []):
        nutrient = item.get("nutrient") or {}
        name = nutrient.get("name") or item.get("nutrientName")
        unit = (nutrient.get("unitName") or item.get("unitName") or "").lower()
        value = item.get("amount", item.get("value"))
        if name in ENERGY_NAMES and unit == "kcal" and kcal is None:
            kcal = value
        elif name == "Protein" and protein is None:
            protein = value
    return kcal, protein


def search(query: str) -> None:
    result = _get("foods/search", query=query, dataType="SR Legacy", pageSize=5)
    for food in result.get("foods", []):
        kcal, protein = nutrients(food)
        print(f"  {food['fdcId']:>7}  {kcal} kcal  {protein} g protein  | {food['description']}")


def fetch(fdc_ids: list[int]) -> dict[int, tuple[str, float, float]]:
    """fdc_id -> (description, kcal, protein), in one request."""
    foods = _get("foods", fdcIds=",".join(map(str, fdc_ids)))
    out = {}
    for food in foods:
        kcal, protein = nutrients(food)
        if kcal is None or protein is None:
            raise SystemExit(f"fdc_id {food['fdcId']} has no kcal or protein value")
        out[food["fdcId"]] = (food["description"], kcal, protein)
    return out


def build() -> None:
    rows = list(csv.DictReader(SOURCES.open(encoding="utf-8")))
    ids = sorted({int(i) for r in rows if r["source"].startswith("usda")
                  for i in r["source"].split(":", 1)[1].split("+")})
    usda = fetch(ids)

    with FOODS.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "kcal_per_100g", "protein_per_100g", "aliases", "portions", "source"])
        for r in rows:
            kind, ref = r["source"].split(":", 1)
            if kind == "label":
                kcal, protein, source = float(r["kcal"]), float(r["protein"]), f"package label: {ref}"
            else:
                parts = [usda[int(i)] for i in ref.split("+")]
                kcal = sum(p[1] for p in parts) / len(parts)
                protein = sum(p[2] for p in parts) / len(parts)
                names = "; ".join(f"{p[0]} ({i})" for p, i in zip(parts, ref.split("+")))
                source = f"USDA FoodData Central: {names}" + (" (average)" if len(parts) > 1 else "")
            writer.writerow([r["name"], round(kcal, 1), round(protein, 2), r["aliases"], r["portions"], source])
    print(f"Wrote {len(rows)} foods to {FOODS}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "search":
        for q in sys.argv[2:]:
            print(f"## {q}")
            search(q)
    elif sys.argv[1:] == ["build"]:
        build()
    else:
        sys.exit(__doc__)
