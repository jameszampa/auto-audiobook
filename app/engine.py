"""Keeps the TTS model in VRAM only while something is actually using it.

The chatterbox container is long-lived (`restart: unless-stopped`) and loads
its model at boot, so without this the ~4 GB stays pinned all day for a machine
that narrates a book now and then. Measured on an RTX 4090 deployment: unloading
takes ~3s and reloading ~11s, which is cheap next to the hours of GPU a book costs.

Work takes a *lease*; the model is loaded before the first lease is granted and
unloaded once the last one has been released for `idle_sec`. Leases are counted
because the job worker and a voice preview can overlap.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from .tts_client import ChatterboxClient, TTSError

log = logging.getLogger(__name__)


class EngineLifecycle:
    def __init__(
        self,
        client: ChatterboxClient,
        *,
        enabled: bool = True,
        idle_sec: float = 180.0,
    ):
        self.client = client
        self.enabled = enabled
        self.idle_sec = idle_sec
        self._lock = asyncio.Lock()
        self._leases: dict[str, int] = {}
        self._idle_task: asyncio.Task | None = None
        # Bumped by every acquire. An idle-unload that was scheduled before the
        # bump knows to stand down even if it never saw the cancellation.
        self._generation = 0
        self.last_action = "nothing yet"

    # ---- introspection ---------------------------------------------------
    @property
    def busy(self) -> bool:
        return bool(self._leases)

    @property
    def users(self) -> list[str]:
        return sorted(self._leases)

    # ---- leases ----------------------------------------------------------
    @asynccontextmanager
    async def leased(self, who: str):
        """Hold the model loaded for the duration of the block.

        Yields True if this lease is what pulled the model into VRAM, so
        callers can say so in their own log.
        """
        loaded_now = await self.acquire(who)
        try:
            yield loaded_now
        finally:
            await self.release(who)

    async def acquire(self, who: str) -> bool:
        self._stand_down()
        async with self._lock:
            self._leases[who] = self._leases.get(who, 0) + 1
            try:
                return await self._ensure_loaded(who)
            except Exception:
                self._drop(who)
                raise

    async def release(self, who: str) -> None:
        async with self._lock:
            self._drop(who)
            if self._leases or not self.enabled:
                return
        self._schedule_idle_unload()

    def _drop(self, who: str) -> None:
        remaining = self._leases.get(who, 0) - 1
        if remaining > 0:
            self._leases[who] = remaining
        else:
            self._leases.pop(who, None)

    # ---- load / unload ---------------------------------------------------
    async def _ensure_loaded(self, who: str) -> bool:
        state = await self.client.engine_state()
        if not state["reachable"]:
            raise TTSError(state["detail"])
        if state["loaded"]:
            return False
        if not self.enabled:
            # Not ours to load - say so rather than failing obscurely later.
            raise TTSError(
                "chatterbox has no model loaded and AUDIOBOOK_MANAGE_ENGINE=0"
            )
        log.info("loading TTS model for %s", who)
        message = await self.client.load()
        self.last_action = f"loaded for {who}"
        log.info("TTS model ready: %s", message)
        return True

    def _schedule_idle_unload(self) -> None:
        self._cancel_idle_task()
        generation = self._generation
        self._idle_task = asyncio.create_task(self._idle_unload(generation))

    async def _idle_unload(self, generation: int) -> None:
        try:
            await asyncio.sleep(self.idle_sec)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self._leases or generation != self._generation:
                return  # work arrived while we were waiting
            state = await self.client.engine_state()
            if not state["reachable"] or not state["loaded"]:
                return
            try:
                await self.client.unload()
            except Exception as e:  # a stuck unload must not kill the app
                log.warning("idle unload failed: %s", e)
                self.last_action = f"idle unload failed: {e}"
                return
            self.last_action = f"unloaded after {self.idle_sec:.0f}s idle"
            log.info("TTS model unloaded after %.0fs idle; VRAM released",
                     self.idle_sec)

    def _stand_down(self) -> None:
        """Call off a pending idle unload before taking a lease."""
        self._generation += 1
        self._cancel_idle_task()

    def _cancel_idle_task(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    # ---- app lifecycle ---------------------------------------------------
    def start(self) -> None:
        """Arm the idle timer at startup.

        The container usually comes up with a model already resident, and an
        app that has just started is by definition idle.
        """
        if self.enabled:
            self._schedule_idle_unload()

    async def shutdown(self) -> None:
        self._stand_down()
        if not self.enabled or self._leases:
            return
        try:
            state = await self.client.engine_state()
            if state["reachable"] and state["loaded"]:
                await self.client.unload()
                self.last_action = "unloaded at shutdown"
        except Exception as e:
            log.warning("unload at shutdown failed: %s", e)

    async def unload_now(self, reason: str = "manual request") -> dict:
        """Free VRAM immediately, unless work is in flight."""
        self._stand_down()
        async with self._lock:
            if self._leases:
                return {"unloaded": False, "detail": f"in use by {', '.join(self.users)}"}
            state = await self.client.engine_state()
            if not state["reachable"]:
                return {"unloaded": False, "detail": state["detail"]}
            if not state["loaded"]:
                return {"unloaded": False, "detail": "model was not loaded"}
            await self.client.unload()
            self.last_action = f"unloaded ({reason})"
            return {"unloaded": True, "detail": "model unloaded, VRAM released"}

    async def state(self) -> dict:
        state = await self.client.engine_state()
        return {
            **state,
            "managed": self.enabled,
            "in_use_by": self.users,
            "idle_unload_sec": self.idle_sec if self.enabled else None,
            "last_action": self.last_action,
        }
