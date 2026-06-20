"""Standalone seeding utility.

Generates AI-voiced language-learning patches (OpenAI text + ElevenLabs TTS)
and inserts them into the content pool.

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
import tts
from config import settings
from languages import is_supported

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seed")


def seed(language: str, native: str, count: int) -> int:
    """Generate ``count`` AI-voiced patches for ``language`` and insert them."""
    db.init_db()

    if not is_supported(language):
        log.warning("Language %r is not in the known set; proceeding anyway.", language)

    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required to generate snippet text.")
    if not settings.elevenlabs_api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is required for AI-voiced patches.")

    openai_client = OpenAI(api_key=settings.openai_api_key)

    inserted = 0
    attempts = 0
    while inserted < count and attempts < count * 3:
        attempts += 1
        try:
            snippet = content_mod.generate_snippet(language, native, client=openai_client)
        except Exception as exc:  # noqa: BLE001
            log.warning("Snippet generation failed: %s", exc)
            continue

        voice_name, voice_id = tts.pick_voice(language)
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
            audio_path=str(ogg_path),
            transcript=snippet["transcript"],
            translation=snippet["translation"],
            vocabulary=snippet["vocabulary"],
            source="elevenlabs",
            attribution=voice_name,
        )
        inserted += 1
        log.info("[%d/%d] seeded id=%d voice=%s theme=%s words=%d: %s",
                 inserted, count, cid, voice_name, snippet["theme"],
                 len(snippet["vocabulary"]), snippet["transcript"][:70])

    log.info("Done. Inserted %d new items. Pool now has %d items for %s.",
             inserted, db.count_content(language), language)
    return inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed lingua_patch content (AI-generated).")
    parser.add_argument("--language", default=settings.default_language, help="target ISO 639-3 code, e.g. ukr")
    parser.add_argument("--native", default=settings.native_language, help="native ISO 639-3 code, e.g. rus")
    parser.add_argument("--count", type=int, default=10, help="number of items to seed")
    args = parser.parse_args(argv)

    try:
        seed(args.language, args.native, args.count)
    except Exception as exc:
        log.error("Seeding error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
