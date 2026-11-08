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
