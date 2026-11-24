"""AI 聊天机器人：走 Bot API，不需要 api_id / api_hash。

与 telegram.py 的区别：
- telegram.py 用 MTProto（Telethon）驱动**你自己的账号**，必须 api_id，能发签到。
- 这个模块用 Bot API（HTTP 长轮询）驱动**一个机器人账号**，只需要 BotFather 的 token。
  机器人发消息不算作你本人签到，所以它不能替代签到，只适合陪聊、答疑、群内互动。

长轮询用 getUpdates + offset 游标，游标落库，进程重启后不会重复回复旧消息。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from . import db

log = logging.getLogger("tg-checkin.bot")

API_ROOT = "https://api.telegram.org"
# 绕过环境里的 http_proxy，避免容器内代理干扰出网
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop: threading.Event | None = None

_state: dict[str, Any] = {
    "running": False,
    "last_error": "",
    "last_poll": "",
    "replies": 0,
    "bot_name": "",
    "bot_username": "",
    "started_at": "",
}

# 每个会话的上次回复时间，用于最小间隔限流
_last_reply: dict[str, float] = {}


class BotError(RuntimeError):
    """Bot API 相关错误，message 直接面向用户展示。"""


def _api(method: str, token: str, payload: dict[str, Any] | None = None, timeout: int = 65) -> Any:
    if not token:
        raise BotError("请先填写 Bot Token（在 @BotFather 用 /newbot 获取）")
    url = f"{API_ROOT}/bot{token}/{method}"
    data = None
    headers = {"User-Agent": "tg-checkin-bot/1.0"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with _opener.open(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            raise BotError(f"Bot API 返回 HTTP {exc.code}：{raw[:200]}") from exc
    except urllib.error.URLError as exc:
        raise BotError(f"无法连接 Telegram Bot API：{exc.reason}") from exc
    except TimeoutError as exc:
        raise BotError("请求 Bot API 超时") from exc

    if not body.get("ok"):
        desc = body.get("description") or "未知错误"
        if "Unauthorized" in desc:
            raise BotError("Bot Token 无效或已被吊销，请在 @BotFather 重新获取")
        raise BotError(desc)
    return body.get("result")


# ---------------- 配置读取 ----------------

def _cfg() -> dict[str, str]:
    return db.get_settings()


def enabled() -> bool:
    return db.get_setting("bot_enabled", "0") == "1"


def _trigger_mode() -> str:
    mode = db.get_setting("bot_trigger", "mention")
    return mode if mode in ("mention", "all", "private") else "mention"


# ---------------- Token 校验 ----------------

def verify_token(token: str | None = None) -> dict[str, Any]:
    """调用 getMe 校验 token，并把机器人信息写入配置。"""
    token = (token or db.get_setting("bot_token", "")).strip()
    me = _api("getMe", token, timeout=20)
    username = me.get("username") or ""
    name = " ".join(
        x for x in (me.get("first_name"), me.get("last_name")) if x
    ) or username
    db.set_setting("bot_username", username)
    with _lock:
        _state["bot_name"] = name
        _state["bot_username"] = username
    return {
        "ok": True,
        "id": me.get("id"),
        "name": name,
        "username": username,
        "can_join_groups": me.get("can_join_groups"),
        "can_read_all_group_messages": me.get("can_read_all_group_messages"),
    }


# ---------------- AI 调用 ----------------

def _ai_messages(chat_id: str, user_text: str, author: str, is_private: bool) -> list[dict[str, str]]:
    cfg = _cfg()
    system = (cfg.get("ai_system_prompt") or "").strip()
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})

    try:
        history_len = max(0, min(int(cfg.get("ai_history_len", "12")), 40))
    except ValueError:
        history_len = 12
    for item in db.ai_history(chat_id, history_len):
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if role == "user" and item.get("author") and not is_private:
            content = f"{item['author']}：{content}"
        messages.append({"role": role, "content": content})

    prefix = "" if is_private else f"{author}："
    messages.append({"role": "user", "content": prefix + user_text})
    return messages


def call_ai(messages: list[dict[str, str]]) -> str:
    """调用兼容 OpenAI 接口的 chat/completions。"""
    cfg = _cfg()
    base = (cfg.get("ai_base_url") or "").strip().rstrip("/")
    key = (cfg.get("ai_api_key") or "").strip()
    model = (cfg.get("ai_model") or "").strip()
    if not base:
        raise BotError("请填写 AI 接口地址（例如 https://api.deepseek.com/v1）")
    if not key:
        raise BotError("请填写 AI API Key")
    if not model:
        raise BotError("请填写模型名（例如 deepseek-chat）")

    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    try:
        max_tokens = max(64, min(int(cfg.get("ai_max_tokens", "800")), 4096))
        temperature = max(0.0, min(float(cfg.get("ai_temperature", "0.7")), 2.0))
    except ValueError:
        max_tokens, temperature = 800, 0.7

    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "tg-checkin-bot/1.0",
        },
    )
    try:
        with _opener.open(request, timeout=90) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        if exc.code == 401:
            raise BotError("AI 接口鉴权失败，请检查 API Key") from exc
        if exc.code == 404:
            raise BotError(f"AI 接口地址或模型名有误（HTTP 404）：{raw[:200]}") from exc
        raise BotError(f"AI 接口返回 HTTP {exc.code}：{raw[:200]}") from exc
    except urllib.error.URLError as exc:
        raise BotError(f"无法连接 AI 接口：{exc.reason}") from exc
    except TimeoutError as exc:
        raise BotError("AI 接口响应超时") from exc

    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BotError(f"AI 返回格式不符合预期：{str(body)[:200]}") from exc
    return str(text or "").strip()


def test_ai() -> dict[str, Any]:
    """用一句最短的话验证 AI 配置是否可用。"""
    started = time.time()
    text = call_ai([{"role": "user", "content": "只回复两个字：可用"}])
    return {"ok": True, "reply": text[:200], "ms": int((time.time() - started) * 1000)}


# ---------------- 消息处理 ----------------

def _chat_title(message: dict[str, Any]) -> str:
    chat = message.get("chat") or {}
    return (
        chat.get("title")
        or " ".join(
            x for x in (chat.get("first_name"), chat.get("last_name")) if x
        )
        or chat.get("username")
        or str(chat.get("id", ""))
    )


def _author(message: dict[str, Any]) -> str:
    sender = message.get("from") or {}
    return (
        " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x)
        or sender.get("username")
        or str(sender.get("id", "某人"))
    )


def _should_reply(message: dict[str, Any], bot_username: str) -> tuple[bool, str]:
    """判断这条消息要不要回。返回 (是否回复, 去掉 @ 之后的文本)。"""
    chat = message.get("chat") or {}
    chat_type = chat.get("type")
    text = (message.get("text") or "").strip()
    if not text:
        return False, ""
    # 忽略机器人自己和其他机器人
    sender = message.get("from") or {}
    if sender.get("is_bot"):
        return False, ""

    is_private = chat_type == "private"
    mode = _trigger_mode()
    if is_private:
        return True, text
    if mode == "private":
        return False, ""

    mentions_me = False
    if bot_username:
        mentions_me = f"@{bot_username}".lower() in text.lower()
    replied_to_me = False
    reply = message.get("reply_to_message") or {}
    if reply:
        reply_sender = reply.get("from") or {}
        replied_to_me = bool(bot_username) and (
            str(reply_sender.get("username") or "").lower() == bot_username.lower()
        )

    if mode == "all":
        return True, text
    if mentions_me or replied_to_me:
        cleaned = text
        if bot_username:
            cleaned = cleaned.replace(f"@{bot_username}", "").replace(
                f"@{bot_username.lower()}", ""
            )
        return True, cleaned.strip() or text
    return False, ""


def _reply(chat_id: Any, text: str, reply_to: int | None) -> None:
    token = db.get_setting("bot_token", "")
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    try:
        _api("sendMessage", token, payload)
    except BotError as exc:
        log.warning("回复失败：%s", exc)
        raise


def _handle_message(message: dict[str, Any]) -> None:
    cfg = _cfg()
    bot_username = (cfg.get("bot_username") or "").strip()
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    should, text = _should_reply(message, bot_username)
    if not should:
        return

    try:
        min_interval = max(0.0, min(float(cfg.get("ai_min_interval", "3")), 60))
    except ValueError:
        min_interval = 3.0

    key = str(chat_id)
    now = time.time()
    if min_interval and now - _last_reply.get(key, 0) < min_interval:
        log.info("会话 %s 距上次回复不足 %.0f 秒，跳过", key, min_interval)
        return

    title = _chat_title(message)
    author = _author(message)
    is_private = chat.get("type") == "private"
    db.add_ai_message(key, title, "user", author, text)

    try:
        messages = _ai_messages(key, text, author, is_private)
        answer = call_ai(messages) or "（模型没有返回内容）"
    except BotError as exc:
        log.warning("AI 调用失败：%s", exc)
        try:
            _reply(chat_id, f"AI 暂时不可用：{exc}", message.get("message_id"))
        except BotError:
            pass
        return

    db.add_ai_message(key, title, "assistant", "bot", answer)
    try:
        _reply(chat_id, answer, message.get("message_id"))
    except BotError:
        return

    _last_reply[key] = time.time()
    with _lock:
        _state["replies"] += 1


# ---------------- 轮询循环 ----------------

def _poll_loop(stop: threading.Event) -> None:
    backoff = 2.0
    while not stop.is_set():
        if not enabled():
            stop.wait(5)
            continue
        token = db.get_setting("bot_token", "").strip()
        if not token:
            stop.wait(5)
            continue

        try:
            offset = int(db.get_setting("bot_update_offset", "0") or 0)
        except ValueError:
            offset = 0

        with _lock:
            _state["last_poll"] = time.strftime("%Y-%m-%d %H:%M:%S")

        try:
            updates = _api(
                "getUpdates",
                token,
                {
                    "offset": offset,
                    "timeout": 50,
                    "allowed_updates": ["message"],
                },
                timeout=70,
            )
            backoff = 2.0
            with _lock:
                _state["last_error"] = ""
        except BotError as exc:
            with _lock:
                _state["last_error"] = str(exc)
            log.warning("拉取更新失败：%s", exc)
            # token 无效时不要疯跑，退避等待
            stop.wait(min(backoff, 60))
            backoff = min(backoff * 2, 60)
            continue

        for update in updates or []:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                db.set_setting("bot_update_offset", update_id + 1)
            message = update.get("message") or {}
            if not message:
                continue
            try:
                _handle_message(message)
            except Exception as exc:  # 单条消息出错不能拖垮整个循环
                log.exception("处理消息异常：%s", exc)

        if not updates:
            # 长轮询本身会阻塞，这里只是兜底，避免空转
            stop.wait(1)


def start() -> None:
    global _thread, _stop
    # 没有 token 或未启用时不起线程：既省资源，也避免面板显示「正在运行」造成误解
    if not (db.get_setting("bot_token", "") or "").strip() or not enabled():
        with _lock:
            _state["running"] = False
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop = threading.Event()
        _thread = threading.Thread(target=_poll_loop, args=(_stop,), name="bot-poll", daemon=True)
        _state["running"] = True
        _state["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _thread.start()
    log.info("AI 机器人轮询线程已启动")


def stop() -> None:
    global _thread, _stop
    with _lock:
        if _stop is not None:
            _stop.set()
        _thread = None
        _stop = None
        _state["running"] = False


def restart() -> None:
    stop()
    start()


def status() -> dict[str, Any]:
    with _lock:
        snapshot = dict(_state)
    cfg = _cfg()
    token = (cfg.get("bot_token") or "").strip()
    return {
        "enabled": enabled(),
        "running": bool(snapshot["running"] and _thread is not None and _thread.is_alive()),
        "token_set": bool(token),
        "bot_name": snapshot["bot_name"] or (cfg.get("bot_username") or ""),
        "bot_username": snapshot["bot_username"] or (cfg.get("bot_username") or ""),
        "trigger": _trigger_mode(),
        "last_poll": snapshot["last_poll"],
        "last_error": snapshot["last_error"],
        "replies": snapshot["replies"],
        "started_at": snapshot["started_at"],
        "ai_base_url": cfg.get("ai_base_url", ""),
        "ai_model": cfg.get("ai_model", ""),
        "ai_key_set": bool((cfg.get("ai_api_key") or "").strip()),
        "stats": db.ai_stats(),
    }


def send_test_message(chat_id: str, text: str) -> dict[str, Any]:
    """从面板直接发一条消息，用来验证机器人是否在目标会话里。"""
    token = db.get_setting("bot_token", "")
    if not str(chat_id).strip():
        raise BotError("请填写目标会话 ID 或 @频道用户名")
    _api("sendMessage", token, {"chat_id": str(chat_id).strip(), "text": text[:4000]})
    return {"ok": True}
