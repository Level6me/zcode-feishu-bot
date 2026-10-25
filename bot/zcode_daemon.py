#!/usr/bin/env python3
"""
ZCode engine keep-alive daemon.

Purpose:
  Keep the zcode app-server runtime warm so quota/account probing and
  first-token latency stay stable. The main bot (main.py) manages its own
  warm sessions via session_pool; this daemon is optional infrastructure
  (PM2: pm2 start venv/bin/python3 --name zcode-daemon -- zcode_daemon.py).

Configuration (env / .env, via config.py):
  ZCODE_BIN   – path to zcode launcher or bundled zcode.cjs (auto-detected)
  ZCODE_HOME  – zcode data root
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from logger import log

RESTART_DELAY_SEC = float(os.environ.get("ZCODE_DAEMON_RESTART_DELAY", "3"))
MAX_BACKOFF_SEC = float(os.environ.get("ZCODE_DAEMON_MAX_BACKOFF", "60"))
PROBE_INTERVAL_SEC = float(os.environ.get("ZCODE_DAEMON_PROBE_INTERVAL", "300"))


class ZcodeDaemon:
    def __init__(self):
        self._backoff = RESTART_DELAY_SEC
        self._stopping = False

    def _engine_argv(self):
        from config import zcode_engine_argv

        argv = zcode_engine_argv()
        if not argv:
            raise FileNotFoundError(
                "zcode engine not found. Install ZCode (Desktop or CLI) or set ZCODE_BIN in .env"
            )
        return argv

    async def _probe_once(self):
        """Create a throwaway session to keep the runtime hot, then close it."""
        from zcode_protocol import ZcodeAgentSession

        home = os.path.expanduser(os.environ.get("ZCODE_WORKSPACE_ROOT", "~"))
        os.makedirs(home, exist_ok=True)
        sess = ZcodeAgentSession(self._engine_argv(), chat_id="daemon", model="glm-5.3-flash", project_dir=home)
        try:
            await sess.start()
            log.info("[zcode-daemon] probe session ok")
        finally:
            await sess.close()

    def stop(self, signum=None, frame=None):
        log.info(f"[zcode-daemon] received signal {signum}, shutting down...")
        self._stopping = True

    def run(self):
        import signal

        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        log.info("[zcode-daemon] starting keep-alive loop")
        while not self._stopping:
            try:
                asyncio.run(self._probe_once())
            except FileNotFoundError as e:
                log.error(f"[zcode-daemon] {e}")
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF_SEC)
                continue
            except Exception as e:
                log.error(f"[zcode-daemon] probe failed: {e}")
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF_SEC)
                continue

            self._backoff = RESTART_DELAY_SEC
            deadline = time.time() + PROBE_INTERVAL_SEC
            while not self._stopping and time.time() < deadline:
                time.sleep(1.0)

        log.info("[zcode-daemon] exited cleanly")
        return 0


def main():
    daemon = ZcodeDaemon()
    code = daemon.run()
    sys.exit(code if isinstance(code, int) else 0)


if __name__ == "__main__":
    main()
