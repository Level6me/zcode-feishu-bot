import os
import sys
import shutil
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Settings(BaseSettings):
    app_id: str = Field(default="", alias="APP_ID")
    feishu_app_id: str = Field(default="", alias="FEISHU_APP_ID")
    app_secret: str = Field(default="", alias="APP_SECRET")
    feishu_app_secret: str = Field(default="", alias="FEISHU_APP_SECRET")
    allowed_users: str = Field(default="", alias="ALLOWED_USERS")
    allowed_chats: str = Field(default="", alias="ALLOWED_CHATS")
    dangerously_skip_permissions: bool = Field(default=True, alias="DANGEROUSLY_SKIP_PERMISSIONS")
    workspace_root: str = Field(
        default_factory=lambda: os.path.expanduser("~"),
        alias="WORKSPACE_ROOT",
    )

    # Optional mirror URL (may embed credentials) used as /update fallback
    gitee_mirror_url: str = Field(default="", alias="GITEE_MIRROR_URL")

    # ZCode engine overrides (portable across machines & containers)
    zcode_bin: str = Field(default="", alias="ZCODE_BIN")
    zcode_home: str = Field(
        default="",
        alias="ZCODE_HOME",
        description="Root dir of zcode data (default: ~/.zcode)",
    )
    zcode_node_bin: str = Field(default="", alias="ZCODE_NODE_BIN")
    zcode_provider_id: str = Field(default="account:zai-start-plan", alias="ZCODE_PROVIDER_ID")
    zcode_model_id: str = Field(default="GLM-5.3", alias="ZCODE_MODEL_ID")
    zcode_api_key: str = Field(default="", alias="ZCODE_API_KEY")

    # Optional default model configuration (bot-facing id, e.g. glm-5.3-flash-high)
    default_model: str = Field(default="glm-5.3-flash-high", alias="DEFAULT_MODEL")

    # Native voice reply settings
    tts_voice: str = Field(default="zh-CN-XiaoxiaoNeural", alias="TTS_VOICE")
    enable_voice_reply: bool = Field(default=True, alias="ENABLE_VOICE_REPLY")

    # TypeSafe AI (System One Model Jev) settings
    typesafe_api_key: str = Field(default="", alias="TYPESAFE_API_KEY")
    typesafe_enabled: bool = Field(default=True, alias="TYPESAFE_ENABLED")
    typesafe_model: str = Field(default="jev-latest", alias="TYPESAFE_MODEL")
    typesafe_base_url: Optional[str] = Field(default=None, alias="TYPESAFE_BASE_URL")
    typesafe_tier: str = Field(default="gateway", alias="TYPESAFE_TIER")  # gateway | sentry | copilot
    typesafe_auto_mode: bool = Field(default=True, alias="TYPESAFE_AUTO_MODE")

    # Task stall & compile watchdog timeouts (seconds)
    stall_timeout: int = Field(default=600, alias="STALL_TIMEOUT")
    think_stall_timeout: int = Field(default=300, alias="THINK_STALL_TIMEOUT")
    stall_hard_timeout: int = Field(default=3600, alias="STALL_HARD_TIMEOUT")
    tool_execution_timeout: int = Field(default=3600, alias="TOOL_EXECUTION_TIMEOUT")

    model_config = SettingsConfigDict(
        env_file=os.path.join(BASE_DIR, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


settings = Settings()

APP_ID = settings.feishu_app_id or settings.app_id
DEFAULT_MODEL = settings.default_model or "glm-5.3-flash-high"
APP_SECRET = settings.feishu_app_secret or settings.app_secret
TTS_VOICE = settings.tts_voice or "zh-CN-XiaoxiaoNeural"
ENABLE_VOICE_REPLY = settings.enable_voice_reply

# Watchdog timeouts exports
STALL_TIMEOUT = settings.stall_timeout
THINK_STALL_TIMEOUT = settings.think_stall_timeout
STALL_HARD_TIMEOUT = settings.stall_hard_timeout
TOOL_EXECUTION_TIMEOUT = settings.tool_execution_timeout

# TypeSafe AI exports
TYPESAFE_API_KEY = settings.typesafe_api_key or os.getenv("TYPESAFE_API_KEY", "")
TYPESAFE_ENABLED = settings.typesafe_enabled
TYPESAFE_MODEL = settings.typesafe_model or "jev-latest"
TYPESAFE_BASE_URL = settings.typesafe_base_url
TYPESAFE_TIER = settings.typesafe_tier or os.getenv("TYPESAFE_TIER", "gateway")
TYPESAFE_AUTO_MODE = getattr(settings, "typesafe_auto_mode", True)

# Engine identity exports used across the bot (kept for import compatibility)
ZCODE_PROVIDER_ID = settings.zcode_provider_id or "account:zai-start-plan"
ZCODE_MODEL_ID = settings.zcode_model_id or "GLM-5.3"
ZCODE_API_KEY = (settings.zcode_api_key or os.environ.get("ZCODE_API_KEY", "")).strip()

SESSION_FILE = os.path.join(BASE_DIR, "chat_sessions.json")
PROFILE_FILE = os.path.join(BASE_DIR, "user_profiles.json")

# Bot-facing model catalogue offered by the /model panel (first entry = default).
ZCODE_MODEL_CATALOGUE = [
    ("glm-5.3-flash-low", "GLM-5.3 Flash (低思考·极速)"),
    ("glm-5.3-flash-medium", "GLM-5.3 Flash (中思考·均衡)"),
    ("glm-5.3-flash-high", "GLM-5.3 Flash (高思考·深度)"),
    ("glm-5.3-low", "GLM-5.3 (低思考·极速)"),
    ("glm-5.3-medium", "GLM-5.3 (中思考·均衡)"),
    ("glm-5.3-high", "GLM-5.3 (高思考·深度)"),
    ("glm-4.7-flash", "GLM-4.7 Flash (免费档后备)"),
]


def _default_zcode_home() -> str:
    return os.path.expanduser("~/.zcode")


def get_zcode_home() -> str:
    """Return the zcode data root (overridable via ZCODE_HOME)."""
    raw = (settings.zcode_home or "").strip()
    if raw:
        return os.path.abspath(os.path.expanduser(raw))
    return _default_zcode_home()


def get_brain_dir() -> str:
    """Conversation brain directory that holds per-conversation transcript logs.

    zcode sessions are journaled here in the upstream-compatible transcript format
    by zcode_protocol.TranscriptJournal, so every existing transcript reader
    keeps working unchanged.
    """
    return os.path.join(get_zcode_home(), "feishu-brain")


def get_transcript_path(conv_id: str) -> str:
    """Canonical transcript.jsonl path for a conversation (zcode session) id."""
    return os.path.join(
        get_brain_dir(),
        conv_id,
        ".system_generated",
        "logs",
        "transcript.jsonl",
    )


def get_oauth_token_path() -> str:
    return os.path.join(get_zcode_home(), "v2", "credentials.json")


def get_global_memory_path() -> str:
    """Global cross-session memory (zcode persistent memory index).

    zcode stores per-project memories under
      ~/.zcode/cli/memories/projects/<project>/memory/MEMORY.md
    Returns the path only if it exists; otherwise empty string signals
    "memory not configured" (the /brain card renders an empty state).
    """
    home = get_zcode_home()
    projects_dir = os.path.join(home, "cli", "memories", "projects")
    candidates = []
    if os.path.isdir(projects_dir):
        try:
            for entry in os.listdir(projects_dir):
                candidate = os.path.join(projects_dir, entry, "memory", "MEMORY.md")
                if os.path.exists(candidate):
                    candidates.append((os.path.getmtime(candidate), candidate))
        except Exception:
            pass
    if candidates:
        candidates.sort(reverse=True)  # most recently updated project first
        return candidates[0][1]
    return ""


def _find_node() -> Optional[str]:
    explicit = (settings.zcode_node_bin or "").strip()
    if explicit:
        return explicit if os.path.exists(explicit) else None
    path = shutil.which("node")
    return path


def find_zcode_bin() -> Optional[list]:
    """Locate the zcode engine and return an argv list.

    Order:
      1. ZCODE_BIN env (path to a `zcode` launcher script or the bundled cjs)
      2. `zcode` in PATH
      3. The ZCode Desktop bundle at /Applications/ZCode.app (node + zcode.cjs)
      4. Node interpreter + ~/.zcode/zcode.cjs
    """
    explicit = (settings.zcode_bin or "").strip()
    if explicit:
        path = os.path.abspath(os.path.expanduser(explicit))
        if os.path.exists(path):
            if path.endswith(".cjs"):
                node = _find_node()
                if node:
                    return [node, path]
            return [path]

    which = shutil.which("zcode")
    if which:
        return [which]

    home = os.path.expanduser("~")
    candidates = [
        "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
        os.path.join(home, ".local/bin/zcode"),
        os.path.join(home, ".zcode/zcode.cjs"),
    ]
    for c in candidates:
        if os.path.exists(c):
            if c.endswith(".cjs"):
                node = _find_node()
                if node:
                    return [node, c]
            return [c]

    # Relative to the current python executable (venv-style installs)
    if sys.executable:
        bin_dir = os.path.dirname(sys.executable)
        c = os.path.join(bin_dir, "zcode")
        if os.path.exists(c):
            return [c]
    return None


ZCODE_BIN = find_zcode_bin()


def zcode_engine_argv() -> Optional[list]:
    """Full argv prefix that spawns the zcode app-server protocol process."""
    bin_argv = find_zcode_bin()
    if not bin_argv:
        return None
    return bin_argv + ["app-server"]


def engine_ready() -> bool:
    return zcode_engine_argv() is not None


# --- Versioning Configuration ---
BASE_VERSION_PREFIX = "v3.2."
VERSION_START_COMMIT = 362  # Used to calculate patch number (commit_count - start_commit)

# --- Whitelist & Permission Configuration ---
ALLOWED_USERS = [uid.strip() for uid in settings.allowed_users.split(",") if uid.strip()]
ALLOWED_CHATS = [cid.strip() for cid in settings.allowed_chats.split(",") if cid.strip()]
DANGEROUSLY_SKIP_PERMISSIONS = settings.dangerously_skip_permissions

# --- Workspace & Project Directory Configuration ---
WORKSPACE_ROOT = settings.workspace_root

GITEE_MIRROR_URL = settings.gitee_mirror_url.strip()

BRAIN_DIR = get_brain_dir()
