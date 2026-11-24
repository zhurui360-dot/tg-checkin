"""FastAPI Web 面板与 API。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, db, keepalive, scheduler, telegram

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
    scheduler.start()
    keepalive.start()
    log.info("TG Checkin Panel %s 已启动", __version__)
    yield
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
            "password_set": bool(_panel_password()),
            "using_default_password": _panel_password() == "admin123",
            "session_set": bool(settings.get("session_string", "")),
            "account_name": settings.get("account_name", ""),
            "keepalive_enabled": settings.get("keepalive_enabled", "0"),
            "keepalive_interval": settings.get("keepalive_interval", "300"),
            "keepalive_url": settings.get("keepalive_url", ""),
        },
        "account": {
            "authorized": bool(settings.get("session_string", "")),
            "name": settings.get("account_name", ""),
            "phone": mask(settings.get("phone", ""), 3),
        },
        "keepalive": keepalive.status(),
    }


# ---------------- 群组管理 ----------------

def _normalize_group(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("name", "")).strip()
    chat = str(body.get("chat", "")).strip()
    command = str(body.get("command", "")).strip() or "/checkin"
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

    return {
        "name": name or chat,
        "chat": chat,
        "command": command,
        "schedule": json.dumps(expressions, ensure_ascii=False),
        "enabled": bool(enabled),
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
        if key in ("retry_count", "retry_delay", "jitter", "keepalive_interval"):
            try:
                number = float(value)
                if number < 0:
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=400, detail=f"{key} 必须是非负数")
        if key == "panel_password" and not value:
            continue  # 留空表示不修改
        db.set_setting(key, value)
        changed.add(key)
    if "timezone" in changed:
        scheduler.reload_jobs()
    if changed & {"keepalive_enabled", "keepalive_interval", "keepalive_url"}:
        keepalive.restart()
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


# ---------------- 配置导出（用于无持久卷环境） ----------------

EXPORT_KEYS = {
    "api_id", "api_hash", "phone", "proxy_url", "session_string", "account_name",
    "timezone", "retry_count", "retry_delay", "jitter", "notify_chat",
    "fail_keywords", "dry_run",
}


@app.get("/api/config/export")
def export_config(_: None = Depends(require_auth)) -> dict[str, Any]:
    """导出可写入 TGCK_CONFIG 环境变量的完整配置（不含面板密码）。"""
    settings = db.get_settings()
    return {
        "settings": {key: settings.get(key, "") for key in sorted(EXPORT_KEYS)},
        "groups": db.list_groups(),
    }


# ---------------- Telegram 账号 ----------------

@app.get("/api/tg/credentials")
def tg_credentials(_: None = Depends(require_auth)) -> dict[str, str]:
    """面板表单回填用：凭据仅在此接口返回原文。"""
    settings = db.get_settings()
    return {
        key: settings.get(key, "")
        for key in ("api_id", "api_hash", "phone", "proxy_url")
    }


@app.get("/api/tg/status")
def tg_status(_: None = Depends(require_auth)) -> dict[str, Any]:
    return telegram.account_status()


@app.post("/api/tg/login-start")
def tg_login_start(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    try:
        return telegram.login_start(
            phone=str(body.get("phone", "")),
            api_id=str(body.get("api_id", "")),
            api_hash=str(body.get("api_hash", "")),
            proxy_url=str(body.get("proxy_url", "")),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=telegram.friendly_error(exc))


@app.post("/api/tg/login-verify")
def tg_login_verify(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    try:
        return telegram.login_verify(
            code=str(body.get("code", "")), password=str(body.get("password", ""))
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=telegram.friendly_error(exc))


@app.post("/api/tg/logout")
def tg_logout(_: None = Depends(require_auth)) -> dict[str, Any]:
    telegram.logout()
    return {"ok": True}
