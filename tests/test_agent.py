"""Tests for the agent loop with a fake Claude client: no API calls, no cost."""

import json
from datetime import date
from types import SimpleNamespace

from src.agent import MAX_TOOL_CALLS, Agent
from src.prompts import SYSTEM_PROMPT
from src.tools import ToolContext

TODAY = date(2026, 2, 15)


# ---------- a scripted stand-in for anthropic.Anthropic ----------

def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(name, tool_input, id_):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=id_)


def response(*blocks, stop_reason=None):
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    usage = SimpleNamespace(input_tokens=100, output_tokens=20,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, usage=usage)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self  # so client.messages.create works

    def create(self, **kwargs):
        # Snapshot the history as it was at call time (the agent keeps appending to the list).
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def make_agent(responses, tmp_path, **kwargs):
    client = FakeClient(responses)
    return Agent(client, "test-model", log_path=tmp_path / "agent.jsonl", **kwargs), client


def log_lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "agent.jsonl").read_text().splitlines()]


# ---------- tests ----------

def test_tool_call_then_answer(sample_db, tmp_path):
    agent, client = make_agent([
        response(tool_use("get_load_summary", {"days": 7}, "t1")),
        response(text("Your ACWR is 1.69, above 1.5.")),
    ], tmp_path)

    reply = agent.ask("How is my load?", ToolContext(sample_db, TODAY))

    assert reply.text == "Your ACWR is 1.69, above 1.5."
    assert reply.tool_calls == [{"name": "get_load_summary", "input": {"days": 7}, "is_error": False}]
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "t1"
    assert json.loads(tool_result["content"])["acwr"] == 1.69


def test_request_shape_caching_and_date_context(sample_db, tmp_path):
    agent, client = make_agent([response(text("Hi"))], tmp_path)
    agent.ask("Hello", ToolContext(sample_db, TODAY))

    request = client.requests[0]
    assert request["system"][0]["text"] == SYSTEM_PROMPT
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["tool_choice"] == {"type": "auto"}
    first_message = request["messages"][0]["content"]
    assert "2026-02-15" in first_message[0]["text"]  # date sent with the message...
    assert "2026-02-15" not in SYSTEM_PROMPT           # ...not in the cached system prompt
    assert first_message[-1]["text"] == "Hello"


def test_parallel_tool_results_go_back_in_one_message(sample_db, tmp_path):
    agent, client = make_agent([
        response(tool_use("get_load_summary", {}, "a"), tool_use("get_active_flags", {}, "b")),
        response(text("Done")),
    ], tmp_path)
    agent.ask("Status?", ToolContext(sample_db, TODAY))

    last_user = client.requests[1]["messages"][-1]
    assert last_user["role"] == "user"
    assert [r["tool_use_id"] for r in last_user["content"]] == ["a", "b"]


def test_full_assistant_content_is_kept_in_history(sample_db, tmp_path):
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    agent, client = make_agent([
        response(thinking, tool_use("get_active_flags", {}, "a")),
        response(text("Done")),
    ], tmp_path)
    agent.ask("Flags?", ToolContext(sample_db, TODAY))
    assert client.requests[1]["messages"][1]["content"][0] is thinking


def test_tool_errors_are_returned_to_the_model(sample_db, tmp_path):
    agent, client = make_agent([
        response(tool_use("get_sessions", {"start_date": "2026-02-15", "end_date": "2026-02-01"}, "a")),
        response(text("Sorry, bad range.")),
    ], tmp_path)
    reply = agent.ask("Sessions?", ToolContext(sample_db, TODAY))
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert reply.tool_calls[0]["is_error"] is True


def test_tool_call_cap_switches_tools_off(sample_db, tmp_path):
    looping = [response(tool_use("get_active_flags", {}, f"t{i}")) for i in range(MAX_TOOL_CALLS)]
    agent, client = make_agent(looping + [response(text("Here is what I have."))], tmp_path)

    reply = agent.ask("Loop forever", ToolContext(sample_db, TODAY))

    assert len(reply.tool_calls) == MAX_TOOL_CALLS
    assert client.requests[-1]["tool_choice"] == {"type": "none"}
    assert all(r["tool_choice"] == {"type": "auto"} for r in client.requests[:-1])
    assert reply.text == "Here is what I have."


def test_extra_calls_in_one_response_get_error_results(sample_db, tmp_path):
    many = [tool_use("get_active_flags", {}, f"t{i}") for i in range(MAX_TOOL_CALLS + 2)]
    agent, client = make_agent([response(*many), response(text("Ok"))], tmp_path)

    reply = agent.ask("Many", ToolContext(sample_db, TODAY))

    results = client.requests[1]["messages"][-1]["content"]
    assert len(results) == MAX_TOOL_CALLS + 2           # every tool_use gets a result
    assert [r["is_error"] for r in results[-2:]] == [True, True]
    executed = [line for line in log_lines(tmp_path) if line["event"] == "tool_call"]
    assert len(executed) == MAX_TOOL_CALLS
    assert client.requests[1]["tool_choice"] == {"type": "none"}
    assert reply.text == "Ok"


def test_loop_stops_even_if_the_model_ignores_tool_choice_none(sample_db, tmp_path):
    many = [tool_use("get_active_flags", {}, f"t{i}") for i in range(MAX_TOOL_CALLS)]
    stubborn = response(tool_use("get_active_flags", {}, "extra"))
    agent, client = make_agent([response(*many), stubborn], tmp_path)

    reply = agent.ask("Many", ToolContext(sample_db, TODAY))

    assert len(client.requests) == 2  # no third request: the loop ends
    assert agent.messages[-1]["content"][0]["is_error"] is True  # the extra call still got a result
    assert "No answer" in reply.text


def test_proposals_are_collected_for_confirmation(sample_db, tmp_path):
    agent, _ = make_agent([
        response(tool_use("log_session", {"date": "2026-02-15", "sport_type": "padel",
                                          "duration_min": 75, "rpe": 6}, "a")),
        response(text("Waiting for your confirmation.")),
    ], tmp_path)
    reply = agent.ask("Played padel 75 min, felt like a 6", ToolContext(sample_db, TODAY))
    assert len(reply.pending_action_ids) == 1


def test_notes_are_sent_with_the_next_message(sample_db, tmp_path):
    agent, client = make_agent([response(text("Noted"))], tmp_path)
    agent.add_note("User confirmed pending action 3")
    agent.ask("Thanks", ToolContext(sample_db, TODAY))
    texts = [b["text"] for b in client.requests[0]["messages"][0]["content"]]
    assert "[User confirmed pending action 3]" in texts


def test_refusal_and_max_tokens_get_clear_messages(sample_db, tmp_path):
    agent, _ = make_agent([response(text(""), stop_reason="refusal")], tmp_path)
    assert "can't help" in agent.ask("x", ToolContext(sample_db, TODAY)).text

    agent, _ = make_agent([response(text("Long answer"), stop_reason="max_tokens")], tmp_path)
    assert "cut off" in agent.ask("x", ToolContext(sample_db, TODAY)).text


def test_every_request_and_tool_call_is_logged(sample_db, tmp_path):
    agent, _ = make_agent([
        response(tool_use("get_load_summary", {}, "a")),
        response(text("Done")),
    ], tmp_path)
    agent.ask("Load?", ToolContext(sample_db, TODAY))

    events = [line["event"] for line in log_lines(tmp_path)]
    assert events == ["request", "tool_call", "request"]
    tool_line = log_lines(tmp_path)[1]
    assert tool_line["tool"] == "get_load_summary" and tool_line["output"]["acwr"] == 1.69
    assert all(line["conversation_id"] == agent.conversation_id for line in log_lines(tmp_path))


def test_reset_starts_a_new_conversation(sample_db, tmp_path):
    agent, _ = make_agent([response(text("Hi"))], tmp_path)
    agent.ask("Hello", ToolContext(sample_db, TODAY))
    old_id = agent.conversation_id
    agent.reset()
    assert agent.messages == [] and agent.conversation_id != old_id


def test_tool_calls_are_announced(sample_db, tmp_path):
    seen = []
    agent, _ = make_agent([
        response(tool_use("get_active_flags", {}, "a")),
        response(text("Done")),
    ], tmp_path, on_tool_call=lambda name, inp: seen.append(name))
    agent.ask("Flags?", ToolContext(sample_db, TODAY))
    assert seen == ["get_active_flags"]
