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


# ---------- phone app (Phase 9) ----------

from src.models import Wellness  # noqa: E402


def test_new_endpoints_need_the_key(client):
    assert client.get("/meta").status_code == 401
    assert client.get("/sessions/recent").status_code == 401
    assert client.post("/food-log/preview", json={"food": "oats", "grams": 50}).status_code == 401
    assert client.post("/wellness", json={"sleep_hours": 8, "soreness": 2, "fatigue": 2,
                                          "stress": 2}).status_code == 401
    assert client.post("/actions/1/confirm").status_code == 401
    assert client.post("/actions/1/reject").status_code == 401


def test_meta(client):
    meta = client.get("/meta", headers=AUTH).json()
    assert meta["today"] == "2026-02-15"
    assert "padel" in meta["sports"] and meta["slots"] == ["breakfast", "lunch", "dinner", "snack"]
    assert meta["suggested_slot"] in meta["slots"]


def test_recent_sessions_newest_first(client):
    body = client.get("/sessions/recent?days=7", headers=AUTH).json()
    dates = [s["date"] for s in body["sessions"]]
    assert dates == sorted(dates, reverse=True) and dates[0] <= "2026-02-15"
    assert body["start_date"] == "2026-02-09" and all("load" in s for s in body["sessions"])
    assert client.get("/sessions/recent?days=0", headers=AUTH).status_code == 422


def test_food_preview(client, bot_state):
    line = client.post("/food-log/preview", headers=AUTH,
                       json={"food": "banana", "portion": "1 medium", "count": 1}).json()
    assert line == {"food": "banana", "grams": 118.0, "kcal": 105.0, "protein": 1.3}
    assert client.post("/food-log/preview", headers=AUTH,
                       json={"food": "banana", "grams": 0}).status_code == 422


def test_session_confirmed_in_the_app(client, bot_state):
    before = count(bot_state, TrainingSession)
    proposal = client.post("/sessions?notify_telegram=false", headers=AUTH,
                           json={"sport_type": "padel", "duration_min": 75, "rpe": 6}).json()
    assert proposal["telegram_sent"] is False and proposal["session_load"] == 450
    action_id = proposal["pending_action_id"]
    confirmed = client.post(f"/actions/{action_id}/confirm", headers=AUTH)
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "confirmed"
    assert count(bot_state, TrainingSession) == before + 1
    # a second tap (or the Telegram button after the app) changes nothing
    assert client.post(f"/actions/{action_id}/confirm", headers=AUTH).status_code == 409
    assert client.post(f"/actions/{action_id}/reject", headers=AUTH).status_code == 409
    assert count(bot_state, TrainingSession) == before + 1


def test_rejected_proposal_saves_nothing(client, bot_state):
    before = count(bot_state, TrainingSession)
    action_id = client.post("/sessions?notify_telegram=false", headers=AUTH,
                            json={"sport_type": "gym", "duration_min": 60, "rpe": 7}).json()["pending_action_id"]
    assert client.post(f"/actions/{action_id}/reject", headers=AUTH).json()["status"] == "rejected"
    assert count(bot_state, TrainingSession) == before
    assert client.post("/actions/9999/confirm", headers=AUTH).status_code == 404


def test_wellness_proposal_and_confirm(client, bot_state):
    before = count(bot_state, Wellness)
    body = {"date": "2026-01-18", "sleep_hours": 7.5, "soreness": 3, "fatigue": 4, "stress": 2}
    proposal = client.post("/wellness?notify_telegram=false", headers=AUTH, json=body)
    assert proposal.status_code == 202
    assert proposal.json()["summary"].startswith("Log wellness for 2026-01-18: sleep 7.5 h")
    assert count(bot_state, Wellness) == before
    client.post(f"/actions/{proposal.json()['pending_action_id']}/confirm", headers=AUTH)
    assert count(bot_state, Wellness) == before + 1
    # that day is logged now, and changing it is not supported
    assert client.post("/wellness", headers=AUTH, json=body).status_code == 422


def test_two_wellness_proposals_for_one_day_conflict(client, bot_state):
    body = {"date": "2026-01-25", "sleep_hours": 8, "soreness": 2, "fatigue": 2, "stress": 2}
    first, second = (client.post("/wellness", headers=AUTH, json=body).json()["pending_action_id"]
                     for _ in range(2))
    assert client.post(f"/actions/{first}/confirm", headers=AUTH).status_code == 200
    assert client.post(f"/actions/{second}/confirm", headers=AUTH).status_code == 409


@pytest.mark.parametrize("body", [
    {"sleep_hours": 30, "soreness": 2, "fatigue": 2, "stress": 2},
    {"sleep_hours": 8, "soreness": 0, "fatigue": 2, "stress": 2},
    {"date": "2026-02-16", "sleep_hours": 8, "soreness": 2, "fatigue": 2, "stress": 2},
])
def test_wellness_validation(client, body):
    assert client.post("/wellness", headers=AUTH, json=body).status_code == 422


def test_app_files_need_no_key_and_send_security_headers(client):
    page = client.get("/app/")
    assert page.status_code == 200 and "LoadWise" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    for path in ("/app/app.js", "/app/app.css", "/app/manifest.webmanifest", "/app/sw.js",
                 "/app/icon-192.png", "/app/icon-512.png"):
        assert client.get(path).status_code == 200, path
    assert "Content-Security-Policy" not in client.get("/health").headers


def test_root_redirects_to_the_app(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/app/"


def test_app_has_no_inline_scripts_or_handlers():
    """The CSP blocks inline code, so any inline script or onclick would silently not work."""
    import re
    from src.api import WEB_DIR

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert not re.search(r"\son[a-z]+=", html)
    assert "style=" not in html
