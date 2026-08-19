# Architecture

Living document. Update it in the same PR as any change to schema, data flow,
or external services. Written so the owner can read it without reading Python.

Last synced with code: commit `98c4535` (PR #35, downtime alerts).

## 1. One-paragraph model

A single Python process (`main.py`) long-polls Telegram. It owns an in-process
scheduler that fires daily patch deliveries, a SQLite database on a persistent
volume, and a `media/` folder of pre-generated OGG voice notes. Content is
generated ahead of time into a **pool** (OpenAI text → ElevenLabs audio → ffmpeg
to OGG) and handed out so that no user ever receives the same patch twice.
Speaking practice is generated on demand (OpenAI) and graded turn by turn; the
learner's voice is transcribed with ElevenLabs STT.

## 2. Modules

| File | Owns | Talks to |
|---|---|---|
| `main.py` | Telegram handlers, scheduler, delivery loop, practice flow orchestration | everything below |
| `db.py` | SQLite schema, migrations, all queries | sqlite |
| `content.py` | patch text generation prompt, mp3→ogg, YouGlish links | OpenAI |
| `generate_content.py` | `seed()` — the pool-filling loop; also a CLI | content, tts, db |
| `tts.py` | ElevenLabs TTS, curated native voice pools per language | ElevenLabs |
| `speaking.py` | practice prompts, STT, response parsing, rich-message HTML | OpenAI, ElevenLabs STT |
| `formatting.py` | HTML for the daily patch message | — |
| `languages.py` | supported languages, ISO codes, YouGlish slugs | — |
| `config.py` | env-based `Settings` (pydantic) | — |
| `monitoring.py` | heartbeat pings to an external uptime monitor | httpx |

Rule of thumb: `main.py` decides *when* and *to whom*; the other modules decide
*what*.

## 3. Data flows

### 3.1 Daily patch

```
scheduler fires (random time in window, or per-user cron)
  → deliver(user)
      → db.pick_unsent_content(user, language, difficulty)   # random unseen row
      → send_voice(ogg) + send_message(html, "Let's practice" button)
      → db.record_sent, db.clear_active_exercise
      → _maybe_expand: if unseen ≤ topup_threshold → background seed()
```

Two scheduling paths coexist:
- **Random window** (default): one job for *all* random-window users, fires once
  a day at a random time in `[SEND_WINDOW_START_HOUR, END)` in the bot's
  `TIMEZONE`; `meta.last_daily_date` guards against double sends.
- **Fixed time** (`/time`): one APScheduler cron job per user, restored on boot.

### 3.2 Pool expansion (`generate_content.seed`)

```
for i in range(count):
    OpenAI(gpt-4o-mini) → {transcript, translation, vocabulary[3-5], theme}
    ElevenLabs TTS (random native voice) → mp3
    ffmpeg → ogg (32k opus)
    db.insert_content(...)
```
Runs in a thread; de-duplicated per `(language, difficulty)` so concurrent
triggers don't double-generate. Themes are a hardcoded list of ~30 everyday topics.

### 3.3 Speaking practice

```
tap "Practice" / "Let's practice"
  → speaking.generate_sentence   (native-language sentence, gpt-4o)   → sent immediately
  → db.set_active_exercise       (wipes previous exercise)
  → speaking.generate_theory     (collapsible "blocks", gpt-4o)       → edited into task msg
  → notes stored as a "tutor" turn
learner sends voice / text
  → STT (ElevenLabs scribe) if voice
  → speaking.respond(source_sentence, turns[:20], ...)  → {verdict, reply, notes?}
  → turns appended; reply + optional notes sent
```
The exercise lives until the next practice or the next delivered patch.

## 4. Schema (SQLite, `db.py:init_db`)

```
users
  user_id INTEGER PK           Telegram id
  join_date TEXT               ISO UTC
  is_active INTEGER            0 once the user blocks the bot
  language TEXT                target, ISO 639-3
  native_language TEXT         ISO 639-3 (only rus/eng/ukr have UI labels)
  difficulty TEXT NULL         easy|medium|hard|NULL (NULL = default pool)
  send_time TEXT NULL          'HH:MM' in bot TIMEZONE; NULL = random window
  awaiting_time INTEGER        ad-hoc FSM flag: next text = custom time
  is_paused INTEGER            scheduled sends skipped; manual still works

content_pool
  id, language, native_language, audio_path, transcript, translation,
  vocabulary_json TEXT         JSON list of {word, translation, context}
  source, attribution          'elevenlabs', voice name
  used_count, created_at, difficulty TEXT NULL

sent_history (user_id, content_id, sent_at)      # the "never repeat" ledger
active_exercises (user_id PK, source_sentence, language, native_language,
                  created_at, turns_json)          # ONE per user; overwritten
meta (key, value)                                  # last_daily_date
```

Migrations are ad hoc: `_add_missing_columns()` for additive changes, one
table-rebuild helper for a legacy column drop. There is no migration version
table.

**What the schema does NOT know:** which words a learner has seen, which they
got wrong, when anything is due. Vocabulary is opaque JSON on the content row.
Practice history is overwritten each session. This is the main structural gap —
see Known issues #1.

## 5. External services & cost surface

| Service | Model | Called when | Cost driver |
|---|---|---|---|
| OpenAI | gpt-4o-mini | pool expansion | per patch, ~1 call |
| OpenAI | gpt-4o | practice: sentence, theory, **every learner turn** (with up to 20-turn history) | per tap; unbounded per user |
| ElevenLabs TTS | multilingual_v2 | pool expansion | per patch, per character |
| ElevenLabs STT | scribe | every voice reply | per second of audio |
| Telegram | — | everything | free |

There is no per-user rate limit or daily quota anywhere. A single user tapping
"Practice" repeatedly or holding a long conversation spends without bound.

## 6. Deployment & ops

- Railway worker from `Dockerfile` (python:3.12-slim + ffmpeg). No port.
- Volume at `/data`: `bot.db` + `media/`. Audio is never deleted.
- Config via env / `.env` (`config.py`). Secrets: `BOT_TOKEN`, `OPENAI_API_KEY`,
  `ELEVENLABS_API_KEY` (+ optional STT key).
- Observability: admin DM on start/stop; heartbeat URL pinged every 60 s;
  `/stats` admin command. No structured metrics, no error aggregation.
- Testing: none automated. `.agents/skills/testing-lingua-patch/SKILL.md`
  describes a manual/mocked procedure. `ruff` passes.

## 7. Known issues (ordered by how much they block the roadmap)

1. **No learner model.** No `word` / `user_word_state` / attempts tables →
   spaced repetition and any "what does this person know" logic cannot be built
   without a schema redesign. Do this before adding lesson-type features.
2. **Timezone is global.** `TIMEZONE` is one value for the whole bot; `/time`
   stores wall-clock in that zone. Users outside it get patches at the wrong time.
   (`config.py` defaults to Europe/Moscow, `.env.example` says Europe/Kyiv.)
3. **No cost limits** — see §5.
4. **No automated tests / evals.** Prompt regressions are only caught by using
   the bot.
5. **Daily-send atomicity.** `send_and_reschedule` writes `last_daily_date`
   *before* the loop; a crash mid-loop marks the day sent for everyone.
6. **`asyncio.create_task` without a reference** in `_maybe_expand` — task can be
   garbage-collected mid-flight. Keep a set of pending tasks.
7. **Hand-rolled FSM** (`awaiting_time` column). Fine for one flag; will not scale
   to lessons. aiogram FSM exists.
8. **Media never cleaned up**; `used_count` is written but never read.
9. **Legacy contrastive vocabulary.** The "words most different from the native
   language" idea in `content.py`'s prompt is a leftover from the ukr-from-rus
   origin. Owner has said it is no longer a product goal.
10. `.env.example` ships a real-looking `ADMIN_ID`. Should be `0`.

## 8. Roadmap (owner intent, not commitments)

Direction: "Duolingo-like" — a daily path with low decision load, spaced
repetition under the hood, AI for content generation and free-form grading,
voice-first. Sequence agreed on 2026-08-19:

1. Eval harness for prompts (`evals/`)
2. Learner model schema (words, exposures, attempts, SRS state)
3. Per-user timezone + cost limits
4. Lesson/SRS delivery on top of (2)
