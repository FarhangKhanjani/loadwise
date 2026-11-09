"""Run the eval scenarios against the real LoadWise agent and print pass rates per category.

  python -m evals.run_evals                                   # model from .env, 3 reps
  python -m evals.run_evals --model claude-opus-5-5 --reps 3
  python -m evals.run_evals --case knee_pain --case chest_pain --reps 1

How a run works:
- Each (scenario, rep) gets a fresh in-memory database built from the scenario's dataset,
  and a new Agent with the same prompt, tools and settings the bot uses (src/agent.py).
- The client is wrapped to record tokens, latency and the model that actually answered;
  a response from a different model fails the run loudly instead of being scored.
- A run passes when all its checks pass (evals/checks.py).
- API errors after the SDK's retries are infrastructure problems, not model failures: they
  go to errors.jsonl and are left out of the pass rate.
- Everything is saved under evals/results/<model>_<time>/: results.jsonl (one row per run,
  with the full transcript and every check), errors.jsonl and summary.md.

Costs come from the API's token counts and the price table below, not from estimates.
"""

import argparse
import json
import math
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import anthropic

from evals.checks import Outcome, grade
from evals.datasets import build
from src.agent import Agent
from src.config import get_settings
from src.db import init_db, make_engine, make_session_factory
from src.models import PendingAction
from src.prompts import SYSTEM_PROMPT
from src.tools import ToolContext

SCENARIOS = Path(__file__).resolve().parent / "scenarios.json"
RESULTS = Path(__file__).resolve().parent / "results"

# USD per million tokens: input, output, cache read, cache write (5-minute TTL).
# Haiku's cache rates are not in the cached price table; they assume the standard
# multipliers (read 0.1x input, write 1.25x input).
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}


class ModelMismatch(Exception):
    pass


class RecordingClient:
    """Wraps anthropic.Anthropic: same messages.create, plus a record of every call."""

    def __init__(self, client, model: str):
        self._client, self.model, self.calls = client, model, []
        self.messages = self

    def create(self, **kwargs):
        started = time.perf_counter()
        response = self._client.messages.create(**kwargs)
        if not response.model.startswith(self.model):
            raise ModelMismatch(f"asked for {self.model}, served by {response.model}")
        usage = response.usage
        self.calls.append({
            "model": response.model,
            "stop_reason": response.stop_reason,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
            "ms": round((time.perf_counter() - started) * 1000),
        })
        return response


def cost_usd(model: str, calls: list[dict]) -> float | None:
    if model not in PRICES:
        return None
    p_in, p_out, p_read, p_write = PRICES[model]
    return sum(c["input_tokens"] * p_in + c["output_tokens"] * p_out
               + c["cache_read_input_tokens"] * p_read + c["cache_creation_input_tokens"] * p_write
               for c in calls) / 1_000_000


def _jsonable(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    return obj


def _context_texts(messages: list[dict]) -> list[str]:
    """What the agent saw: user text (with today's date and notes) and every tool result."""
    texts = [SYSTEM_PROMPT]
    for message in messages:
        if message["role"] != "user":
            continue
        for block in message["content"]:
            if block.get("type") == "text":
                texts.append(block["text"])
            elif block.get("type") == "tool_result":
                texts.append(str(block["content"]))
    return texts


def run_case(client, model: str, scenario: dict, rep: int) -> dict:
    """One scenario run. Raises on infrastructure errors (API down, wrong model)."""
    engine = make_engine(":memory:")
    init_db(engine)
    recorder = RecordingClient(client, model)
    agent = Agent(recorder, model, log_path=None)
    try:
        with make_session_factory(engine)() as db:
            build(scenario["dataset"], db)
            ctx = ToolContext(db=db, today=date.fromisoformat(scenario["today"]))
            replies = [agent.ask(text, ctx) for text in scenario["messages"]]
            proposals = [{"action_type": a.action_type, "payload": a.payload}
                         for a in db.query(PendingAction).order_by(PendingAction.id)]
    finally:
        engine.dispose()

    outcome = Outcome(
        reply="\n\n".join(r.text for r in replies),
        tools_called=[c["name"] for r in replies for c in r.tool_calls],
        proposals=proposals,
        stop_reasons=[r.stop_reason for r in replies],
        context=_context_texts(agent.messages),
    )
    checks = grade(scenario, outcome)
    return {
        "case": scenario["id"], "category": scenario["category"], "rep": rep, "model": model,
        "passed": all(c.passed for c in checks),
        "failed": [{"check": c.name, "detail": c.detail} for c in checks if not c.passed],
        "checks": [c.__dict__ for c in checks],
        "reply": outcome.reply,
        "tools_called": outcome.tools_called,
        "proposals": proposals,
        "stop_reasons": outcome.stop_reasons,
        "refused": "refusal" in outcome.stop_reasons,
        "requests": len(recorder.calls),
        "cost_usd": cost_usd(model, recorder.calls),
        "latency_ms": sum(c["ms"] for c in recorder.calls),
        "calls": recorder.calls,
        "transcript": _jsonable(agent.messages),
    }


def regrade(run_dir: Path) -> tuple[str, list[dict], int]:
    """Grade a saved run again with the current graders, without calling the API.
    Grader fixes must not need new (paid, and different) model answers."""
    scenarios = {s["id"]: s for s in json.loads(SCENARIOS.read_text(encoding="utf-8"))["scenarios"]}
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in rows:
        outcome = Outcome(reply=row["reply"], tools_called=row["tools_called"], proposals=row["proposals"],
                          stop_reasons=row["stop_reasons"], context=_context_texts(row["transcript"]))
        checks = grade(scenarios[row["case"]], outcome)
        row["passed"] = all(c.passed for c in checks)
        row["failed"] = [{"check": c.name, "detail": c.detail} for c in checks if not c.passed]
        row["checks"] = [c.__dict__ for c in checks]
    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as f:
        f.writelines(json.dumps(row, default=str) + "\n" for row in rows)
    errors = (run_dir / "errors.jsonl").read_text(encoding="utf-8").splitlines()
    return rows[0]["model"], rows, len(errors)


def wilson_interval(passed: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a pass rate. Unlike "p ± 1.96·sqrt(p(1-p)/n)" it does not
    collapse to ±0 at 100%: 60/60 passes still leaves real uncertainty."""
    if n == 0:
        return 0.0, 1.0
    p = passed / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def _pct(passed: int, total: int) -> str:
    return f"{100 * passed / total:.0f}%" if total else "n/a"


def summarize(model: str, rows: list[dict], errors: list[dict], reps: int) -> str:
    lines = [f"# Eval results: {model}", "",
             f"{len({r['case'] for r in rows})} scenarios × {reps} reps = {len(rows)} scored runs, "
             f"{len(errors)} infrastructure error(s) (not scored).", ""]

    by_category = defaultdict(list)
    for r in rows:
        by_category[r["category"]].append(r)
    lines += ["| Category | Passed | Pass rate |", "|---|---|---|"]
    for category in sorted(by_category):
        group = by_category[category]
        passed = sum(r["passed"] for r in group)
        lines.append(f"| {category} | {passed}/{len(group)} | {_pct(passed, len(group))} |")
    passed, n = sum(r["passed"] for r in rows), len(rows)
    low, high = wilson_interval(passed, n)
    interval = f" (95% CI {100 * low:.0f}–{100 * high:.0f}%)" if n else ""
    lines.append(f"| **overall** | **{passed}/{n}** | **{_pct(passed, n)}**{interval} |")

    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    lines += ["",
              f"- Cost: ${sum(costs):.4f} total, ${sum(costs) / len(costs):.5f} per scenario run" if costs else "- Cost: unknown model price",
              f"- Latency: {sum(r['latency_ms'] for r in rows) / max(n, 1) / 1000:.1f} s per scenario run (API time only)",
              f"- Refusals: {sum(r['refused'] for r in rows)}", ""]

    lines += ["| Scenario | Category | Passed | Failed checks |", "|---|---|---|---|"]
    by_case = defaultdict(list)
    for r in rows:
        by_case[r["case"]].append(r)
    for case, group in by_case.items():
        failed = Counter(f["check"] for r in group for f in r["failed"])
        failed_text = ", ".join(f"{name} ×{count}" for name, count in failed.items()) or "–"
        lines.append(f"| {case} | {group[0]['category']} | {sum(r['passed'] for r in group)}/{len(group)} | {failed_text} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run the LoadWise agent evals.")
    parser.add_argument("--model", default=settings.llm_model)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--category", help="Only this category")
    parser.add_argument("--case", action="append", help="Only this scenario id (repeatable)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--regrade", type=Path, metavar="RUN_DIR",
                        help="Grade a saved run again with the current graders (no API calls)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.regrade:
        model, rows, n_errors = regrade(args.regrade)
        reps = max(r["rep"] for r in rows)
        summary = summarize(model, rows, [{}] * n_errors, reps)
        (args.regrade / "summary.md").write_text(summary, encoding="utf-8")
        print(summary)
        return

    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))["scenarios"]
    scenarios = [s for s in scenarios
                 if (not args.category or s["category"] == args.category)
                 and (not args.case or s["id"] in args.case)]
    if not scenarios:
        sys.exit("No scenarios match.")

    client = anthropic.Anthropic()
    out = RESULTS / f"{args.model}_{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True)
    jobs = [(s, rep) for s in scenarios for rep in range(1, args.reps + 1)]
    print(f"{len(jobs)} runs ({len(scenarios)} scenarios × {args.reps} reps) on {args.model} -> {out}")

    def run(job):
        scenario, rep = job
        try:
            return run_case(client, args.model, scenario, rep), None
        except (anthropic.APIError, ModelMismatch) as e:
            return None, {"case": scenario["id"], "rep": rep, "error": type(e).__name__, "message": str(e)}

    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool, \
            (out / "results.jsonl").open("w", encoding="utf-8") as results_file, \
            (out / "errors.jsonl").open("w", encoding="utf-8") as errors_file:
        for row, error in pool.map(run, jobs):
            if error:
                errors.append(error)
                errors_file.write(json.dumps(error) + "\n")
                print(f"  ERROR {error['case']} #{error['rep']}: {error['error']}")
                continue
            rows.append(row)
            results_file.write(json.dumps(row, default=str) + "\n")
            status = "pass" if row["passed"] else "FAIL " + ", ".join(f["check"] for f in row["failed"])
            print(f"  {row['case']} #{row['rep']}: {status}")

    summary = summarize(args.model, rows, errors, args.reps)
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary)


if __name__ == "__main__":
    main()
