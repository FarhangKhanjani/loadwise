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
    assert body["summary"] == "Log padel, 75 min, RPE 6 on 2026-02-15 (evening)"
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
