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
