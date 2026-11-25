"""FastAPI Web 面板与 API。"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (
    __version__,
    assistant,
    backup,
    bot,
    db,
    keepalive,
    memory,
    scheduler,
    security,
    telegram,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tg-checkin")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = security.SESSION_COOKIE

# 值守模式：同一套代码部署了多个实例时（比如主用 + 备用），
# 备用实例设 STANDBY=1 就只开面板、不启动调度和机器人，
# 否则两个进程会抢同一个 bot 的 getUpdates（Conflict）并重复签到。
STANDBY = os.getenv("STANDBY", "").strip().lower() in ("1", "true", "yes", "on")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # 数据目录里是 bot token、登录会话、AI Key：能收紧的权限一律收紧
    security.harden_permissions(db.DATA_DIR, db.DB_PATH, backup.BACKUP_DIR)
    if security.default_password_in_use():
        log.warning("面板仍在使用默认密码 admin123，任何人都能登录——请立刻在「系统设置」里改掉")
    # 无持久卷的平台（容器重建即清空）先尝试从云端备份把数据接回来
    try:
        backup.auto_restore_on_boot()
    except Exception as exc:
        log.warning("开机自动恢复失败：%s", exc)
    if STANDBY:
        log.warning("STANDBY=1：值守模式，只提供面板，不启动签到调度 / 机器人 / 自动备份")
    else:
        scheduler.start()
        scheduler.start_catchup()
        keepalive.start()
        bot.start()
        backup.start_auto()
        if db.get_setting("assistant_enabled", "0") == "1":
            assistant.start()
    log.info("TG Checkin Panel %s 已启动", __version__)
    yield
    if not STANDBY:
        assistant.stop()
        scheduler.stop_catchup()
        backup.stop_auto()
        bot.stop()
        keepalive.stop()
        scheduler.shutdown()


app = FastAPI(title="TG Checkin Panel", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def guard_requests(request: Request, call_next):
    """统一入口守卫：记公网地址、限体积、防 CSRF、补安全响应头。"""
    path = request.url.path
    host = request.headers.get("host", "")
    scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if host and "keepalive" not in path:
        # 反向代理后面 request.url.scheme 恒为 http，真实协议在 X-Forwarded-Proto
        keepalive.learn_from_request(scheme or request.url.scheme, host)

    # 备份恢复会带一份完整 JSON，正常几十 KB；超过 32MB 直接拒，别让内存被吃光
    if request.method in ("POST", "PUT", "PATCH"):
        raw_length = request.headers.get("content-length", "")
        if raw_length.isdigit() and int(raw_length) > security.MAX_BODY:
            return JSONResponse({"detail": "请求体太大，已拒绝"}, status_code=413)
        # 跨站表单在没有 CORS 时也会带上 cookie，这里再挡一层
        if path.startswith("/api/") and not security.same_origin(request.headers):
            return JSONResponse({"detail": "请求来源不可信，已拒绝"}, status_code=403)

    response = await call_next(request)
    security.apply_headers(response, path, https=(scheme == "https"))
    return response


# ---------------- 鉴权 ----------------

def _panel_password() -> str:
    return db.get_setting("panel_password") or os.getenv("PANEL_PASSWORD", "")


def _client_ip(request: Request) -> str:
    """取真实来源 IP：Cloudflare 会把 cf-connecting-ip 写死成访客地址。

    不清洗 X-Forwarded-For —— 那个头客户端可以随便伪造，用来做限速键等于没限。
    """
    for header in ("cf-connecting-ip", "true-client-ip"):
        value = (request.headers.get(header) or "").strip()
        if value:
            return value
    return request.client.host if request.client else "unknown"


def require_auth(request: Request) -> None:
    password = _panel_password()
    if not password:
        # 没设密码时面板等于裸奔。只允许本机直连做初始化，公网一律拒绝。
        if _client_ip(request) not in ("127.0.0.1", "::1"):
            raise HTTPException(
                status_code=401,
                detail="还没有设置面板密码，请先设置 PANEL_PASSWORD 环境变量",
            )
        return
    if not security.verify_session(request.cookies.get(COOKIE_NAME, ""), password):
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
def panel_login(body: dict[str, Any], request: Request, response: Response) -> dict[str, Any]:
    ip = _client_ip(request)
    wait = security.login_throttle.wait_seconds(ip)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"密码错误次数过多，请 {wait} 秒后再试",
            headers={"Retry-After": str(wait)},
        )
    password = str(body.get("password", ""))
    expected = _panel_password()
    if not expected:
        raise HTTPException(
            status_code=401,
            detail="还没有设置面板密码，请先设置 PANEL_PASSWORD 环境变量",
        )
    if not security.password_ok(password, expected):
        security.login_throttle.fail(ip)
        log.warning("面板登录失败，来源 %s", ip)
        raise HTTPException(status_code=401, detail="面板密码错误")
    security.login_throttle.reset(ip)
    https = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    response.set_cookie(
        COOKIE_NAME,
        security.issue_session(expected),
        httponly=True,
        samesite="lax",
        secure=https or request.url.scheme == "https",
        path="/",
        max_age=security.SESSION_TTL,
    )
    return {"ok": True}


@app.post("/api/panel/logout")
def panel_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path="/")
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
        "standby": STANDBY,
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
            "webdav_dir": settings.get("webdav_dir", ""),
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
            # 智能体记忆 / 账号自动回复
            "memory_enabled": settings.get("memory_enabled", "1"),
            "memory_learn": settings.get("memory_learn", "1"),
            "memory_max_notes": settings.get("memory_max_notes", "8"),
            "assistant_enabled": settings.get("assistant_enabled", "0"),
            "assistant_private": settings.get("assistant_private", "1"),
            "assistant_groups": settings.get("assistant_groups", "0"),
            "assistant_trigger": settings.get("assistant_trigger", "mention"),
            "assistant_whitelist": settings.get("assistant_whitelist", ""),
            "assistant_blacklist": settings.get("assistant_blacklist", ""),
            "assistant_ignore_bots": settings.get("assistant_ignore_bots", "1"),
            "assistant_cooldown": settings.get("assistant_cooldown", "5"),
            "assistant_max_per_hour": settings.get("assistant_max_per_hour", "30"),
            "assistant_quiet": settings.get("assistant_quiet", ""),
            "assistant_account_id": settings.get("assistant_account_id", ""),
            "assistant_agent_id": settings.get("assistant_agent_id", ""),
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
                "ai_agent_id": item.get("ai_agent_id"),
            }
            for item in bots
        ],
        "ai_agents": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "base_url": item.get("base_url"),
                "model": item.get("model"),
                "temperature": item.get("temperature"),
                "max_tokens": item.get("max_tokens"),
                "history_len": item.get("history_len"),
                "min_interval": item.get("min_interval"),
                "proxy": item.get("proxy"),
                "enabled": item.get("enabled"),
                "key_set": item.get("key_set"),
                "is_default": str(item.get("id")) == str(settings.get("ai_agent_id", "")),
            }
            for item in db.list_agents()
        ],
        "ai_agent_id": settings.get("ai_agent_id", ""),
        "catchup": scheduler.catchup_status(),
        "backup": backup.status(),
        "keepalive": keepalive.status(),
        "bot": bot.status(),
        "assistant": assistant.status(),
        "memory": {
            str(item.get("id")): db.note_stats(int(item["id"]))
            for item in db.list_agents()
        },
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
    "bot_enabled", "bot_token", "bot_trigger", "ai_agent_id",
    "ai_base_url", "ai_api_key", "ai_model", "ai_system_prompt",
    "ai_history_len", "ai_max_tokens", "ai_temperature", "ai_min_interval",
    "ai_proxy",
    # 智能体记忆
    "memory_enabled", "memory_learn", "memory_max_notes",
    # 用自己账号自动回复
    "assistant_enabled", "assistant_account_id", "assistant_agent_id",
    "assistant_private", "assistant_groups", "assistant_trigger",
    "assistant_whitelist", "assistant_blacklist", "assistant_ignore_bots",
    "assistant_cooldown", "assistant_max_per_hour", "assistant_quiet",
    # 备份 / 恢复
    "backup_auto", "backup_interval", "backup_keep", "backup_restore_on_boot",
    "webdav_url", "webdav_dir", "webdav_username", "webdav_password", "webdav_auto", "webdav_keep",
    "github_token", "github_repo", "github_branch", "github_path",
    "github_auto", "github_keep",
}

BOT_NUMERIC_KEYS = {"ai_history_len", "ai_max_tokens", "ai_temperature", "ai_min_interval"}
NUMERIC_SETTING_KEYS = {
    "retry_count", "retry_delay", "jitter", "keepalive_interval",
    "catchup_interval", "catchup_cooldown",
    "backup_interval", "backup_keep", "webdav_keep", "github_keep",
    "memory_max_notes", "assistant_cooldown", "assistant_max_per_hour",
}
# 留空表示不修改的字段（避免面板回填掩码后把真实值覆盖成星号）
SECRET_KEYS = {
    "panel_password", "bot_token", "ai_api_key", "webdav_password", "github_token",
}
# 只允许 0 / 1 的开关
TOGGLE_KEYS = {
    "catchup_enabled", "bot_enabled", "backup_auto",
    "backup_restore_on_boot", "webdav_auto", "github_auto",
    "memory_enabled", "memory_learn",
    "assistant_enabled", "assistant_private", "assistant_groups", "assistant_ignore_bots",
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
        if key == "webdav_dir":
            try:
                value = backup.normalize_webdav_dir(value)
            except backup.BackupError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        if key == "ai_agent_id" and value:
            if not value.isdigit() or not db.get_agent(int(value)):
                raise HTTPException(status_code=400, detail="指定的 AI 智能体不存在")
        if key == "assistant_agent_id" and value:
            if not value.isdigit() or not db.get_agent(int(value)):
                raise HTTPException(status_code=400, detail="指定的 AI 智能体不存在")
        if key == "assistant_account_id" and value:
            if not value.isdigit() or not db.get_account(int(value)):
                raise HTTPException(status_code=400, detail="指定的账号不存在")
        if key == "assistant_trigger" and value not in ("mention", "reply", "all"):
            raise HTTPException(status_code=400, detail="assistant_trigger 只能是 mention / reply / all")
        if key == "assistant_quiet" and value:
            import re as _re

            if not _re.fullmatch(r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}", value):
                raise HTTPException(status_code=400, detail="免打扰时段格式应为 23:30-08:00")
        if key == "memory_max_notes" and value:
            db.set_setting(key, str(max(1, min(int(float(value)), 20))))
            changed.add(key)
            continue
        if key == "assistant_cooldown" and value:
            db.set_setting(key, str(max(0, min(int(float(value)), 120))))
            changed.add(key)
            continue
        if key == "assistant_max_per_hour" and value:
            db.set_setting(key, str(max(1, min(int(float(value)), 200))))
            changed.add(key)
            continue
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
    if changed & {"keepalive_enabled", "keepalive_interval", "keepalive_url"} and not STANDBY:
        keepalive.restart()
    if changed & {"bot_enabled", "bot_token", "bot_trigger"} and not STANDBY:
        bot.restart()
    if changed & {"catchup_enabled"} and not STANDBY:
        if db.get_setting("catchup_enabled", "1") == "1":
            scheduler.start_catchup()
    if changed & {"backup_auto", "backup_interval"} and not STANDBY:
        if db.get_setting("backup_auto", "0") == "1":
            backup.start_auto()
    if changed & {
        "assistant_enabled", "assistant_account_id", "assistant_agent_id",
        "assistant_private", "assistant_groups", "assistant_trigger",
        "assistant_whitelist", "assistant_blacklist", "assistant_ignore_bots",
        "assistant_cooldown", "assistant_max_per_hour", "assistant_quiet",
    } and not STANDBY:
        # 开关或范围变了就重启线程，让新配置立刻生效；关掉时 stop 即可
        if db.get_setting("assistant_enabled", "0") == "1":
            assistant.restart()
        else:
            assistant.stop()
    result: dict[str, Any] = {"ok": True}
    # 地址或文件夹刚填好，就把云端目录建出来，省得用户再去别处手动新建
    if changed & {"webdav_url", "webdav_dir", "webdav_username", "webdav_password"}:
        if db.get_setting("webdav_url", "").strip():
            try:
                info = backup.webdav_ensure_dir()
                result["webdav"] = {
                    "ok": True,
                    "dir": info["dir"],
                    "created": len(info.get("created") or []),
                }
            except backup.BackupError as exc:
                result["webdav"] = {"ok": False, "error": str(exc)}
    return result


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
def bot_test_ai(body: dict[str, Any] | None = None, _: None = Depends(require_auth)) -> dict[str, Any]:
    """测 AI 连通性。传 agent_id 就测那个智能体，不传按当前默认的测。"""
    agent = None
    raw = str((body or {}).get("agent_id", "") or "").strip()
    if raw:
        if not raw.isdigit() or not db.get_agent(int(raw)):
            raise HTTPException(status_code=400, detail="指定的 AI 智能体不存在")
        agent = db.get_agent(int(raw))
    else:
        agent = db.agent_for_bot(None)
    try:
        result = bot.test_ai(agent=agent)
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result["agent"] = (agent or {}).get("name", "")
    return result


# ---------------- AI 智能体 ----------------

def _agent_payload(body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="给智能体起个名字，方便区分")
    base_url = bot.normalize_base(str(body.get("base_url", "")))
    if not base_url:
        raise HTTPException(status_code=400, detail="请填写接口地址（例如 https://api.deepseek.com/v1）")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="接口地址要以 http:// 或 https:// 开头")
    model = str(body.get("model", "")).strip()
    if not model:
        raise HTTPException(status_code=400, detail="请填写模型名（例如 deepseek-chat）")
    # Key 留空表示不修改，避免面板回填掩码后把真实 Key 覆盖掉
    api_key = str(body.get("api_key", "")).strip()
    if not api_key and current:
        api_key = str(current.get("api_key") or "")
    if not api_key:
        raise HTTPException(status_code=400, detail="请填写 API Key")

    def number(key: str, default: float, low: float, high: float) -> float:
        raw = body.get(key, default)
        # 面板留空会传 ""，这不是「填错」，而是「用默认值」
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raw = default
        try:
            return max(low, min(float(raw), high))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key} 必须是数字")

    enabled = body.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("1", "true", "on", "yes")
    return {
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "system_prompt": str(body.get("system_prompt", "")).strip(),
        "temperature": f"{number('temperature', 0.7, 0.0, 2.0):g}",
        "max_tokens": int(number("max_tokens", 800, 64, 4096)),
        "history_len": int(number("history_len", 12, 0, 40)),
        "min_interval": int(number("min_interval", 3, 0, 60)),
        "proxy": str(body.get("proxy", "")).strip(),
        "enabled": bool(enabled),
    }


@app.post("/api/ai/models")
def ai_available_models(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    """按接口地址 + Key 拉模型列表。编辑已有智能体时 Key 留空，就用存下来的那把。"""
    agent: dict[str, Any] | None = None
    raw_id = body.get("agent_id")
    if raw_id not in (None, "", 0, "0"):
        try:
            agent = db.get_agent(int(raw_id))
        except (TypeError, ValueError):
            agent = None
    base_url = str(body.get("base_url", "")).strip() or str((agent or {}).get("base_url") or "")
    api_key = str(body.get("api_key", "")).strip() or str((agent or {}).get("api_key") or "")
    proxy = str(body.get("proxy", "")).strip() or str((agent or {}).get("proxy") or "")
    try:
        return bot.list_models(base_url, api_key, proxy)
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/ai/agents")
def list_ai_agents(_: None = Depends(require_auth)) -> dict[str, Any]:
    default_id = db.default_agent_id()
    agents = []
    for item in db.list_agents():
        data = dict(item)
        data.pop("api_key", None)
        data["is_default"] = item["id"] == default_id
        agents.append(data)
    return {"agents": agents, "default_id": default_id}


@app.post("/api/ai/agents")
def create_ai_agent(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    agent_id = db.add_agent(_agent_payload(body))
    # 第一个智能体自动成为默认，省得再点一次
    if not db.get_setting("ai_agent_id", "").strip():
        db.set_setting("ai_agent_id", str(agent_id))
    return {"ok": True, "id": agent_id}


@app.put("/api/ai/agents/{agent_id}")
def edit_ai_agent(agent_id: int, body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    current = db.get_agent(agent_id)
    if not current:
        raise HTTPException(status_code=404, detail="智能体不存在")
    db.update_agent(agent_id, _agent_payload(body, current))
    return {"ok": True}


@app.delete("/api/ai/agents/{agent_id}")
def remove_ai_agent(agent_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.delete_agent(agent_id):
        raise HTTPException(status_code=404, detail="智能体不存在")
    if db.get_setting("ai_agent_id", "") == str(agent_id):
        # 删的是默认智能体，就把默认让给剩下的第一个
        remain = db.default_agent_id()
        db.set_setting("ai_agent_id", str(remain or ""))
    return {"ok": True, "default_id": db.default_agent_id()}


@app.post("/api/ai/agents/{agent_id}/default")
def set_default_ai_agent(agent_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.get_agent(agent_id):
        raise HTTPException(status_code=404, detail="智能体不存在")
    db.set_setting("ai_agent_id", str(agent_id))
    return {"ok": True, "default_id": agent_id}


@app.post("/api/ai/agents/{agent_id}/test")
def test_ai_agent(agent_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    agent = db.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="智能体不存在")
    try:
        return bot.test_ai(agent=agent)
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------- 智能体记忆 ----------------

@app.get("/api/memory")
def list_memory(
    agent_id: int = Query(0),
    scope: str = Query(""),
    limit: int = Query(200),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    if scope and scope not in ("global", "chat", "user"):
        raise HTTPException(status_code=400, detail="scope 只能是 global / chat / user")
    return {
        "notes": db.list_notes(agent_id, scope or None, None, limit),
        "stats": db.note_stats(agent_id),
    }


@app.post("/api/memory")
def add_memory(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    """手动塞一条记忆：面板里补一句「他叫小王，在杭州做后端」比等模型自己悟更可靠。"""
    try:
        agent_id = int(body.get("agent_id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="agent_id 必须是数字")
    content = str(body.get("content", "")).strip()
    if not content:
        raise HTTPException(status_code=400, detail="记忆内容不能为空")
    if len(content) > 300:
        raise HTTPException(status_code=400, detail="一条记忆别超过 300 字，拆成几条更好用")
    scope = str(body.get("scope", "global")).strip() or "global"
    if scope not in ("global", "chat", "user"):
        raise HTTPException(status_code=400, detail="scope 只能是 global / chat / user")
    scope_key = str(body.get("scope_key", "")).strip()
    if scope != "global" and not scope_key:
        raise HTTPException(status_code=400, detail="按会话或按人记的时候，要填会话 ID 或对方名字")
    note_id = db.add_note(agent_id, scope, scope_key, content, source="manual")
    return {"ok": True, "id": note_id, "stats": db.note_stats(agent_id)}


@app.delete("/api/memory/{note_id}")
def remove_memory(note_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.delete_note(note_id):
        raise HTTPException(status_code=404, detail="这条记忆不存在")
    return {"ok": True}


@app.delete("/api/memory")
def clear_memory(
    agent_id: int = Query(0),
    scope: str = Query(""),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    removed = db.clear_notes(agent_id, scope or "")
    return {"ok": True, "removed": removed, "stats": db.note_stats(agent_id)}


# ---------------- 用我的账号自动回复 ----------------

@app.get("/api/assistant")
def assistant_state(_: None = Depends(require_auth)) -> dict[str, Any]:
    return assistant.status()


@app.post("/api/assistant/restart")
def assistant_restart(_: None = Depends(require_auth)) -> dict[str, Any]:
    if db.get_setting("assistant_enabled", "0") != "1":
        return {"ok": False, "error": "先打开「用我的账号自动回复」再启动", **assistant.status()}
    return {"ok": True, **assistant.restart()}


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


def _bot_agent_id(body: dict[str, Any]) -> int | None:
    """机器人绑定哪个智能体；0 / 空表示跟随全局默认。"""
    raw = body.get("ai_agent_id")
    if raw in ("", None, 0, "0"):
        return None
    try:
        agent_id = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="ai_agent_id 必须是数字")
    if not db.get_agent(agent_id):
        raise HTTPException(status_code=400, detail="指定的 AI 智能体不存在")
    return agent_id


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
            "ai_agent_id": _bot_agent_id(body),
        }
    )
    if not STANDBY:
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
    if "ai_agent_id" in body:
        fields["ai_agent_id"] = _bot_agent_id(body)
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


@app.post("/api/bots/{bot_id}/toggle")
def toggle_bot(
    bot_id: int, body: dict[str, Any] | None = None, _: None = Depends(require_auth)
) -> dict[str, Any]:
    """一键停用 / 启用：只动 enabled，不碰 token、群组绑定这些配置。

    不传 body 就是「取反」，传 {"enabled": true/false} 则设成指定值。
    """
    record = db.get_bot(bot_id)
    if not record:
        raise HTTPException(status_code=404, detail="机器人不存在")
    wanted = (body or {}).get("enabled")
    target = bool(wanted) if wanted is not None else not bool(record.get("enabled"))
    db.update_bot(bot_id, {"enabled": target})
    if STANDBY:
        # 值守模式本来就不跑后台任务，只落库
        pass
    elif target:
        bot.start(bot_id)
    else:
        bot.stop(bot_id)
    detail = bot.bot_detail(bot_id)
    detail["ok"] = True
    return detail


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
    "webdav_url", "webdav_dir", "webdav_username", "webdav_password", "webdav_auto", "webdav_keep",
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
