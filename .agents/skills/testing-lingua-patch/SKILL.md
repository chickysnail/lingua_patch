---
name: testing-lingua-patch
description: Test the lingua_patch Telegram bot end-to-end. Use when verifying bot handlers, DB logic, content generation, or delivery flows.
---

# Testing lingua_patch

## Devin Secrets Needed

- `BOT_TOKEN` — Telegram Bot API token for `@lingua_patch_bot`
- `OPENAI_API_KEY` — OpenAI API key for text generation
- `ELEVENLABS_API_KEY` — ElevenLabs API key for TTS

## Environment Setup

```bash
cd /home/ubuntu/repos/lingua-patch
./.venv/bin/python -c "import aiogram; import openai; print('deps OK')"
which ffmpeg  # required for OGG conversion
```

Lint: `./.venv/bin/ruff check .`
Run bot: `./.venv/bin/python main.py`

## Pre-flight Checks

1. **Verify bot API access:**
   ```python
   import httpx, os
   token = os.environ['BOT_TOKEN']
   print(httpx.get(f'https://api.telegram.org/bot{token}/getMe').json())
   ```

2. **Check for conflicting instances** — if no webhook is set and `getUpdates` returns data, no other instance is long-polling. If a webhook IS set, a production instance might be using webhooks (safe to long-poll locally).
   ```python
   print(httpx.get(f'https://api.telegram.org/bot{token}/getWebhookInfo').json())
   ```

3. **Check ElevenLabs quota** — quota may be exhausted. Test with a short TTS call first before running full tests. If quota is low, use synthetic OGG files instead:
   ```bash
   ffmpeg -y -f lavfi -i anullsrc=r=48000:cl=mono -t 1 -c:a libopus -b:a 32k media/test.ogg
   ```

## Testing Strategy

This is a Telegram bot — no web UI. Testing is **shell-based** (no screen recording needed).

### DB-level tests
- Delete `bot.db`, call `db.init_db()`, verify schema with `PRAGMA table_info(content_pool)`
- Seed synthetic content via `db.insert_content()` with dummy OGG files
- Test `pick_unsent_content`, `count_unsent`, `record_sent` for no-repeat logic

### Handler tests
Use `unittest.mock` to test bot handlers without a live Telegram connection:
```python
from unittest.mock import AsyncMock, MagicMock
import main as bot_main

mock_message = AsyncMock()
mock_message.from_user = MagicMock(id=TEST_USER_ID)
mock_message.answer = AsyncMock()
mock_bot = AsyncMock()

import asyncio
asyncio.run(bot_main.cmd_start(mock_message, mock_bot))
# Check mock_message.answer.call_args for response text and reply_markup
```

### Live bot test
1. Clear pending updates: `getUpdates` with offset past latest `update_id`
2. Start bot: `ADMIN_ID=<user_id> ./.venv/bin/python main.py`
3. Bot logs show scheduler start, pool size, and polling status
4. Stop with SIGTERM (Ctrl+C or `kill <pid>`)

### Content generation test
```python
import content
from openai import OpenAI
from config import settings
client = OpenAI(api_key=settings.openai_api_key)
snippet = content.generate_snippet('ukr', 'rus', client=client)
# Returns: {transcript, translation, vocabulary, theme}
```

## Key User IDs

The bot owner's Telegram ID can be found from `getUpdates` responses. Set `ADMIN_ID` env var to this ID for admin notification testing.

## Common Issues

- **ElevenLabs quota exceeded** — TTS returns 401. The bot handles this gracefully (logs warning, skips item). Use synthetic OGG files to test delivery logic without TTS.
- **Bot conflicts** — only one long-polling instance can run at a time. Check `getWebhookInfo` first.
- **DB locked** — SQLite is single-writer. Don't run test scripts while the bot is running against the same `bot.db`.
