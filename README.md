# lingua_patch

A lightweight Telegram bot for **passive language learning**. Once a day it
sends one short *patch*: a real **native-speaker audio** clip with its
transcript, a translation into your native language, and a tiny vocabulary
breakdown of the words that differ most from your mother tongue — plus a
tap-through [YouGlish](https://youglish.com) link to hear each word used in real
videos.

No streaks, no menus, no gamification. Open the notification, listen, read, done.

## How it works

```
Tatoeba (native audio + translations)  ──►  OpenAI (pick words that differ)
                                            │
                                            ▼
                                       SQLite content_pool
                                            │
                          APScheduler (daily cron)  ──►  Telegram voice note + text
```

* **Content source:** [Tatoeba](https://tatoeba.org) — crowd-sourced sentences
  with native-speaker recordings, openly licensed (CC-BY). Audio is converted to
  OGG/Opus so Telegram shows a real voice note. No YouTube/movie scraping.
* **Vocabulary:** OpenAI (`gpt-4o-mini`) selects the 3-5 words most different
  from your native language and translates them.
* **Language switching:** one command (`/language`) — every part of the
  pipeline is keyed by ISO 639-3 code, so switching is a single parameter.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in BOT_TOKEN and OPENAI_API_KEY
```

## Seed the content pool

```bash
python generate_content.py --language ukr --count 10
# other languages:
python generate_content.py --language spa --count 5
```

This downloads native audio, builds the vocabulary, and inserts rows into
`bot.db`. Re-run any time to top up the pool.

## Run the bot

```bash
python main.py
```

The bot starts long-polling and an in-process scheduler that fires **once a day
at a random time** (BeReal-style) inside the
`[SEND_WINDOW_START_HOUR, SEND_WINDOW_END_HOUR)` window in `TIMEZONE`. After each
send it picks a fresh random time for the next day; on restart it re-plans the
next send automatically.

> The scheduler only fires while the process is running — host it somewhere
> always-on (a small VPS / container / systemd service) for reliable daily
> delivery.

## Commands

| Command / button        | Who      | What |
|--------------------------|----------|------|
| `/start`                 | everyone | Register, see your current language, and get the 🎧 button. |
| `/patch` or 🎧 button     | everyone | On-demand: "I want some words right now" — sends a patch immediately. |
| `/language`              | everyone | Switch the language you're learning (buttons, or `/language spa`). |

The persistent **🎧 Хочу патч зараз** keyboard button is a one-tap alias for
`/patch`. (`/test_send` is kept as a hidden alias of `/patch`.)

## Configuration

All settings come from environment variables / `.env` — see
[`.env.example`](.env.example).

## Project layout

| File                  | Responsibility |
|-----------------------|----------------|
| `config.py`           | Env-based settings. |
| `languages.py`        | Supported languages + YouGlish slugs. |
| `db.py`               | SQLite schema + CRUD (thread-safe). |
| `content.py`          | Tatoeba fetch, audio download/convert, OpenAI vocabulary, YouGlish links. |
| `formatting.py`       | Builds the scannable Telegram message. |
| `generate_content.py` | Standalone seeding script. |
| `main.py`             | Bot handlers + APScheduler daily job. |

## Attribution

Audio and sentences come from [Tatoeba](https://tatoeba.org) contributors under
CC-BY; each message links back to the recording's author.
