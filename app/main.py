"""FastAPI Web 面板与 API。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, backup, bot, db, keepalive, scheduler, telegram

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tg-checkin")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "tgck_session"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # 无持久卷的平台（容器重建即清空）先尝试从云端备份把数据接回来
    try:
        backup.auto_restore_on_boot()
    except Exception as exc:
        log.warning("开机自动恢复失败：%s", exc)
    scheduler.start()
    scheduler.start_catchup()
    keepalive.start()
    bot.start()
    backup.start_auto()
    log.info("TG Checkin Panel %s 已启动", __version__)
    yield
    scheduler.stop_catchup()
    backup.stop_auto()
    bot.stop()
    keepalive.stop()
    scheduler.shutdown()


app = FastAPI(title="TG Checkin Panel", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def learn_public_host(request: Request, call_next):
    """从外部请求里学出公网地址，供保活自检使用。"""
    host = request.headers.get("host", "")
    if host and "keepalive" not in request.url.path:
        # 反向代理后面 request.url.scheme 恒为 http，真实协议在 X-Forwarded-Proto
        scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        keepalive.learn_from_request(scheme or request.url.scheme, host)
    return await call_next(request)


# ---------------- 鉴权 ----------------

def _panel_password() -> str:
    return db.get_setting("panel_password") or os.getenv("PANEL_PASSWORD", "")


def _auth_token(password: str) -> str:
    return hashlib.sha256(f"tgck-panel|{password}".encode("utf-8")).hexdigest()


def require_auth(request: Request) -> None:
    password = _panel_password()
    if password and request.cookies.get(COOKIE_NAME) != _auth_token(password):
        raise HTTPException(status_code=401, detail="请先登录管理面板")


def mask(value: str, keep: int = 3) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return text[:keep] + "***" + text[-keep:]


# ---------------- 页面 ----------------

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


# ---------------- 面板登录 ----------------

@app.post("/api/panel/login")
def panel_login(body: dict[str, Any], response: Response) -> dict[str, Any]:
    password = str(body.get("password", ""))
    expected = _panel_password()
    if not expected or password != expected:
        raise HTTPException(status_code=401, detail="面板密码错误")
    response.set_cookie(
        COOKIE_NAME,
        _auth_token(expected),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return {"ok": True}


@app.post("/api/panel/logout")
def panel_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


# ---------------- 概览 ----------------

@app.get("/api/bootstrap")
def bootstrap(_: None = Depends(require_auth)) -> dict[str, Any]:
    settings = db.get_settings()
    runs = scheduler.next_runs()
    accounts = db.list_accounts()
    bots = db.list_bots()
    account_default = db.default_account()
    authorized = bool(account_default and account_default.get("authorized"))
    return {
        "version": __version__,
        "groups": db.list_groups(),
        "logs": db.list_logs(limit=60),
        "stats": db.stats(),
        "next_runs": runs,
        "scheduler_jobs": scheduler.job_count(),
        "settings": {
            "api_id": mask(settings.get("api_id", ""), 2),
            "api_hash": mask(settings.get("api_hash", ""), 4),
            "phone": mask(settings.get("phone", ""), 3),
            "proxy_url": mask(settings.get("proxy_url", "")),
            "timezone": settings.get("timezone", "Asia/Shanghai"),
            "retry_count": settings.get("retry_count", "2"),
            "retry_delay": settings.get("retry_delay", "10"),
            "jitter": settings.get("jitter", "5"),
            "notify_chat": settings.get("notify_chat", ""),
            "fail_keywords": settings.get("fail_keywords", ""),
            "dry_run": settings.get("dry_run", "0"),
            "checkin_mode": settings.get("checkin_mode", "bot"),
            "catchup_enabled": settings.get("catchup_enabled", "1"),
            "catchup_interval": settings.get("catchup_interval", "600"),
            "catchup_cooldown": settings.get("catchup_cooldown", "1800"),
            "password_set": bool(_panel_password()),
            "using_default_password": _panel_password() == "admin123",
            "session_set": bool(settings.get("session_string", "")) or authorized,
            "account_name": (account_default or {}).get("name", "") or settings.get("account_name", ""),
            "keepalive_enabled": settings.get("keepalive_enabled", "0"),
            "keepalive_interval": settings.get("keepalive_interval", "300"),
            "keepalive_url": settings.get("keepalive_url", ""),
            # 备份 / 恢复
            "backup_auto": settings.get("backup_auto", "0"),
            "backup_interval": settings.get("backup_interval", "12"),
            "backup_keep": settings.get("backup_keep", "14"),
            "backup_restore_on_boot": settings.get("backup_restore_on_boot", "1"),
            "webdav_url": settings.get("webdav_url", ""),
            "webdav_username": settings.get("webdav_username", ""),
            "webdav_password": mask(settings.get("webdav_password", "")),
            "webdav_auto": settings.get("webdav_auto", "0"),
            "webdav_keep": settings.get("webdav_keep", "14"),
            "github_token": mask(settings.get("github_token", "")),
            "github_repo": settings.get("github_repo", ""),
            "github_branch": settings.get("github_branch", "main"),
            "github_path": settings.get("github_path", "tgck-backups"),
            "github_auto": settings.get("github_auto", "0"),
            "github_keep": settings.get("github_keep", "14"),
        },
        "account": {
            "authorized": authorized,
            "name": (account_default or {}).get("name", ""),
            "phone": mask((account_default or {}).get("phone", ""), 3),
        },
        "accounts": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "phone": mask(item.get("phone", ""), 3),
                "authorized": item.get("authorized"),
                "enabled": item.get("enabled"),
            }
            for item in accounts
        ],
        "bots": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "username": item.get("username"),
                "enabled": item.get("enabled"),
                "token_set": item.get("token_set"),
                "trigger": item.get("trigger"),
            }
            for item in bots
        ],
        "catchup": scheduler.catchup_status(),
        "backup": backup.status(),
        "keepalive": keepalive.status(),
        "bot": bot.status(),
    }


# ---------------- 群组管理 ----------------

def _normalize_group(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("name", "")).strip()
    chat = str(body.get("chat", "")).strip()
    command = str(body.get("command", "")).strip() or "签到"
    if not chat:
        raise HTTPException(status_code=400, detail="请填写群组链接、用户名或 ID")

    raw = body.get("schedule", ["0 8 * * *"])
    if isinstance(raw, str):
        expressions = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    elif isinstance(raw, list):
        expressions = [str(p).strip() for p in raw if str(p).strip()]
    else:
        expressions = []
    if not expressions:
        raise HTTPException(status_code=400, detail="至少填写一个签到时间")

    for expression in expressions:
        try:
            scheduler.parse_expression(expression)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"cron 表达式无效：{expression}（{exc}）")

    enabled = body.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("1", "true", "on", "yes")

    # 每个群可以单独选执行者；空值表示跟随全局默认
    account_id = body.get("account_id")
    if account_id in ("", None, "0", 0):
        account_id = None
    else:
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="account_id 必须是数字")
        if not db.get_account(account_id):
            raise HTTPException(status_code=400, detail="指定的 Telegram 账号不存在")

    bot_id = body.get("bot_id")
    if bot_id in ("", None, "0", 0):
        bot_id = None
    else:
        try:
            bot_id = int(bot_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="bot_id 必须是数字")
        if not db.get_bot(bot_id):
            raise HTTPException(status_code=400, detail="指定的机器人不存在")

    mode = str(body.get("mode", "") or "").strip().lower()
    if mode not in ("", "bot", "user"):
        raise HTTPException(status_code=400, detail="签到方式只能是 默认 / bot / user")

    return {
        "name": name or chat,
        "chat": chat,
        "command": command,
        "schedule": json.dumps(expressions, ensure_ascii=False),
        "enabled": bool(enabled),
        "account_id": account_id,
        "bot_id": bot_id,
        "mode": mode,
    }


@app.get("/api/groups")
def list_groups(_: None = Depends(require_auth)) -> dict[str, Any]:
    return {"groups": db.list_groups(), "next_runs": scheduler.next_runs()}


@app.post("/api/groups")
def create_group(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    data = _normalize_group(body)
    group_id = db.add_group(data)
    scheduler.reload_jobs()
    return {"ok": True, "id": group_id}


@app.put("/api/groups/{group_id}")
def edit_group(group_id: int, body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.update_group(group_id, _normalize_group(body)):
        raise HTTPException(status_code=404, detail="群组不存在")
    scheduler.reload_jobs()
    return {"ok": True}


@app.delete("/api/groups/{group_id}")
def remove_group(group_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.delete_group(group_id):
        raise HTTPException(status_code=404, detail="群组不存在")
    scheduler.reload_jobs()
    return {"ok": True}


@app.post("/api/groups/{group_id}/test")
def test_group(group_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    result = telegram.execute_checkin(group_id, source="manual")
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["message"])
    return result


@app.post("/api/cron/validate")
def validate_cron(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    expression = str(body.get("expression", "")).strip()
    try:
        trigger = scheduler.parse_expression(expression)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "trigger": str(trigger)}


# ---------------- 日志 ----------------

@app.get("/api/logs")
def get_logs(
    limit: int = Query(100, ge=1, le=1000),
    status: str = Query(""),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    return {"logs": db.list_logs(limit=limit, status=status)}


@app.delete("/api/logs")
def delete_logs(_: None = Depends(require_auth)) -> dict[str, Any]:
    db.clear_logs()
    return {"ok": True}


# ---------------- 系统设置 ----------------

SETTING_KEYS = {
    "api_id", "api_hash", "phone", "proxy_url", "timezone", "retry_count",
    "retry_delay", "jitter", "notify_chat", "fail_keywords", "dry_run",
    "panel_password", "keepalive_enabled", "keepalive_interval", "keepalive_url",
    "checkin_mode", "catchup_enabled", "catchup_interval", "catchup_cooldown",
    # AI 机器人
    "bot_enabled", "bot_token", "bot_trigger",
    "ai_base_url", "ai_api_key", "ai_model", "ai_system_prompt",
    "ai_history_len", "ai_max_tokens", "ai_temperature", "ai_min_interval",
    "ai_proxy",
    # 备份 / 恢复
    "backup_auto", "backup_interval", "backup_keep", "backup_restore_on_boot",
    "webdav_url", "webdav_username", "webdav_password", "webdav_auto", "webdav_keep",
    "github_token", "github_repo", "github_branch", "github_path",
    "github_auto", "github_keep",
}

BOT_NUMERIC_KEYS = {"ai_history_len", "ai_max_tokens", "ai_temperature", "ai_min_interval"}
NUMERIC_SETTING_KEYS = {
    "retry_count", "retry_delay", "jitter", "keepalive_interval",
    "catchup_interval", "catchup_cooldown",
    "backup_interval", "backup_keep", "webdav_keep", "github_keep",
}
# 留空表示不修改的字段（避免面板回填掩码后把真实值覆盖成星号）
SECRET_KEYS = {
    "panel_password", "bot_token", "ai_api_key", "webdav_password", "github_token",
}
# 只允许 0 / 1 的开关
TOGGLE_KEYS = {
    "catchup_enabled", "bot_enabled", "backup_auto",
    "backup_restore_on_boot", "webdav_auto", "github_auto",
}


@app.put("/api/settings")
def update_settings(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    changed: set[str] = set()
    for key in SETTING_KEYS:
        if key not in body:
            continue
        value = str(body[key]).strip()
        if key == "timezone":
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(value)
            except Exception:
                raise HTTPException(status_code=400, detail=f"无效时区：{value}")
        if key in NUMERIC_SETTING_KEYS:
            try:
                number = float(value)
                if number < 0:
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=400, detail=f"{key} 必须是非负数")
        if key == "catchup_interval" and value:
            db.set_setting(key, str(max(120, min(int(float(value)), 3600))))
            changed.add(key)
            continue
        if key == "catchup_cooldown" and value:
            db.set_setting(key, str(max(0, int(float(value)))))
            changed.add(key)
            continue
        if key in BOT_NUMERIC_KEYS and value:
            try:
                number = float(value)
                if number < 0:
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=400, detail=f"{key} 必须是非负数")
        if key in SECRET_KEYS and not value:
            continue  # 留空表示不修改
        if key == "bot_trigger" and value not in ("mention", "all", "private"):
            raise HTTPException(status_code=400, detail="bot_trigger 只能是 mention / all / private")
        if key == "checkin_mode" and value not in ("bot", "user"):
            raise HTTPException(status_code=400, detail="checkin_mode 只能是 bot / user")
        if key in TOGGLE_KEYS and value not in ("0", "1"):
            raise HTTPException(status_code=400, detail=f"{key} 只能是 0 或 1")
        if key == "backup_interval" and value:
            db.set_setting(key, str(max(1, min(int(float(value)), 168))))
            changed.add(key)
            continue
        if key in ("backup_keep", "webdav_keep", "github_keep") and value:
            db.set_setting(key, str(max(1, min(int(float(value)), 200))))
            changed.add(key)
            continue
        db.set_setting(key, value)
        changed.add(key)
    if "timezone" in changed:
        scheduler.reload_jobs()
    if changed & {"keepalive_enabled", "keepalive_interval", "keepalive_url"}:
        keepalive.restart()
    if changed & {"bot_enabled", "bot_token", "bot_trigger"}:
        bot.restart()
    if changed & {"catchup_enabled"}:
        if db.get_setting("catchup_enabled", "1") == "1":
            scheduler.start_catchup()
    if changed & {"backup_auto", "backup_interval"}:
        if db.get_setting("backup_auto", "0") == "1":
            backup.start_auto()
    return {"ok": True}


# ---------------- 保活自检 ----------------

@app.get("/api/keepalive")
def keepalive_status(_: None = Depends(require_auth)) -> dict[str, Any]:
    return keepalive.status()


@app.post("/api/keepalive/ping")
def keepalive_ping_now(_: None = Depends(require_auth)) -> dict[str, Any]:
    """立即执行一次自检，返回最新状态。"""
    if not keepalive.enabled():
        raise HTTPException(status_code=400, detail="保活未启用，请先在设置里打开")
    if not keepalive.target_url():
        raise HTTPException(
            status_code=400,
            detail="还没有探测到公网地址，请填写保活目标地址，或先用公网地址访问一次面板",
        )
    return keepalive.ping_now()


# ---------------- AI 聊天机器人 ----------------

@app.get("/api/bot/status")
def bot_status(_: None = Depends(require_auth)) -> dict[str, Any]:
    return bot.status()


@app.get("/api/bot/config")
def bot_config(_: None = Depends(require_auth)) -> dict[str, Any]:
    """面板表单回填用：返回原文，密钥除外。"""
    settings = db.get_settings()
    return {
        "bot_token": settings.get("bot_token", ""),
        "ai_base_url": settings.get("ai_base_url", ""),
        "ai_model": settings.get("ai_model", ""),
        "ai_api_key_set": bool(settings.get("ai_api_key", "")),
        "ai_system_prompt": settings.get("ai_system_prompt", ""),
        "ai_history_len": settings.get("ai_history_len", "12"),
        "ai_max_tokens": settings.get("ai_max_tokens", "800"),
        "ai_temperature": settings.get("ai_temperature", "0.7"),
        "ai_min_interval": settings.get("ai_min_interval", "3"),
        "ai_proxy": settings.get("ai_proxy", ""),
    }


@app.get("/api/bot/chats")
def bot_chats(bot_id: int | None = Query(None), _: None = Depends(require_auth)) -> dict[str, Any]:
    """机器人待过的群，面板可以直接点选填进群组表单。"""
    if bot_id:
        return {"chats": bot.known_chats(bot_id)}
    # 不指定机器人时把所有机器人的群合并去重，方便直接挑
    seen: dict[Any, dict[str, Any]] = {}
    for record in db.list_bots() or [None]:
        for chat in bot.known_chats((record or {}).get("id")):
            seen.setdefault(chat.get("id"), {**chat, "bot_id": (record or {}).get("id")})
    return {"chats": list(seen.values())}


@app.post("/api/bot/verify")
def bot_verify(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    """校验 Bot Token，成功则保存到默认机器人（老接口，兼容面板旧按钮）。"""
    token = str(body.get("token", "")).strip() or db.get_setting("bot_token", "")
    bot_id = body.get("bot_id")
    try:
        bot_id = int(bot_id) if bot_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bot_id 必须是数字")
    try:
        info = bot.verify_token(token, bot_id)
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    record = db.get_bot(bot_id) if bot_id else db.default_bot()
    if record and record.get("id"):
        db.update_bot(int(record["id"]), {"token": token, "username": info.get("username", "")})
    else:
        db.set_setting("bot_token", token)
    bot.restart(bot_id)
    return info


@app.post("/api/bot/test-ai")
def bot_test_ai(_: None = Depends(require_auth)) -> dict[str, Any]:
    try:
        return bot.test_ai()
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/bot/send")
def bot_send(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    """从面板发一条测试消息到指定会话。"""
    bot_id = body.get("bot_id")
    try:
        bot_id = int(bot_id) if bot_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bot_id 必须是数字")
    try:
        return bot.send_test_message(
            str(body.get("chat_id", "")),
            str(body.get("text", "测试消息")) or "测试消息",
            bot_id,
        )
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/bot/messages")
def bot_messages(
    limit: int = Query(60),
    bot_id: int | None = Query(None),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    return {
        "messages": db.list_ai_messages(limit, bot_id),
        "stats": db.ai_stats(bot_id),
    }


@app.delete("/api/bot/messages")
def bot_messages_clear(bot_id: int | None = Query(None), _: None = Depends(require_auth)) -> dict[str, Any]:
    db.clear_ai_messages(bot_id)
    return {"ok": True}


@app.post("/api/bot/restart")
def bot_restart(body: dict[str, Any] | None = None, _: None = Depends(require_auth)) -> dict[str, Any]:
    bot_id = (body or {}).get("bot_id")
    try:
        bot_id = int(bot_id) if bot_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bot_id 必须是数字")
    bot.restart(bot_id)
    return bot.status(bot_id)


# ---------------- 多账号管理 ----------------

@app.get("/api/accounts")
def list_accounts(_: None = Depends(require_auth)) -> dict[str, Any]:
    accounts = db.list_accounts()
    default = db.default_account()
    return {
        "accounts": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "phone": mask(item.get("phone", ""), 3),
                "api_id": item.get("api_id", ""),
                "proxy_url": item.get("proxy_url", ""),
                "authorized": item.get("authorized"),
                "enabled": item.get("enabled"),
                "created_at": item.get("created_at"),
                "default": bool(default and default.get("id") == item.get("id")),
            }
            for item in accounts
        ],
        "count": len(accounts),
    }


@app.post("/api/accounts")
def create_account(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    name = str(body.get("name", "")).strip()
    phone = str(body.get("phone", "")).strip()
    if not name and not phone:
        raise HTTPException(status_code=400, detail="请填写账号备注名或手机号")
    account_id = db.add_account(
        {
            "name": name or phone,
            "phone": phone,
            "api_id": str(body.get("api_id", "")).strip(),
            "api_hash": str(body.get("api_hash", "")).strip(),
            "proxy_url": str(body.get("proxy_url", "")).strip(),
            "enabled": bool(body.get("enabled", True)),
        }
    )
    return {"ok": True, "id": account_id}


@app.put("/api/accounts/{account_id}")
def edit_account(account_id: int, body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key in ("name", "phone", "api_id", "api_hash", "proxy_url"):
        if key in body:
            fields[key] = str(body[key]).strip()
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])
    # 会话留空表示不修改，避免面板回填掩码把真实会话覆盖掉
    if str(body.get("session_string", "")).strip():
        fields["session_string"] = str(body["session_string"]).strip()
    if not db.update_account(account_id, fields):
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
def remove_account(account_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.delete_account(account_id):
        raise HTTPException(status_code=404, detail="账号不存在")
    scheduler.reload_jobs()
    return {"ok": True}


@app.post("/api/accounts/{account_id}/verify")
def verify_account(account_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    """连一次 Telegram 确认这个账号的会话还能用。"""
    if not db.get_account(account_id):
        raise HTTPException(status_code=404, detail="账号不存在")
    result = telegram.account_status(account_id)
    if not result.get("authorized"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "这个账号还没登录，请用扫码或验证码登录",
        )
    return result


# ---------------- 多机器人管理 ----------------

@app.get("/api/bots")
def list_bots(_: None = Depends(require_auth)) -> dict[str, Any]:
    bots = db.list_bots()
    default = db.default_bot()
    return {
        "bots": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "username": item.get("username"),
                "enabled": item.get("enabled"),
                "token_set": item.get("token_set"),
                "trigger": item.get("trigger"),
                "known_chats": item.get("known_chats", ""),
                "status": bot.bot_detail(item["id"]) if item.get("id") else {},
                "default": bool(default and default.get("id") == item.get("id")),
            }
            for item in bots
        ],
        "count": len(bots),
    }


@app.post("/api/bots")
def create_bot(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    token = str(body.get("token", "")).strip()
    if not token:
        raise HTTPException(status_code=400, detail="请填写 Bot Token")
    try:
        info = bot.verify_token(token)
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    bot_id = db.add_bot(
        {
            "name": str(body.get("name", "")).strip() or info.get("name") or info.get("username"),
            "token": token,
            "username": info.get("username", ""),
            "enabled": bool(body.get("enabled", True)),
            "trigger": str(body.get("trigger", "mention") or "mention"),
        }
    )
    bot.start(bot_id)
    return {"ok": True, "id": bot_id, "username": info.get("username", ""), "name": info.get("name", "")}


@app.put("/api/bots/{bot_id}")
def edit_bot(bot_id: int, body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.get_bot(bot_id):
        raise HTTPException(status_code=404, detail="机器人不存在")
    fields: dict[str, Any] = {}
    for key in ("name", "username"):
        if key in body:
            fields[key] = str(body[key]).strip()
    if "trigger" in body:
        trigger = str(body["trigger"] or "mention")
        if trigger not in ("mention", "all", "private"):
            raise HTTPException(status_code=400, detail="触发方式只能是 mention / all / private")
        fields["trigger"] = trigger
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])
    token = str(body.get("token", "")).strip()
    if token:
        try:
            info = bot.verify_token(token, bot_id)
        except bot.BotError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        fields["token"] = token
        fields.setdefault("username", info.get("username", ""))
        if not fields.get("name"):
            fields["name"] = info.get("name", "")
    db.update_bot(bot_id, fields)
    bot.restart(bot_id)
    return {"ok": True}


@app.delete("/api/bots/{bot_id}")
def remove_bot(bot_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    bot.stop(bot_id)
    if not db.delete_bot(bot_id):
        raise HTTPException(status_code=404, detail="机器人不存在")
    scheduler.reload_jobs()
    return {"ok": True}


@app.post("/api/bots/{bot_id}/verify")
def verify_bot(bot_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    record = db.get_bot(bot_id)
    if not record:
        raise HTTPException(status_code=404, detail="机器人不存在")
    try:
        info = bot.verify_token(str(record.get("token") or ""), bot_id)
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return info


@app.post("/api/bots/{bot_id}/restart")
def restart_bot(bot_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.get_bot(bot_id):
        raise HTTPException(status_code=404, detail="机器人不存在")
    bot.restart(bot_id)
    return bot.bot_detail(bot_id)


# ---------------- 多账号：扫码 / 验证码登录（按账号） ----------------

@app.post("/api/tg/qr/start")
def tg_qr_start(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    account_id = body.get("account_id")
    try:
        account_id = int(account_id) if account_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="account_id 必须是数字")
    try:
        return telegram.qr_start(
            api_id=str(body.get("api_id", "")),
            api_hash=str(body.get("api_hash", "")),
            proxy_url=str(body.get("proxy_url", "")),
            account_id=account_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=telegram.friendly_error(exc))


@app.post("/api/tg/login-start")
def tg_login_start(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    account_id = body.get("account_id")
    try:
        account_id = int(account_id) if account_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="account_id 必须是数字")
    try:
        return telegram.login_start(
            phone=str(body.get("phone", "")),
            api_id=str(body.get("api_id", "")),
            api_hash=str(body.get("api_hash", "")),
            proxy_url=str(body.get("proxy_url", "")),
            account_id=account_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=telegram.friendly_error(exc))


@app.post("/api/tg/logout")
def tg_logout(body: dict[str, Any] | None = None, _: None = Depends(require_auth)) -> dict[str, Any]:
    account_id = (body or {}).get("account_id")
    try:
        account_id = int(account_id) if account_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="account_id 必须是数字")
    telegram.logout(account_id)
    return {"ok": True}


# ---------------- 补签 ----------------

@app.get("/api/catchup")
def catchup_info(_: None = Depends(require_auth)) -> dict[str, Any]:
    return scheduler.catchup_status()


@app.post("/api/catchup/run")
def catchup_run(_: None = Depends(require_auth)) -> dict[str, Any]:
    """立刻做一次补签检查（用来验证「上线就补签」是否生效）。"""
    results = scheduler.catch_up("面板手动")
    return {
        "ok": True,
        "count": len(results),
        "results": results,
        "message": f"本次补签 {len(results)} 个群组" if results else "所有群组今天都已签到成功，无需补签",
    }


# ---------------- 备份 / 恢复 / 云备份 ----------------

def _backup_guard(func, *args, **kwargs) -> Any:
    """把备份模块的异常翻成 400，面板就能直接显示原因。"""
    try:
        return func(*args, **kwargs)
    except backup.BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/backup/status")
def backup_status(_: None = Depends(require_auth)) -> dict[str, Any]:
    return backup.status()


@app.get("/api/backup/export")
def backup_export(include_logs: int = Query(1), _: None = Depends(require_auth)) -> Response:
    """下载完整备份（含登录会话、账号与机器人凭据）。"""
    bundle = backup.build_bundle(include_logs=bool(include_logs))
    body = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="tgck-{stamp}.json"'},
    )


@app.post("/api/backup/restore")
def backup_restore(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    """用上传的备份 JSON 恢复。body: {data: <备份对象>, mode, restore_password}"""
    data = body.get("data")
    mode = str(body.get("mode") or "replace")
    result = _backup_guard(
        backup.restore_bundle,
        data,
        mode,
        bool(body.get("restore_password", False)),
    )
    scheduler.reload_jobs()
    if db.get_setting("backup_auto", "0") == "1":
        backup.start_auto()
    return result


@app.get("/api/backup/local")
def backup_local_list(_: None = Depends(require_auth)) -> dict[str, Any]:
    return {"files": backup.list_local()}


@app.post("/api/backup/local")
def backup_local_create(_: None = Depends(require_auth)) -> dict[str, Any]:
    return {"ok": True, **backup.save_local()}


@app.post("/api/backup/local/{name}/restore")
def backup_local_restore(name: str, _: None = Depends(require_auth)) -> dict[str, Any]:
    bundle = _backup_guard(backup.load_local, name)
    result = _backup_guard(backup.restore_bundle, bundle, "replace", True)
    scheduler.reload_jobs()
    return result


@app.delete("/api/backup/local/{name}")
def backup_local_delete(name: str, _: None = Depends(require_auth)) -> dict[str, Any]:
    if not _backup_guard(backup.delete_local, name):
        raise HTTPException(status_code=404, detail="备份文件不存在")
    return {"ok": True}


@app.post("/api/backup/run")
def backup_run_now(_: None = Depends(require_auth)) -> dict[str, Any]:
    """立刻按当前开关跑一次备份（本地 + 云端）。"""
    return backup.run_auto()


@app.post("/api/backup/webdav/test")
def backup_webdav_test(_: None = Depends(require_auth)) -> dict[str, Any]:
    return _backup_guard(backup.webdav_test)


@app.get("/api/backup/webdav")
def backup_webdav_list(_: None = Depends(require_auth)) -> dict[str, Any]:
    return {"files": _backup_guard(backup.webdav_list)}


@app.post("/api/backup/webdav/upload")
def backup_webdav_upload(_: None = Depends(require_auth)) -> dict[str, Any]:
    result = _backup_guard(backup.webdav_upload)
    db.set_setting("backup_last_run", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    db.set_setting("backup_last_target", "webdav")
    db.set_setting("backup_last_error", "")
    return result


@app.post("/api/backup/webdav/restore")
def backup_webdav_restore(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    bundle = _backup_guard(backup.webdav_download, name)
    result = _backup_guard(backup.restore_bundle, bundle, "replace", True)
    scheduler.reload_jobs()
    return result


@app.post("/api/backup/github/test")
def backup_github_test(_: None = Depends(require_auth)) -> dict[str, Any]:
    return _backup_guard(backup.github_test)


@app.get("/api/backup/github")
def backup_github_list(_: None = Depends(require_auth)) -> dict[str, Any]:
    return {"files": _backup_guard(backup.github_list)}


@app.post("/api/backup/github/upload")
def backup_github_upload(_: None = Depends(require_auth)) -> dict[str, Any]:
    result = _backup_guard(backup.github_upload)
    db.set_setting("backup_last_run", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    db.set_setting("backup_last_target", "github")
    db.set_setting("backup_last_error", "")
    return result


@app.post("/api/backup/github/restore")
def backup_github_restore(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    bundle = _backup_guard(backup.github_download, name)
    result = _backup_guard(backup.restore_bundle, bundle, "replace", True)
    scheduler.reload_jobs()
    return result


# ---------------- 配置导出（用于无持久卷环境） ----------------

EXPORT_KEYS = {
    "api_id", "api_hash", "phone", "proxy_url", "session_string", "account_name",
    "timezone", "retry_count", "retry_delay", "jitter", "notify_chat",
    "fail_keywords", "dry_run", "checkin_mode",
    "catchup_enabled", "catchup_interval", "catchup_cooldown",
    "bot_enabled", "bot_token", "bot_trigger", "ai_base_url", "ai_api_key",
    "ai_model", "ai_system_prompt", "ai_history_len", "ai_max_tokens",
    "ai_temperature", "ai_min_interval", "ai_proxy", "business_connections",
    "known_chats",
    # 备份 / 云备份：一起带走，换平台时新容器能直接接上
    "backup_auto", "backup_interval", "backup_keep", "backup_restore_on_boot",
    "webdav_url", "webdav_username", "webdav_password", "webdav_auto", "webdav_keep",
    "github_token", "github_repo", "github_branch", "github_path",
    "github_auto", "github_keep",
}


@app.get("/api/config/export")
def export_config(_: None = Depends(require_auth)) -> dict[str, Any]:
    """导出可写入 TGCK_CONFIG 环境变量的完整配置（不含面板密码）。"""
    settings = db.get_settings()
    return {
        "settings": {key: settings.get(key, "") for key in sorted(EXPORT_KEYS)},
        "groups": db.list_groups(),
        # 多账号 / 多机器人也一起导出，否则容器重建后这些身份会丢
        "accounts": db.list_accounts(),
        "bots": db.list_bots(),
    }


# ---------------- Telegram 账号 ----------------

@app.get("/api/tg/credentials")
def tg_credentials(_: None = Depends(require_auth)) -> dict[str, str]:
    """面板表单回填用：凭据仅在此接口返回原文。"""
    settings = db.get_settings()
    default = db.default_account() or {}
    return {
        "api_id": str(default.get("api_id") or settings.get("api_id", "")),
        "api_hash": str(default.get("api_hash") or settings.get("api_hash", "")),
        "phone": str(default.get("phone") or settings.get("phone", "")),
        "proxy_url": str(default.get("proxy_url") or settings.get("proxy_url", "")),
    }


@app.get("/api/tg/status")
def tg_status(account_id: int | None = Query(None), _: None = Depends(require_auth)) -> dict[str, Any]:
    return telegram.account_status(account_id)


@app.post("/api/tg/login-verify")
def tg_login_verify(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    try:
        return telegram.login_verify(
            code=str(body.get("code", "")), password=str(body.get("password", ""))
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=telegram.friendly_error(exc))


# ---------------- 扫码登录 ----------------

@app.get("/api/tg/qr/status")
def tg_qr_status(_: None = Depends(require_auth)) -> dict[str, Any]:
    return telegram.qr_status()


@app.post("/api/tg/qr/refresh")
def tg_qr_refresh(_: None = Depends(require_auth)) -> dict[str, Any]:
    try:
        return telegram.qr_refresh()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=telegram.friendly_error(exc))


@app.post("/api/tg/qr/password")
def tg_qr_password(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    try:
        return telegram.qr_password_submit(str(body.get("password", "")))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=telegram.friendly_error(exc))


@app.post("/api/tg/qr/cancel")
def tg_qr_cancel(_: None = Depends(require_auth)) -> dict[str, Any]:
    telegram.qr_cancel()
    return {"ok": True}
