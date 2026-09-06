# Architecture

Living document. Update it in the same PR as any change to schema, data flow,
or external services. Written so the owner can read it without reading Python.

Last synced with code: commit `c5a3153` (PR #36, docs: add AGENTS.md, architecture, workflow spec).

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
| `generate_content.py` | `seed()` — the pool-filling loop; also a CLI | content, tts, db, OpenAI |
| `tts.py` | ElevenLabs TTS, curated native voice pools per language | ElevenLabs |
| `speaking.py` | practice prompts, STT, response parsing, rich-message HTML | OpenAI, ElevenLabs STT, content.py (`THEMES`), memory.py |
| `memory.py` | tutor memory document: fixed sections, size caps, delta application, the update prompt, prompt-injection formatting | OpenAI |
| `formatting.py` | HTML for the daily patch message | content.py, languages.py |
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
      → db.clear_active_exercise, db.record_sent
      → _maybe_expand: if unseen ≤ topup_threshold → background seed()
```

Two scheduling paths coexist:
- **Random window** (default): one job for *all* random-window users, fires once
  a day at a random time in `[SEND_WINDOW_START_HOUR, END)` in the bot's
  `TIMEZONE`; `meta.last_daily_date` guards against double sends.
- **Fixed time** (`/time`): one APScheduler cron job per user, restored on boot.

`_maybe_expand` is not delivery-only — it is called from six sites: `deliver`,
`send_patch_now`, `cmd_start`, `cmd_language`, `on_set_language`, and
`on_set_level`. So `/start`, `/language` and `/level` can each independently
trigger a background pool-expansion batch, not just the scheduled send.

Tapping "Let's practice" on a patch also feeds that patch's vocabulary into
the practice exercise: `_patch_vocabulary(content)` pulls its words and passes
them into `_start_practice(..., vocabulary_hint=...)`, so the generated
sentence reuses one or two words the learner just heard (see §3.3).

### 3.2 Pool expansion (`generate_content.seed`)

```
while inserted < count and attempts < count * 3:
    OpenAI(gpt-4o-mini) → {transcript, translation, vocabulary[3-5], theme}
    ElevenLabs TTS (random native voice) → mp3
    ffmpeg → ogg (32k opus)
    db.insert_content(...)
    # a failed attempt (bad snippet JSON, TTS error) is skipped and retried
    # within the attempts budget, without counting toward `inserted`
```
Runs in a thread; de-duplicated per `(language, difficulty)` so concurrent
triggers don't double-generate. Themes are a hardcoded list of ~30 everyday topics.

### 3.3 Speaking practice

```
tap "Practice" / "Let's practice"
  → main._schedule_memory_update (background; folds the PREVIOUS exercise, if
      any learner turn is in it, into tutor_memory before it is overwritten)
  → speaking.generate_sentence   (native-language sentence, gpt-4o, + tutor_memory) → sent immediately
  → db.set_active_exercise       (wipes previous exercise)
  → speaking.generate_theory     (collapsible "blocks", gpt-4o, + tutor_memory)     → edited into task msg
  → notes stored as a "tutor" turn
learner sends voice / text
  → STT (ElevenLabs scribe) if voice
  → speaking.respond(source_sentence, turns, ..., tutor_memory)  → {verdict, reply, notes?}
      # only the most recent 20 turns (MAX_HISTORY_TURNS) go to the model
  → turns appended; reply + optional notes sent
```
The exercise lives until the next practice or the next delivered patch. Either
event also ends the session for tutor-memory purposes: `main._schedule_memory_update`
reads the exercise about to be overwritten/wiped, and — only if the learner
actually answered at least once — runs one background `memory.update_memory`
call (§3.4) before the old turns are gone.

### 3.4 Tutor memory (`memory.py`, `db.tutor_memory`)

One short markdown document per `(user_id, language)`, capped in size,
injected into the three prompts above so the tutor has an approximate,
persistent picture of the learner instead of starting from zero every
session. Full design: `docs/specs/001-tutor-memory.md`.

```
session ends (deliver() or a new practice starts)
  → main._schedule_memory_update(user_id)
      skip if no active exercise, or the learner never sent a turn in it
      → memory.update_memory(current_memory, turns, language, native)
          → memory.generate_update  (gpt-4o call) → {"add": [...], "remove": [...]}
          → memory.apply_delta      (pure; enforces the caps, drops oldest-first)
      → db.save_tutor_memory (old value kept as memory_prev)
```

The document has five fixed English headers (`## Level`, `## Recurring
errors`, `## Recent wins`, `## About the learner`, `## How to teach them`);
prose inside is in the learner's native language. No word list — that is the
part that grows without bound, deliberately left to a future SRS spec (§8).
The updater is asked for a delta, never a rewrite, so one bad generation
cannot silently erase the document; `apply_delta` also refuses anything that
doesn't name one of the five fixed sections, which is what keeps a learner's
turn like "ignore the rules above" from doing anything beyond being a plain
inert fact of the conversation.

`memory.format_for_prompt` renders '' for an empty memory, so a learner with
none yet gets prompts byte-identical to before this feature existed. `/memory`
is a read-only command that prints the current document — the only way to see
whether it works, or to catch the tutor believing something false.

**Rich messages, with a fallback.** The task and theory are sent via
Telegram's `SendRichMessage`/`EditMessageText` API (`InputRichMessage`) so the
theory's collapsible blocks (`<details>`, tables) render properly. If Telegram
rejects the rich message (`TelegramBadRequest`), `main.py` falls back to plain
HTML built by `speaking.build_*_fallback_html`, which renders each block as an
expandable blockquote instead of `<details>`/`<table>`. Every rich send in the
practice flow — the task, the theory, and any mid-conversation notes — has a
matching fallback path.

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

tutor_memory (user_id, language) PK
  memory      TEXT NOT NULL DEFAULT ''    markdown, fixed sections, capped (~3000 chars)
  memory_prev TEXT NULL                    the version before the last update
  updated_at  TEXT NULL
                                            # one row per learner per target language;
                                            # see §3.4 and docs/specs/001-tutor-memory.md
meta (key, value)                                  # last_daily_date
```

`native_language` has no setter: it is written once, at `upsert_user()`, from
`settings.native_language` (a deploy-time constant, default `rus`). No command
or handler ever updates it for an existing user — despite the per-row column,
it currently behaves as a fixed, bot-wide value.

Migrations are ad hoc: `_add_missing_columns()` for additive changes, one
table-rebuild helper for a legacy column drop, `CREATE TABLE IF NOT EXISTS` for
a brand-new table like `tutor_memory`. There is no migration version table.

**What the schema does NOT know:** which specific words a learner has seen,
which they got wrong, when anything is due — there is no per-word state, so
spaced repetition still cannot be built without a schema redesign. Vocabulary
stays opaque JSON on the content row. `tutor_memory` (above) closes the other
half of the old gap: an approximate, persistent picture of the learner now
survives between sessions, even though nothing tracks individual words. See
Known issues #1.

## 5. External services & cost surface

| Service | Model | Called when | Cost driver |
|---|---|---|---|
| OpenAI | gpt-4o-mini | pool expansion | per patch, ~1 call |
| OpenAI | gpt-4o | practice: sentence, theory, **every learner turn** (with up to 20-turn history) | per tap; unbounded per user |
| OpenAI | gpt-4o | tutor memory update, once per *completed* session (§3.4) | per session that had ≥1 learner turn, not per turn |
| ElevenLabs TTS | multilingual_v2 | pool expansion | per patch, per character |
| ElevenLabs STT | scribe | every voice reply | per second of audio |
| Telegram | — | everything | free |

There is no per-user rate limit or daily quota anywhere. Pool expansion itself
is bounded, not a loop: `_maybe_expand` only fires while a user's unseen count
for a `(language, difficulty)` tier is ≤ `topup_threshold` (5), and
`_expand_pool` holds a per-tier lock, so repeated `/level`/`/language`
toggling does not refire once a tier is stocked. Worst case is one
`topup_count` (10) batch per tier — 16 languages × 4 tiers ≈ 640 items total —
a bounded, one-time cost shared across every user, not an unbounded per-user
spend.

The actual unbounded vector is the practice conversation: every learner turn
(voice or text) costs one `gpt-4o` call carrying up to 20 turns of history,
per user, with no daily cap. A user holding a long conversation, or tapping
"Practice" repeatedly, spends without bound.

Tutor memory adds one more `gpt-4o` call, but bounded the other way: at most
once per *session* (not per turn), skipped entirely for a session with no
learner turn, and the document itself is capped in size (§3.4) so the call's
input never grows with usage the way per-turn history does.

## 6. Deployment & ops

- Railway worker from `Dockerfile` (python:3.12-slim + ffmpeg). No port.
- Volume at `/data`: `bot.db` + `media/`. Audio is never deleted.
- Config via env / `.env` (`config.py`). Secrets: `BOT_TOKEN`, `OPENAI_API_KEY`,
  `ELEVENLABS_API_KEY` (+ optional STT key).
- Observability: admin DM on start/stop; heartbeat URL pinged every 60 s;
  `/stats` admin command. No structured metrics, no error aggregation.
- Testing: `tests/` (pytest) covers `db.py` and the pure/mockable parts of
  `memory.py`, `speaking.py` and `main.py` — no prompt-quality checks, that
  needs the eval harness (§8, still its own spec).
  `.agents/skills/testing-lingua-patch/SKILL.md` describes a manual/mocked
  procedure for everything else. `ruff` passes.

## 7. Known issues (ordered by how much they block the roadmap)

1. **No per-word learner model.** `tutor_memory` (§3.4) now gives the tutor an
   approximate, persistent picture of the learner — level, recurring errors,
   what they care about — but there is still no `word` / `user_word_state` /
   attempts table. "Which of these 40 words is due today" is arithmetic over
   dates that prose cannot answer reliably, so spaced repetition specifically
   still needs a schema redesign before it can be built. Do this before
   lesson/SRS-type features (§8).
2. **Timezone is global.** `TIMEZONE` is one value for the whole bot; `/time`
   stores wall-clock in that zone. Users outside it get patches at the wrong time.
   (`config.py` defaults to Europe/Moscow, `.env.example` says Europe/Kyiv.)
3. **`native_language` is not user-changeable.** The column exists per-row, but
   no setter or handler ever updates it — it's fixed at registration from the
   deploy-time `settings.native_language` and stays that way for the account's
   whole lifetime.
4. **`native_language` is passed inconsistently.** `main.py:436` (`cmd_language`)
   and `main.py:451` (`on_set_language`) pass `settings.native_language` into
   `_maybe_expand` instead of the user's stored `native_language` — every other
   call site uses `user.get("native_language", settings.native_language)`.
   Harmless while the field is frozen (#3 above), but a real bug the moment
   `native_language` becomes user-settable.
5. **No cost limits** — see §5.
6. **Pool expansion is bounded, not unbounded.** `_maybe_expand` only fires
   while a tier's unseen count is ≤ `topup_threshold`, and `_expand_pool` is
   locked per `(language, difficulty)`, so toggling `/level`/`/language` does
   not refire once a tier is stocked. Worst case is one `topup_count` batch per
   tier — 16 languages × 4 tiers ≈ 640 items total — a bounded, one-time,
   shared cost. The real unbounded cost is the practice conversation (#5/§5).
7. **No evals; unit tests now exist but are narrow.** `tests/` (bootstrapped by
   the tutor-memory PR) covers schema, pure logic and prompt-injection points
   with mocked clients — it cannot catch a prompt regression, only a code
   regression. That still needs the eval harness (§8).
8. **Daily-send atomicity.** `send_and_reschedule` writes `last_daily_date`
   *before* the loop; a crash mid-loop marks the day sent for everyone.
9. **`asyncio.create_task` without a reference** in `_maybe_expand` — task can be
   garbage-collected mid-flight. Keep a set of pending tasks.
10. **Hand-rolled FSM** (`awaiting_time` column). Fine for one flag; will not
    scale to lessons. aiogram FSM exists.
11. **Media never cleaned up**; `used_count` is written but never read.
12. **Legacy contrastive vocabulary.** The "words most different from the native
    language" idea in `content.py`'s prompt is a leftover from the ukr-from-rus
    origin. Owner has said it is no longer a product goal.
13. `.env.example` ships a real-looking `ADMIN_ID`. Should be `0`.

## 8. Roadmap (owner intent, not commitments)

Direction: "Duolingo-like" — a daily path with low decision load, spaced
repetition under the hood, AI for content generation and free-form grading,
voice-first. Sequence agreed on 2026-08-19:

1. Eval harness for prompts (`evals/`)
2. Learner model schema (words, exposures, attempts, SRS state)
3. Per-user timezone + cost limits
4. Lesson/SRS delivery on top of (2)
