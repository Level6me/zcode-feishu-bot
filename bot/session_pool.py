"""Persistent session pool for ZCode app-server protocol sessions.

Maintains warm, long-lived `zcode app-server` processes per chat/project to
eliminate cold-start overhead and enable instant responses. The public surface
mirrors the upstream-era pool surface: PersistentSession (ZcodeAgentSession based),
SessionPool.get_or_create / prewarm / reset_session / update_conversation_id,
and TurnEventStream (from zcode_protocol).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

import config
from config import BASE_DIR
from zcode_protocol import ZcodeAgentSession, _QueueTurnStream as TurnEventStream
from utils.auth import is_admin, has_scope, SCOPE_PROJECT, SCOPE_SHELL
from logger import log
import app_state


class PersistentSession:
    """Represents a long-lived zcode engine session for one chat."""

    def __init__(self, chat_id: str, model: str, project_dir: Optional[str] = None, conversation_id: str = "", effort: str = "medium", sandbox_enabled: bool = False):
        self.chat_id = chat_id
        self.model = model
        self.effort = effort if effort in ("low", "medium", "high") else "medium"
        self.sandbox_enabled = sandbox_enabled
        self.project_dir = project_dir
        self.conversation_id = conversation_id
        self.lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self.last_active_time = time.time()
        self._closing = False
        self._inner: Optional[ZcodeAgentSession] = None

        # Restricted chats (non-admin without shell scope) run in build mode
        # with the terminal tool denied — the zcode equivalent of upstream sandboxing.
        denylist = []
        if sandbox_enabled:
            denylist = ["Bash", "Task", "Agent"]

        engine_argv = config.zcode_engine_argv()
        if not engine_argv:
            raise FileNotFoundError(
                "zcode engine not found. Install the zcode CLI (or ZCode Desktop) "
                "or set ZCODE_BIN in .env"
            )
        self._engine_argv = engine_argv
        self._denylist = denylist

    # -- upstream-era compat surface -----------------------------------------

    def is_alive(self) -> bool:
        return self._inner is not None and self._inner.client.is_alive()

    def get_recent_stderr(self) -> str:
        return self._inner.client.recent_stderr if self._inner else ""

    def clear_recent_stderr(self):
        if self._inner:
            self._inner.client.recent_stderr = ""

    @property
    def process(self):
        """Compat: executor tracks the OS-level process for watchdog checks."""
        return self._inner.client.process if self._inner else None

    async def start(self):
        # prewarm and request execution may race to start the same session.
        async with self._start_lock:
            if self.is_alive():
                return
            await self._start_session()

    async def _start_session(self):
        """Spawn the app-server process and create/attach the zcode session."""
        if self.is_alive():
            return
        sess = ZcodeAgentSession(
            self._engine_argv,
            chat_id=self.chat_id,
            model=self.model,
            project_dir=self.project_dir,
            effort=self.effort,
            sandbox_enabled=self.sandbox_enabled,
            conversation_id=self.conversation_id,
            tool_denylist=self._denylist,
        )
        log.info(
            f"[SessionPool] Spawning warm zcode session for chat {self.chat_id} "
            f"(model={self.model}, effort={self.effort}, sandbox={self.sandbox_enabled}, cwd={self.project_dir})"
        )
        try:
            conv_id = await sess.start()
            if conv_id:
                self.conversation_id = conv_id
        except Exception:
            await sess.close()
            raise
        self._inner = sess
        self.last_active_time = time.time()
        app_state.running_processes[self.chat_id] = sess.client.process

    async def send_prompt_and_stream(self, prompt_text: str) -> TurnEventStream:
        """Send a prompt turn to the session and return a TurnEventStream."""
        if not self.is_alive():
            await self.start()
        self.clear_recent_stderr()
        self.last_active_time = time.time()
        return await self._inner.send_prompt_and_stream(prompt_text)

    async def set_model_effort(self, model: str, effort: str):
        """Hot-switch model/effort on the live session (zcode session/setModel)."""
        self.model = model
        self.effort = effort if effort in ("low", "medium", "high") else "medium"
        if self._inner:
            await self._inner.set_model(model, self.effort)

    async def stop_turn(self):
        if self._inner:
            await self._inner.stop_turn()

    async def close(self):
        """Gracefully terminate the warm process."""
        self._closing = True
        inner, self._inner = self._inner, None
        proc = inner.client.process if inner else None
        if app_state.running_processes.get(self.chat_id) is proc:
            app_state.running_processes.pop(self.chat_id, None)
        if inner:
            await inner.close()

    def __getattr__(self, item):
        # Transparent delegate for anything else the executor pokes at.
        if item.startswith("_"):
            raise AttributeError(item)
        if self._inner is None:
            raise AttributeError(f"session not started: {item}")
        return getattr(self._inner, item)


class SessionPool:
    """Manages active persistent sessions across chats."""

    def __init__(self):
        self._sessions: dict = {}

    def get_or_create(self, chat_id: str, model: str, project_dir: Optional[str] = None, conversation_id: str = "", effort: str = "medium") -> PersistentSession:
        sess = self._sessions.get(chat_id)
        # conversation_id 比对：只在"双方均非空且不同"时才视作需要重建
        conv_mismatch = bool(conversation_id and sess and sess.conversation_id and sess.conversation_id != conversation_id)

        normalized_effort = effort if effort in ("low", "medium", "high") else "medium"
        # Enforce sandbox both when Co-Pilot mode is active AND when a non-admin
        # user lacks SCOPE_SHELL (deny terminal tools on the engine side).
        real_chat_id = str(chat_id).split("cron_")[-1] if str(chat_id).startswith("cron_") else str(chat_id)
        user_lacks_shell = not is_admin(real_chat_id) and not has_scope(real_chat_id, SCOPE_SHELL)
        sandbox_enabled = bool((config.TYPESAFE_ENABLED and config.TYPESAFE_TIER == "copilot") or user_lacks_shell)
        # Model/effort/sandbox policy are hot-switchable via session/setModel,
        # so a live process no longer needs a full respawn on model change.
        model_mismatch = bool(sess and sess.sandbox_enabled != sandbox_enabled)

        if sess is None or model_mismatch or sess.project_dir != project_dir or conv_mismatch or not sess.is_alive():
            if sess:
                asyncio.create_task(sess.close())
            sess = PersistentSession(chat_id, model, project_dir, conversation_id, normalized_effort, sandbox_enabled)
            self._sessions[chat_id] = sess
        elif sess.model != model or sess.effort != normalized_effort:
            try:
                asyncio.create_task(sess.set_model_effort(model, normalized_effort))
            except Exception:
                pass
        return sess

    def update_conversation_id(self, chat_id: str, conversation_id: str):
        """Sync runtime assigned session ID into memory to maintain continuity."""
        if not chat_id or not conversation_id:
            return
        sess = self._sessions.get(chat_id)
        if sess:
            sess.conversation_id = conversation_id

    async def prewarm(self, chat_id: str, model: str, project_dir: Optional[str] = None, conversation_id: str = "", effort: str = "medium"):
        """Asynchronously pre-spawns a warm session so next message has 0s start latency."""
        try:
            sess = self.get_or_create(chat_id, model, project_dir, conversation_id, effort=effort)
            if not sess.is_alive():
                await sess.start()
        except Exception as e:
            log.warning(f"[SessionPool] Prewarm failed for chat {chat_id}: {e}")

    async def reset_session(self, chat_id: str):
        """Close and purge a chat's session."""
        sess = self._sessions.pop(chat_id, None)
        if sess:
            await sess.close()


session_pool = SessionPool()
