# PROJECT BRIEF: LoadWise, a Training Load and Recovery Monitoring Agent

## 0. How to use this file

1. Create an empty folder `loadwise/`, put this file inside as `PROJECT_BRIEF.md`.
2. Open the folder in Claude Code and paste the starter prompt from Section 12.
3. Work one phase at a time. Review, test, and commit after each phase before moving on.

---

## 1. Context

I am Farhang, an MSc Artificial Intelligence student at JKU Linz. I am building this as a portfolio
project that proves I can design, build, test, and ship an AI agent, not just call an LLM.
I will explain every part of it in job interviews, so I need to understand every decision.

I am a former athlete and will use the agent myself with my own training data.

## 2. The product in one sentence

An AI agent that tracks an athlete's training load and recovery, calculates established
sports-science metrics, flags overtraining and injury-risk patterns, answers questions about
the athlete's own data in plain language, and sends proactive alerts through Telegram.

**It is strictly informational monitoring, never medical diagnosis.**

## 3. Scope

### In scope (MVP)
- Log training sessions and daily wellness through a Telegram bot or a CSV import
- Calculate training load metrics with deterministic Python code (never by the LLM)
- An LLM agent (Claude API with tool use) that answers questions by calling tools
- Human in the loop: the agent proposes changes (for example, a lighter session), the user confirms before anything is saved
- A daily scheduled check that sends a Telegram alert when a risk flag appears
- Unit tests for all calculations and an evaluation set for the agent
- Docker setup and a clear README

### Out of scope (do NOT build)
- Wearable integrations (Garmin, Strava), web frontend, user accounts or multi-user auth
- Any diagnosis, treatment advice, or injury prediction claims

## 4. Tech stack

| Part | Choice |
| --- | --- |
| Language | Python 3.11+ |
| LLM | Claude API via the `anthropic` Python SDK, tool use. Model name from env var `LLM_MODEL` |
| Storage | SQLite with SQLAlchemy (simple, file-based) |
| Interface | Telegram bot with `python-telegram-bot` |
| API | FastAPI (health check plus endpoints for logging and metrics) |
| Scheduling | APScheduler inside the app for the daily check |
| Tests | pytest |
| Packaging | Docker and docker-compose |
| Config | `.env` file (never committed), `.env.example` committed |

## 5. Data model

**sessions**: id, date, sport_type, duration_min, rpe (1 to 10), notes, created_at

**wellness**: id, date, sleep_hours, soreness (1 to 10), fatigue (1 to 10), stress (1 to 10), resting_hr (optional), notes

**alerts**: id, date, flag_type, severity, message, sent (bool)

**pending_actions**: id, created_at, action_type, payload (JSON), status (proposed / confirmed / rejected)

## 6. Metrics (implemented in `src/metrics.py`, pure functions, fully unit tested)

- **Session load** = duration_min x RPE (session RPE method, Foster)
- **Daily load** = sum of session loads per day (0 on rest days)
- **Acute load** = sum of daily load over the last 7 days
- **Chronic load** = average weekly load over the last 28 days (sum of 28 days / 4)
- **ACWR** = acute / chronic. Return None if fewer than 28 days of history
- **Monotony** = mean daily load / standard deviation of daily load over 7 days
- **Strain** = weekly load x monotony

### Flag rules (in `src/rules.py`)
| Flag | Condition | Severity |
| --- | --- | --- |
| High load spike | ACWR > 1.5 | high |
| Elevated load | ACWR between 1.3 and 1.5 | medium |
| Undertraining | ACWR < 0.8 | low |
| High monotony | monotony > 2.0 | medium |
| Poor sleep | sleep < 6h on 3 of the last 4 days | medium |
| High soreness | soreness >= 7 for 2 days in a row | medium |

Note for the README: ACWR is a widely used monitoring heuristic, but its scientific validity
is debated. The project presents it as a monitoring signal, not a prediction. Know this for interviews.

## 7. Agent design

### Tools the agent can call (in `src/tools.py`)
- `log_session(date, sport_type, duration_min, rpe, notes)` (requires user confirmation)
- `log_wellness(date, sleep_hours, soreness, fatigue, stress, notes)` (requires user confirmation)
- `get_sessions(start_date, end_date)`
- `get_load_summary(days)` returns daily loads, acute, chronic, ACWR, monotony, strain
- `get_wellness_trend(days)`
- `get_active_flags()`
- `propose_plan_change(date, suggestion, reason)` creates a pending action, never applies it directly

### System prompt rules (in `src/prompts.py`)
- Always call tools to get numbers. Never invent or estimate metrics
- Quote the actual values behind every statement ("your ACWR is 1.62, above the 1.5 threshold")
- Never diagnose, never give medical or treatment advice
- If the user mentions pain, injury, chest symptoms, or dizziness: recommend seeing a doctor or physiotherapist and stop training advice
- Keep answers short and practical
- Any change to stored data goes through confirmation first

### Agent loop (in `src/agent.py`)
Standard tool-use loop: send message plus tool definitions, execute requested tools, return
results, repeat until the model gives a final answer. Cap at 8 tool calls per user message.
Log every tool call and result to a file for debugging and evaluation.

## 8. Repo structure

```
loadwise/
  PROJECT_BRIEF.md
  BUILD_LOG.md
  README.md
  .env.example
  requirements.txt
  Dockerfile
  docker-compose.yml
  data/sample_training.csv      (60 days of realistic synthetic data)
  src/
    config.py
    db.py
    models.py
    metrics.py
    rules.py
    tools.py
    prompts.py
    agent.py
    bot.py
    scheduler.py
    api.py
    import_csv.py
  tests/
    test_metrics.py
    test_rules.py
    test_tools.py
  evals/
    scenarios.json
    run_evals.py
```

## 9. Build phases (stop after each phase for review)

### Phase 1: Setup
Repo structure, requirements, config, `.env.example`, SQLite models, CSV import script, and a
script that generates 60 days of synthetic data with one clear load spike in week 6.
**Done when:** the sample data imports and I can query it.

### Phase 2: Metrics and rules
`metrics.py` and `rules.py` with full pytest coverage, including edge cases (rest days, fewer
than 28 days of data, zero standard deviation).
**Done when:** `pytest` passes and the week 6 spike triggers the high load flag.

### Phase 3: Agent
Tools, system prompt, and the agent loop. A simple command-line chat (`python -m src.agent`) for testing.
**Done when:** asking "How is my training load this week?" makes the agent call tools and answer with real numbers.

### Phase 4: Telegram bot and confirmations
Bot commands: `/log`, `/wellness`, `/status`, plus free-text chat with the agent. Inline
Confirm / Reject buttons for pending actions.
**Done when:** I can log a session from my phone and confirm it with a button.

### Phase 5: Daily check and alerts
APScheduler job at 08:00 Europe/Vienna that runs the rules and sends new flags to Telegram,
without sending the same alert twice.
**Done when:** a flag created in the data produces exactly one Telegram alert.

### Phase 6: Evaluation
`evals/scenarios.json` with about 20 test cases. Each has a setup (data state), a user message,
and checks: which tools must be called, which numbers must appear, and forbidden content
(diagnosis, made-up numbers). Include safety cases (user mentions knee pain).
`run_evals.py` prints a pass rate per category.
**Done when:** the eval runs end to end and I record the pass rate in the README.

### Phase 7: Docker and README
Dockerfile, docker-compose, and a README with: problem, architecture diagram (Mermaid),
metrics explanation, eval results, limitations, and a "How I built this with AI" section
based on BUILD_LOG.md.

## 10. Working rules for Claude Code

- Work on ONE phase at a time. At the end of a phase, summarize what you built and stop.
- Before writing code for a phase, explain your plan in a few bullet points and wait for my OK.
- Explain non-obvious decisions briefly, so I can defend them in interviews.
- Keep the code simple and readable. No unnecessary abstractions.
- Every calculation gets a unit test.
- Never put secrets in code. Use `.env`.
- After each phase, append to BUILD_LOG.md: what was built, key decisions, what you generated,
  and what I changed or decided myself (ask me for that last part).
- Suggest a git commit message at the end of each phase.

## 11. CV bullets when done (fill in real numbers)

- Built an AI agent that monitors athlete training load using Claude tool use, computing ACWR, monotony, and strain with deterministic, unit-tested Python
- Added human-in-the-loop confirmations and safety guardrails, validated with a 20-scenario evaluation set reaching [X]% pass rate
- Delivered proactive Telegram alerts via a scheduled daily check, packaged with FastAPI and Docker

## 12. Starter prompt to paste into Claude Code

```
Read PROJECT_BRIEF.md fully. Then summarize the project back to me in 5 bullet points
and list any questions or risks you see. After I answer, start Phase 1 by explaining your
plan first. Follow the working rules in Section 10 for the whole project.
```

## 13. What I need before starting

- An Anthropic API key (console.anthropic.com), saved in `.env` as `ANTHROPIC_API_KEY`
- A Telegram bot token: message @BotFather on Telegram, run /newbot, save as `TELEGRAM_BOT_TOKEN`
- My Telegram chat ID for alerts, saved as `TELEGRAM_CHAT_ID` (Claude Code can show me how to get it in Phase 4)
