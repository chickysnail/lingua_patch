# 001 — Translate messages sent outside practice
Status: implemented

## Why
Outside a practice session the bot ignores typed text and answers a voice
message with "tap Practice first". The owner wants those messages to be useful:
the learner says (or types) something, and the bot shows how to say it in the
language they are learning, with a short note on why when it is not obvious.

## What changes
- A voice message or typed text from a user **with no active exercise** is
  translated into the user's target language (`users.language`: Portuguese for
  `por`/`por_pt` users, and the same for every other language).
- Voice is transcribed with the existing ElevenLabs STT (no language hint).
- One OpenAI call (`openai_exercise_model`) returns
  `{"translation", "explanation"}`. The explanation is in the user's native
  language and is **empty unless something needs explaining** (an idiom, a
  construction that does not map one-to-one, two natural options). If the
  message is already in the target language, the bot returns the natural
  version and says what it changed.
- The reply echoes the transcript (voice only), shows the translation in bold,
  then "💡 Why it's said like this" when there is an explanation.
- The translation is then voiced with a random native ElevenLabs voice and
  sent as a voice-note reply. If the language has no voice pool, or TTS fails,
  the text reply stands alone.
- New module `translate.py`: prompt, parsing, rendering, `voice_note()`.

## What does NOT change
- Practice: while an exercise is active, voice and text still go to the tutor.
- Practice prompts in `speaking.py`: untouched.
- Schema: nothing is stored; translation is stateless.
- The custom-time flow: while `awaiting_time` is set, text is parsed as a time.

## Edge cases
- An exercise lives until the next practice or the next patch, so after a
  practice session voice messages keep going to the tutor until the next
  patch arrives. Ending practice explicitly is a separate spec.
- Bursts: one translation per user at a time (`_translating`); a second
  message during one gets "still translating".
- Input is capped at 1000 chars before the prompt, and voice at 60 s, which
  bounds the per-message cost. There is still no per-user daily cap (Known issue #5).
- User text reaches the prompt inside `<message>` tags, and the system
  prompt says it is data, not instructions.

## Done when
- `tests/test_translate.py`: parsing, bad payloads, escaping, routing
  (voice/text in and out of practice, awaiting time, busy guard).
- `evals/translate/cases.json`: explanation-only-when-needed, pt-PT vs pt-BR,
  injection, already-in-target.
- `docs/architecture.md` has the new flow and cost row.
