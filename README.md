# lingua_patch

A lightweight Telegram bot for **passive language learning**. Once a day it
sends one short *patch*: a voice clip with its transcript, a translation into
your native language, and a tiny vocabulary breakdown of the words that differ
most from your mother tongue — plus a tap-through [YouGlish](https://youglish.com)
link to hear each word used in real videos.

Audio comes from one of two sources (set `AUDIO_SOURCE`):
* **`elevenlabs`** (default) — a freshly generated 2-4 sentence themed snippet,
  voiced by ElevenLabs AI with a random voice each time. Unlimited content in
  any supported language.
* **`tatoeba`** — real native-speaker recordings from [Tatoeba](https://tatoeba.org)
  (CC-BY), limited to that crowd-sourced pool.

No streaks, no menus, no gamification. Open the notification, listen, read, done.

## How it works

```
OpenAI snippet + ElevenLabs voice  (or Tatoeba native audio)
                                            │
                          OpenAI picks words that differ
                                            ▼
                                       SQLite content_pool
                                            │
                          APScheduler (daily cron)  ──►  Telegram voice note + text
```

* **Content source:** ElevenLabs TTS on OpenAI-generated themed snippets
  (`AUDIO_SOURCE=elevenlabs`), or Tatoeba native-speaker recordings
  (`AUDIO_SOURCE=tatoeba`, CC-BY). Audio is converted to OGG/Opus so Telegram
  shows a real voice note. No YouTube/movie scraping.
* **Vocabulary:** OpenAI (`gpt-4o-mini`) selects the 3-5 words most different
  from your native language and translates them.
* **Language switching:** one command (`/language`) — every part of the
  pipeline is keyed by ISO 639-3 code, so switching is a single parameter.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in BOT_TOKEN, OPENAI_API_KEY (+ ELEVENLABS_API_KEY for TTS)
```

## Seed the content pool

```bash
python generate_content.py --language ukr --count 10
# force a source regardless of AUDIO_SOURCE:
python generate_content.py --language spa --count 5 --source elevenlabs
python generate_content.py --language spa --count 5 --source tatoeba
```

This generates (or downloads) the audio, builds the vocabulary, and inserts rows
into `bot.db`. Re-run any time to top up the pool.

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

## Deploy to Railway

The bot is a long-polling **worker** (no web port). A `Dockerfile` (with ffmpeg)
and `railway.json` are included.

1. **New Project → Deploy from GitHub repo** → pick `lingua_patch`. Railway builds
   the Dockerfile automatically.
2. **Variables:** set `BOT_TOKEN` and `OPENAI_API_KEY`. For the default AI-voice
   source also set `ELEVENLABS_API_KEY` and `AUDIO_SOURCE=elevenlabs` (set
   `AUDIO_SOURCE=tatoeba` to use native recordings instead). Optionally
   `ADMIN_ID`, `TIMEZONE`, `SEND_WINDOW_START_HOUR`, `SEND_WINDOW_END_HOUR`,
   `ELEVENLABS_MODEL`.
3. **Volume:** add a volume mounted at **`/data`** (the image already points
   `DB_PATH=/data/bot.db` and `MEDIA_DIR=/data/media` there) so the DB + audio
   survive redeploys.
4. **Seed the pool** — pick one:
   - Set `SEED_ON_START=ukr` (and optional `SEED_COUNT`, default 10). On first boot
     the bot tops up the pool into the volume. You can remove it after the first
     successful boot.
   - Or run a one-off in the service shell: `python generate_content.py --language ukr --count 10`.

No healthcheck/port is needed — it's a worker, not a web service.

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
| `content.py`          | Tatoeba fetch, OpenAI snippet/vocabulary, audio convert, YouGlish links. |
| `tts.py`              | ElevenLabs text-to-speech (curated random voices). |
| `formatting.py`       | Builds the scannable Telegram message. |
| `generate_content.py` | Standalone seeding script. |
| `main.py`             | Bot handlers + APScheduler daily job. |

## Attribution

With `AUDIO_SOURCE=tatoeba`, audio and sentences come from
[Tatoeba](https://tatoeba.org) contributors under CC-BY and each message links
back to the recording's author. With `AUDIO_SOURCE=elevenlabs`, audio is
AI-generated by [ElevenLabs](https://elevenlabs.io) and the message notes the
voice used.
