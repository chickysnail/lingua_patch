# 001 — Learner model: schema and recording

Status: draft

## Why

The bot has no memory of the learner. `content_pool.vocabulary_json` is opaque
JSON that no code ever reads back, and `active_exercises` is wiped on every new
practice or delivered patch. So the bot cannot answer "which words has this
person met", "which ones do they get wrong", or "what is due" — which is the
whole premise of the product direction in `docs/architecture.md § 8` (a daily
path with spaced repetition under the hood). This is Known issue #1 and it
blocks every lesson-type feature.

This spec adds the tables and starts filling them. Nothing reads them yet.
Reading them (SRS scheduling, a progress view) is a later spec, deliberately:
recording is safe to ship on its own, and once the data is accumulating, the
next spec has something real to work against.

## What changes

### Schema (`db.py:init_db`)

Four new tables, created idempotently alongside the existing ones.

```
words                                  # the lexicon, deduped across patches
  id              INTEGER PK
  language        TEXT NOT NULL        # ISO 639-3, target language
  lemma           TEXT NOT NULL        # dictionary form, as the generator gave it
  native_language TEXT NOT NULL        # ISO 639-3, whose gloss this is
  gloss           TEXT                 # translation into native_language
  created_at      TEXT NOT NULL
  UNIQUE (language, lemma, native_language)

word_exposures                         # every time a word was SHOWN to a user
  id          INTEGER PK
  user_id     INTEGER NOT NULL REFERENCES users(user_id)
  word_id     INTEGER NOT NULL REFERENCES words(id)
  source      TEXT NOT NULL            # 'patch' | 'practice_hint'
  source_id   INTEGER                  # content_pool.id for 'patch', else NULL
  created_at  TEXT NOT NULL

attempts                               # one row per graded learner turn
  id                  INTEGER PK
  user_id             INTEGER NOT NULL REFERENCES users(user_id)
  exercise_started_at TEXT NOT NULL    # active_exercises.created_at — groups turns
  language            TEXT NOT NULL
  native_language     TEXT NOT NULL
  source_sentence     TEXT NOT NULL
  kind                TEXT NOT NULL    # 'voice' | 'text'
  verdict             TEXT NOT NULL    # correct | almost | incorrect | none
  learner_text        TEXT             # what they said/typed (see Decisions)
  created_at          TEXT NOT NULL

attempt_words                          # which tracked words the exercise used
  attempt_id INTEGER NOT NULL REFERENCES attempts(id)
  word_id    INTEGER NOT NULL REFERENCES words(id)
  PRIMARY KEY (attempt_id, word_id)
```

Indexes: `word_exposures(user_id, word_id)`, `attempts(user_id, created_at)`.

### Recording points (`main.py`)

Three places, all additive — no existing call is reordered or removed.

1. **`deliver()`**, right after `db.record_sent(...)`: parse the patch's
   vocabulary with `formatting.vocab_list(content)`, upsert each item into
   `words` (keyed on `(language, lemma, native_language)`), and insert one
   `word_exposures` row per word with `source='patch'`,
   `source_id=content['id']`.
2. **`_start_practice()`**, when `vocabulary_hint` is present (i.e. the exercise
   was started from a patch's "Let's practice" button): insert
   `word_exposures` rows with `source='practice_hint'`, `source_id=NULL`, for
   the words that seeded the exercise.
3. **`_reply_in_session()`**, after `speaking.respond` returns and the turns are
   appended: insert one `attempts` row carrying the returned `verdict`, the
   turn `kind`, and the learner's text; link it via `attempt_words` to the words
   that seeded this exercise.

All writes go through new functions in `db.py` and must not break delivery or
practice if they fail — wrap each in its own try/except and log a warning. A
learner losing their patch because a bookkeeping insert failed is strictly worse
than a gap in the data.

### Grouping turns into one exercise

`attempts.exercise_started_at` is a copy of `active_exercises.created_at`, which
`db.get_active_exercise()` already returns. That is enough to group the turns of
one practice session without touching the `active_exercises` table.

### Backfill

Historical `sent_history` rows carry real exposures that are cheap to recover:
for each row, parse the linked content's `vocabulary_json` and insert the
corresponding `words` + `word_exposures`. Run this once, guarded by a
`meta` key (`learner_model_backfilled`), so `init_db()` stays idempotent.
Practice attempts cannot be backfilled — that history was overwritten.

## What does NOT change

- **Nothing the learner sees.** No new command, no change to the patch message,
  the practice flow, or any prompt.
- **No SRS scheduling.** No `due_at`, `interval` or `ease` columns yet: those
  belong with the algorithm that defines their semantics (spec 002). Adding
  them here would mean inventing a scheduler nobody calls.
- **No `user_word_state` aggregate table.** Counters like "times seen / times
  correct" are derivable from `word_exposures` + `attempts` at this data volume;
  a cached aggregate is an optimisation for later, not schema truth.
- **No word matching in free-form learner speech.** `attempt_words` links only
  the words that *seeded* the exercise, which we know exactly. Detecting which
  words a learner actually used in their sentence needs lemmatisation and is its
  own spec.
- **No change to `content_pool.vocabulary_json`.** It stays as the generator's
  raw output; `words` is derived from it, not a replacement.
- **`native_language` stays frozen bot-wide** (Known issue #3). The schema is
  keyed so that making it per-user later does not require a rewrite.

## Edge cases

- **Existing DB.** All four tables are `CREATE TABLE IF NOT EXISTS`; the
  backfill is guarded by its `meta` flag. Must be verified against a DB created
  before this change, not just a fresh one.
- **Empty or malformed vocabulary.** `vocabulary_json` may be `[]`, or items may
  have a blank `word`. Skip them; no word row, no exposure, no error.
- **Same patch to two users.** The second delivery reuses the existing `words`
  row and inserts only a new exposure. The `UNIQUE` constraint is what enforces
  this — use `INSERT ... ON CONFLICT DO NOTHING` plus a lookup, not a
  read-then-write race.
- **User mid-exercise during a redeploy.** Attempts are append-only and
  `active_exercises` is untouched, so a session in flight keeps working; at
  worst the first turn after restart is recorded with no linked words.
- **Word casing.** Store the lemma exactly as the generator produced it.
  Lowercasing would merge German nouns with verbs and is not reversible.
- **A failed bookkeeping write** must never surface to the learner (see above).

## Decisions the owner should confirm

1. **Storing `learner_text`.** The attempt row keeps what the learner said (STT
   transcript) or typed. It is needed to make attempts reviewable later, and it
   is the learner's own data in the learner's own row — but `AGENTS.md` forbids
   logging full voice transcripts at INFO, and this is adjacent. Alternative:
   store only the verdict and drop the text.
2. **Backfill on or off.** Recovering exposures from `sent_history` gives the
   next spec real data to work with immediately, at the cost of a slightly
   larger migration.

## Done when

- `init_db()` creates the four tables on a **fresh** DB and on a DB created
  before this change, and is safe to run twice.
- Delivering a patch with N vocabulary items inserts N `words` rows (first time)
  and N `word_exposures` rows; delivering the same patch to a second user adds N
  exposures and **zero** new `words` rows.
- One graded practice turn inserts exactly one `attempts` row carrying the
  verdict that `speaking.respond` returned.
- A forced failure in any of the three recording paths still delivers the patch /
  answers the learner.
- `tests/` exists and covers the above at the `db.py` level (this PR bootstraps
  pytest for the repo — there are no tests today, which is Known issue #7).
- `docs/architecture.md` § 4 (schema) and § 7 (Known issues #1) updated in the
  same PR.
- `ruff check .` passes.
