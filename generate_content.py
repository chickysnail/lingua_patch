"""Standalone seeding utility.

Fetches native-audio sentences from Tatoeba for a target language, downloads and
converts the audio to OGG/Opus voice notes, builds the vocabulary breakdown with
OpenAI, and inserts everything into ``bot.db``.

Usage:
    python generate_content.py --language ukr --count 10
    python generate_content.py --language spa --count 5 --native rus
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from openai import OpenAI

import content as content_mod
import db
from config import settings
from languages import is_supported

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seed")


def seed(language: str, native: str, count: int, kind: str = "long") -> int:
    """Seed ``count`` items of a given ``kind`` ("short" or "long") for ``language``.

    "long" items are always AI-voiced (ElevenLabs) 2-4 sentence snippets. "short"
    items mix real native clips (Tatoeba) with one-sentence AI snippets so each
    short pull randomly turns out native or AI; if a language has no native audio,
    the whole short pool falls back to AI.
    """
    db.init_db()
    if kind == "short":
        return seed_short(language, native, count)
    return seed_tts(language, native, count, length="long")


def seed_short(language: str, native: str, count: int) -> int:
    """Fill the short pool with a native/AI mix (native first, AI for the rest)."""
    native_target = round(count * settings.short_native_ratio)
    inserted_native = 0
    if native_target > 0:
        try:
            inserted_native = seed_tatoeba(language, native, native_target)
        except Exception as exc:  # noqa: BLE001 - no native audio for this language is fine
            log.info("No native (Tatoeba) audio for %s (%s); using AI for the short pool.", language, exc)
    remaining = count - inserted_native
    inserted_tts = 0
    if remaining > 0:
        inserted_tts = seed_tts(language, native, remaining, length="short")
    return inserted_native + inserted_tts


def seed_tts(language: str, native: str, count: int, length: str = "long") -> int:
    """Generate themed snippets with OpenAI and voice them with ElevenLabs."""
    import tts

    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required to generate snippet text.")
    if not settings.elevenlabs_api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is required for AI-voiced snippets.")
    openai_client = OpenAI(api_key=settings.openai_api_key)

    inserted = 0
    attempts = 0
    while inserted < count and attempts < count * 3:
        attempts += 1
        try:
            snippet = content_mod.generate_snippet(language, native, client=openai_client, length=length)
        except Exception as exc:  # noqa: BLE001
            log.warning("Snippet generation failed: %s", exc)
            continue

        voice_name, voice_id = tts.pick_voice()
        stamp = int(time.time() * 1000)
        mp3_path = settings.media_dir / f"{language}_tts_{stamp}.mp3"
        ogg_path = settings.media_dir / f"{language}_tts_{stamp}.ogg"
        try:
            tts.synthesize(snippet["transcript"], language, mp3_path, voice_id=voice_id)
            content_mod.to_voice_ogg(mp3_path, ogg_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping snippet (TTS failed): %s", exc)
            mp3_path.unlink(missing_ok=True)
            continue
        finally:
            mp3_path.unlink(missing_ok=True)

        cid = db.insert_content(
            language=language,
            native_language=native,
            tatoeba_id=None,
            audio_path=str(ogg_path),
            transcript=snippet["transcript"],
            translation=snippet["translation"],
            vocabulary=snippet["vocabulary"],
            source="elevenlabs",
            attribution=voice_name,
            length=length,
        )
        inserted += 1
        log.info("[%d/%d] seeded %s TTS id=%d voice=%s theme=%s words=%d: %s",
                 inserted, count, length, cid, voice_name, snippet["theme"],
                 len(snippet["vocabulary"]), snippet["transcript"][:70])

    log.info("Done. Inserted %d new %s TTS items. Pool now has %d items for %s.",
             inserted, length, db.count_content(language), language)
    return inserted


def seed_tatoeba(language: str, native: str, count: int) -> int:
    if not is_supported(language):
        log.warning("Language %r is not in the known set; proceeding anyway (Tatoeba may still have it).", language)

    openai_client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None
    if openai_client is None:
        log.warning("OPENAI_API_KEY not set — content will be seeded without a vocabulary breakdown.")

    # Over-fetch: some sentences may already be in the pool or fail to download.
    sentences = content_mod.fetch_sentences(language, native, limit=count * 3)
    log.info("Fetched %d candidate sentences from Tatoeba.", len(sentences))

    inserted = 0
    for sent in sentences:
        if inserted >= count:
            break

        tatoeba_id = sent.get("id")
        if tatoeba_id is None or db.content_exists(tatoeba_id):
            continue

        audios = sent.get("audios") or []
        if not audios:
            continue
        audio_id = audios[0].get("id")
        if audio_id is None:
            continue

        transcript = sent.get("text", "").strip()
        if not transcript:
            continue

        translation = content_mod.extract_native_translation(sent, native)
        attribution = audios[0].get("attribution_url")

        mp3_path = settings.media_dir / f"{language}_{tatoeba_id}.mp3"
        ogg_path = settings.media_dir / f"{language}_{tatoeba_id}.ogg"
        try:
            content_mod.download_audio(audio_id, mp3_path)
            content_mod.to_voice_ogg(mp3_path, ogg_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping sentence %s (audio failed): %s", tatoeba_id, exc)
            mp3_path.unlink(missing_ok=True)
            continue
        finally:
            mp3_path.unlink(missing_ok=True)

        vocabulary: list[dict[str, str]] = []
        if openai_client is not None:
            for attempt in range(2):
                vocabulary = content_mod.build_vocabulary(
                    transcript, translation, language, native, client=openai_client
                )
                if vocabulary:
                    break
                time.sleep(1)

        cid = db.insert_content(
            language=language,
            native_language=native,
            tatoeba_id=tatoeba_id,
            audio_path=str(ogg_path),
            transcript=transcript,
            translation=translation,
            vocabulary=vocabulary,
            source="tatoeba",
            attribution=attribution,
            length="short",
        )
        inserted += 1
        log.info("[%d/%d] seeded content id=%d tatoeba=%s words=%d: %s",
                 inserted, count, cid, tatoeba_id, len(vocabulary), transcript)

    log.info("Done. Inserted %d new items. Pool now has %d items for %s.",
             inserted, db.count_content(language), language)
    return inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed lingua_patch content (ElevenLabs TTS or Tatoeba).")
    parser.add_argument("--language", default=settings.default_language, help="target ISO 639-3 code, e.g. ukr")
    parser.add_argument("--native", default=settings.native_language, help="native ISO 639-3 code, e.g. rus")
    parser.add_argument("--count", type=int, default=10, help="number of items to seed")
    parser.add_argument("--kind", choices=["short", "long"], default="long",
                        help="patch length: 'short' (native+AI mix) or 'long' (AI snippet)")
    args = parser.parse_args(argv)

    try:
        seed(args.language, args.native, args.count, kind=args.kind)
    except content_mod.TatoebaError as exc:
        log.error("Tatoeba error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
