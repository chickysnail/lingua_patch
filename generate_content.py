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


def seed(language: str, native: str, count: int, source: str | None = None) -> int:
    """Seed ``count`` items for ``language`` using the configured audio source."""
    db.init_db()
    source = source or settings.audio_source
    if source == "elevenlabs":
        return seed_elevenlabs(language, native, count)
    return seed_tatoeba(language, native, count)


def seed_elevenlabs(language: str, native: str, count: int) -> int:
    """Generate themed snippets with OpenAI and voice them with ElevenLabs."""
    import tts

    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required to generate snippet text.")
    if not settings.elevenlabs_api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is required for the elevenlabs audio source.")
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
        )
        inserted += 1
        log.info("[%d/%d] seeded TTS id=%d voice=%s theme=%s words=%d: %s",
                 inserted, count, cid, voice_name, snippet["theme"],
                 len(snippet["vocabulary"]), snippet["transcript"][:70])

    log.info("Done. Inserted %d new TTS items. Pool now has %d items for %s.",
             inserted, db.count_content(language), language)
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
    parser.add_argument("--source", choices=["elevenlabs", "tatoeba"], default=None,
                        help="audio source (defaults to AUDIO_SOURCE / config)")
    args = parser.parse_args(argv)

    try:
        seed(args.language, args.native, args.count, source=args.source)
    except content_mod.TatoebaError as exc:
        log.error("Tatoeba error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
