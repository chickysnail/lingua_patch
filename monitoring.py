"""Outbound heartbeat pings so an external monitor notices when the bot dies.

lingua_patch is a worker: there is no port to probe, and it can sit silent for
hours between patches, so a crash looks exactly like a quiet day. Instead the bot
pings a monitor URL on a fixed interval and the monitor raises an alert when a
ping fails to arrive — silence becomes the signal.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

log = logging.getLogger("lingua_patch.monitoring")

REQUEST_TIMEOUT_SECONDS = 10

Sender = Callable[[str], Awaitable[None]]


async def send_ping(url: str) -> None:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.get(url)
        response.raise_for_status()


class Heartbeat:
    """Pings ``url`` every ``interval_seconds`` until stopped.

    Disabled (a no-op) when ``url`` is empty, so the bot runs unchanged without
    monitoring configured.
    """

    def __init__(
        self, url: str, interval_seconds: int = 60, sender: Sender = send_ping
    ) -> None:
        self._url = url.strip()
        self._interval_seconds = max(1, interval_seconds)
        self._sender = sender
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        log.info("Heartbeat every %d s", self._interval_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def ping(self) -> bool:
        try:
            await self._sender(self._url)
        except Exception as exc:  # noqa: BLE001
            # A missed ping is what alerts the owner, so only log it here.
            log.warning("Heartbeat ping failed: %s", exc)
            return False
        return True

    async def _loop(self) -> None:
        while True:
            await self.ping()
            await asyncio.sleep(self._interval_seconds)
