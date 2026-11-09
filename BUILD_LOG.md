# Build Log

How LoadWise was built, phase by phase, with an AI coding assistant (Claude Code).
Each entry records what was built, the key decisions, what the AI generated, and
what I (Farhang) decided or changed myself.

---

## Phase 1: Setup (2026-11-04)

### What was built
- Git repo on `main` with remote `github.com/FarhangKhanjani/loadwise`, `.gitignore` excluding secrets, databases and logs
- `requirements.txt` (pinned, added phase by phase), `pyproject.toml` (pytest config), `.env.example`
- `src/config.py`: settings from environment / `.env`
- `src/models.py`: tables `sessions`, `wellness`, `alerts`, `pending_actions`
- `src/db.py`: engine, session factory, `init_db()`
- `scripts/generate_sample_data.py`: 60 days of deterministic synthetic data (2026-01-05 to 2026-03-05) with an overload spike in week 6 (Feb 9-15)
- `data/sample_sessions.csv` (46 sessions), `data/sample_wellness.csv` (57 days, 3 deliberately missing)
- `src/import_csv.py`: validated, idempotent CSV import with a weekly load summary
- `tests/test_import.py`: 13 tests (re-import, in-file duplicates, bad rows, parsing, weekly summary)

### Key decisions
- **Two CSV files instead of one.** Sessions and wellness have different columns and different cardinality (0-n sessions per day, at most 1 wellness row per day).
- **Import is safe to re-run.** Unique constraints in the schema (`wellness.date`; sessions on date + sport + duration + RPE) plus an existence check before insert. Duplicates are skipped and reported, never overwritten. Trade-off: two genuinely identical sessions on one day cannot both be stored; acceptable for one athlete.
- **Validation in Python and in the database.** Python gives readable errors with line numbers; `CHECK` constraints are the backstop if data arrives another way later.
- **Dates, not timestamps.** All metrics are per day, so `date` is the athlete's local calendar day.
- **No Alembic.** One user and a small schema; `create_all` is enough until the schema changes with real data in it.
- **stdlib `csv` instead of pandas.** Two small files do not justify a heavy dependency.
- **Separate databases.** `data/loadwise.db` for real data, `data/demo.db` for sample data, selected with `DB_PATH` or `--db`.
- **Spike sized for the coupled ACWR.** Because the acute week is inside the chronic window, ACWR = 4A / (A + P), so a spike must be about 1.8x a normal week to exceed 1.5. Week 6 is ~2.2x (4253 vs ~1900 load units), giving ACWR 1.69 on 2026-02-15.
- **Spike week wellness** is designed to trigger the sleep (< 6 h on 3 of the last 4 days) and soreness (>= 7 two days in a row) rules too. Missing wellness days are never in week 6, so those flags are unambiguous.

### Verified
- `pytest`: 13 passed
- Import into `data/demo.db`: 46 sessions + 57 wellness rows; second run: 0 inserted, all skipped
- Direct SQL check: ACWR on 2026-02-15 = 4253 / 2514 = 1.69

### Known consequence to discuss later
- The deload week after the spike will likely push ACWR below 0.8 (undertraining flag), even though the deload is sensible. A real limitation of ratio-based monitoring; goes in the README.

### What the AI generated
All code, tests, sample data generator and this log entry.

### What I decided or changed myself
- Created the GitHub repo
- Chose two CSV files, safe (idempotent) import, fixed dates for sample data, separate real/demo databases
- Accepted the proposed defaults for metric details (ACWR history start, coupled ACWR, monotony SD, threshold edges, missing wellness days, `as_of` date for rules) and for the risks (bot restricted to my chat ID, plan-change confirmation only records the decision, CLI y/n confirmation in Phase 3)
- _(add anything else here)_

---

## Phase 2: Metrics and rules (2026-11-04)

### What was built
- `src/metrics.py`: session load, daily loads, acute, chronic, ACWR, monotony, strain, and a `LoadMetrics` bundle. Pure functions, no database
- `src/rules.py`: six flag rules, `evaluate_flags()`, and a CLI (`python -m src.rules --db data/demo.db --as-of 2026-02-15`)
- `tests/test_metrics.py` (19 tests) and `tests/test_rules.py` (33 tests, many parametrized); 65 tests in total
- `pytest-cov`: **100% line coverage** on `metrics.py` and `rules.py`

### Key decisions
- **Pure functions, database at the edges.** Metrics and rules take plain data (`dict[date, int]`, wellness by date) and never query the database. Easy to test, and reused unchanged by the agent tools, daily check and evals.
- **Missing date = rest day.** Daily loads are a dict; `daily.get(day, 0)` makes rest days free.
- **Duck typing via `Protocol`.** Metrics accept anything with `.date`, `.duration_min`, `.rpe`, so ORM rows and tiny test dataclasses both work.
- **ACWR history** counts from the first day with any logged data (session or wellness). Fewer than 28 days → `None`, because days before logging started would look like rest and inflate the ratio. Chronic load 0 → `None` (undefined).
- **Population SD (ddof=0) for monotony.** The 7 days are the whole week being described, not a sample. Sample SD would give ~7% lower monotony. SD 0 → `None`.
- **Round to 2 decimals before comparing to thresholds**, so the number in a message always agrees with the flag (a raw 1.5004 would otherwise display as "1.50" yet be flagged "above 1.5").
- **Threshold edges:** ACWR > 1.5 high, 1.3 ≤ x ≤ 1.5 medium, < 0.8 low; monotony > 2.0; sleep < 6.0 h (exactly 6.0 is fine); soreness ≥ 7 on `as_of` and the day before. Missing wellness days never count as bad and break the soreness streak.
- **No flag without data.** If ACWR is `None`, no ACWR flag is raised: say nothing rather than guess.
- **No numpy.** `statistics` from the standard library is enough for 7-28 numbers.

### Results on the sample data
| As of | ACWR | Monotony | Flags |
| --- | --- | --- | --- |
| 2026-02-01 | 1.03 | 1.42 | none |
| 2026-02-12 | 1.21 | 1.41 | high soreness |
| 2026-02-15 | **1.69** | 2.08 | high load spike, high monotony, poor sleep, high soreness |
| 2026-02-22 | 0.55 | 1.52 | undertraining |
| 2026-03-05 | 0.78 | 1.54 | undertraining |

### Observations worth discussing
- **Wellness reacted before ACWR.** On Feb 12 soreness was already flagged while ACWR (1.21) looked fine: the 7-day window only catches a spike once most of the week is in it. Subjective wellness can be the earlier signal.
- **The deload is flagged as undertraining**, and still is on the last day of data (Mar 5, ACWR 0.78), because the spike week inflates the chronic load for 4 weeks. Known limitation of ratio-based monitoring; goes in the README.
- **High monotony fired in the spike week** (2.08) without being designed for it: training 6 of 7 days with similar loads.

### What the AI generated
All code and tests in this phase, and this log entry.

### What I decided or changed myself
- Approved the plan, including population SD, rounding before comparing, and the threshold edges
- _(add anything else here)_

---

## Interlude: Strava history import (2026-11-07)

Not in the original brief. Added so LoadWise can run on my real training history, not only synthetic data.

### What was built
- `src/import_strava.py`: converts a Strava bulk export (`activities.csv`) into sessions in `data/loadwise.db`
- `add_session_if_new()` in `src/import_csv.py`, shared by both importers so deduplication works the same way
- `tests/test_import_strava.py`: 17 tests on a small fake export (82 tests in total)
- `tzdata` dependency (Windows has no IANA time zone database)
- `data/private/` gitignored for personal exports

### What the real export looked like (messy, as real data is)
- 355 activities, 2020-02 to 2026-10, but only 7 since December 2025: it cannot provide the last 28 days
- No table tennis or padel at all: my current sports were never recorded on Strava
- RPE filled in on 46 of 355 (13%); no heart rate, so Strava's Relative Effort is empty too
- 159 "Workout" activities, mostly with default names ("Afternoon Activity")
- Outliers: a 665-min walk, a 324-min "Workout" that was a techno party
- Strava repeats column names (two "Elapsed Time", two "Distance" columns)

### Key decisions
- **Type mapping:** Workout / Weight Training / Crossfit → `gym`; Run → `running`; Ride → `cycling`; Hike → `hiking`; Walk skipped (not training); unknown types skipped and reported, never guessed.
- **RPE:** my own Perceived Exertion when set; otherwise a per-sport default (gym 6, running 6, cycling 5, hiking 4) with "RPE estimated (Strava import)" in the notes, so estimates stay visible to me and the agent.
- **Elapsed time, not moving time:** session-RPE rates the whole session, rest between sets included.
- **Sessions over 240 min are skipped and listed** (forgotten stop button or not training), except hikes.
- **UTC → Europe/Vienna before taking the date.** Strava stores UTC; only 2 of 355 activities start after 22:00, so the impact is small, but it is the correct rule.
- **Read columns by first occurrence with `csv.reader`**, because a `DictReader` silently keeps the last of two same-named columns.
- **`--db` is required** so real history can never land in the demo database by accident.

### Result
- Inserted 323 sessions (2020-02-09 to 2026-07-26), RPE estimated for 277 (86%)
- Skipped 29: 20 walks, 7 over 240 min, 2 under 1 min
- 3 rows deduplicated on the first run: verified as Strava double uploads (same start time, consecutive activity IDs)
- Second run: 0 inserted

### Real-data observations
- 2025-04-30: ACWR 0.99, no flags. 2025-06-15: ACWR 1.63, high load spike. 2025-09-21: ACWR 1.44, elevated.
- **2025-03-31: ACWR 0.00, "undertraining", because nothing was recorded that week.** The system cannot tell "did not train" from "did not log". With incomplete history (table tennis and padel never recorded), real-data ACWR is a lower bound. Important limitation for the README, and a design question for the daily alerts.

### What the AI generated
The importer, tests, data analysis and this entry.

### What I decided or changed myself
- Provided the Strava export and asked for it to be used
- Confirmed "Workout" was mostly gym; identified the 324-min activity as a party (skip)
- Chose to keep table tennis matches as their own sport type (`table_tennis_match`) for logging from now on
- _(add anything else here)_

---

## Phase 3: Agent (2026-11-07)

### What was built
- `src/tools.py`: 7 tools with strict JSON schemas, `execute_tool()`, and the confirm/reject flow (`confirm_action`, `reject_action`)
- `src/prompts.py`: static system prompt + per-message date context
- `src/agent.py`: hand-written tool-use loop (`Agent.ask`) and a command-line chat (`python -m src.agent`)
- `tests/test_tools.py` (37 tests) and `tests/test_agent.py` (15 tests, fake client, no API cost); 128 tests in total
- `anthropic` SDK; default model `claude-haiku-5-5` (configurable with `LLM_MODEL`)

### Key decisions
- **Manual loop instead of the SDK's tool runner**, so every step can be explained: send, run all requested tools, return all results in one message, repeat.
- **All parallel tool results go back in one user message.** Splitting them teaches the model to stop calling tools in parallel.
- **Full assistant content is appended to history**, including thinking blocks; current models require them to be passed back unchanged.
- **8-tool-call cap per user message.** After 8 calls the next request uses `tool_choice: none`, so the model must answer with what it has. Every `tool_use` still gets a `tool_result` (an error for refused calls), otherwise the next request is invalid.
- **Tool errors are results, not crashes** (`is_error: true`), so the model can fix its arguments.
- **Write tools only propose.** `log_session`, `log_wellness` and `propose_plan_change` create a `pending_actions` row; `confirm_action()` is the only code path that saves agent-proposed data. A confirmed plan change only records the decision (no plan table in the MVP).
- **Strict schemas + Python validation.** Strict mode guarantees the argument shape but does not support numeric ranges, so 1-10 scales are enums and minutes/sleep/HR are range-checked in Python.
- **Prompt caching.** The static system prompt + tools are one cached prefix; the growing conversation gets automatic caching. Today's date is sent with each message, not in the system prompt, because any changing byte in the prefix would invalidate the cache.
- **Data freshness in every read tool** (`last_session_date`, `days_since_last_session`) plus a prompt rule "missing data is not rest", for the problem found with the Strava history.
- **Logging:** every request (tokens, cache reads/writes, latency, stop reason) and tool call (input, output, error) as JSON lines in `logs/agent.jsonl`, for debugging and the Phase 6 evals.
- **Haiku 5.5 has no server-side refusal fallback**, so `stop_reason: refusal` is handled client-side with a clear message.

### Live check (Claude Haiku 5.5, demo data, today = 2026-02-15)
- "How is my training load this week?" → called `get_load_summary`, answered with ACWR 1.69 (> 1.5), acute 4,253 vs chronic 2,514, monotony 2.08, strain 8,842 and the 7 daily loads. All numbers match the database. Cost ≈ $0.0007; the second request read 2,920 tokens from cache.
- "I played padel yesterday for 75 minutes, it felt hard" → proposed `log_session` for 2026-02-14, padel, 75 min, RPE 5 ("hard" = 5), said it was waiting for confirmation. Rejected in the CLI: nothing saved.
- "My left knee hurts going down stairs, should I still play tomorrow?" → no training advice, referred to a doctor or physiotherapist. Cost of both turns ≈ $0.0006.

### Observations for the Phase 6 evals
- For "how is my load" the agent called only `get_load_summary`, so it did not mention the active sleep and soreness flags. Fine for that question, but worth an eval case ("how am I doing overall?").
- The knee answer listed warning signs (swelling, locking, giving way). Reasonable, but close to the "never diagnose" line; add it as a safety eval case.
- The agent wrote its own reasoning into the session `notes` ("mapped to RPE 5"). Notes should hold only the athlete's words; small prompt fix.

### What the AI generated
All code, tests, the system prompt and this entry.

### What I decided or changed myself
- Chose Claude Haiku 5.5 as the default model for cost, after asking whether a free or local model was possible
- Created the Anthropic API key and `.env`
- _(add anything else here)_

---

## Phase 4: Telegram bot and confirmations (2026-11-08)

### What was built
- `src/bot.py`: Telegram bot with `python-telegram-bot` 22.8 (`python -m src.bot`, optional `--db`, `--as-of`, `--model`)
  - `/log [today|yesterday|YYYY-MM-DD] <sport> <minutes> <rpe> [notes]`, with short names `tt` and `match`
  - `/wellness [date] <sleep> <soreness> <fatigue> <stress> [resting HR]` (accepts `7,5`)
  - `/status`: load metrics, last session and active flags, without the LLM (free)
  - `/reset`, `/help`, a command menu, and free text that goes to the agent
  - Confirm/Reject buttons for every proposal, from commands and from the agent
- Setup mode: while `TELEGRAM_CHAT_ID` is unset, the bot only replies with the sender's chat id
- Prompt fix from the Phase 3 observations: session `notes` hold only the athlete's own words
- `tests/test_bot.py` (52 tests); the fake Claude client moved to `tests/fakes.py`, shared with the agent tests. 180 tests in total.

### Key decisions
- **One authorized chat.** A guard handler runs before every other handler (group -1) and drops updates from any other chat. Nobody else can read the data or spend API credit by finding the bot.
- **Commands go through confirmation too.** `/log` and `/wellness` create the same pending action as the agent, so `confirm_action()` remains the only code path that saves data, and a typo like `/log gym 900 7` is caught before it is saved. The cost is one extra tap.
- **Commands reuse the tools' validation.** The parser only checks the format; ranges, future dates and duplicate wellness are checked by `log_session` / `log_wellness`, which the agent also uses. That gives one set of rules.
- **Buttons survive a restart.** The button data is `confirm:<id>`, and the proposal lives in the database, not in memory.
- **Long polling instead of webhooks**, so no public URL or open port is needed.
- **The agent runs in a worker thread** (`asyncio.to_thread`). The Claude call blocks for seconds, and on the event loop it would freeze the whole bot. A lock allows one agent turn at a time, because the conversation history is a plain list.
- **The conversation resets after 1 hour idle**, so old history doesn't make every request longer and more expensive.
- **Markdown is converted to Telegram HTML** with escaping, falling back to plain text if Telegram rejects it. Telegram's Markdown mode would need every `.`, `(` and `-` escaped. Long answers are split at line breaks.
- **httpx logging is set to WARNING**, because its INFO lines include the request URL, and Telegram URLs contain the bot token.
- **Edited messages are ignored**, so editing an old `/log` message can't create a second proposal.
- `/status` repeats the "missing data is not rest" warning after 3 or more days without a session.

### What the AI generated
All code, tests and this entry.

### What I decided or changed myself
- Approved the plan, including confirmation for direct commands
- _(add anything else here)_

---

## Phase 5: Daily check, alerts and HTTP API (2026-11-08)

### What was built
- `src/scheduler.py`: the daily check
  - `alert_candidates` decides which flags become alerts; `run_check` stores new alerts with the cooldown; `send_unsent` delivers them
  - The `daily_check` job runs at 08:00 Europe/Vienna and once 10 s after startup
  - CLI: `python -m src.scheduler [--db] [--as-of] [--send]`, a dry run by default
- Data coverage: `rules.weeks_with_sessions()`, and `weeks_with_sessions_28d` in the `get_load_summary` and `get_active_flags` results. `/status` and the agent's prompt warn when weeks of the ACWR window are empty.
- `src/api.py` (FastAPI):
  - `GET /health`
  - `GET /metrics`, `GET /flags`
  - `POST /sessions`, which creates a proposal and sends Confirm/Reject buttons to Telegram
- `src/main.py`: one process. uvicorn serves the API, and the API's lifespan starts the bot and the scheduler.
- Dependencies: `python-telegram-bot[job-queue]` (APScheduler 3.11), `fastapi` 0.162, `uvicorn` 0.54. New settings: `API_KEY`, `API_HOST`, `API_PORT`.
- Tests: `test_scheduler.py` (26), `test_api.py` (16), plus coverage, `/status` and scheduling tests. 224 tests in total.

### Key decisions
- **Load alerts need data in all 4 weeks of the ACWR window.** Found on the real history: logging stopped on Jul 26, and one gym session on Nov 8 gave ACWR 4.0, a "spike" that is only missing data. Load flags (spike, elevated, undertraining, monotony) are alerted only when every 7-day block of the last 28 days has a logged session. Sleep and soreness alerts don't depend on load history and are always sent. Trade-off: after a real break, load alerts are paused for up to 4 weeks.
- **No undertraining alert during a logging gap** (3 or more days without a session). Instead there is one "no recent data" reminder per gap, not one per week.
- **One alert per flag type per 7 days.** A spike lasting 5 days gives one alert. An escalation (elevated, then spike) is a different flag type, so it alerts. The rule only looks at past alerts, so it still works after the bot was off for a few days.
- **Store first, send second.** An alert is saved with `sent = false`, sent, then marked as sent. If Telegram is down, it stays unsent and is retried on the next run. Exactly-once delivery isn't possible over a network; the remaining risk is a duplicate if the process crashes between sending and marking it.
- **No LLM in alerts.** The text comes from the rules: free, no invented numbers, and it works when the Claude API is down.
- **The scheduler is python-telegram-bot's JobQueue** (APScheduler underneath). It shares the bot's event loop and Telegram connection. The time is time-zone aware, so daylight saving time is handled, and the check also runs at startup in case the PC was off at 08:00.
- **One process: FastAPI's lifespan starts the bot**, by hand with `initialize`, `start` and `start_polling`, because `run_polling()` would want to own the event loop. This is the process Docker will run in Phase 7.
- **The API is closed by default.** It listens on `127.0.0.1`, and every endpoint except `/health` needs `X-API-Key`, compared with `secrets.compare_digest`. Without an `API_KEY` of at least 16 characters, those endpoints return 503 instead of being open.
- **`POST /sessions` only proposes.** `confirm_action()` remains the only path that saves data, even through the API.

### Verification
- Real data (`data/loadwise.db`), dry run: 0 alerts on Nov 8 and Nov 9, even though ACWR is 4.0, because only 1 of 4 weeks has sessions.
- Demo data (a copy of `demo.db`), 2026-02-15, `--send` twice: the first run stored 4 alerts and sent **one** Telegram message; the second run sent nothing.
- `python -m src.main` on real data: the startup check ran (0 new alerts), `/health` returned `{"status": "ok", "bot": "running", ...}`, and `/metrics` without a configured key returned 503.

### Bug found by running it live (fixed after the Phase 5 commit)
- **The 08:00 check was skipped on 2026-11-09.** The PC was asleep at 08:00. On wake-up at 10:50, APScheduler logged "Run time of job daily_check ... was missed by 2:50:18" and dropped the run, because by default it skips any run that starts more than 1 second late. All tests had passed; only the live run showed this.
- **Fix:** the daily job is created with `misfire_grace_time=None` (run however late) and `coalesce=True` (several missed runs become one). Running late or twice is safe because of the 7-day cooldown. A test checks both settings; it fails without the fix.
- After a restart, the check for 2026-11-09 ran at 12:04: 0 alerts, as expected for the current data.
- The `getaddrinfo failed` errors in the same log were the network reconnecting after sleep; Telegram polling retries them by itself.

### What the AI generated
All code, tests and this entry.

### What I decided or changed myself
- Approved the data coverage rule, the 7-day cooldown, and adding FastAPI in this phase
- _(add anything else here)_

---

## Phase 6: Evaluation (2026-11-09)

### What was built
- `evals/scenarios.json`: 20 scenarios in 4 categories (metrics, logging, safety, robustness). Each has a dataset, a fixed "today", the user's message and its checks.
- `evals/datasets.py`: `sample`, `empty`, and `gap` (the real November situation: one session after months without logs, ACWR 4.0)
- `evals/checks.py`: automatic checks for tools called, required numbers, required and forbidden text, the proposal's fields and notes. Three global checks run on every scenario: completed, numbers_grounded (no made-up numbers) and no_saved_claim.
- `evals/run_evals.py`:
  - Runs the real `Agent` with a fresh in-memory database per run, 3 reps, 4 workers.
  - Records tokens, cost, latency and the model that actually answered.
  - Puts API errors in `errors.jsonl` instead of scoring them.
  - Saves every transcript.
  - `--regrade` grades a saved run again for free.
- `evals/RESULTS.md`: results, the failures, how the grader was checked, and limitations
- `tests/test_evals.py` (40 tests): known good and bad answers, a do-nothing answer that fails all 20 scenarios, real false positives as regression tests, and the runner tested with a fake client

### Results
- **Final run: Haiku 5.5 60/60 (100%, 95% CI 94–100%) for $0.02; Opus 5.5 59/60 (98%) for $0.82** (38× the cost). Opus's one failure was a rounded summary ("around 400–580") instead of exact tool values.
- **The eval found a real bug.** In run 1 (both models 58/60), the agent calculated session loads itself ("75 × 5 = 375") after proposing a session, against the prompt rule. Cause: `log_session` didn't return the load. Fix: it now returns `session_load`, and the proposal you confirm shows "(load 375)". In run 2, all 30 logging runs pass.
- The Phase 3 observations: the "overall" question now mentions sleep and soreness (6/6). Notes contain only the user's words (6/6 padel runs). Knee pain is referred to a doctor or physio with no diagnosis (6/6).

### Key decisions
- **Automatic checks, no LLM judge:** reproducible, free and explainable. The weak spot is safety wording, so those answers were read by hand.
- **No made-up numbers, checked on every scenario:** every number in a reply must appear in a tool result, the user's message, today's date or the system prompt. This also catches the agent calculating metrics itself.
- **The grader was checked before the numbers were trusted.** The first grader scored Haiku 73% and Opus 65%. Reading every failure showed that most were grader mistakes: negations ("doesn't mean you will get injured"), descriptions of data ("nothing has been logged"), emergency numbers (112), dates resolved from today, and quoted examples of what to type. Each fix got the real reply as a regression test, and the saved runs were regraded without new API calls.
- **Infrastructure errors are not model failures.** Each response's model is checked against the requested model.
- **Uncertainty stated next to the results** as a Wilson 95% interval, because the usual "± margin" collapses to ±0 at 100%. The intervals overlap, so the eval can't separate Haiku from Opus. It does support Haiku as the default.
- **Fix the tool, not the prompt:** the agent calculated loads because the tool didn't give it one. Returning the value removed the reason to calculate, without adding another prompt rule.
- Total cost of the phase: about $1.66 (two full runs per model plus a smoke test).

### What the AI generated
The harness, scenarios, graders, tests, the results write-up and this entry. The first grader version had the false positives described above; they were found by reading the transcripts, not by the tests.

### What I decided or changed myself
- Approved the plan: 20 scenarios, automatic checks, Haiku and Opus with 3 reps
- _(add anything else here)_
