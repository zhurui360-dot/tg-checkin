"""Telegram 执行层：所有 Telegram 操作串行跑在同一个工作线程上。

这样做的原因：Telethon 的同步客户端依赖事件循环， login / 签到 / 通知如果散落在
FastAPI 线程池和 APScheduler 线程里各自建客户端，容易出现事件循环冲突和会话竞争。
"""
from __future__ import annotations

import asyncio
import datetime
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

# 必须导入 telethon.sync：它会把 TelegramClient 的公开方法改写成「自己跑事件循环」的
# 同步版本。少了这一行，client.connect() / get_me() 只会返回协程而不执行，
# 账号模式的登录、签到全部静默失效（历史上踩过这个坑）。
import telethon.sync  # noqa: F401  isort:skip
from telethon import TelegramClient, errors as tg_errors, functions, types
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


def make_client(
    account: dict[str, Any] | None = None, session_string: str | None = None
) -> TelegramClient:
    """按账号记录构造客户端。

    account 为 None 时退回 settings 里的单账号配置（老配置兼容）。
    session_string 显式传空串表示「要一条全新会话」（登录用）。
    """
    cfg = db.get_settings()
    account = account or {}
    api_id = str(account.get("api_id") or cfg.get("api_id", "")).strip()
    api_hash = str(account.get("api_hash") or cfg.get("api_hash", "")).strip()
    if not api_id.isdigit() or not api_hash:
        raise RuntimeError("这个账号还没填 API ID 和 API Hash，请到「账号与登录」补上")

    kwargs: dict[str, Any] = {
        "device_model": "TG Checkin Panel",
        "system_version": "docker",
        "app_version": "1.0",
        "lang_code": "zh",
        "request_retries": 2,
        "connection_retries": 2,
        "timeout": 15,
    }
    proxy = _proxy_tuple(str(account.get("proxy_url") or cfg.get("proxy_url", "")))
    if proxy:
        kwargs["proxy"] = proxy
    if session_string is None:
        session_string = str(account.get("session_string") or "")
    return TelegramClient(StringSession(session_string or ""), int(api_id), api_hash, **kwargs)


def resolve_account(group: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """给群组挑一个执行账号：群里指定 → 默认账号 → 老的单账号配置。"""
    if group:
        raw = group.get("account_id")
        if raw:
            try:
                found = db.get_account(int(raw))
            except (TypeError, ValueError):
                found = None
            if found and found.get("authorized"):
                return found
    found = db.default_account()
    if found:
        return found
    cfg = db.get_settings()
    if str(cfg.get("session_string") or "").strip():
        return {
            "id": None,
            "name": cfg.get("account_name") or cfg.get("phone") or "默认账号",
            "phone": cfg.get("phone", ""),
            "api_id": cfg.get("api_id", ""),
            "api_hash": cfg.get("api_hash", ""),
            "session_string": cfg.get("session_string", ""),
            "proxy_url": cfg.get("proxy_url", ""),
            "enabled": True,
            "authorized": True,
        }
    return None


def resolve_bot_token(group: dict[str, Any] | None = None) -> str:
    """给群组挑一个机器人 token：群里指定 → 默认机器人 → 老的单机器人配置。"""
    if group:
        raw = group.get("bot_id")
        if raw:
            try:
                found = db.get_bot(int(raw))
            except (TypeError, ValueError):
                found = None
            if found and str(found.get("token") or "").strip():
                return str(found["token"]).strip()
    found = db.default_bot()
    if found and str(found.get("token") or "").strip():
        return str(found["token"]).strip()
    return str(db.get_setting("bot_token", "")).strip()


def _save_session(
    account: dict[str, Any] | None, client: Any, user: Any, name: str, fallback_phone: str = ""
) -> dict[str, Any]:
    """登录成功后把会话写回账号记录；没有记录就新建一条。"""
    session_string = client.session.save()
    phone = getattr(user, "phone", "") or fallback_phone
    payload = {"session_string": session_string, "phone": phone, "name": name or phone}
    account_id = (account or {}).get("id")
    if account_id:
        db.update_account(int(account_id), payload)
        saved_id = int(account_id)
    else:
        saved_id = db.add_account(
            {
                "name": payload["name"],
                "phone": phone,
                "api_id": str((account or {}).get("api_id", "")),
                "api_hash": str((account or {}).get("api_hash", "")),
                "session_string": session_string,
                "proxy_url": str((account or {}).get("proxy_url", "")),
                "enabled": True,
            }
        )
    # 老版本只认 settings 里的单账号，这里补一份，避免升级后原有配置失效
    if not db.get_setting("session_string").strip():
        db.set_setting("session_string", session_string)
        db.set_setting("account_name", name)
        if phone:
            db.set_setting("phone", phone)
    return {"id": saved_id, "session_string": session_string, "phone": phone, "name": name}


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
    if isinstance(exc, tg_errors.ChatAdminRequiredError):
        return (
            "这个目标你在里面没有发言权限：它可能是「频道」而不是群，"
            "或者群把发言权限限制成了管理员。请把群组改成 -100 开头的群 ID（或群链接 https://t.me/c/<数字>/1）"
        )
    if isinstance(exc, tg_errors.ChatWriteForbiddenError):
        return "你在这个群里被禁言了，或者群设置了「仅管理员可发言」"
    if isinstance(exc, tg_errors.ChannelPrivateError):
        return "这个群/频道是私有的，你的账号还没加入，先在 Telegram 里加入再签到"
    if isinstance(exc, tg_errors.SessionPasswordNeededError):
        return "账号启用了两步验证，登录时需要填写验证密码"
    if isinstance(exc, tg_errors.PhoneCodeInvalidError):
        return "验证码错误或已过期，请重新获取验证码"
    if isinstance(exc, tg_errors.PhoneCodeExpiredError):
        return "验证码已过期，请重新获取验证码"
    if isinstance(exc, tg_errors.AuthKeyUnregisteredError):
        return "会话已失效，请重新登录 Telegram 账号"
    return f"{type(exc).__name__}: {exc}"


_PERMISSION_ERRORS = tuple(
    getattr(tg_errors, name)
    for name in (
        "ChatAdminRequiredError",
        "ChatWriteForbiddenError",
        "ChannelPrivateError",
        "ChatForbiddenError",
    )
    if hasattr(tg_errors, name)
)


def is_permission_error(exc: BaseException) -> bool:
    """权限类错误重试也没用（再发 10 次还是没权限），要立刻换方式或提示用户改配置。"""
    if isinstance(exc, BotApiError):
        text = str(exc)
        return any(k in text for k in ("权限", "管理员", "找不到", "不在群", "已被踢出"))
    return isinstance(exc, _PERMISSION_ERRORS)


def _resolve_entity(client: Any, chat: str) -> tuple[Any, str]:
    """把群组配置解析成可发送的目标。

    传进来的是频道链接（例如 https://t.me/dnshecom）时，直接发频道会报
    CHAT_ADMIN_REQUIRED；这里自动改发到该频道的关联讨论群，并返回一句说明。
    """
    entity = client.get_entity(normalize_chat(chat))
    if not (isinstance(entity, types.Channel) and getattr(entity, "broadcast", True)):
        return entity, ""

    linked_id = getattr(entity, "linked_chat_id", None)
    if not linked_id:
        try:
            full = client(functions.channels.GetFullChannelRequest(channel=entity))
            linked_id = getattr(full.full_chat, "linked_chat_id", None)
        except Exception:
            linked_id = None
    if not linked_id:
        raise RuntimeError(
            f"「{getattr(entity, 'title', chat)}」是频道，账号不是管理员没法在里面发言。"
            "请把群组改成 -100 开头的群 ID（群链接形如 https://t.me/c/<数字>/1）"
        )

    group_entity = None
    for peer in (types.PeerChannel(linked_id), types.InputPeerChannel(linked_id, 0)):
        try:
            group_entity = client.get_entity(peer)
            break
        except Exception:
            group_entity = None
    if group_entity is None:
        raise RuntimeError("识别到了频道，但拿不到它的讨论群，请直接把群组改成 -100 开头的群 ID")
    title = getattr(group_entity, "title", str(linked_id))
    return group_entity, f"链接指向频道「{getattr(entity, 'title', chat)}」，已自动改发到关联讨论群「{title}」"


# ---------------- 账号状态 ----------------

def account_status(account_id: int | None = None) -> dict[str, Any]:
    """检查某个账号的登录状态；不传 account_id 就查默认账号。"""
    account = db.get_account(int(account_id)) if account_id else resolve_account()
    if not account or not str(account.get("session_string") or "").strip():
        return {
            "authorized": False,
            "name": (account or {}).get("name", ""),
            "username": "",
            "phone": (account or {}).get("phone", ""),
            "account_id": (account or {}).get("id"),
        }

    def job() -> dict[str, Any]:
        client = make_client(account)
        client.connect()
        try:
            if not client.is_user_authorized():
                return {
                    "authorized": False,
                    "name": account.get("name", ""),
                    "username": "",
                    "phone": account.get("phone", ""),
                    "account_id": account.get("id"),
                }
            me = client.get_me()
            name = " ".join(x for x in (me.first_name, me.last_name) if x)
            return {
                "authorized": True,
                "name": name or account.get("name", ""),
                "username": me.username or "",
                "phone": me.phone or account.get("phone", ""),
                "account_id": account.get("id"),
                "user_id": me.id,
            }
        finally:
            client.disconnect()

    try:
        return run_tg(job)
    except Exception as exc:
        return {
            "authorized": False,
            "name": account.get("name", ""),
            "username": "",
            "phone": account.get("phone", ""),
            "account_id": account.get("id"),
            "error": friendly_error(exc),
        }


# ---------------- 登录流程 ----------------

def _login_account(account_id: int | None, phone: str) -> dict[str, Any] | None:
    """定位这次登录要写进哪条账号记录；找不到就返回 None（登录成功后新建）。"""
    if account_id:
        found = db.get_account(int(account_id))
        if found:
            return found
    for item in db.list_accounts():
        if phone and str(item.get("phone") or "").strip() == phone:
            return item
    return None


def _apply_login_credentials(account: dict[str, Any] | None, **fields: str) -> None:
    """把登录时填的凭据落到账号记录（没有记录时落到 settings，注册流程里再搬）."""
    if account and account.get("id"):
        db.update_account(int(account["id"]), fields)
        return
    for key, value in fields.items():
        if key in ("api_id", "api_hash", "phone", "proxy_url"):
            db.set_setting(key, value)


def login_start(
    phone: str,
    api_id: str,
    api_hash: str,
    proxy_url: str,
    account_id: int | None = None,
) -> dict[str, Any]:
    phone = str(phone or "").strip()
    if not phone:
        raise RuntimeError("请填写手机号（带国家区号，例如 +8613800138000）")

    account = _login_account(account_id, phone)
    api_id, api_hash, proxy_url = _resolve_credentials(account, api_id, api_hash, proxy_url)
    if not str(api_id).strip().isdigit():
        raise RuntimeError("API ID 必须是纯数字")
    if not str(api_hash).strip():
        raise RuntimeError("请填写 API Hash")

    _apply_login_credentials(
        account,
        api_id=str(api_id).strip(),
        api_hash=str(api_hash).strip(),
        phone=phone,
        proxy_url=str(proxy_url or "").strip(),
    )
    if account:
        account = {**account, "api_id": str(api_id).strip(), "api_hash": str(api_hash).strip(),
                   "proxy_url": str(proxy_url or "").strip()}

    def job() -> None:
        with _login_lock:
            old = _login_state.pop("client", None)
            if old is not None:
                try:
                    old.disconnect()
                except Exception:
                    pass
            # 用全新会话请求验证码，确保拿到的是当前输入的账号
            client = make_client(account, session_string="")
            client.connect()
            sent = client.send_code_request(phone)
            _login_state.clear()
            _login_state.update(
                client=client,
                phone=phone,
                phone_code_hash=sent.phone_code_hash,
                created=time.time(),
                account=account,
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

            me = client.get_me()
            name = " ".join(x for x in (me.first_name, me.last_name) if x)
            saved = _save_session(
                _login_state.get("account"), client, me, name, _login_state.get("phone", "")
            )
            try:
                client.disconnect()
            except Exception:
                pass
            _login_state.clear()
            return {
                "ok": True,
                "name": name,
                "account_id": saved["id"],
                "message": f"登录成功：{name or 'Telegram 账号'}",
            }

    return run_tg(job)


# ---------------- 二维码登录 ----------------
# Telegram 的二维码登录：用 api_id/api_hash 调 auth.ExportLoginToken 拿一次性 token，
# 手机端在已登录的 Telegram 里扫码后把授权导入这个 token，我们随即收到 UpdateLoginToken。
# token 有效期约 30 秒，过期要重新生成，所以下面做成「专用线程 + 自动续期」：
# Telethon 客户端绑定在创建它的事件循环上，跨线程调用会直接崩，因此连接、等待、
# 两步验证密码全部在同一线程内完成，HTTP 接口只读写状态字典。
_qr_lock = threading.RLock()
_qr_state: dict[str, Any] = {}
_qr_cancel = threading.Event()
_qr_refresh_flag = threading.Event()
_qr_password: dict[str, Any] = {"value": "", "event": threading.Event()}


def qr_status() -> dict[str, Any]:
    """当前二维码进度，面板轮询用。"""
    with _qr_lock:
        state = dict(_qr_state)
    if not state:
        return {"active": False, "status": "idle", "seconds_left": 0,
                "messages_key": "", "message": "还没有生成二维码", "url": "", "svg": "",
                "name": "", "account_id": None}
    expires = float(state.get("expires") or 0)
    return {
        "active": state.get("status") in ("waiting", "need_password"),
        "status": state.get("status", "idle"),
        "url": state.get("url", ""),
        "svg": state.get("svg", ""),
        "expires": expires,
        "seconds_left": max(0, int(expires - time.time())) if expires else 0,
        "message": state.get("message", ""),
        "name": state.get("name", ""),
        "account_id": state.get("account_id"),
    }


def _qr_svg(url: str) -> str:
    """把 tg://login?token=… 渲染成内联 SVG，面板直接插进 DOM 即可。"""
    try:
        import io

        import qrcode
        import qrcode.image.svg

        image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        buffer = io.BytesIO()
        image.save(buffer)
        return buffer.getvalue().decode("utf-8")
    except Exception:
        return ""


def _qr_update(**fields: Any) -> None:
    with _qr_lock:
        _qr_state.update(fields)


def _qr_publish(qr: Any) -> None:
    """刷新 url / svg / 过期时间。"""
    expires = qr.expires
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=datetime.timezone.utc)
    _qr_update(url=qr.url, svg=_qr_svg(qr.url), expires=expires.timestamp())


def _qr_finish(client: Any, user: Any) -> None:
    """把 session 落库到本次登录指定的账号，状态改成已登录。"""
    name = " ".join(x for x in (getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or "") if x)
    with _qr_lock:
        account = _qr_state.get("account")
    saved = _save_session(account, client, user, name)
    _qr_update(
        status="authorized",
        name=name,
        account_id=saved["id"],
        message=f"登录成功：{name or 'Telegram 账号'}",
    )


def _qr_worker() -> None:
    """后台线程：生成二维码 → 等扫码 → 需要的话补密码 → 落库。"""
    loop = None
    client = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        with _qr_lock:
            account = _qr_state.get("account")
        client = make_client(account, session_string="")
        client.connect()  # syncified 调用会用当前线程已设置的 loop
        if client.is_user_authorized():
            me = client.get_me()
            _qr_finish(client, me)
            _qr_update(message="这个会话已经登录过了，无需扫码")
            return

        qr = client.qr_login()
        _qr_update(qr=qr, status="waiting", message="用手机 Telegram 扫一下上面的二维码")
        _qr_publish(qr)

        while not _qr_cancel.is_set():
            if _qr_refresh_flag.is_set():
                _qr_refresh_flag.clear()
                loop.run_until_complete(qr.recreate())
                _qr_publish(qr)
                _qr_update(status="waiting", message="二维码已刷新，请重新扫码")
                continue
            remaining = float(_qr_state.get("expires") or 0) - time.time()
            if remaining <= 1:
                # token 过期，换一张（旧码作废，面板轮询会自动换成新的）
                loop.run_until_complete(qr.recreate())
                _qr_publish(qr)
                continue
            try:
                user = loop.run_until_complete(qr.wait(timeout=remaining))
            except asyncio.TimeoutError:
                continue
            except tg_errors.SessionPasswordNeededError:
                _qr_update(
                    status="need_password",
                    message="该账号开启了两步验证，请输入验证密码",
                )
                _qr_password["event"].clear()
                _qr_password["value"] = ""
                # 最多等 5 分钟，期间接口会把密码塞进来
                if not _qr_password["event"].wait(300):
                    _qr_update(status="expired", message="等待验证密码超时，请重新生成二维码")
                    return
                password = str(_qr_password.get("value") or "")
                if not password:
                    _qr_update(status="expired", message="已取消登录")
                    return
                user = client.sign_in(password=password)
                _qr_finish(client, user)
                return
            _qr_finish(client, user)
            return

        _qr_update(status="idle", message="已取消扫码登录")
    except Exception as exc:
        _qr_update(status="error", message=f"扫码登录失败：{friendly_error(exc)}")
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
        if loop is not None:
            try:
                loop.close()
            except Exception:
                pass


def _resolve_credentials(
    account: dict[str, Any] | None, api_id: str, api_hash: str, proxy_url: str = ""
) -> tuple[str, str, str]:
    """表单里留空的字段回落到账号记录里已存的凭据，避免把已配置的值覆盖成空。"""
    stored = account or {}
    api_id = str(api_id or "").strip() or str(stored.get("api_id") or "").strip()
    api_hash = str(api_hash or "").strip() or str(stored.get("api_hash") or "").strip()
    proxy_url = str(proxy_url or "").strip() or str(stored.get("proxy_url") or "").strip()
    return api_id, api_hash, proxy_url


def qr_start(
    api_id: str, api_hash: str, proxy_url: str = "", account_id: int | None = None
) -> dict[str, Any]:
    """生成登录二维码。顺手把凭据落到本次要登录的账号记录上。"""
    account = db.get_account(int(account_id)) if account_id else None
    api_id, api_hash, proxy_url = _resolve_credentials(account, api_id, api_hash, proxy_url)
    if not str(api_id).strip().isdigit():
        raise RuntimeError("API ID 必须是纯数字")
    if not str(api_hash).strip():
        raise RuntimeError("请填写 API Hash")

    if account:
        db.update_account(
            int(account["id"]),
            {
                "api_id": str(api_id).strip(),
                "api_hash": str(api_hash).strip(),
                "proxy_url": str(proxy_url or "").strip(),
            },
        )
        account = {
            **account,
            "api_id": str(api_id).strip(),
            "api_hash": str(api_hash).strip(),
            "proxy_url": str(proxy_url or "").strip(),
        }
    else:
        db.set_setting("api_id", str(api_id).strip())
        db.set_setting("api_hash", str(api_hash).strip())
        db.set_setting("proxy_url", str(proxy_url or "").strip())

    qr_cancel()  # 先清掉上一个（会等旧线程退出）
    _qr_cancel.clear()
    with _qr_lock:
        _qr_state.clear()
        _qr_state.update(
            status="starting",
            message="正在向 Telegram 申请二维码…",
            expires=0,
            url="",
            svg="",
            account=account,
        )

    thread = threading.Thread(target=_qr_worker, name="tg-qr", daemon=True)
    thread.start()
    with _qr_lock:
        _qr_state["thread"] = thread

    # 等第一张码出来再返回，面板就能立刻渲染（握手+建链通常 3~8 秒）
    deadline = time.time() + 20
    while time.time() < deadline:
        time.sleep(0.25)
        with _qr_lock:
            if _qr_state.get("svg") or _qr_state.get("status") in ("authorized", "error"):
                break
    return qr_status()


def qr_refresh() -> dict[str, Any]:
    """换一张码。真正生成在扫码线程里做，这里只发信号，避免跨线程操作客户端。"""
    with _qr_lock:
        status = _qr_state.get("status")
        qr = _qr_state.get("qr")
    if qr is None or status in ("idle", "authorized", "error"):
        raise RuntimeError("当前没有可刷新的二维码，请先点击生成")
    _qr_refresh_flag.set()
    return {"ok": True, "message": "正在刷新二维码…", **qr_status()}


def qr_password_submit(password: str) -> dict[str, Any]:
    """扫码通过但账号有两步验证时，把密码交给后台线程去完成登录。"""
    password = str(password or "")
    if not password:
        raise RuntimeError("请输入两步验证密码")
    with _qr_lock:
        status = _qr_state.get("status")
    if status != "need_password":
        raise RuntimeError("当前没有等待验证密码的登录，请重新扫码")
    _qr_password["value"] = password
    _qr_password["event"].set()
    return {"ok": True, "message": "已提交验证密码，正在登录…", **qr_status()}


def qr_cancel() -> None:
    """停掉扫码线程并释放客户端。"""
    with _qr_lock:
        thread = _qr_state.get("thread")
    _qr_cancel.set()
    _qr_refresh_flag.clear()
    _qr_password["value"] = ""
    _qr_password["event"].set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=12)
    with _qr_lock:
        _qr_state.clear()
    _qr_password["event"].clear()
    _qr_password["value"] = ""


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


def logout(account_id: int | None = None) -> None:
    """退出某个账号：清掉它的会话。不传 id 就退出默认账号。"""
    login_cancel()
    if account_id:
        db.update_account(int(account_id), {"session_string": ""})
        return
    account = resolve_account()
    if account and account.get("id"):
        db.update_account(int(account["id"]), {"session_string": ""})
    # 老版本的单账号配置也一并清掉，避免「退出了但还能签到」
    db.set_setting("session_string", "")
    db.set_setting("account_name", "")


# ---------------- 签到执行 ----------------

def _send_checkin(group: dict[str, Any], cfg: dict[str, str]) -> dict[str, Any]:
    account = resolve_account(group)
    if not account:
        raise RuntimeError("还没有可用的 Telegram 账号，请到「账号与登录」添加并登录一个")
    client = make_client(account)
    client.connect()
    try:
        if not client.is_user_authorized():
            raise RuntimeError(
                f"账号「{account.get('name') or account.get('phone') or account.get('id')}」"
                "未登录或会话已失效，请到「账号与登录」重新登录"
            )

        entity, hint = _resolve_entity(client, group["chat"])
        recent = client.get_messages(entity, limit=1)
        last_id = recent[0].id if recent else 0

        if str(cfg.get("dry_run", "0")) == "1":
            note = "模拟运行：已连接到群组，但未真正发送消息"
            prefix = f"账号「{account.get('name') or account.get('phone')}」"
            return {
                "note": f"{prefix}｜{note}（{hint}）" if hint else f"{prefix}｜{note}",
                "failed": False,
                "account": account.get("name") or account.get("phone") or "",
            }

        message = client.send_message(entity, group["command"] or "签到")
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
        if hint:
            note = f"{hint}｜{note}"
        return {
            "note": note,
            "message_id": message.id,
            "failed": failed,
            "account": account.get("name") or account.get("phone") or "",
        }
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def _notify(text: str, group: dict[str, Any] | None = None) -> None:
    target = db.get_setting("notify_chat").strip()
    if not target:
        return
    account = resolve_account(group)
    if not account:
        return
    client = make_client(account)
    client.connect()
    try:
        client.send_message(normalize_chat(target), text)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def _group_mode(group: dict[str, Any], cfg: dict[str, str]) -> str:
    """签到一律用已登录的账号（Telethon）。

    历史上支持过「用机器人签到」，但机器人发的不算本人签到，容易白签。
    现在统一成账号，函数保留是为了不动其它调用点的签名。
    """
    return "user"


def execute_checkin(group_id: int, source: str = "auto") -> dict[str, Any]:
    """执行单个群组的签到，含重试、随机延迟、日志和通知。"""
    group = db.get_group(group_id)
    if not group:
        return {"ok": False, "message": "群组不存在"}

    cfg = db.get_settings()
    # 签到只用账号：群里指定的账号 → 默认账号 → 老的单账号配置
    mode = _group_mode(group, cfg)
    account = resolve_account(group)
    if account is None:
        note = "没有可用账号：先到「账号与登录」登录一个账号，再回来签到"
        db.add_log(group_id, group["name"], source, "failed", note, 0)
        return {
            "ok": False,
            "message": note,
            "group_id": group_id,
            "group_name": group["name"],
            "source": source,
            "mode": mode,
            "mode_configured": mode,
            "account": "",
            "bot": "",
            "duration": 0,
        }
    try:
        retries = max(0, min(int(cfg.get("retry_count", "2")), 5))
        retry_delay = max(0, min(float(cfg.get("retry_delay", "10")), 300))
        jitter = max(0, min(float(cfg.get("jitter", "5")), 120))
    except ValueError:
        retries, retry_delay, jitter = 2, 10.0, 5.0

    started = time.time()
    result: dict[str, Any] | None = None
    error = ""
    primary_error = ""
    used_mode = mode

    def run_mode(which: str) -> tuple[dict[str, Any] | None, str]:
        """跑一种签到方式，返回 (结果, 错误)。权限类错误不重试，直接返回。"""
        last_error = ""
        for attempt in range(1, retries + 2):
            if jitter:
                time.sleep(random.uniform(0, jitter))
            try:
                if which == "user":
                    return run_tg(_send_checkin, group, cfg), ""
                from . import bot as bot_api

                return bot_api.checkin(group, cfg), ""
            except Exception as exc:
                last_error = friendly_error(exc)
                if is_permission_error(exc) or attempt > retries:
                    return None, last_error
                time.sleep(retry_delay)
        return None, last_error

    # 现在只有一种方式：用已登录的账号发。以前还有「机器人代发」的备选，
    # 但机器人发的消息不算本人签到，失败时反而让人以为签到了，所以去掉。
    modes = ["user"]

    for which in modes:
        result, error = run_mode(which)
        used_mode = which
        if result is not None:
            break
        if not primary_error:
            primary_error = error

    if result is not None and used_mode != mode:
        label = {"bot": "机器人", "user": "账号"}
        error = (
            f"{label.get(mode, mode)}方式失败（{primary_error}），已自动改用"
            f"{label.get(used_mode, used_mode)}方式补发。"
        )

    duration = int((time.time() - started) * 1000)
    failed_by_keyword = bool(result and result.get("failed"))
    ok = result is not None and not failed_by_keyword
    note = (result or {}).get("note", "") or error
    if failed_by_keyword:
        note = "机器人回复命中失败关键词：" + note
    if ok and used_mode != mode:
        note = f"{error}｜{note}"

    db.add_log(group_id, group["name"], source, "success" if ok else "failed", note, duration)

    if cfg.get("notify_chat", "").strip():
        flag = "成功" if ok else "失败"
        text = f"[TG 签到] {group['name']} 签到{flag}\n备注：{note[:300]}"
        try:
            if used_mode == "user":
                run_tg(_notify, text, group)
            else:
                from . import bot as bot_api

                bot_api.send_text(cfg["notify_chat"], text, group)
        except Exception:
            pass

    return {
        "ok": ok,
        "message": note,
        "group_id": group_id,
        "group_name": group["name"],
        "source": source,
        "mode": used_mode,
        "mode_configured": mode,
        "account": (result or {}).get("account", ""),
        "bot": (result or {}).get("bot", ""),
        "duration": duration,
    }
