# 000 — How we build this

Status: accepted · 2026-08-19

## Roles

| Role | Who | Produces |
|---|---|---|
| Owner | Artem | decisions, product direction, uses the bot |
| Architect | Claude (chat) | specs in `docs/specs/`, `docs/architecture.md` |
| Implementer | one agent session (Claude Code) | one PR per spec |
| Reviewer | a **fresh** agent session that did not write the code | review report on the diff |

The reviewer's value is context isolation, not expertise. The same session that
wrote the code will defend it. A new session with the diff, the spec and the
checklist below is the closest thing we have to an independent check.

## Cycle

1. Owner states what they want, in plain language.
2. Architect writes `docs/specs/NNN-short-name.md` (template below). Owner reads
   it and confirms *this is what I meant*. **This is the owner's main checkpoint.**
3. Implementer gets the spec path and nothing else. Builds it. Opens a PR whose
   description links the spec and lists what tests/evals were added.
4. Reviewer (fresh session) gets: the PR diff, the spec, `AGENTS.md`, and the
   checklist below. Writes a report; either approves or lists blocking items.
5. Owner merges. Owner uses the bot. Behaviour is the only ground truth that is
   not a language model.

If the implementer discovers the spec is wrong, it stops and says so in the PR
rather than improvising. The spec gets amended, then work resumes.

## Spec template

```
# NNN — Title
Status: draft | accepted | implemented | superseded by NNN

## Why
One paragraph. What problem, why now.

## What changes
Behaviour, schema, prompts, config — concretely.

## What does NOT change
Explicit non-goals, so the implementer doesn't widen scope.

## Edge cases
The awkward ones. Migration on existing DBs. Users mid-exercise. Empty pool.

## Done when
Observable checks. Tests/evals that must exist. Docs that must be updated.
```

## Review checklist

Behavioural
- Does the diff do what the spec says — no more, no less?
- Any prompt changed? Is there an eval case in `evals/` covering the old
  behaviour it might have broken?
- Any schema change? Idempotent migration + `docs/architecture.md` updated?

Safety (this project's actual risk surface — not generic OWASP)
- New OpenAI/ElevenLabs calls: what bounds them per user per day?
- User-supplied text reaching a prompt: is it clearly delimited from instructions?
- Anything written to logs at INFO that contains user content or keys?
- Voice files: written to temp, deleted in `finally`?
- Secrets: nothing new hardcoded; `.env.example` still has placeholders only.

Hygiene
- `ruff check .` passes.
- No new dependency without a stated reason.
- README touched only if setup steps changed.

## Automated instead of an agent

These do not need a reviewer session and should be CI:
`ruff`, `pytest`, GitHub secret scanning, Dependabot.

## Evals (`evals/`)

Prompt behaviour is the product. Each eval case is a small YAML/JSON file:
inputs (language, native, sentence / turns) and assertions on the output
(must contain / must not contain / verdict equals / max blocks). A runner
script executes them against the live model and reports pass/fail. Cases are
added whenever a prompt rule is added — the case *is* the reason the rule exists.
