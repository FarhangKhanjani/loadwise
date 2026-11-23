import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.api import create_app
from src.models import PendingAction, TrainingSession

KEY = "test-key-0123456789abcdef"
AUTH = {"X-API-Key": KEY}


@pytest.fixture
def client(bot_state):
    with TestClient(create_app(bot_state, api_key=KEY)) as c:  # no Telegram in tests
        yield c


def count(bot_state, model) -> int:
    with bot_state.session_factory() as db:
        return db.scalar(select(func.count()).select_from(model))


def test_health_needs_no_key(client):
    assert client.get("/health").json() == {"status": "ok", "database": "ok", "bot": "disabled",
                                            "last_check": None}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong-key-0123456789"}])
def test_data_endpoints_need_the_key(client, headers):
    for path in ("/metrics", "/flags"):
        assert client.get(path, headers=headers).status_code == 401
    assert client.post("/sessions", json={"sport_type": "gym", "duration_min": 60, "rpe": 7},
                       headers=headers).status_code == 401


@pytest.mark.parametrize("server_key", [None, "", "your-api-key"])  # unset or the placeholder
def test_api_is_closed_without_a_usable_server_key(bot_state, server_key):
    with TestClient(create_app(bot_state, api_key=server_key)) as c:
        assert c.get("/metrics", headers={"X-API-Key": server_key or "x"}).status_code == 503


def test_metrics_match_the_tools(client):
    body = client.get("/metrics", headers=AUTH).json()  # today = 2026-02-15 (spike day)
    assert body["as_of"] == "2026-02-15"
    assert body["acwr"] == 1.69 and body["acute_load_7d"] == 4253
    assert body["weeks_with_sessions_28d"] == 4
    assert client.get("/metrics?as_of=2026-01-20", headers=AUTH).json()["acwr"] is None


def test_bad_query_values_are_422(client):
    assert client.get("/metrics?days=0", headers=AUTH).status_code == 422
    assert client.get("/metrics?as_of=yesterday", headers=AUTH).status_code == 422


def test_flags(client):
    body = client.get("/flags", headers=AUTH).json()
    assert {f["flag_type"] for f in body["flags"]} == {
        "high_load_spike", "high_monotony", "poor_sleep", "high_soreness"}


def test_post_session_only_proposes(client, bot_state):
    before = count(bot_state, TrainingSession)
    response = client.post("/sessions", headers=AUTH,
                           json={"sport_type": "padel", "duration_min": 75, "rpe": 6, "notes": "evening"})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "proposed" and body["telegram_sent"] is False
    assert body["summary"] == "Log padel, 75 min, RPE 6 (load 450) on 2026-02-15 (evening)"
    assert count(bot_state, TrainingSession) == before  # nothing saved until confirmed
    assert count(bot_state, PendingAction) == 1


@pytest.mark.parametrize("payload", [
    {"sport_type": "gym", "duration_min": 60, "rpe": 11},
    {"sport_type": "chess", "duration_min": 60, "rpe": 5},
    {"date": "2026-02-16", "sport_type": "gym", "duration_min": 60, "rpe": 5},  # future
    {"sport_type": "gym", "rpe": 5},                                            # missing field
])
def test_post_session_validation(client, bot_state, payload):
    assert client.post("/sessions", headers=AUTH, json=payload).status_code == 422
    assert count(bot_state, PendingAction) == 0


# ---------- calorie tracker ----------

def test_food_search_uses_aliases(client):
    foods = client.get("/foods?q=haferflocken", headers=AUTH).json()
    assert foods[0]["name"] == "oats" and foods[0]["kcal_per_100g"] == 379.0
    assert "USDA" in foods[0]["source"]
    assert len(client.get("/foods", headers=AUTH).json()) >= 20  # no query: the whole list


def test_meals_list_default_totals(client):
    meals = {m["name"]: m for m in client.get("/meals", headers=AUTH).json()}
    assert meals["oatmeal bowl"]["default"]["kcal"] == 622.6
    assert meals["oatmeal bowl"]["optional"] == ["blueberries", "mango"]


def test_log_food_and_see_it_today(client):
    response = client.post("/food-log", headers=AUTH, json={"slot": "snack", "food": "banana",
                                                           "portion": "1 medium", "count": 1})
    assert response.status_code == 201 and response.json()["kcal"] == 105.0
    day = client.get("/nutrition/day", headers=AUTH).json()  # today = 2026-02-15 (bot_state)
    assert day["date"] == "2026-02-15" and day["kcal"] == 105


def test_preview_then_log_meal_then_delete(client):
    body = {"meal": "oatmeal bowl", "include_optional": ["mango"]}
    preview = client.post("/food-log/meal/preview", headers=AUTH, json=body).json()
    assert preview["kcal"] == 682.6  # + 100 g mango (60)
    logged = client.post("/food-log/meal", headers=AUTH, json=body)
    assert logged.status_code == 201 and logged.json()["kcal"] == 682.6
    first = logged.json()["entries"][0]["id"]
    deleted = client.delete(f"/food-log/{first}?whole_meal=true", headers=AUTH).json()
    assert deleted["deleted"] == 4
    assert client.delete(f"/food-log/{first}", headers=AUTH).status_code == 404


def test_month_history(client):
    client.post("/food-log", headers=AUTH, json={"date": "2026-02-10", "slot": "lunch", "food": "pasta",
                                                 "grams": 100})
    month = client.get("/nutrition/month?month=2026-02", headers=AUTH).json()
    assert month["days_logged"] == 1 and month["total_kcal"] == 371 and month["days_in_range"] == 15
    assert client.get("/nutrition/month?month=feb", headers=AUTH).status_code == 422


@pytest.mark.parametrize("body", [
    {"slot": "brunch", "food": "oats", "grams": 100},
    {"slot": "lunch", "food": "unicorn steak", "grams": 100},
    {"slot": "lunch", "food": "oats", "grams": 0},
])
def test_log_food_validation(client, body):
    assert client.post("/food-log", headers=AUTH, json=body).status_code == 422


def test_food_endpoints_need_the_key(client):
    assert client.get("/foods").status_code == 401
    assert client.post("/food-log", json={"slot": "lunch", "food": "oats", "grams": 1}).status_code == 401
