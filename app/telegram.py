"""Telegram 执行层：所有 Telegram 操作串行跑在同一个工作线程上。

这样做的原因：Telethon 的同步客户端依赖事件循环， login / 签到 / 通知如果散落在
FastAPI 线程池和 APScheduler 线程里各自建客户端，容易出现事件循环冲突和会话竞争。
"""
from __future__ import annotations

import asyncio
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from telethon import TelegramClient, errors as tg_errors
from telethon.sessions import StringSession

from . import db
from .bot import BotError as BotApiError

T = TypeVar("T")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tg-worker")
_login_lock = threading.RLock()
_login_state: dict[str, Any] = {}


def _ensure_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def run_tg(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """把 Telegram 操作提交到唯一工作线程并等待结果。"""

    def wrapped() -> T:
        _ensure_loop()
        return fn(*args, **kwargs)

    return _executor.submit(wrapped).result(timeout=240)


def _proxy_tuple(url: str) -> tuple | None:
    if not url:
        return None
    try:
        import socks  # PySocks
        from urllib.parse import urlparse

        parsed = urlparse(url if "://" in url else "socks5://" + url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port or (1080 if scheme.startswith("socks") else 80)
        if not host:
            return None
        if scheme in ("socks5", "socks5h"):
            return (socks.SOCKS5, host, port, True, parsed.username or None, parsed.password or None)
        if scheme in ("http", "https"):
            return (socks.HTTP, host, port, True, parsed.username or None, parsed.password or None)
    except Exception:
        return None
    return None


def make_client(session_string: str | None = None) -> TelegramClient:
    cfg = db.get_settings()
    api_id = str(cfg.get("api_id", "")).strip()
    api_hash = str(cfg.get("api_hash", "")).strip()
    if not api_id.isdigit() or not api_hash:
        raise RuntimeError("请先在「账号与登录」中填写 API ID 和 API Hash")

    kwargs: dict[str, Any] = {
        "device_model": "TG Checkin Panel",
        "system_version": "docker",
        "app_version": "1.0",
        "lang_code": "zh",
        "request_retries": 2,
        "connection_retries": 2,
        "timeout": 15,
    }
    proxy = _proxy_tuple(str(cfg.get("proxy_url", "")))
    if proxy:
        kwargs["proxy"] = proxy
    return TelegramClient(StringSession(session_string or ""), int(api_id), api_hash, **kwargs)


def normalize_chat(chat: str) -> str | int:
    """把 t.me/c/xxx/yyy 链接转成 -100xxx，纯数字转成 int。"""
    value = str(chat or "").strip()
    match = re.match(r"^https?://t\.me/c/(\d+)/", value)
    if match:
        return int("-100" + match.group(1))
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, BotApiError):
        return str(exc)
    if isinstance(exc, tg_errors.FloodWaitError):
        return f"触发 Telegram 限流，请等待约 {exc.seconds} 秒后再试"
    if isinstance(exc, tg_errors.UsernameInvalidError):
        return "群组用户名格式不正确"
    if isinstance(exc, tg_errors.UsernameNotOccupiedError):
        return "找不到该群组用户名，请检查链接是否正确"
    chat_invalid = getattr(tg_errors, "ChatInvalidError", tg_errors.ChannelInvalidError)
    if isinstance(exc, (tg_errors.ChannelInvalidError, chat_invalid)):
        return "无法访问该群组，请确认账号已加入该群且 ID/链接正确"
    if isinstance(exc, tg_errors.SessionPasswordNeededError):
        return "账号启用了两步验证，登录时需要填写验证密码"
    if isinstance(exc, tg_errors.PhoneCodeInvalidError):
        return "验证码错误或已过期，请重新获取验证码"
    if isinstance(exc, tg_errors.PhoneCodeExpiredError):
        return "验证码已过期，请重新获取验证码"
    if isinstance(exc, tg_errors.AuthKeyUnregisteredError):
        return "会话已失效，请重新登录 Telegram 账号"
    return f"{type(exc).__name__}: {exc}"


# ---------------- 账号状态 ----------------

def account_status() -> dict[str, Any]:
    session = db.get_setting("session_string")
    if not session:
        return {"authorized": False, "name": "", "username": "", "phone": db.get_setting("phone")}

    def job() -> dict[str, Any]:
        client = make_client(session)
        client.connect()
        try:
            if not client.is_user_authorized():
                return {"authorized": False, "name": "", "username": "", "phone": db.get_setting("phone")}
            me = client.get_me()
            return {
                "authorized": True,
                "name": " ".join(x for x in (me.first_name, me.last_name) if x),
                "username": me.username or "",
                "phone": me.phone or db.get_setting("phone"),
            }
        finally:
            client.disconnect()

    try:
        return run_tg(job)
    except Exception as exc:
        return {
            "authorized": False,
            "name": "",
            "username": "",
            "phone": db.get_setting("phone"),
            "error": friendly_error(exc),
        }


# ---------------- 登录流程 ----------------

def login_start(phone: str, api_id: str, api_hash: str, proxy_url: str) -> dict[str, Any]:
    phone = str(phone or "").strip()
    if not phone:
        raise RuntimeError("请填写手机号（带国家区号，例如 +8613800138000）")
    if not str(api_id).strip().isdigit():
        raise RuntimeError("API ID 必须是纯数字")
    if not str(api_hash).strip():
        raise RuntimeError("请填写 API Hash")

    db.set_setting("api_id", str(api_id).strip())
    db.set_setting("api_hash", str(api_hash).strip())
    db.set_setting("phone", phone)
    db.set_setting("proxy_url", str(proxy_url or "").strip())

    def job() -> None:
        with _login_lock:
            old = _login_state.pop("client", None)
            if old is not None:
                try:
                    old.disconnect()
                except Exception:
                    pass
            # 用全新会话请求验证码，确保拿到的是当前输入的账号
            client = make_client("")
            client.connect()
            sent = client.send_code_request(phone)
            _login_state.clear()
            _login_state.update(
                client=client,
                phone=phone,
                phone_code_hash=sent.phone_code_hash,
                created=time.time(),
            )

    run_tg(job)
    return {"ok": True, "message": "验证码已发送，请在 Telegram 中查收（可能来自 Telegram 官方账号）"}


def login_verify(code: str, password: str = "") -> dict[str, Any]:
    code = str(code or "").strip()
    if not code and not password:
        raise RuntimeError("请填写验证码")

    def job() -> dict[str, Any]:
        with _login_lock:
            client = _login_state.get("client")
            if client is None:
                raise RuntimeError("登录会话已失效，请重新发送验证码")
            if time.time() - float(_login_state.get("created", 0)) > 600:
                raise RuntimeError("登录会话已超时，请重新发送验证码")
            try:
                if password:
                    client.sign_in(password=password)
                else:
                    client.sign_in(
                        phone=_login_state["phone"],
                        code=code,
                        phone_code_hash=_login_state["phone_code_hash"],
                    )
            except tg_errors.SessionPasswordNeededError:
                if not password:
                    return {"need_password": True, "message": "该账号开启了两步验证，请输入验证密码"}
                client.sign_in(password=password)

            session_string = client.session.save()
            me = client.get_me()
            name = " ".join(x for x in (me.first_name, me.last_name) if x)
            db.set_setting("session_string", session_string)
            db.set_setting("phone", me.phone or _login_state["phone"])
            db.set_setting("account_name", name)
            try:
                client.disconnect()
            except Exception:
                pass
            _login_state.clear()
            return {"ok": True, "name": name, "message": f"登录成功：{name or 'Telegram 账号'}"}

    return run_tg(job)


def login_cancel() -> None:
    def job() -> None:
        with _login_lock:
            client = _login_state.pop("client", None)
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    pass
            _login_state.clear()

    run_tg(job)


def logout() -> None:
    db.set_setting("session_string", "")
    db.set_setting("account_name", "")
    login_cancel()


# ---------------- 签到执行 ----------------

def _send_checkin(group: dict[str, Any], cfg: dict[str, str]) -> dict[str, Any]:
    client = make_client(db.get_setting("session_string"))
    client.connect()
    try:
        if not client.is_user_authorized():
            raise RuntimeError("Telegram 账号未登录或会话已失效，请到「账号与登录」重新登录")

        entity = client.get_entity(normalize_chat(group["chat"]))
        recent = client.get_messages(entity, limit=1)
        last_id = recent[0].id if recent else 0

        if str(cfg.get("dry_run", "0")) == "1":
            return {"note": "模拟运行：已连接到群组，但未真正发送消息", "failed": False}

        message = client.send_message(entity, group["command"] or "/checkin")
        time.sleep(2)

        replies = list(client.get_messages(entity, limit=10, min_id=last_id, reverse=True))
        texts = []
        for item in replies:
            if item.out or not item.message:
                continue
            texts.append(" ".join(str(item.message).split())[:160])

        keywords = [k.strip() for k in str(cfg.get("fail_keywords", "")).split(",") if k.strip()]
        failed = any(any(k in text for k in keywords) for text in texts) if keywords else False
        note = " | ".join(texts[:3]) or "消息已发送，未捕获到机器人回复"
        return {"note": note, "message_id": message.id, "failed": failed}
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def _notify(text: str) -> None:
    target = db.get_setting("notify_chat").strip()
    if not target:
        return
    client = make_client(db.get_setting("session_string"))
    client.connect()
    try:
        client.send_message(normalize_chat(target), text)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def execute_checkin(group_id: int, source: str = "auto") -> dict[str, Any]:
    """执行单个群组的签到，含重试、随机延迟、日志和通知。"""
    group = db.get_group(group_id)
    if not group:
        return {"ok": False, "message": "群组不存在"}

    cfg = db.get_settings()
    # 默认用机器人签到（只要 bot_token）；填 user 才走自己的账号（需要 api_id）
    mode = (cfg.get("checkin_mode") or "bot").strip().lower()
    try:
        retries = max(0, min(int(cfg.get("retry_count", "2")), 5))
        retry_delay = max(0, min(float(cfg.get("retry_delay", "10")), 300))
        jitter = max(0, min(float(cfg.get("jitter", "5")), 120))
    except ValueError:
        retries, retry_delay, jitter = 2, 10.0, 5.0

    started = time.time()
    result: dict[str, Any] | None = None
    error = ""

    for attempt in range(1, retries + 2):
        if jitter:
            time.sleep(random.uniform(0, jitter))
        try:
            if mode == "user":
                result = run_tg(_send_checkin, group, cfg)
            else:
                from . import bot as bot_api

                result = bot_api.checkin(group, cfg)
            error = ""
            break
        except Exception as exc:
            error = friendly_error(exc)
            if attempt <= retries:
                time.sleep(retry_delay)

    duration = int((time.time() - started) * 1000)
    failed_by_keyword = bool(result and result.get("failed"))
    ok = result is not None and not failed_by_keyword
    note = (result or {}).get("note", "") or error
    if failed_by_keyword:
        note = "机器人回复命中失败关键词：" + note

    db.add_log(group_id, group["name"], source, "success" if ok else "failed", note, duration)

    if cfg.get("notify_chat", "").strip():
        flag = "成功" if ok else "失败"
        text = f"[TG 签到] {group['name']} 签到{flag}\n备注：{note[:300]}"
        try:
            if mode == "user":
                run_tg(_notify, text)
            else:
                from . import bot as bot_api

                bot_api.send_text(cfg["notify_chat"], text)
        except Exception:
            pass

    return {
        "ok": ok,
        "message": note,
        "group_id": group_id,
        "group_name": group["name"],
        "source": source,
        "mode": mode,
        "duration": duration,
    }
