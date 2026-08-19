# AGENTS.md — how to work in this repo

This file is read by every coding agent (Claude Code via `CLAUDE.md` → `@AGENTS.md`,
Devin, Codex, Cursor). Keep it short; put detail in `docs/`.

## What this is

lingua_patch: a Telegram bot for passive language learning. Once a day it sends a
"patch" (AI-voiced snippet + transcript + translation + vocabulary); the learner can
also start a voice-based speaking exercise graded by an LLM.
Python 3.12 · aiogram 3 · APScheduler · SQLite · OpenAI · ElevenLabs · Railway worker.

Read `docs/architecture.md` before touching `db.py` or any prompt.

## The product is prompts + schema, not plumbing

The two things a change is most likely to break silently:

1. **Prompts** in `speaking.py` and `content.py`. Every odd-looking rule in there
   ("never transpose the conjugation table", "never gloss the sentence chunk by
   chunk") is a fix for a real regression. Do not reword, shorten or "clean up"
   a prompt unless the task is about that prompt. If you change one, add an
   eval case that would have caught the regression (see `evals/`).
2. **The SQLite schema** in `db.py`. Every schema change needs (a) an idempotent
   migration in `init_db()` that works on an existing DB, and (b) an update to the
   schema section of `docs/architecture.md` in the same PR.

## Workflow (see docs/specs/000-workflow.md)

- One spec → one PR. Do not widen scope. If the spec turns out to be wrong,
  stop and say so instead of improvising around it.
- Every PR adds or updates a test in `tests/` or an eval case in `evals/`.
  A PR with no test change must justify why in its description.
- Update `docs/architecture.md` when behaviour or schema changes. Do not update
  the README for internal detail — the README is for humans setting the bot up.
- Do not add dependencies without a one-line reason in the PR description.
- Do not commit `.env`, `bot.db`, or anything under `media/`.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # ffmpeg must be on PATH
ruff check .                             # lint — must pass
python -m pytest tests/                  # once tests exist
python generate_content.py --language ukr --count 3   # needs OPENAI + ELEVENLABS keys
python main.py                           # long-polling bot; needs BOT_TOKEN
```

Only one long-polling instance may run per bot token — do not start `main.py`
against the production token from a dev environment.

## Conventions

- Language codes are ISO 639-3 everywhere (`ukr`, `spa`, `por_pt`); the single
  source of truth is `languages.py`.
- All SQL is parameterised. Never format user input into a query.
- User-facing strings are Russian by default; `speaking.py` has per-native-language
  labels in `_LABELS`. Do not hardcode a language elsewhere.
- Background work goes through `asyncio.to_thread` (OpenAI/ElevenLabs SDKs are sync).
- Log with the module logger; never log secrets or full user voice transcripts
  at INFO.

## Known debt (do not "fix" in passing — each is its own spec)

Tracked in `docs/architecture.md § Known issues`. Highest priority:
no learner model in the schema (blocks spaced repetition), global timezone,
no per-user cost limits, no automated tests.
