"""ZCode Feishu Bot entrypoint."""
import asyncio
import base64
import fcntl
import http
import json
import os
import signal
import sys
import threading
import time

# Ensure common user-local bin paths are available under PM2 / non-login shells
_home = os.path.expanduser("~")
os.environ["PATH"] += (
    os.pathsep + os.path.join(_home, ".npm-global/bin")
    + os.pathsep + os.path.join(_home, ".local/bin")
)

import lark_oapi as lark

from config import APP_ID, APP_SECRET, BASE_DIR
from logger import log
from lark_client import send_reply_sdk
from garbage_collection import garbage_collector
import app_state
from handlers import do_p2_im_message_receive_v1, do_p2_card_action_trigger

# ---------------------------------------------------------------------------
# Patch: lark-oapi 1.6.8 ws client drops card-action frames.
# The WebSocket client returns early for MessageType.CARD, so card button
# callbacks (card.action.trigger) get no response and Feishu reports
# "出错了，请稍候重试 code:200671". Route CARD frames through the same event
# dispatcher as EVENT frames. Safe on newer SDK versions too (same envelope).
# ---------------------------------------------------------------------------
from lark_oapi.ws import client as _ws_client_mod
from lark_oapi.ws.const import (
    HEADER_BIZ_RT,
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TRACE_ID,
    HEADER_TYPE,
)
from lark_oapi.ws.enum import MessageType as _WsMessageType
from lark_oapi.ws.model import Response as _WsResponse
from lark_oapi.core.json import JSON as _WsJSON
from lark_oapi.core.const import UTF_8 as _UTF8


async def _patched_handle_data_frame(self, frame):
    hs = frame.headers
    msg_id = _ws_client_mod._get_by_key(hs, HEADER_MESSAGE_ID)
    trace_id = _ws_client_mod._get_by_key(hs, HEADER_TRACE_ID)
    sum_ = _ws_client_mod._get_by_key(hs, HEADER_SUM)
    seq = _ws_client_mod._get_by_key(hs, HEADER_SEQ)
    type_ = _ws_client_mod._get_by_key(hs, HEADER_TYPE)

    pl = frame.payload
    if int(sum_) > 1:
        pl = self._combine(msg_id, int(sum_), int(seq), pl)
        if pl is None:
            return

    message_type = _WsMessageType(type_)
    resp = _WsResponse(code=http.HTTPStatus.OK)
    try:
        start = int(round(time.time() * 1000))
        if message_type == _WsMessageType.CARD:
            _ws_client_mod.logger.info(
                self._fmt_log("CARD frame received, dispatching card callback, msg_id: {}", msg_id)
            )
            result = self._event_handler._do_without_validation(pl)
        elif message_type == _WsMessageType.EVENT:
            result = self._event_handler._do_without_validation(pl)
        else:
            return
        end = int(round(time.time() * 1000))
        header = hs.add()
        header.key = HEADER_BIZ_RT
        header.value = str(end - start)
        if result is not None:
            resp.data = base64.b64encode(_WsJSON.marshal(result).encode(_UTF8))
    except Exception as e:
        _ws_client_mod.logger.error(
            self._fmt_log(
                "handle message failed, message_type: {}, message_id: {}, trace_id: {}, err: {}",
                message_type.value,
                msg_id,
                trace_id,
                e,
            )
        )
        # 输出 payload 摘要，便于定位 CARD 帧结构与 SDK 预期不一致的问题
        try:
            preview = pl.decode("utf-8", errors="replace")[:300]
            _ws_client_mod.logger.error(f"payload preview: {preview}")
        except Exception:
            pass
        resp = _WsResponse(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

    frame.payload = _WsJSON.marshal(resp).encode(_UTF8)
    await self._write_message(frame.SerializeToString())


_ws_client_mod.Client._handle_data_frame = _patched_handle_data_frame


# ---------------------------------------------------------------------------
# 反僵尸三重防线：
#   1. flock 单实例锁 —— 第二个实例启动即退出（锁随进程死亡自动释放）
#   2. 主循环看门狗 —— 事件循环一旦死亡，1 秒内进程整体退出交给 PM2 重启
#   3. 硬退出兜底 —— main() 无论正常返回还是异常，都强制退出进程，
#      杜绝"WS 线程还在、主循环已死"的半死状态吃消息
# ---------------------------------------------------------------------------
_LOCK_FH = None


def _acquire_single_instance_lock():
    """flock 单实例锁；已有存活实例时立即退出。"""
    global _LOCK_FH
    lock_path = os.path.join(BASE_DIR, ".bot.lock")
    # O_RDWR|O_CREAT 不截断文件：避免抢锁失败时抹掉持有者 PID
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    _LOCK_FH = os.fdopen(fd, "r+")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FH.seek(0)
        _LOCK_FH.truncate()
        _LOCK_FH.write(str(os.getpid()))
        _LOCK_FH.flush()
        return True
    except OSError:
        try:
            _LOCK_FH.seek(0)
            other_pid = _LOCK_FH.read().strip() or "?"
        except Exception:
            other_pid = "?"
        print(f"[SingleInstance] 已有实例在运行 (PID {other_pid})，本实例退出。", flush=True)
        return False


def _start_loop_watchdog():
    """守护线程：主事件循环死亡 → 立即退出进程（PM2 拉起全新实例）。"""
    def _watch():
        while True:
            time.sleep(1)
            loop = app_state.main_loop
            if loop is not None and not loop.is_running():
                print("[Watchdog] main loop 已死亡，进程退出等待守护重启", flush=True)
                os._exit(1)
    threading.Thread(target=_watch, daemon=True, name="loop-watchdog").start()


async def main():
    app_state.main_loop = asyncio.get_running_loop()
    log.info("Starting Lark WS Client...")

    # Send post-update notification if applicable
    try:
        from database import get_and_clear_pending_update_notice
        from commands import get_version_string
        from cards.common import create_footer
        from lark_client import send_interactive_card_sdk, send_reply_sdk
        
        data = None
        pending_file = os.path.join(BASE_DIR, ".update_pending.json")
        if os.path.exists(pending_file):
            try:
                with open(pending_file, "r") as f:
                    data = json.load(f)
                os.remove(pending_file)
                try:
                    with open(os.path.join(BASE_DIR, ".update_buzzer_pending"), "w") as bf:
                        bf.write("pending")
                except Exception:
                    pass
            except Exception as e:
                log.error(f"Error reading .update_pending.json: {e}")

        if not data:
            data = await asyncio.get_running_loop().run_in_executor(None, get_and_clear_pending_update_notice)

        if data:
            message_id = data.get("message_id")
            old_ver = data.get("old_version", "")
            if message_id:
                new_ver = await asyncio.get_running_loop().run_in_executor(None, lambda: get_version_string("HEAD"))
                ver_info = f"从 ~`{old_ver}`~ 升级至 **`{new_ver}`**" if old_ver else f"**`{new_ver}`**"
                card = {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "template": "green",
                        "title": {"content": "🎉 系统升级成功！", "tag": "plain_text"}
                    },
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": f"✅ **核心代码与服务组件已成功热升级！**\n\n> 🔖 **当前版本**：{ver_info}\n> 🔄 **运行状态**：后台 PM2 进程已完成自动重启与加载。\n\n系统所有功能与指令插件均已恢复，欢迎继续使用！"
                        },
                        create_footer()
                    ]
                }
                res = await asyncio.get_running_loop().run_in_executor(None, lambda: send_interactive_card_sdk(message_id, card))
                if not res:
                    fallback_text = f"🎉 系统升级成功！当前运行版本：{new_ver}"
                    await asyncio.get_running_loop().run_in_executor(None, lambda: send_reply_sdk(message_id, fallback_text))
                log.info(f"[PostUpdate] Notification sent to message_id {message_id}")
    except Exception as e:
        log.error(f"Failed to process post-update notification: {e}")

    # Local z.ai API streaming proxy (unsigned plain-Bearer path for the engine)
    try:
        import api_proxy
        port = api_proxy.start_proxy_thread()
        log.info(f"[ApiProxy] local engine API proxy listening on 127.0.0.1:{port}")
    except Exception as e:
        log.warning(f"[ApiProxy] failed to start local API proxy: {e}")

    # Register system commands & load plugins
    from plugin_manager import plugin_manager
    plugin_manager.register_system_commands([
        "/help", "/model", "/card", "/menu", "/project", "/note", "/notes",
        "/status", "/context", "/clear", "/stop", "/update", "/ping",
        "/newproj_resolve", "/cron", "/schedule", "/plugin", "/plugins", "/user",
        "/brain", "/auth", "/s1", "/sys1", "/system1", "/plan"
    ])
    plugin_manager.load_all_plugins()

    # Start background GC task
    asyncio.create_task(garbage_collector())

    # Start background Cron scheduler engine
    from cron_engine import cron_engine
    cron_engine.start()

    # Restore queued tasks persisted across a previous process lifetime
    await restore_pending_tasks()

    _start_loop_watchdog()

    dispatcher_builder = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .register_p2_card_action_trigger(do_p2_card_action_trigger)
    )
    # 已读回执事件无需处理，注册空处理器避免 SDK "processor not found" 噪音
    try:
        def _noop_message_read(data):
            return None
        dispatcher_builder = dispatcher_builder.register_p2_im_message_read_v1(_noop_message_read)
    except Exception:
        pass
    event_handler = dispatcher_builder.build()

    cli = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )

    loop = asyncio.get_running_loop()
    # lark-oapi >= 1.7 的 ws Client.start() 可能启动后台线程后立即返回或抛错；
    # 无论哪种情况主协程都必须保活，否则事件循环关闭后所有消息都会被丢弃。
    def _run_ws_client():
        try:
            cli.start()
            log.warning("[Main] lark ws client returned from start(); keeping main loop alive")
        except Exception as e:
            log.error(f"[Main] lark ws client crashed: {e}; keeping main loop alive for reconnect")
    await loop.run_in_executor(None, _run_ws_client)
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass


async def restore_pending_tasks():
    """Re-queue tasks persisted in SQLite after a restart/crash.
    Interrupted tasks will be resumed with retry protection."""
    from database import delete_pending_task, save_pending_task, load_pending_tasks
    from handlers.pipeline import process_chat_queue
    from card_builder import CardBuilder
    from lark_client import send_interactive_card_sdk, patch_interactive_card_sdk

    tasks = load_pending_tasks()
    if not tasks:
        return

    recovered = {}
    loop = asyncio.get_running_loop()

    for chat_id, task, created_at in tasks:
        retry_count = task.get("retry_count", 0)
        message_id = task.get("message_id", "")
        bot_reply_msg_id = task.get("bot_reply_msg_id")

        if retry_count >= 2:
            # 连续多次重启仍然失败的任务，丢弃以防崩溃循环，并发送友好卡片通知
            delete_pending_task(chat_id, created_at)
            log.warning(f"[restore] Task for chat {chat_id} (msg {message_id}) exceeded max retry count ({retry_count}), dropping.")
            err_card = CardBuilder.build_ai_response(
                "⚠️ **任务未能恢复**\n\n该任务在执行过程中导致服务异常重启超过上限，系统已自动保护并终止该任务。如需继续请重新发送指令。",
                is_error=True
            )
            try:
                if bot_reply_msg_id:
                    await loop.run_in_executor(None, lambda: patch_interactive_card_sdk(bot_reply_msg_id, err_card))
                elif message_id:
                    await loop.run_in_executor(None, lambda: send_interactive_card_sdk(message_id, err_card))
            except Exception as e:
                log.error(f"[restore] Failed to send abort notification: {e}")
            continue

        # 标记为断点恢复任务，递增重试计数并持久化
        task["retry_count"] = retry_count + 1
        task["resumed"] = True
        save_pending_task(chat_id, task)
        recovered.setdefault(chat_id, []).append(task)

    total = 0
    for chat_id, items in recovered.items():
        if chat_id not in app_state.chat_queues:
            app_state.chat_queues[chat_id] = asyncio.Queue()
        for t in items:
            await app_state.chat_queues[chat_id].put(t)
            total += 1
        if chat_id not in app_state.chat_workers or app_state.chat_workers[chat_id].done():
            app_state.chat_workers[chat_id] = asyncio.create_task(process_chat_queue(chat_id))

    log.info(f"[restore] recovered {total} pending tasks across {len(recovered)} chats for automatic resumption")


def cleanup(signum, frame):
    log.warning("Gracefully shutting down... killing zombie processes")
    try:
        from plugin_manager import plugin_manager
        plugin_manager.dispatch_service_restarting()
    except Exception:
        pass
    for process in app_state.running_processes.values():
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as e:
            log.error(f"Failed to kill process group {process.pid}: {e}")
            try:
                process.kill()
            except Exception:
                pass
    sys.exit(0)


if __name__ == "__main__":
    if not _acquire_single_instance_lock():
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    _exit_code = 0
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[Fatal] main() 异常退出: {e}", flush=True)
        _exit_code = 1
    # 无论何种路径结束，都强制退出进程：绝不留半死实例（PM2 会拉起新实例）
    os._exit(_exit_code)
