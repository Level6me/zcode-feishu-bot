"""ZCode Protocol adapter: bridges the Feishu bot engine layer onto the
`zcode app-server` stdio JSON-RPC protocol.

This module reproduces the event surface the rest of the bot was originally
built against (the upstream `stream-json` event contract):

    init                 -> {"event": "init", "conversation_id": ...}
    step_update(text)    -> {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": ...}}
    step_update(tool)    -> {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE"|"DONE", "tool_name": ..., "tool_info": {"parameters": ...}}}
    result               -> {"event": "result", "result": {"status": "SUCCESS", "response": ...}}

It ALSO maintains an upstream-compatible `transcript.jsonl` journal per session so
every existing transcript consumer (ask_question choice cards, background task
tracking, context preview) keeps working unmodified.

Wire format (reverse-engineered from the ZCode Protocol agent server):
  * Newline-delimited JSON on stdin/stdout.
  * Client requests:  {"id": <int>, "method": "...", "params": {...}}   (NO jsonrpc field!)
  * Responses:        {"id": <int>, "result": ...} | {"id": <int>, "error": {...}}
  * Notifications:    {"method": "...", "params": {...}}
  * Server->client requests carry a string id ("server-N") and must be answered
    with {"id": <same>, "result": {...}}.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import re
import time
from typing import Any, Dict, Optional

from logger import log

# ---------------------------------------------------------------------------
# Model mapping: bot-facing model ids -> zcode modelSelection
#   bot syntax:  glm-5.3 | glm-5.3-flash | glm-5.2 | glm-5-turbo  ([-low|-medium|-high])
#   zcode:       {providerId, modelId, options: {reasoningLevel: low|high|max}}
# ---------------------------------------------------------------------------

def _engine_setting(env_name: str, attr: str, fallback: str = "") -> str:
    """Resolve an engine setting from os.environ first, then config .env settings."""
    val = os.environ.get(env_name, "").strip()
    if val:
        return val
    try:
        import config as _config
        val = (getattr(_config.settings, attr, "") or "").strip()
    except Exception:
        val = ""
    return val or fallback


def _load_plan_credential() -> str:
    """Decrypt the zcode JWT (zcodejwttoken) from the shared credential store.

    The JWT is the credential the ZCode desktop uses on the coding-plan
    channel; we hand it to the engine as requestAuth.apiKey so the engine's
    own V4 signing (handshake + PoW) passes server risk control.
    """
    import subprocess
    script = (
        "const c=require('crypto'),f=require('fs'),o=require('os');"
        "const d=JSON.parse(f.readFileSync(o.homedir()+'/.zcode/v2/credentials.json','utf8'));"
        "const k=c.createHash('sha256').update(`zcode-credential-fallback:${o.platform()}:${o.homedir()}:${o.userInfo().username}`).digest();"
        "const v=d['zcodejwttoken']||'';if(!v.startsWith('enc:v1:')){process.stdout.write(v);process.exit(0)};"
        "const [iv,tag,ct]=v.slice(7).split('.');"
        "const d2=c.createDecipheriv('aes-256-gcm',k,Buffer.from(iv,'base64url'));"
        "d2.setAuthTag(Buffer.from(tag,'base64url'));"
        "process.stdout.write(Buffer.concat([d2.update(Buffer.from(ct,'base64url')),d2.final()]).toString());"
    )
    try:
        return subprocess.check_output(["node", "-e", script], text=True, timeout=10).strip()
    except Exception:
        return ""


# Manual coding-plan connection (ZCODE_API_KEY, official CLI storage format):
# the individual coding-plan account provider uses the stored API key directly
# and requestProviderRuntimeHeaders is answered with the same key.
ZCODE_API_KEY = _engine_setting("ZCODE_API_KEY", "zcode_api_key")
if ZCODE_API_KEY:
    DEFAULT_PROVIDER_ID = _engine_setting("ZCODE_PROVIDER_ID", "zcode_provider_id", "zai-api")
    if DEFAULT_PROVIDER_ID == "account:zai-start-plan":
        DEFAULT_PROVIDER_ID = "zai-api"
else:
    DEFAULT_PROVIDER_ID = _engine_setting("ZCODE_PROVIDER_ID", "zcode_provider_id", "account:zai-start-plan")
DEFAULT_MODEL_ID = _engine_setting("ZCODE_MODEL_ID", "zcode_model_id", "GLM-5.3")

ZCODE_PLAN_CREDENTIAL = os.environ.get("ZCODE_PLAN_CREDENTIAL", "").strip() or _load_plan_credential()
def _builtin_revision_id() -> str:
    """zcode-builtin:<revision>:<sha256(builtin file path)> — the exact format
    the ZCode desktop uses when pushing account provider configs."""
    import glob
    home = os.path.expanduser("~")
    candidates = sorted(glob.glob(os.path.join(
        home, ".zcode/v2/runtime/provider/*/*/*/zcode-builtin.json")))
    for path in reversed(candidates):  # newest version dir last
        try:
            import json as _json
            data = _json.load(open(path))
            rev = str(data.get("revision", 33))
        except Exception:
            rev = "33"
        h = hashlib.sha256(path.encode()).hexdigest()
        return f"zcode-builtin:{rev}:{h}"
    return "33"


ZCODE_BUILTIN_REVISION_ID = _builtin_revision_id()


def _latest_start_plan_connection_key() -> str:
    """从本机 ZCode 桌面日志提取最新的 account:zai-start-plan connectionKey。

    connectionKey 与账号/套餐绑定；用本机日志里的真实值可保证 entitlement
    推送在任意账号上匹配。找不到时回退到通用占位值（多数情况下仍可用）。
    """
    import glob as _glob
    home = os.path.expanduser("~")
    files = sorted(_glob.glob(os.path.join(home, ".zcode", "v2", "logs", "*.log")), reverse=True)[:2]
    import re as _re
    pat = _re.compile(
        r'"account:zai-start-plan":\{[^{}]*"connectionKey":"([0-9a-f]{64})"'
    )
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "account:zai-start-plan" not in line:
                        continue
                    m = pat.search(line)
                    if m:
                        return m.group(1)
        except Exception:
            continue
    return ""  # 本机日志暂无记录时返回空：entitlement 推送会省略该可选字段


ZCODE_START_PLAN_CONNECTION_KEY = _latest_start_plan_connection_key()

# 计划直连模式优先（有 JWT 即用计划通道，吃订阅/活动额度）；API Key 模式仅作后备。
if ZCODE_PLAN_CREDENTIAL:
    forced = _engine_setting("ZCODE_PROVIDER_ID", "zcode_provider_id", "")
    DEFAULT_PROVIDER_ID = forced if forced.startswith("account:") else "account:zai-start-plan"

# bot-facing default models (display order matters for /model panel)
ZCODE_MODEL_ALIASES = {
    "glm-4.7-flash": "glm-4.7-flash",
    "glm-5.3": "GLM-5.3",
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-5": "GLM-5",
}

# effort -> zcode reasoningLevel (zcode tiers: low / high / max)
EFFORT_TO_REASONING = {"low": "low", "medium": "high", "high": "max"}

# zcode tool name -> upstream tool name (so card rendering + fast-path sets match)
ZCODE_TOOL_ALIASES = {
    "Bash": "run_command",
    "Read": "view_file",
    "Write": "write_to_file",
    "Edit": "replace_file_content",
    "NotebookEdit": "replace_file_content",
    "Glob": "find_by_name",
    "Grep": "grep_search",
    "WebFetch": "read_url_content",
    "WebSearch": "search_web",
    "TodoWrite": "manage_task",
    "Task": "invoke_subagent",
    "Agent": "invoke_subagent",
    "AskUserQuestion": "ask_question",
    "ListModels": "manage_task",
}

TOOL_PARAM_KEYS = ("input", "parameters", "args", "arguments")


def normalize_tool_name(name: str) -> str:
    if not name:
        return ""
    return ZCODE_TOOL_ALIASES.get(name, name)


def parse_model_selection(bot_model: str, effort: str = "medium") -> Dict[str, Any]:
    """Translate a bot-side model id (+effort) into a zcode modelSelection dict."""
    m = (bot_model or "").strip()
    # "apifree:" 前缀：强制走 zai-api 个人 provider（Coding Plan API Key 通道）
    api_free = m.lower().startswith("apifree:")
    if api_free:
        m = m[len("apifree:"):]
    base = m.lower()
    reasoning = ""
    for suffix in ("-xhigh", "-max", "-high", "-medium", "-low"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            effort_hint = suffix.lstrip("-")
            reasoning = EFFORT_TO_REASONING.get(
                effort_hint if effort_hint != "xhigh" and effort_hint != "max" else "high",
                "max" if effort_hint in ("xhigh", "max") else "high",
            )
            break
    model_id = ZCODE_MODEL_ALIASES.get(base)
    if model_id is None:
        # Maybe the user configured a raw zcode model id (e.g. "GLM-5.3-Flash")
        for alias, real in ZCODE_MODEL_ALIASES.items():
            if base == real.lower():
                model_id = real
                break
    if model_id is None:
        model_id = DEFAULT_MODEL_ID
    if not reasoning:
        reasoning = EFFORT_TO_REASONING.get((effort or "medium").lower(), "high")
    # Per-family valid reasoningLevel values (from zcode builtin provider-site rules):
    #   glm-5.3 / glm-5.3-flash -> low|high|max ; glm-5.2 -> high|max ;
    #   glm-5.1 -> enabled ; default spec -> disabled|enabled
    m = model_id.lower()
    if m.startswith(("glm-5.3",)):
        level = reasoning if reasoning in ("low", "high", "max") else "high"
    elif m.startswith("glm-5.2"):
        level = "max" if reasoning == "max" else "high"
    elif m.startswith("glm-5.1"):
        level = "enabled"
    else:
        level = "disabled" if reasoning == "low" else "enabled"
    provider = "zai-api" if api_free else DEFAULT_PROVIDER_ID
    return {
        "providerId": provider,
        "modelId": model_id,
        "options": {"reasoningLevel": level},
    }


# ---------------------------------------------------------------------------
# Transcript journal (upstream-compatible) — see config.get_transcript_path
# ---------------------------------------------------------------------------


class TranscriptJournal:
    """Appends upstream-shaped transcript.jsonl entries for one zcode session."""

    def __init__(self, conv_id: str):
        from config import get_transcript_path

        self.path = get_transcript_path(conv_id)
        self.enabled = bool(self.path)
        if self.path:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
            except Exception:
                self.enabled = False

    def _write(self, obj: Dict[str, Any]):
        if not self.enabled:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception as e:
            log.debug(f"[ZcodeProtocol] journal write failed: {e}")

    def write_user_input(self, text: str):
        self._write({"type": "USER_INPUT", "content": text or ""})

    def write_planner_response(self, text: str, tool_calls=None, done: bool = False):
        entry: Dict[str, Any] = {
            "type": "PLANNER_RESPONSE",
            "source": "MODEL",
            "content": text or "",
            "timestamp": int(time.time() * 1000),
        }
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if done:
            entry["status"] = "DONE"
        self._write(entry)


# ---------------------------------------------------------------------------
# Protocol client
# ---------------------------------------------------------------------------


class ZcodeProtocolClient:
    """Async client for one `zcode app-server` child process."""

    def __init__(
        self,
        bin_argv,
        cwd: Optional[str] = None,
        on_user_input_request=None,
        on_permission_request=None,
        env: Optional[Dict[str, str]] = None,
    ):
        self.bin_argv = list(bin_argv)
        self.cwd = cwd
        self.env = env
        self.process: Optional[asyncio.subprocess.Process] = None
        self._next_id = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._buf = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._stderr_task = None
        self._reader_task = None
        self.recent_stderr = ""
        # server->client interaction hooks
        self.on_user_input_request = on_user_input_request  # async fn(request) -> value
        self.on_permission_request = on_permission_request  # async fn(request) -> {"decision": "allow"|"deny"}
        self.session_id: str = ""
        self._closing = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self):
        if self.is_alive():
            return
        env = os.environ.copy() if self.env is None else {**os.environ, **self.env}
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        # Trust the local HTTPS API proxy certificate (api_proxy.py) when present.
        _cert = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", "proxy.crt")
        if os.path.exists(_cert) and not env.get("NODE_EXTRA_CA_CERTS"):
            env["NODE_EXTRA_CA_CERTS"] = _cert
        env.setdefault("DEBIAN_FRONTEND", "noninteractive")
        env.setdefault("GIT_ASKPASS", "echo")
        env.setdefault("PYTHONUNBUFFERED", "1")
        log.info(f"[ZcodeProtocol] spawning app-server: {' '.join(self.bin_argv[:3])} ...")
        self.process = await asyncio.create_subprocess_exec(
            *self.bin_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,
            cwd=self.cwd,
            env=env,
        )
        self._closing = False
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def close(self):
        self._closing = True
        try:
            if self.session_id and self.is_alive():
                await asyncio.wait_for(self.request("session/close", {"sessionId": self.session_id}), timeout=3)
        except Exception:
            pass
        proc = self.process
        self.process = None
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        self._reader_task = self._stderr_task = None
        if proc and proc.returncode is None:
            try:
                import signal as _signal

                os.killpg(os.getpgid(proc.pid), _signal.SIGTERM)
                await asyncio.sleep(0.2)
                if proc.returncode is None:
                    os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # -- wire ---------------------------------------------------------------

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 60.0):
        """Send a client->server request and await its response result."""
        if not self.is_alive():
            raise ConnectionError("zcode app-server process is not running")
        rid = self._next_id
        self._next_id += 1
        payload = {"id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            self.process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.process.stdin.drain()
        except Exception as e:
            self._pending.pop(rid, None)
            raise ConnectionError(f"failed writing to app-server: {e}")
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(f"zcode app-server did not answer {method} within {timeout}s")

    def _respond(self, rid, result):
        """Answer a server->client request."""
        try:
            self.process.stdin.write((json.dumps({"id": rid, "result": result}, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            pass

    async def _read_stdout(self):
        try:
            while self.process and self.process.returncode is None:
                chunk = await self.process.stdout.read(8192)
                if not chunk:
                    break
                self._buf += self._decoder.decode(chunk)
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except Exception:
                        log.debug(f"[ZcodeProtocol] non-json stdout: {line[:160]}")
                        continue
                    try:
                        await self._handle_message(msg)
                    except Exception as e:
                        log.warning(f"[ZcodeProtocol] message handling error: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning(f"[ZcodeProtocol] stdout reader crashed: {e}")

    async def _drain_stderr(self):
        try:
            while self.process and self.process.returncode is None:
                chunk = await self.process.stderr.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                if text:
                    self.recent_stderr = (self.recent_stderr + text)[-32768:]
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _handle_message(self, msg: Dict[str, Any]):
        has_id = msg.get("id") is not None
        is_response = ("result" in msg) or ("error" in msg)
        if has_id and is_response:
            fut = self._pending.pop(int(msg["id"]), None) if isinstance(msg["id"], int) else None
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(json.dumps(msg["error"], ensure_ascii=False)[:600]))
                else:
                    fut.set_result(msg.get("result"))
            return
        # server -> client request
        if has_id and msg.get("method"):
            await self._handle_server_request(msg)
            return
        # notification
        method = msg.get("method")
        if method:
            params = msg.get("params") or {}
            await self._handle_notification(method, params)

    # -- server->client requests -------------------------------------------

    async def _handle_server_request(self, msg: Dict[str, Any]):
        method = msg.get("method", "")
        rid = msg.get("id")
        params = msg.get("params") or {}
        if method == "session/requestRuntimePreferences":
            self._respond(rid, {
                "additionalDirectories": [],
                "nativeSearchEnhancementsEnabled": False,
                "toolSearchMode": "off",
                "memoryEnabled": False,
                "askUserQuestionAutoResolutionEnabled": True,
                "modelContextBudgetStrategy": "preflight-v1",
            })
            return
        if method == "interaction/requestUserInput":
            value = None
            if self.on_user_input_request is not None:
                try:
                    value = await self.on_user_input_request(params)
                except Exception as e:
                    log.warning(f"[ZcodeProtocol] user-input hook failed: {e}")
            # Auto-answer so headless turns never deadlock; the Feishu choice
            # card (if any) is rendered from the journalled ask_question event.
            if value is None:
                input_type = params.get("inputType", "text")
                if input_type == "choice" and params.get("choices"):
                    value = params["choices"][0]
                else:
                    value = ""
            self._respond(rid, value)
            return
        if method == "interaction/requestPermission":
            decision = None
            if self.on_permission_request is not None:
                try:
                    decision = await self.on_permission_request(params)
                except Exception as e:
                    log.warning(f"[ZcodeProtocol] permission hook failed: {e}")
            if decision is None:
                # Default-deny keeps unattended sessions safe; yolo mode rarely asks.
                decision = {"decision": "deny", "reason": "Feishu bot headless auto-deny"}
            self._respond(rid, decision)
            return
        if method == "interaction/requestProviderRuntimeHeaders":
            provider = str((params or {}).get("providerId", ""))
            if provider.startswith("account:"):
                # 计划通道：JWT（zcodejwttoken）是桌面端同款凭证
                credential = ZCODE_PLAN_CREDENTIAL or ZCODE_API_KEY
            else:
                # API 通道： Coding Plan API Key
                credential = ZCODE_API_KEY or ZCODE_PLAN_CREDENTIAL
            if credential:
                self._respond(rid, {
                    "headersApplied": True,
                    "requestAuth": {"apiKey": credential},
                })
            else:
                self._respond(rid, {"headersApplied": False})
            return
        if method == "interaction/requestOfficialMcpAuthHeaders":
            self._respond(rid, {})
            return
        # Unknown server request: answer with an empty object to avoid stalls.
        log.debug(f"[ZcodeProtocol] unanswered server request {method} -> {{}}")
        self._respond(rid, {})

    # -- notifications (session events) ------------------------------------

    async def _handle_notification(self, method: str, params: Dict[str, Any]):
        # event notifications carry their type both in method and params.type
        ev_type = params.get("type") or method
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        if method == "state.updated":
            return
        if method.startswith(("startup/", "process/", "v4/", "computer-use/")):
            return
        handler = getattr(self, f"_on_{re.sub(r'[^a-zA-Z0-9_]', '_', ev_type)}", None)
        if handler is not None:
            await handler(payload or params)
        else:
            log.debug(f"[ZcodeProtocol] unhandled event {ev_type}")


# ---------------------------------------------------------------------------
# Session wrapper used by session_pool.PersistentSession
# ---------------------------------------------------------------------------


class ZcodeAgentSession:
    """One zcode session bound to one Feishu chat, exposing the upstream event API."""

    def __init__(
        self,
        bin_argv,
        chat_id: str,
        model: str,
        project_dir: Optional[str] = None,
        effort: str = "medium",
        sandbox_enabled: bool = False,
        conversation_id: str = "",
        on_user_input_request=None,
        tool_denylist=None,
    ):
        self.chat_id = chat_id
        self.model = model
        self.effort = effort if effort in ("low", "medium", "high") else "medium"
        self.sandbox_enabled = sandbox_enabled
        self.project_dir = project_dir
        self.conversation_id = conversation_id  # zcode sessionId (sess_...)
        self.tool_denylist = list(tool_denylist or [])
        self.client = ZcodeProtocolClient(
            bin_argv,
            cwd=project_dir,
            on_user_input_request=on_user_input_request,
        )
        self.journal: Optional[TranscriptJournal] = None
        self._subscribed = False
        self._event_listeners = []
        self._accumulated_turn_text = ""

    # -- lifecycle ---------------------------------------------------------

    async def start(self):
        await self.client.start()
        ws_path = self.project_dir or os.path.expanduser(
            os.environ.get("ZCODE_WORKSPACE_ROOT", "~")
        )
        try:
            os.makedirs(ws_path, exist_ok=True)
        except Exception:
            pass
        # Plan-direct mode: push the desktop-identical entitled account config
        # so the registry materializes start-plan models (GLM-5.3 family).
        if ZCODE_PLAN_CREDENTIAL:
            try:
                await self.client.request("provider/updateAccountConfig", {
                    "revision": f"bot-{int(time.time())}",
                    "basedOnZCodeBuiltinRevision": ZCODE_BUILTIN_REVISION_ID,
                    "providers": {
                        "account:zai-start-plan": {
                            "builtinModelIds": ["GLM-5.3", "GLM-5.3-Flash"],
                            "access": {"type": "zhipu-account", "entitled": True},
                        },
                    },
                    "states": {
                        "account:zai-start-plan": {
                            "availability": "available",
                            "entitled": True,
                            "current": True,
                            **({"connectionKey": ZCODE_START_PLAN_CONNECTION_KEY}
                               if ZCODE_START_PLAN_CONNECTION_KEY else {}),
                        },
                    },
                }, timeout=30)
                await self.client.request("provider/refreshRegistry", {"reason": "bot-plan-direct"}, timeout=30)
            except Exception as e:
                log.warning(f"[ZcodeSession] plan account config push failed: {e}")

        result = None
        if self.conversation_id:
            # 恢复已有会话：session/resume（session/create 带 sessionId 会报 already exists）
            try:
                result = await self.client.request("session/resume", {
                    "sessionId": self.conversation_id,
                    "workspace": {"workspacePath": ws_path, "workspaceKey": ws_path},
                }, timeout=60)
            except Exception as e:
                log.info(f"[ZcodeSession] resume {self.conversation_id} failed ({str(e)[:100]}), creating new session")
                result = None
        if result is None:
            create_params: Dict[str, Any] = {
                "workspace": {"workspacePath": ws_path, "workspaceKey": ws_path},
                "mode": "build" if self.sandbox_enabled else "yolo",
                "model": parse_model_selection(self.model, self.effort),
            }
            if self.tool_denylist:
                create_params["toolDenylist"] = self.tool_denylist
            result = await self.client.request("session/create", create_params, timeout=90)
        session_obj = (result or {}).get("session") or {}
        new_sid = session_obj.get("sessionId") or ""
        if not new_sid:
            raise RuntimeError(f"session/create returned no sessionId: {json.dumps(result, ensure_ascii=False)[:300]}")
        if self.conversation_id != new_sid:
            self.conversation_id = new_sid
        self.client.session_id = new_sid
        if not self.conversation_id.startswith("sess_"):
            self.conversation_id = new_sid
        self.journal = TranscriptJournal(self.conversation_id)
        await self.client.request(
            "session/subscribe",
            {"sessionId": new_sid, "deliveryKind": "desktop-continuous", "includeSnapshot": False},
            timeout=30,
        )
        self._subscribed = True
        self._wire_client_events()
        log.info(
            f"[ZcodeSession] ready for chat {self.chat_id}: session={new_sid} "
            f"model={self.model} effort={self.effort} sandbox={self.sandbox_enabled}"
        )
        return new_sid

    def _wire_client_events(self):
        """Attach translation handlers onto the raw protocol client."""

        async def _emit(event: Dict[str, Any]):
            for listener in list(self._event_listeners):
                try:
                    await listener(event)
                except Exception:
                    pass

        client = self.client

        async def user_input_hook(params: Dict[str, Any]):
            # journal ask_question so extract_choice_card_data() finds it
            if self.journal is not None:
                self.journal.write_planner_response(
                    "",
                    tool_calls=[{
                        "name": "ask_question",
                        "args": {
                            "questions": [{
                                "question": params.get("prompt", "请选择："),
                                "options": list(params.get("choices") or []),
                            }]
                        },
                    }],
                )
            return None  # auto-answer handled by ZcodeProtocolClient

        client.on_user_input_request = user_input_hook

        orig_handle_notification = client._handle_notification

        async def handle_notification(method: str, params: Dict[str, Any]):
            ev_type = params.get("type") or method
            payload = params.get("payload") if isinstance(params.get("payload"), dict) else params
            now_ms = int(time.time() * 1000)
            if ev_type == "turn.started":
                await _emit({"event": "init", "conversation_id": self.conversation_id, "timestamp": now_ms})
                if self.journal is not None:
                    # a new USER_INPUT marker will be written by send_prompt; nothing else needed
                    pass
            elif ev_type == "part.delta":
                field = payload.get("field") or "text"
                delta = payload.get("delta") or ""
                if field == "text" and delta:
                    self._accumulated_turn_text += delta
                    await _emit({
                        "event": "step_update",
                        "step_update": {"step_type": "agent_response", "text_delta": delta},
                    })
                elif field == "reasoning" and delta:
                    # 深度思考流：不进正文，但必须作为心跳重置执行器看门狗，
                    # 否则 GLM-5.3 长思考（>2 分钟无 text 输出）会被误判卡死
                    await _emit({
                        "event": "step_update",
                        "step_update": {"step_type": "reasoning", "text_delta": delta},
                    })
                elif field in ("input", "output") and delta:
                    # 工具流式输入/输出：同样作为心跳
                    await _emit({
                        "event": "step_update",
                        "step_update": {"step_type": "tool_stream", "text_delta": ""},
                    })
            elif ev_type == "part.upserted" or ev_type == "part.started":
                part = payload.get("part") or {}
                if part.get("type") == "tool":
                    await self._emit_tool_event(_emit, part)
            elif ev_type == "tool.updated":
                await self._emit_tool_event(_emit, payload)
            elif ev_type == "turn.completed":
                response = payload.get("response")
                if response is None:
                    response = self._extract_response_from_last_message()
                if self.journal is not None:
                    self.journal.write_planner_response(response or "", done=True)
                self._accumulated_turn_text = ""
                await _emit({
                    "event": "result",
                    "result": {"status": "SUCCESS", "response": response or ""},
                })
            elif ev_type == "turn.failed":
                err = payload.get("error") or {}
                message = err.get("message") or payload.get("reason") or "zcode turn failed"
                if self.journal is not None:
                    self.journal.write_planner_response(f"[error] {message}", done=True)
                self._accumulated_turn_text = ""
                await _emit({
                    "event": "result",
                    "result": {"status": "ERROR_QUOTA_EXHAUSTED" if "quota" in str(message).lower() else "ERROR_OTHER", "error": message},
                })
            elif ev_type == "model.streaming":
                # 引擎生成心跳：任何模型侧活动都重置执行器看门狗
                await _emit({
                    "event": "step_update",
                    "step_update": {"step_type": "model_streaming", "text_delta": ""},
                })
            else:
                await orig_handle_notification(method, params)

        client._handle_notification = handle_notification

    async def _emit_tool_event(self, _emit, payload: Dict[str, Any]):
        tool_name = payload.get("toolName") or payload.get("tool") or ""
        kind = payload.get("kind") or payload.get("state") or ""
        call_id = payload.get("toolCallId") or payload.get("callId") or ""
        input_params = None
        for key in TOOL_PARAM_KEYS:
            if isinstance(payload.get(key), dict):
                input_params = payload[key]
                break
        if input_params is None and isinstance(payload.get("part"), dict):
            part = payload["part"]
            tool_name = tool_name or part.get("tool") or ""
            if isinstance(part.get("input"), dict):
                input_params = part["input"]
        if input_params is None:
            input_params = {}
        # map states: scheduled/started/running => ACTIVE; else DONE
        state = "DONE" if str(kind).lower() in ("done", "completed", "finished", "success", "error", "failed", "cancelled") else "ACTIVE"
        await _emit({
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": state,
                "tool_name": normalize_tool_name(tool_name),
                "call_id": call_id,
                "tool_info": {"parameters": input_params},
                "background": bool(payload.get("background")),
            },
        })

    def _extract_response_from_last_message(self) -> str:
        """Fallback: use accumulated streamed text when turn.completed has no response."""
        return self._accumulated_turn_text.strip()

    # -- turn execution ------------------------------------------------------

    async def send_prompt_and_stream(self, prompt_text: str, system_note: str = ""):
        """Send one user turn; returns an async iterator-ish event queue.

        Compatible with the TurnEventStream contract in session_pool.py.
        """
        if not self.client.is_alive():
            await self.start()
        queue: asyncio.Queue = asyncio.Queue()

        async def listener(event: Dict[str, Any]):
            await queue.put(event)

        self._event_listeners.append(listener)
        if self.journal is not None:
            self.journal.write_user_input(prompt_text)
        self._accumulated_turn_text = ""
        ack = await self.client.request(
            "session/send",
            {
                "sessionId": self.conversation_id,
                "content": prompt_text,
                "modelSelection": parse_model_selection(self.model, self.effort),
            },
            timeout=120,
        )
        log.debug(f"[ZcodeSession] send ack: {json.dumps(ack, ensure_ascii=False)[:200]}")

        return _QueueTurnStream(queue, listener, self)

    async def set_model(self, model: str, effort: str = "medium"):
        self.model = model
        self.effort = effort
        if not self.client.is_alive() or not self.conversation_id:
            return
        try:
            await self.client.request(
                "session/setModel",
                {"sessionId": self.conversation_id, "model": parse_model_selection(model, effort)},
                timeout=30,
            )
        except Exception as e:
            log.warning(f"[ZcodeSession] setModel failed: {e}")

    async def stop_turn(self):
        if self.client.is_alive() and self.conversation_id:
            try:
                await self.client.request("session/stop", {"sessionId": self.conversation_id}, timeout=10)
            except Exception as e:
                log.warning(f"[ZcodeSession] stop failed: {e}")

    async def close(self):
        self._event_listeners.clear()
        await self.client.close()


class _QueueTurnStream:
    """Mirror of session_pool.TurnEventStream (same get_event/drain_all API)."""

    _TERMINAL_EVENTS = {"result", "error", "turn_error", "cancelled", "fatal", "process_exit", "read_error"}

    def __init__(self, queue: asyncio.Queue, listener, session: ZcodeAgentSession):
        self._queue = queue
        self._listener = listener
        self._session = session
        self.is_done = False

    async def get_event(self, timeout=None):
        if self.is_done:
            return None
        try:
            if timeout is not None:
                event = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            else:
                event = await self._queue.get()
            if event.get("event") in self._TERMINAL_EVENTS:
                self.is_done = True
            return event
        except asyncio.TimeoutError:
            return None

    def drain_all(self):
        events = []
        while not self.is_done and not self._queue.empty():
            try:
                ev = self._queue.get_nowait()
                events.append(ev)
                if ev.get("event") in self._TERMINAL_EVENTS:
                    self.is_done = True
                    break
            except asyncio.QueueEmpty:
                break
        return events

    async def aclose(self):
        try:
            self._session._event_listeners.remove(self._listener)
        except ValueError:
            pass
