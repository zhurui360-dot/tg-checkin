"""用你自己的 Telegram 账号自动回复私聊 / 群聊（Userbot）。

和 bot.py 的区别：bot.py 走 Bot API，是「机器人」在说话；这里走 MTProto
（Telethon），用的是你已经登录的账号本身，所以对方看到的是你本人在回。

默认关闭，必须在面板里显式打开。开启后读的是你的真实会话，涉及隐私，也涉及
账号风控：被举报或被判定为自动化，账号可能被限制甚至封禁。所以这里做了几层
保险——只回白名单、忽略机器人、同会话冷却、每小时上限、免打扰时段，并且
绝不回复自己发的消息（否则会自问自答死循环）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import datetime
from typing import Any

from . import db, memory
from . import bot as bot_mod
from . import telegram as tg_mod

log = logging.getLogger("tg-checkin.assistant")

_lock = threading.RLock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_last_reply: dict[str, float] = {}
_hourly: dict[str, list[float]] = {}
_state: dict[str, Any] = {
    "running": False,
    "connected": False,
    "me": "",
    "account": "",
    "started_at": "",
    "last_event": "",
    "last_error": "",
    "replies": 0,
    "skipped": 0,
    "seen": 0,
    "last_skip": "",
}

MAX_REPLY_CHARS = 3800
MAX_INPUT_CHARS = 1500


def _skip(reason: str, *, chat_id: str = "", title: str = "", author: str = "", preview: str = "") -> None:
    """记一条「没回」的原因。

    以前这里是直接 return 的，用户只看到「没回复」，查不出到底哪条规则挡住的。
    现在每条都落库 + 写进状态，面板上按时间倒序就能看到。
    """
    with _lock:
        _state["skipped"] = int(_state.get("skipped", 0)) + 1
        _state["last_skip"] = f"{time.strftime('%H:%M:%S')} {reason}"
    try:
        db.add_assistant_log(chat_id, title, author, preview, "skipped", reason)
    except Exception as exc:  # 记日志本身不该影响主流程
        log.debug("写处理流水失败：%s", exc)
    log.info("跳过消息（%s）%s %s: %s", reason, title, author, preview[:60])


def _cfg() -> dict[str, str]:
    return db.get_settings()


def _account(cfg: dict[str, str]) -> dict[str, Any] | None:
    raw = str(cfg.get("assistant_account_id") or "").strip()
    if raw.isdigit():
        row = db.get_account(int(raw))
        if row:
            return row
    return db.default_account()


def _targets(raw: str) -> list[str]:
    return [part.strip().lower().lstrip("@") for part in re.split(r"[,，\s]+", raw or "") if part.strip()]


def _chat_tags(message: Any, chat: Any) -> set[str]:
    tags = {str(getattr(chat, "id", "")).lower()}
    username = getattr(chat, "username", "") or ""
    if username:
        tags.add(str(username).lower())
    title = getattr(chat, "title", "") or ""
    if title:
        tags.add(str(title).lower())
    return {tag for tag in tags if tag}


def _in_list(tags: set[str], raw: str) -> bool:
    """名单里可以写 id、@username 或群标题，命中任意一种即算匹配。"""
    items = _targets(raw)
    if not items:
        return False
    return any(item in tags or f"@{item}" in tags for item in items)


def _quiet_now(raw: str) -> bool:
    """免打扰时段，格式 23:30-08:00，可跨天。"""
    text = (raw or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", text)
    if not match:
        return False
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    now = datetime.now()
    current = now.hour * 60 + now.minute
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _allow(cfg: dict[str, str], chat_id: str) -> bool:
    cooldown = 5.0
    try:
        cooldown = max(0.0, min(float(cfg.get("assistant_cooldown", "5") or 5), 120))
    except ValueError:
        pass
    now = time.time()
    if cooldown and now - _last_reply.get(chat_id, 0) < cooldown:
        return False
    try:
        cap = max(1, min(int(float(cfg.get("assistant_max_per_hour", "30") or 30)), 200))
    except ValueError:
        cap = 30
    recent = [t for t in _hourly.get(chat_id, []) if now - t < 3600]
    _hourly[chat_id] = recent
    return len(recent) < cap


def _mark(chat_id: str) -> None:
    now = time.time()
    _last_reply[chat_id] = now
    _hourly.setdefault(chat_id, []).append(now)


def _agent_of(cfg: dict[str, str]) -> dict[str, Any] | None:
    raw = str(cfg.get("assistant_agent_id") or "").strip()
    if raw.isdigit():
        row = db.get_agent(int(raw))
        if row:
            return row
    return db.agent_for_bot(None)


def _answer(agent: dict[str, Any] | None, chat_id: str, text: str, author: str, is_private: bool) -> str:
    # 和机器人共用同一套上下文 + 记忆，bot_id 用 -1 区分「账号回复」这条线
    messages = bot_mod._ai_messages(chat_id, text, author, is_private, -1, agent)
    return bot_mod.call_ai(messages, agent=agent)


async def _handle(client: Any, event: Any, me_id: int, me_username: str) -> None:
    cfg = _cfg()
    message = getattr(event, "message", None)
    if message is None:
        return
    text = (getattr(message, "raw_text", "") or "").strip()

    # 自己发的（包括手机端发的）绝对不能回，否则死循环。这类连日志都不记，太吵。
    if getattr(event, "out", False) or getattr(message, "out", False):
        return
    if getattr(message, "sender_id", None) == me_id:
        return

    if cfg.get("assistant_enabled") != "1":
        _skip("总开关是关的", preview=text)
        return
    if not text:
        _skip("没有文本内容（图片/贴纸/文件不回）")
        return
    if len(text) > MAX_INPUT_CHARS:
        _skip(f"文字太长（{len(text)} 字，上限 {MAX_INPUT_CHARS}）", preview=text)
        return
    if text.startswith("/"):
        _skip("是 / 命令，不当聊天处理", preview=text)
        return

    chat = await event.get_chat()
    chat_id = str(getattr(chat, "id", "") or getattr(message, "chat_id", ""))
    title = getattr(chat, "title", "") or getattr(chat, "first_name", "") or ""
    if not chat_id or chat_id == str(me_id):
        _skip("是自己的收藏夹 / 无法确定会话", preview=text)
        return
    if getattr(message, "is_channel", False) and not getattr(message, "is_group", False):
        _skip("来自频道（不是群，也不是私聊）", chat_id=chat_id, title=title, preview=text)
        return

    sender = None
    try:
        sender = await event.get_sender()
    except Exception:
        sender = None
    author = (
        getattr(sender, "first_name", "")
        or getattr(sender, "title", "")
        or title
        or "对方"
    )

    if getattr(sender, "bot", False) and cfg.get("assistant_ignore_bots", "1") == "1":
        _skip("对方是机器人，按设置忽略", chat_id=chat_id, title=title, author=author, preview=text)
        return
    if getattr(sender, "id", None) == me_id:
        return

    tags = _chat_tags(message, chat)
    if _in_list(tags, cfg.get("assistant_blacklist", "")):
        _skip("命中黑名单", chat_id=chat_id, title=title, author=author, preview=text)
        return
    whitelist = cfg.get("assistant_whitelist", "")
    if _targets(whitelist) and not _in_list(tags, whitelist):
        _skip(
            f"不在白名单（当前白名单：{whitelist[:60]}）",
            chat_id=chat_id, title=title, author=author, preview=text,
        )
        return
    if _quiet_now(cfg.get("assistant_quiet", "")):
        _skip(
            f"处于免打扰时段（{cfg.get('assistant_quiet', '')}）",
            chat_id=chat_id, title=title, author=author, preview=text,
        )
        return

    is_private = bool(getattr(message, "is_private", False))
    is_group = bool(getattr(message, "is_group", False))
    if is_private:
        if cfg.get("assistant_private", "1") != "1":
            _skip("私聊回复是关的（回复范围没勾私聊）", chat_id=chat_id, title=title, author=author, preview=text)
            return
    elif is_group:
        if cfg.get("assistant_groups", "0") != "1":
            _skip("群聊回复是关的（回复范围没勾群聊）", chat_id=chat_id, title=title, author=author, preview=text)
            return
        trigger = cfg.get("assistant_trigger", "mention")
        mentioned = bool(getattr(message, "mentioned", False)) or (
            bool(me_username) and f"@{me_username}" in text.lower()
        )
        replied_to_me = False
        if getattr(message, "is_reply", False):
            try:
                replied = await event.get_reply_message()
                replied_to_me = bool(replied and getattr(replied, "sender_id", None) == me_id)
            except Exception:
                replied_to_me = False
        if trigger == "mention" and not (mentioned or replied_to_me):
            _skip("群里没 @ 我、也不是回复我的消息", chat_id=chat_id, title=title, author=author, preview=text)
            return
        if trigger == "reply" and not replied_to_me:
            _skip("设置成只在回复我的消息时回，这条不是", chat_id=chat_id, title=title, author=author, preview=text)
            return
    else:
        _skip("既不是私聊也不是群聊", chat_id=chat_id, title=title, author=author, preview=text)
        return

    if not _allow(cfg, chat_id):
        cooldown = cfg.get("assistant_cooldown", "5")
        cap = cfg.get("assistant_max_per_hour", "30")
        _skip(
            f"触发了限流（同会话间隔 {cooldown} 秒 / 每小时最多 {cap} 条）",
            chat_id=chat_id, title=title, author=author, preview=text,
        )
        return

    agent = _agent_of(cfg)
    loop = asyncio.get_running_loop()
    with _lock:
        _state["seen"] = int(_state.get("seen", 0)) + 1
    try:
        answer = await loop.run_in_executor(None, _answer, agent, chat_id, text, author, is_private)
    except Exception as exc:
        _state["last_error"] = f"AI 调用失败：{exc}"[:200]
        log.warning("账号自动回复失败：%s", exc)
        try:
            db.add_assistant_log(chat_id, title, author, text, "error", f"AI 调用失败：{exc}"[:200])
        except Exception:
            pass
        return
    answer = (answer or "").strip()
    if not answer:
        _skip("模型返回了空内容", chat_id=chat_id, title=title, author=author, preview=text)
        return

    try:
        await client.send_message(chat, answer[:MAX_REPLY_CHARS], reply_to=getattr(message, "id", None))
    except Exception as exc:
        _state["last_error"] = f"发送失败：{exc}"[:200]
        log.warning("账号回复发送失败：%s", exc)
        try:
            db.add_assistant_log(chat_id, title, author, text, "error", f"发送失败：{exc}"[:200])
        except Exception:
            pass
        return

    _mark(chat_id)
    with _lock:
        _state["replies"] = int(_state.get("replies", 0)) + 1
        _state["last_event"] = f"{time.strftime('%H:%M:%S')} {title} · {author}"
    try:
        db.add_assistant_log(chat_id, title, author, text, "replied", "已回复", answer[:300])
    except Exception:
        pass
    scope_id = int(agent["id"]) if agent and agent.get("id") is not None else 0
    db.add_ai_message(chat_id, title, "user", author, text, bot_id=scope_id)
    db.add_ai_message(chat_id, title, "assistant", "me", answer, bot_id=scope_id)
    memory.learn_async(scope_id, chat_id, title, author, text, answer, agent=agent)


async def _serve() -> None:
    from telethon import events

    cfg = _cfg()
    account = _account(cfg)
    if not account:
        raise RuntimeError("还没有可用的 Telegram 账号，请先在「账号与登录」里登录一个")
    if not str(account.get("session_string") or "").strip():
        raise RuntimeError("这个账号还没有登录会话，请先在「账号与登录」里扫码登录")

    client = tg_mod.make_client(account)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("账号登录已失效，请重新扫码登录")

    me = await client.get_me()
    me_username = (getattr(me, "username", "") or "").lower()
    with _lock:
        _state.update(
            {
                "running": True,
                "connected": True,
                "me": f"{getattr(me, 'first_name', '') or ''}"
                + (f" @{me_username}" if me_username else ""),
                "account": str(account.get("name") or account.get("phone") or ""),
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_error": "",
            }
        )
    log.info("账号自动回复已启动：%s", _state["me"])

    @client.on(events.NewMessage(incoming=True))
    async def _on_new(event: Any) -> None:  # noqa: ANN401
        try:
            await _handle(client, event, int(me.id), me_username)
        except Exception as exc:
            _state["last_error"] = str(exc)[:200]
            log.warning("处理消息出错：%s", exc)

    backoff = 5
    while not _stop.is_set():
        await asyncio.sleep(1.0)
        if client.disconnected and not _stop.is_set():
            _state["connected"] = False
            try:
                await client.connect()
                _state["connected"] = True
                backoff = 5
                log.info("账号连接已恢复")
            except Exception as exc:
                _state["last_error"] = f"断线重连失败：{exc}"[:200]
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
    await client.disconnect()


def _worker() -> None:
    try:
        asyncio.run(_serve())
    except Exception as exc:
        _state["last_error"] = str(exc)[:200]
        log.warning("账号自动回复线程退出：%s", exc)
    finally:
        with _lock:
            _state["running"] = False
            _state["connected"] = False


def start() -> dict[str, Any]:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return status()
        _stop.clear()
        _thread = threading.Thread(target=_worker, name="assistant", daemon=True)
        _thread.start()
    log.info("已请求启动账号自动回复")
    return status()


def stop() -> dict[str, Any]:
    _stop.set()
    thread = _thread
    if thread and thread.is_alive():
        thread.join(timeout=8)
    with _lock:
        _state["running"] = False
        _state["connected"] = False
    log.info("账号自动回复已停止")
    return status()


def restart() -> dict[str, Any]:
    stop()
    return start()


def status() -> dict[str, Any]:
    cfg = _cfg()
    account = _account(cfg)
    agent = _agent_of(cfg)
    with _lock:
        data = dict(_state)
    data.update(
        {
            "enabled": cfg.get("assistant_enabled", "0") == "1",
            "account_id": (account or {}).get("id"),
            "account_name": (account or {}).get("name") or (account or {}).get("phone") or "",
            "agent_id": (agent or {}).get("id"),
            "agent_name": (agent or {}).get("name") or "",
            "private": cfg.get("assistant_private", "1") == "1",
            "groups": cfg.get("assistant_groups", "0") == "1",
            "trigger": cfg.get("assistant_trigger", "mention"),
            "whitelist": cfg.get("assistant_whitelist", ""),
            "blacklist": cfg.get("assistant_blacklist", ""),
            "ignore_bots": cfg.get("assistant_ignore_bots", "1") == "1",
            "cooldown": cfg.get("assistant_cooldown", "5"),
            "max_per_hour": cfg.get("assistant_max_per_hour", "30"),
            "quiet": cfg.get("assistant_quiet", ""),
            "has_session": bool(str((account or {}).get("session_string") or "").strip()),
            "seen": int(data.get("seen", 0)),
            "last_skip": data.get("last_skip", ""),
            "log": db.assistant_log_stats(),
        }
    )
    return data
