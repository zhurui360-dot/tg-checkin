"""Telegram 机器人：走 Bot API，不需要 api_id / api_hash。

两种用法，都只需要 BotFather 给的 token：
- 群签到：机器人被拉进群后，按 cron 往群里发签到指令（见 checkin()）。
- AI 聊天 / 秘书模式：私聊、群里被 @ 时由 AI 回复；开启 Telegram Business
  后还能以账号主人身份回复私聊（见 business 相关函数）。

与 telegram.py 的区别：telegram.py 用 MTProto（Telethon）驱动**你自己的账号**，
需要 api_id，适合「签到机器人只认真人账号」这类场景，作为备用方式保留。

支持多个机器人：每个 bot 一条记录（bots 表），各自一个轮询线程、各自的
update_offset / 最近消息缓存 / Business 连接，互不干扰。
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

_lock = threading.RLock()
# bot_id -> 运行时（线程、停止信号、状态、最近消息）
_runtimes: dict[int, dict[str, Any]] = {}

DEFAULT_RUNTIME_KEY = 0

# 每个会话的上次回复时间，用于最小间隔限流（按机器人分开，避免互相影响）
_last_reply: dict[tuple[int, str], float] = {}


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

# 老版本只有一条机器人配置，存在 settings 里。升级后这条记录被搬进 bots 表，
# 但如果用户没迁移过（比如刚 reset 库），这里仍然要能读出来，所以保留回落。

def _cfg() -> dict[str, str]:
    return db.get_settings()


def _record(bot_id: int | None = None) -> dict[str, Any] | None:
    """取机器人记录：指定优先，否则默认机器人，再否则老 settings 里的单机器人。"""
    if bot_id:
        found = db.get_bot(int(bot_id))
        if found:
            return found
    found = db.default_bot()
    if found:
        return found
    # 一个机器人记录都没有时，退回老的单机器人配置（可能连 token 都还没填）
    return {
        "id": DEFAULT_RUNTIME_KEY,
        "name": db.get_setting("bot_username", "") or "默认机器人",
        "token": str(db.get_setting("bot_token", "")).strip(),
        "username": db.get_setting("bot_username", ""),
        "enabled": db.get_setting("bot_enabled", "0") == "1",
        "trigger": db.get_setting("bot_trigger", "mention"),
        "update_offset": db.get_setting("bot_update_offset", "0"),
        "business_connections": db.get_setting("business_connections", ""),
        "known_chats": db.get_setting("known_chats", ""),
    }


_SETTING_ALIASES = {
    "username": "bot_username",
    "enabled": "bot_enabled",
    "trigger": "bot_trigger",
    "update_offset": "bot_update_offset",
}


def _persist(record: dict[str, Any], **fields: Any) -> None:
    """写回机器人字段；老的单机器人配置落到 settings。"""
    rid = int(record.get("id") or 0)
    if rid and db.get_bot(rid):
        db.update_bot(rid, fields)
        return
    for key, value in fields.items():
        if key == "enabled":
            db.set_setting("bot_enabled", "1" if value else "0")
            continue
        db.set_setting(_SETTING_ALIASES.get(key, key), str(value))


def enabled(bot_id: int | None = None) -> bool:
    record = _record(bot_id)
    return bool(record and record.get("enabled"))


def _trigger_mode(bot_id: int | None = None) -> str:
    record = _record(bot_id) or {}
    mode = str(record.get("trigger") or "mention")
    return mode if mode in ("mention", "all", "private") else "mention"


def token_of(bot_id: int | None = None) -> str:
    record = _record(bot_id)
    return str((record or {}).get("token") or "").strip()


# ---------------- 运行时（每个机器人一份） ----------------

def _runtime(bot_id: int | None = None) -> dict[str, Any]:
    """取（必要时创建）某个机器人的运行时容器。"""
    key = int(bot_id or DEFAULT_RUNTIME_KEY)
    with _lock:
        rt = _runtimes.get(key)
        if rt is None:
            rt = {
                "id": key,
                "thread": None,
                "stop": None,
                "recent_lock": threading.RLock(),
                "recent": {},
                "state": {
                    "running": False,
                    "last_error": "",
                    "last_poll": "",
                    "replies": 0,
                    "bot_name": "",
                    "bot_username": "",
                    "started_at": "",
                    "business_replies": 0,
                },
            }
            _runtimes[key] = rt
        return rt


def _state_of(bot_id: int | None = None) -> dict[str, Any]:
    return _runtime(bot_id)["state"]


# ---------------- Token 校验 ----------------

def verify_token(token: str | None = None, bot_id: int | None = None) -> dict[str, Any]:
    """调用 getMe 校验 token，并把机器人信息写回记录（或老配置）。"""
    record = _record(bot_id) if bot_id else None
    token = (token or str((record or {}).get("token") or "")).strip() or db.get_setting("bot_token", "").strip()
    me = _api("getMe", token, timeout=20)
    username = me.get("username") or ""
    name = " ".join(
        x for x in (me.get("first_name"), me.get("last_name")) if x
    ) or username
    target = record or _record()
    if target:
        _persist(target, username=username, name=target.get("name") or name)
    else:
        db.set_setting("bot_username", username)
    state = _state_of(bot_id)
    with _lock:
        state["bot_name"] = name
        state["bot_username"] = username
    return {
        "ok": True,
        "id": me.get("id"),
        "name": name,
        "username": username,
        "can_join_groups": me.get("can_join_groups"),
        "can_read_all_group_messages": me.get("can_read_all_group_messages"),
        "can_connect_to_business": me.get("can_connect_to_business"),
    }


# ---------------- AI 调用 ----------------

def _agent_cfg(agent: dict[str, Any] | None) -> dict[str, str]:
    """把智能体记录摊平成和老配置同名的字段，没绑智能体时就用老配置。"""
    cfg = dict(_cfg())
    if not agent:
        return cfg
    mapping = {
        "ai_base_url": agent.get("base_url"),
        "ai_api_key": agent.get("api_key"),
        "ai_model": agent.get("model"),
        "ai_system_prompt": agent.get("system_prompt"),
        "ai_temperature": agent.get("temperature"),
        "ai_max_tokens": agent.get("max_tokens"),
        "ai_history_len": agent.get("history_len"),
        "ai_min_interval": agent.get("min_interval"),
        "ai_proxy": agent.get("proxy"),
    }
    for key, value in mapping.items():
        if value not in (None, ""):
            cfg[key] = str(value)
    return cfg


def _ai_messages(
    chat_id: str, user_text: str, author: str, is_private: bool, bot_id: int | None = None
) -> list[dict[str, str]]:
    cfg = _agent_cfg(db.agent_for_bot(bot_id))
    system = (cfg.get("ai_system_prompt") or "").strip()
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})

    try:
        history_len = max(0, min(int(cfg.get("ai_history_len", "12")), 40))
    except ValueError:
        history_len = 12
    for item in db.ai_history(chat_id, history_len, bot_id=bot_id or 0):
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


def call_ai(messages: list[dict[str, str]], agent: dict[str, Any] | None = None) -> str:
    """调用兼容 OpenAI 接口的 chat/completions。agent 为空时用全局老配置。"""
    cfg = _agent_cfg(agent)
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

    # 配置了代理就走代理，否则直连（容器内环境变量常带代理，直连必须显式清空）
    proxy = (cfg.get("ai_proxy") or "").strip()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = _opener

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
    # 连接被重置/超时属于跨境链路问题，多半是接口只对大陆网络开放，
    # 云主机（多在境外）连不上。重试一次仍失败就给出可操作的提示。
    last_net_error: Exception | None = None
    for attempt in (1, 2):
        try:
            with opener.open(request, timeout=90) as response:
                body = json.loads(response.read().decode("utf-8"))
            last_net_error = None
            break
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            if exc.code == 401:
                raise BotError("AI 接口鉴权失败，请检查 API Key") from exc
            if exc.code == 404:
                raise BotError(f"AI 接口地址或模型名有误（HTTP 404）：{raw[:200]}") from exc
            raise BotError(f"AI 接口返回 HTTP {exc.code}：{raw[:200]}") from exc
        except urllib.error.URLError as exc:
            last_net_error = exc
        except (TimeoutError, ConnectionResetError) as exc:
            last_net_error = exc
        if attempt == 1:
            time.sleep(2)

    if last_net_error is not None:
        reason = getattr(last_net_error, "reason", last_net_error)
        detail = str(reason)
        if "timed out" in detail.lower() or "reset" in detail.lower():
            raise BotError(
                f"无法连接 AI 接口：{detail}。这个地址在当前网络下不可达，"
                "常见原因是接口（或中转）只对大陆网络开放；部署在境外云主机时会被墙/被拒。"
                "请换用全球可达的服务，例如 https://api.deepseek.com/v1、"
                "https://api.siliconflow.cn/v1 或 https://openrouter.ai/api/v1。"
            )
        raise BotError(f"无法连接 AI 接口：{detail}")

    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BotError(f"AI 返回格式不符合预期：{str(body)[:200]}") from exc
    return str(text or "").strip()


def test_ai(agent: dict[str, Any] | None = None) -> dict[str, Any]:
    """用一句最短的话验证 AI 配置是否可用。"""
    started = time.time()
    text = call_ai([{"role": "user", "content": "只回复两个字：可用"}], agent=agent)
    return {"ok": True, "reply": text[:200], "ms": int((time.time() - started) * 1000)}


def list_models(base_url: str, api_key: str, proxy: str = "") -> dict[str, Any]:
    """读 OpenAI 兼容接口的 /models，让面板直接把可选模型列出来。"""
    base = (base_url or "").strip().rstrip("/")
    key = (api_key or "").strip()
    if not base:
        raise BotError("先填接口地址（例如 https://api.deepseek.com/v1）")
    if not base.startswith(("http://", "https://")):
        raise BotError("接口地址要以 http:// 或 https:// 开头")
    if not key:
        raise BotError("先填 API Key，读取模型列表要用它鉴权")
    url = base if base.endswith("/models") else f"{base}/models"

    proxy = (proxy or "").strip()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = _opener

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "tg-checkin-bot/1.0",
        },
    )
    try:
        with opener.open(request, timeout=25) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code in (401, 403):
            raise BotError("鉴权失败，Key 可能不对或没开通权限") from exc
        if exc.code == 404:
            raise BotError(
                "这个地址没有 /models 接口，模型名手动填也一样能用；"
                "也可以把 base_url 补到 /v1 为止再试"
            ) from exc
        raise BotError(f"读取模型失败（HTTP {exc.code}）：{detail}") from exc
    except urllib.error.URLError as exc:
        raise BotError(f"连不上接口：{getattr(exc, 'reason', exc)}") from exc
    except (TimeoutError, ConnectionResetError) as exc:
        raise BotError(f"连接接口超时或被重置：{exc}") from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise BotError(f"接口返回的不是 JSON，可能是把网页地址填成了 base_url：{raw[:120]}") from exc

    items: Any = None
    if isinstance(data, dict):
        items = data.get("data")
        if items is None:
            items = data.get("models")
    models: list[str] = []
    for item in items or []:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("id") or item.get("name") or "").strip()
        else:
            name = ""
        if name and name not in models:
            models.append(name)
    if not models:
        raise BotError("接口没返回任何模型，可能这把 Key 下没有可用模型，或服务没有实现 /models")
    models.sort()
    return {"ok": True, "models": models, "count": len(models), "endpoint": url}


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


def _should_reply(message: dict[str, Any], bot_username: str, bot_id: int | None = None) -> tuple[bool, str]:
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
    mode = _trigger_mode(bot_id)
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


# ---------------- Business（秘书模式）----------------
#
# Telegram Business 允许把机器人绑定到用户账号，让它代表用户回复私聊。
# 这条链路不需要 api_id：机器人读 business_message，回复时带上
# business_connection_id，消息就以「用户本人」的身份出现在对方会话里。
#
# 关键坑：用户自己发的消息也会以 business_message 回显，如果不按 owner_id
# 过滤，AI 会对着自己的回复再回一次，形成死循环。

def business_connections(bot_id: int | None = None) -> dict[str, Any]:
    """已绑定的 Business 连接，key 是 business_connection_id。"""
    record = _record(bot_id) or {}
    raw = str(record.get("business_connections") or "") or db.get_setting("business_connections", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_business_connection(conn: dict[str, Any], bot_id: int | None = None) -> None:
    conn_id = str(conn.get("id") or "")
    if not conn_id:
        return
    user = conn.get("user") or {}
    data = business_connections(bot_id)
    data[conn_id] = {
        "owner_id": user.get("id"),
        "owner_name": " ".join(
            x for x in (user.get("first_name"), user.get("last_name")) if x
        ) or user.get("username") or "",
        "owner_username": user.get("username") or "",
        "is_enabled": bool(conn.get("is_enabled")),
        "can_reply": bool(conn.get("can_reply")),
        "user_chat_id": conn.get("user_chat_id"),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    record = _record(bot_id)
    if record and record.get("id"):
        db.update_bot(int(record["id"]), {"business_connections": json.dumps(data, ensure_ascii=False)})
    db.set_setting("business_connections", json.dumps(data, ensure_ascii=False))
    with _lock:
        _state_of(bot_id)["business"] = list(data.values())


def _handle_business_connection(conn: dict[str, Any], bot_id: int | None = None) -> None:
    _save_business_connection(conn, bot_id)
    name = (conn.get("user") or {}).get("first_name") or "用户"
    state = "已启用" if conn.get("is_enabled") else "已停用"
    log.info("Business 连接更新：%s %s", name, state)


def _business_owner_id(connection_id: str, bot_id: int | None = None) -> Any:
    info = business_connections(bot_id).get(connection_id) or {}
    return info.get("owner_id")


def is_own_business_message(message: dict[str, Any], bot_id: int | None = None) -> bool:
    """判断这条 business 消息是不是账号主人自己发的（含我们自己的回复回显）。"""
    conn_id = message.get("business_connection_id")
    if not conn_id:
        return False
    owner_id = _business_owner_id(conn_id, bot_id)
    sender_id = (message.get("from") or {}).get("id")
    return owner_id is not None and sender_id == owner_id


def _ensure_business_connection(connection_id: str, bot_id: int | None = None) -> None:
    """本地缓存里没有这个连接时，向 Bot API 取回来。

    容器没有持久卷，重建后 business_connections 是空的；不知道 owner_id 就
    分不清「对方发的」和「自己回显的」，会让 AI 自问自答。所以必须按需补齐。
    """
    if not connection_id or connection_id in business_connections(bot_id):
        return
    token = token_of(bot_id)
    if not token:
        return
    try:
        conn = _api(
            "getBusinessConnection",
            token,
            {"business_connection_id": connection_id},
        )
    except BotError as exc:
        log.warning("取回 Business 连接 %s 失败：%s", connection_id, exc)
        return
    if isinstance(conn, dict):
        _save_business_connection(conn, bot_id)


def _reply(
    chat_id: Any,
    text: str,
    reply_to: int | None,
    business_connection_id: str | None = None,
    bot_id: int | None = None,
) -> None:
    token = token_of(bot_id)
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}
    if business_connection_id:
        # 带上这个参数，消息才会以账号主人的身份发出
        payload["business_connection_id"] = business_connection_id
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    try:
        _api("sendMessage", token, payload)
    except BotError as exc:
        log.warning("回复失败：%s", exc)
        raise


def _handle_message(message: dict[str, Any], bot_id: int | None = None) -> None:
    agent = db.agent_for_bot(bot_id)
    cfg = _agent_cfg(agent)
    record = _record(bot_id) or {}
    bot_username = str(record.get("username") or "").strip()
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    state = _state_of(bot_id)

    # Business 模式下，账号主人自己的消息会回显，必须跳过，否则自问自答死循环
    biz = message.get("business_connection_id")
    if biz:
        _ensure_business_connection(biz, bot_id)
        if is_own_business_message(message, bot_id):
            return
        if _business_owner_id(biz, bot_id) is None:
            # 拿不到 owner 就分不清是谁发的，宁可这条不回，也不能冒死循环的风险
            log.warning("Business 连接 %s 的 owner 未知，跳过该消息", biz)
            return

    should, text = _should_reply(message, bot_username, bot_id)
    if not should:
        return

    try:
        min_interval = max(0.0, min(float(cfg.get("ai_min_interval", "3")), 60))
    except ValueError:
        min_interval = 3.0

    key = (int(bot_id or DEFAULT_RUNTIME_KEY), str(chat_id))
    now = time.time()
    if min_interval and now - _last_reply.get(key, 0) < min_interval:
        log.info("会话 %s 距上次回复不足 %.0f 秒，跳过", chat_id, min_interval)
        return

    title = _chat_title(message)
    author = _author(message)
    is_private = chat.get("type") == "private"
    storage_id = str(chat_id)
    db.add_ai_message(storage_id, title, "user", author, text, bot_id=bot_id or 0)

    try:
        messages = _ai_messages(storage_id, text, author, is_private, bot_id)
        answer = call_ai(messages, agent=agent) or "（模型没有返回内容）"
    except BotError as exc:
        log.warning("AI 调用失败：%s", exc)
        # 同步到面板状态，否则用户只能翻容器日志才知道为什么没回复
        state["last_error"] = f"AI 调用失败：{exc}"
        try:
            _reply(chat_id, f"AI 暂时不可用：{exc}", message.get("message_id"), biz, bot_id)
        except BotError:
            pass
        return

    db.add_ai_message(storage_id, title, "assistant", "bot", answer, bot_id=bot_id or 0)
    if state["last_error"].startswith("AI 调用失败"):
        state["last_error"] = ""
    try:
        _reply(chat_id, answer, message.get("message_id"), biz, bot_id)
    except BotError:
        return

    _last_reply[key] = time.time()
    with _lock:
        state["replies"] = int(state.get("replies", 0)) + 1
        if biz:
            state["business_replies"] = int(state.get("business_replies", 0)) + 1


# ---------------- 轮询循环 ----------------

def _poll_loop(stop: threading.Event, bot_id: int | None = None) -> None:
    rt = _runtime(bot_id)
    state = rt["state"]
    backoff = 2.0
    while not stop.is_set():
        if not enabled(bot_id):
            stop.wait(5)
            continue
        token = token_of(bot_id)
        if not token:
            stop.wait(5)
            continue

        record = _record(bot_id) or {}
        try:
            offset = int(str(record.get("update_offset") or "0") or 0)
        except ValueError:
            offset = 0

        with _lock:
            state["last_poll"] = time.strftime("%Y-%m-%d %H:%M:%S")

        try:
            updates = _api(
                "getUpdates",
                token,
                {
                    "offset": offset,
                    "timeout": 50,
                    "allowed_updates": [
                        "message",
                        "my_chat_member",
                        "business_connection",
                        "business_message",
                    ],
                },
                timeout=70,
            )
            backoff = 2.0
            with _lock:
                state["last_error"] = ""
        except BotError as exc:
            with _lock:
                state["last_error"] = str(exc)
            log.warning("拉取更新失败（机器人 %s）：%s", bot_id, exc)
            # token 无效时不要疯跑，退避等待
            stop.wait(min(backoff, 60))
            backoff = min(backoff * 2, 60)
            continue

        for update in updates or []:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                fresh = _record(bot_id) or {}
                _persist(fresh, update_offset=str(update_id + 1))
            try:
                member = update.get("my_chat_member")
                if member:
                    # 机器人被拉进群 / 被踢出群：记下群信息，面板可选
                    remember_chat(member.get("chat") or {}, bot_id)
                    continue
                conn = update.get("business_connection")
                if conn:
                    _handle_business_connection(conn, bot_id)
                    continue
                # business_message 是「以用户身份」收发的私聊消息
                message = update.get("message") or update.get("business_message") or {}
                if not message:
                    continue
                # 顺便缓存群消息：签到结果校验要用（见 checkin()）
                _remember_group_message(message, bot_id)
                _handle_message(message, bot_id)
            except Exception as exc:  # 单条消息出错不能拖垮整个循环
                log.exception("处理消息异常：%s", exc)

        if not updates:
            # 长轮询本身会阻塞，这里只是兜底，避免空转
            stop.wait(1)


def _start_one(bot_id: int | None = None) -> None:
    rt = _runtime(bot_id)
    if not token_of(bot_id) or not enabled(bot_id):
        with _lock:
            rt["state"]["running"] = False
        return
    with _lock:
        thread = rt.get("thread")
        if thread is not None and thread.is_alive():
            return
        stop = threading.Event()
        rt["stop"] = stop
        rt["thread"] = threading.Thread(
            target=_poll_loop, args=(stop, bot_id),
            name=f"bot-poll-{bot_id or DEFAULT_RUNTIME_KEY}", daemon=True,
        )
        rt["state"]["running"] = True
        rt["state"]["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    rt["thread"].start()
    log.info("机器人 %s 轮询线程已启动", bot_id)


def _stop_one(bot_id: int | None = None) -> None:
    rt = _runtime(bot_id)
    with _lock:
        stop = rt.get("stop")
        if stop is not None:
            stop.set()
        rt["thread"] = None
        rt["stop"] = None
        rt["state"]["running"] = False


def start(bot_id: int | None = None) -> None:
    """启动轮询：不传 bot_id 就把当前所有启用的机器人全部拉起来。"""
    if bot_id:
        _start_one(bot_id)
        return
    records = db.list_bots()
    if records:
        for item in records:
            _start_one(item.get("id"))
        return
    # 老配置（没有 bots 表记录）仍按单机器人跑
    _start_one(None)


def stop(bot_id: int | None = None) -> None:
    if bot_id:
        _stop_one(bot_id)
        return
    with _lock:
        keys = list(_runtimes.keys())
    for key in keys:
        _stop_one(None if key == DEFAULT_RUNTIME_KEY else key)
    _last_reply.clear()


def restart(bot_id: int | None = None) -> None:
    stop(bot_id)
    start(bot_id)


def _status_one(bot_id: int | None = None) -> dict[str, Any]:
    rt = _runtime(bot_id)
    snapshot = dict(rt["state"])
    record = _record(bot_id) or {}
    agent = db.agent_for_bot(record.get("id") or bot_id)
    cfg = _agent_cfg(agent)
    token = str(record.get("token") or "").strip()
    thread = rt.get("thread")
    return {
        "id": record.get("id"),
        "name": record.get("name") or "",
        "enabled": bool(record.get("enabled")),
        "running": bool(snapshot["running"] and thread is not None and thread.is_alive()),
        "token_set": bool(token),
        "bot_name": snapshot.get("bot_name") or record.get("username") or "",
        "bot_username": snapshot.get("bot_username") or record.get("username") or "",
        "trigger": _trigger_mode(bot_id),
        "last_poll": snapshot.get("last_poll", ""),
        "last_error": snapshot.get("last_error", ""),
        "replies": snapshot.get("replies", 0),
        "business_replies": snapshot.get("business_replies", 0),
        "business": list(business_connections(bot_id).values()),
        "started_at": snapshot.get("started_at", ""),
        "chats": known_chats(bot_id),
        "stats": db.ai_stats(bot_id if bot_id else None),
        "ai_agent_id": record.get("ai_agent_id"),
        "ai_agent_name": (agent or {}).get("name", ""),
        "ai_base_url": cfg.get("ai_base_url", ""),
        "ai_model": cfg.get("ai_model", ""),
        "ai_key_set": bool((cfg.get("ai_api_key") or "").strip()),
    }


def status(bot_id: int | None = None) -> dict[str, Any]:
    """面板展示用：不传 bot_id 返回汇总（含每个机器人的明细）。"""
    if bot_id:
        return _status_one(bot_id)

    records = db.list_bots()
    items = [_status_one(item.get("id")) for item in records]
    if not items:
        items = [_status_one(None)]
    total_replies = sum(int(i.get("replies") or 0) for i in items)
    total_business = sum(int(i.get("business_replies") or 0) for i in items)
    primary = items[0]
    return {
        **primary,
        "id": primary.get("id"),
        "bots": [
            {
                "id": i.get("id"),
                "name": i.get("name"),
                "username": i.get("bot_username"),
                "enabled": i.get("enabled"),
                "running": i.get("running"),
                "token_set": i.get("token_set"),
                "trigger": i.get("trigger"),
                "last_poll": i.get("last_poll"),
                "last_error": i.get("last_error"),
                "replies": i.get("replies"),
                "business": i.get("business"),
                "business_replies": i.get("business_replies"),
            }
            for i in items
        ],
        "running": any(bool(i.get("running")) for i in items),
        "enabled": any(bool(i.get("enabled")) for i in items),
        "token_set": any(bool(i.get("token_set")) for i in items),
        "replies": total_replies,
        "business_replies": total_business,
    }


def bot_detail(bot_id: int) -> dict[str, Any]:
    """面板拉某个机器人的状态时用，避免和汇总混在一起。"""
    return _status_one(bot_id)


def send_test_message(chat_id: str, text: str, bot_id: int | None = None) -> dict[str, Any]:
    """从面板直接发一条消息，用来验证机器人是否在目标会话里。"""
    token = token_of(bot_id)
    if not str(chat_id).strip():
        raise BotError("请填写目标会话 ID 或 @频道用户名")
    _api("sendMessage", token, {"chat_id": str(chat_id).strip(), "text": text[:4000]})
    return {"ok": True}


# ---------------- 群签到（Bot API）----------------
#
# 用机器人代替自己的账号去群里签到，只需要 bot_token，不需要 api_id。
# 前提：机器人已经被拉进群，并且在群里能发消息。
#
# 结果校验靠轮询线程收到的群消息（会命中「签到机器人对我们这条消息的回复」），
# 所以轮询没开时只能确认「消息已发出」，判断不了签到是否成功。

_RECENT_LIMIT = 40

_LINK_PREFIXES = (
    "https://t.me/",
    "http://t.me/",
    "https://telegram.me/",
    "http://telegram.me/",
    "t.me/",
    "telegram.me/",
)


def normalize_chat(value: str) -> Any:
    """把面板里填的群标识转成 Bot API 能用的 chat_id。"""
    text = str(value or "").strip()
    lowered = text.lower()
    for prefix in _LINK_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.split("?")[0].strip().strip("/")
    if not text:
        raise BotError("群组标识为空，请填群 ID（-100 开头）或 @群用户名")
    # t.me/c/1234567890/45 这种私有群链接，转成 -1001234567890
    if text.startswith("c/"):
        parts = text.split("/")
        if len(parts) >= 2 and parts[1].isdigit():
            return int("-100" + parts[1])
    if text.startswith("@"):
        return text
    body = text.lstrip("-")
    if body.isdigit():
        return int(text)
    if text.startswith("+") or text.startswith("joinchat"):
        raise BotError(
            "这是邀请链接，Bot API 解析不了。请先把机器人拉进群，"
            "再用 -100 开头的群 ID 或 @群用户名 填写"
        )
    return "@" + text


def _remember_group_message(message: dict[str, Any], bot_id: int | None = None) -> None:
    """缓存群里的消息，供签到结果校验使用。"""
    chat = message.get("chat") or {}
    if (chat.get("type") or "") not in ("group", "supergroup"):
        return
    remember_chat(chat, bot_id)
    text = message.get("text") or message.get("caption") or ""
    if not str(text).strip():
        return
    sender = message.get("from") or {}
    item = {
        "id": int(message.get("message_id") or 0),
        "from": sender.get("id"),
        "from_name": " ".join(
            x for x in (sender.get("first_name"), sender.get("last_name")) if x
        ) or sender.get("username") or "",
        "text": " ".join(str(text).split())[:300],
    }
    key = str(chat.get("id") or "")
    if not key:
        return
    rt = _runtime(bot_id)
    with rt["recent_lock"]:
        bucket = rt["recent"].setdefault(key, [])
        bucket.append(item)
        del bucket[:-_RECENT_LIMIT]


def recent_group_messages(
    chat_key: Any, since_id: int = 0, bot_id: int | None = None
) -> list[dict[str, Any]]:
    key = str(chat_key)
    rt = _runtime(bot_id)
    with rt["recent_lock"]:
        return [m for m in rt["recent"].get(key, []) if m.get("id", 0) > since_id]


def known_chats(bot_id: int | None = None) -> list[dict[str, Any]]:
    """机器人待过的群：面板里点一下就能填进群组表单，省得自己找群 ID。"""
    record = _record(bot_id) or {}
    raw = str(record.get("known_chats") or "") or db.get_setting("known_chats", "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def remember_chat(chat: dict[str, Any], bot_id: int | None = None) -> None:
    if (chat.get("type") or "") not in ("group", "supergroup"):
        return
    chat_id = chat.get("id")
    if chat_id is None:
        return
    title = chat.get("title") or chat.get("username") or str(chat_id)
    items = [c for c in known_chats(bot_id) if c.get("id") != chat_id]
    items.insert(0, {"id": chat_id, "title": title, "username": chat.get("username") or ""})
    payload = json.dumps(items[:50], ensure_ascii=False)
    record = _record(bot_id)
    if record and record.get("id"):
        db.update_bot(int(record["id"]), {"known_chats": payload})
    db.set_setting("known_chats", payload)


def polling_alive(bot_id: int | None = None) -> bool:
    rt = _runtime(bot_id)
    with _lock:
        thread = rt.get("thread")
        return bool(rt["state"]["running"] and thread is not None and thread.is_alive())


def send_text(chat_id: Any, text: str, group: dict[str, Any] | None = None) -> dict[str, Any]:
    """用机器人发一条纯文本（通知用）。"""
    bot_id = (group or {}).get("bot_id")
    from .telegram import resolve_bot_token  # 延迟导入避免循环依赖

    token = resolve_bot_token(group) if group else token_of(bot_id)
    return _api(
        "sendMessage",
        token,
        {"chat_id": normalize_chat(str(chat_id)), "text": text[:4000]},
    )


def _group_error_hint(text: str, target: Any) -> str:
    """把 Bot API 的英文报错翻成能照着做的中文提示。"""
    lowered = text.lower()
    if "chat not found" in lowered or "peer_id_invalid" in lowered:
        return (
            f"找不到这个群（{target}）。请确认：① 机器人已经被拉进群；"
            "② 群 ID 是 -100 开头的完整数字，或 @群用户名"
        )
    if "not a member" in lowered or "chat_member" in lowered:
        return f"机器人不在群（{target}）里，请先把它拉进群"
    if "not enough rights" in lowered or "have no rights" in lowered or "chat_write_forbidden" in lowered:
        return f"机器人在群（{target}）里没有发言权限，请解除限制或给它管理员权限"
    if "chat admin privileges" in lowered or "chat_admin_required" in lowered:
        return (
            f"这个目标（{target}）要求管理员权限：它可能是频道，或者群把发言限制成了管理员。"
            "请改成 -100 开头的群 ID（群链接形如 https://t.me/c/<数字>/1）"
        )
    if "bot was kicked" in lowered or "user is deactivated" in lowered:
        return f"机器人已被踢出群（{target}），请重新拉进去"
    if "too many requests" in lowered:
        return "Bot API 限流了，稍后再试"
    return text


def _resolve_target(token: str, target: Any) -> tuple[Any, str]:
    """用 getChat 看一眼目标：如果填的是频道，就改发到它的关联讨论群。

    频道里发言必须是管理员，机器人/普通账号都会被 Telegram 拒绝
    （CHAT_ADMIN_REQUIRED），所以这里提前换成能说话的群。
    """
    info = _api("getChat", token, {"chat_id": target}) or {}
    if (info.get("type") or "") != "channel":
        return target, ""

    title = info.get("title") or info.get("username") or str(target)
    linked = info.get("linked_chat_id")
    if not linked:
        raise BotError(
            f"「{title}」是频道，机器人不是管理员没法在里面发言。"
            "请把群组改成 -100 开头的群 ID（群链接形如 https://t.me/c/<数字>/1）"
        )
    chat_id = int(linked)
    if chat_id > 0:
        chat_id = int(f"-100{chat_id}")
    return chat_id, f"链接指向频道「{title}」，已自动改发到关联讨论群"


def checkin(group: dict[str, Any], cfg: dict[str, str]) -> dict[str, Any]:
    """用机器人给群发签到消息，返回 {"note":…, "failed":…}。"""
    from .telegram import resolve_bot_token

    raw_bot_id = group.get("bot_id")
    bot_id: int | None = None
    if raw_bot_id:
        try:
            bot_id = int(raw_bot_id)
        except (TypeError, ValueError):
            bot_id = None
    token = resolve_bot_token(group)
    if not token:
        raise BotError("没有 Bot Token，无法用机器人签到。请到「AI 机器人」页添加一个机器人")

    target = normalize_chat(group.get("chat", ""))
    command = (group.get("command") or "签到").strip() or "签到"

    try:
        target, hint = _resolve_target(token, target)
        if str(cfg.get("dry_run", "0")) == "1":
            info = _api("getChat", token, {"chat_id": target}) or {}
            name = info.get("title") or info.get("username") or str(target)
            note = f"模拟运行：机器人能看到「{name}」，未发送消息"
            return {"note": f"{hint}｜{note}" if hint else note, "failed": False}

        sent = _api("sendMessage", token, {"chat_id": target, "text": command[:4000]})
    except BotError as exc:
        raise BotError(_group_error_hint(str(exc), target)) from exc
    sent_id = int(sent.get("message_id") or 0)
    chat = sent.get("chat") or {}
    title = chat.get("title") or chat.get("username") or str(target)
    self_id = (sent.get("from") or {}).get("id")
    chat_key = chat.get("id") or target

    time.sleep(2.5)
    replies = [
        item
        for item in recent_group_messages(chat_key, sent_id, bot_id)
        if item.get("from") != self_id
    ]
    texts = [item["text"] for item in replies if item.get("text")][:3]

    keywords = [k.strip() for k in str(cfg.get("fail_keywords", "")).split(",") if k.strip()]
    failed = any(any(k in text for k in keywords) for text in texts) if keywords else False

    record = db.get_bot(bot_id) if bot_id else None
    bot_label = (record or {}).get("username") or (record or {}).get("name") or ""

    if texts:
        note = " | ".join(texts)
    elif polling_alive(bot_id):
        note = f"已在「{title}」发出 {command}，未捕获到签到机器人的回复"
    else:
        note = f"已在「{title}」发出 {command}（机器人轮询未开启，未校验结果）"
    if hint:
        note = f"{hint}｜{note}"
    return {
        "note": note,
        "message_id": sent_id,
        "failed": failed,
        "chat_title": title,
        "bot": bot_label or "机器人",
    }
