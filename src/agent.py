"""The LoadWise agent: a hand-written Claude tool-use loop, plus a command-line chat.

One user message runs this loop:
  1. Send the conversation + tool definitions to Claude.
  2. If Claude asks for tools, run ALL requested calls and send ALL results back in ONE
     user message (splitting them teaches the model to stop making parallel calls).
     A failing tool returns an error result instead of crashing, so the model can recover.
  3. Repeat until Claude answers. After MAX_TOOL_CALLS calls, tools are switched off
     (tool_choice "none") so the model must answer with what it has.
The full assistant content (including thinking blocks) is appended to the history every
time, not just its text: current models require their thinking blocks to be passed back.

Every request and tool call is logged as one JSON line to logs/agent.jsonl.

Usage:
  python -m src.agent --db data/demo.db --as-of 2026-02-15
"""

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from src.config import get_settings
from src.prompts import SYSTEM_PROMPT, turn_context
from src.tools import PROPOSAL_TOOLS, TOOLS, ToolContext, execute_tool

MAX_TOOL_CALLS = 8
MAX_TOKENS = 16000
EFFORT = "medium"
LOG_PATH = Path("logs/agent.jsonl")


@dataclass
class AgentReply:
    text: str
    stop_reason: str
    tool_calls: list[dict] = field(default_factory=list)       # {"name", "input", "is_error"}
    pending_action_ids: list[int] = field(default_factory=list)


class Agent:
    def __init__(self, client, model: str, log_path: Path | None = LOG_PATH,
                 on_tool_call: Callable[[str, dict], None] | None = None):
        self.client = client
        self.model = model
        self.log_path = log_path
        self.on_tool_call = on_tool_call
        self.conversation_id = uuid.uuid4().hex[:12]
        self.messages: list[dict] = []
        self._notes: list[str] = []  # facts to pass along with the next user message

    def add_note(self, note: str) -> None:
        """E.g. 'User confirmed pending action 3'. Sent with the next message so the model knows."""
        self._notes.append(note)

    def reset(self) -> None:
        self.messages, self._notes = [], []
        self.conversation_id = uuid.uuid4().hex[:12]

    # ---------- the loop ----------

    def ask(self, user_text: str, ctx: ToolContext) -> AgentReply:
        content = [{"type": "text", "text": turn_context(ctx.today)}]
        content += [{"type": "text", "text": f"[{note}]"} for note in self._notes]
        content.append({"type": "text", "text": user_text})
        self._notes = []
        self.messages.append({"role": "user", "content": content})

        reply = AgentReply(text="", stop_reason="")
        while True:
            tools_allowed = len(reply.tool_calls) < MAX_TOOL_CALLS
            response = self._create(tools_allowed)
            # Append the FULL content: thinking blocks must be passed back unchanged.
            self.messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                break
            # Every tool_use must get a tool_result, otherwise the next request is invalid.
            self.messages.append({"role": "user", "content": self._run_tools(tool_uses, ctx, reply)})
            if not tools_allowed:  # should not happen with tool_choice "none"; never loop forever
                break

        reply.stop_reason = response.stop_reason
        reply.text = self._final_text(response)
        return reply

    def _create(self, tools_allowed: bool):
        started = time.perf_counter()
        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            # Cache breakpoint on the static system prompt: tools + system are the cached prefix.
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            tool_choice={"type": "auto"} if tools_allowed else {"type": "none"},
            output_config={"effort": EFFORT},
            # Automatic caching of the growing conversation, so each loop iteration re-reads it cheaply.
            cache_control={"type": "ephemeral"},
            messages=self.messages,
        )
        usage = response.usage
        self._log({
            "event": "request",
            "model": self.model,
            "stop_reason": response.stop_reason,
            "tools_allowed": tools_allowed,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
            "ms": round((time.perf_counter() - started) * 1000),
        })
        return response

    def _run_tools(self, tool_uses: list, ctx: ToolContext, reply: AgentReply) -> list[dict]:
        results = []
        for block in tool_uses:
            if len(reply.tool_calls) >= MAX_TOOL_CALLS:
                # Every tool_use needs a tool_result, even the ones we refuse to run.
                output, is_error = json.dumps({"error": "Tool call limit reached. Answer with the "
                                                        "information you already have."}), True
            else:
                if self.on_tool_call:
                    self.on_tool_call(block.name, block.input)
                started = time.perf_counter()
                output, is_error = execute_tool(ctx, block.name, block.input)
                self._log({"event": "tool_call", "tool": block.name, "input": block.input,
                           "output": json.loads(output), "is_error": is_error,
                           "ms": round((time.perf_counter() - started) * 1000)})
                if block.name in PROPOSAL_TOOLS and not is_error:
                    reply.pending_action_ids.append(json.loads(output)["pending_action_id"])
            reply.tool_calls.append({"name": block.name, "input": block.input, "is_error": is_error})
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output, "is_error": is_error})
        return results

    @staticmethod
    def _final_text(response) -> str:
        if response.stop_reason == "refusal":
            return ("Sorry, I can't help with that request. If this is about pain or an injury, "
                    "please see a doctor or physiotherapist.")
        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "max_tokens":
            text += "\n\n[The answer was cut off because it got too long.]"
        return text or "[No answer was produced. Please try rephrasing.]"

    def _log(self, record: dict) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now().isoformat(timespec="seconds"),
                  "conversation_id": self.conversation_id, **record}
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


# ---------- command-line chat ----------

def _ask_yes_no(prompt: str) -> bool:
    while True:
        answer = input(prompt).strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def main() -> None:
    import anthropic

    from src.db import init_db, make_engine, make_session_factory
    from src.models import PendingAction
    from src.tools import confirm_action, describe_payload, reject_action

    settings = get_settings()
    parser = argparse.ArgumentParser(description="Chat with the LoadWise agent.")
    parser.add_argument("--db", help="SQLite file (default: DB_PATH from .env)")
    parser.add_argument("--as-of", type=date.fromisoformat,
                        help="Pretend today is this date (for demo data), YYYY-MM-DD")
    parser.add_argument("--model", default=settings.llm_model)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        client = anthropic.Anthropic()
    except anthropic.AnthropicError as e:
        sys.exit(f"Could not create the Claude client: {e}\nSet ANTHROPIC_API_KEY in .env.")

    engine = make_engine(args.db)
    init_db(engine)
    agent = Agent(client, args.model,
                  on_tool_call=lambda name, inp: print(f"  -> {name}({json.dumps(inp)})"))
    print(f"LoadWise agent ({args.model}) on {engine.url.database}"
          + (f", today = {args.as_of}" if args.as_of else "") + ". Commands: /reset, /quit")

    with make_session_factory(engine)() as db:
        while True:
            try:
                text = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text == "/quit":
                break
            if text == "/reset":
                agent.reset()
                print("(new conversation)")
                continue

            today = args.as_of or datetime.now(ZoneInfo(settings.timezone)).date()
            try:
                reply = agent.ask(text, ToolContext(db=db, today=today))
            except anthropic.AuthenticationError:
                sys.exit("Authentication failed: check ANTHROPIC_API_KEY in .env.")
            except anthropic.RateLimitError:
                print("Rate limited by the API. Wait a moment and try again.")
                continue
            except anthropic.APIConnectionError:
                print("Could not reach the Claude API. Check your internet connection.")
                continue
            except anthropic.APIStatusError as e:
                print(f"Claude API error ({e.status_code}): {e.message}")
                continue

            print(f"\nloadwise> {reply.text}")

            for action_id in reply.pending_action_ids:
                action = db.get(PendingAction, action_id)
                summary = describe_payload(action.action_type, action.payload)
                if _ask_yes_no(f"\n  Confirm: {summary}? [y/n] "):
                    result = confirm_action(db, action_id)
                    print(f"  Saved ({result}).")
                    agent.add_note(f"User confirmed pending action {action_id}: {summary}")
                else:
                    reject_action(db, action_id)
                    print("  Rejected, nothing saved.")
                    agent.add_note(f"User rejected pending action {action_id}: {summary}")


if __name__ == "__main__":
    main()
