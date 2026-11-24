"""SQLite 持久化层：配置、群组、运行日志。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "checkin.db"

_lock = threading.RLock()

DEFAULTS: dict[str, str] = {
    "api_id": "",
    "api_hash": "",
    "phone": "",
    "proxy_url": "",
    "session_string": "",
    "account_name": "",
    "timezone": "Asia/Shanghai",
    "retry_count": "2",
    "retry_delay": "10",
    "jitter": "5",
    "notify_chat": "",
    "fail_keywords": "",
    "dry_run": "0",
    # 签到执行者：bot = 用机器人（Bot API，只要 token）；user = 用自己账号（Telethon，需要 api_id）
    "checkin_mode": os.getenv("CHECKIN_MODE", "").strip() or "bot",
    "keepalive_enabled": "1" if os.getenv("PUBLIC_URL", "").strip() else "0",
    "keepalive_interval": "300",
    "keepalive_url": os.getenv("PUBLIC_URL", "").strip(),
    "panel_password": os.getenv("PANEL_PASSWORD", "admin123"),
    # ---------- AI 聊天机器人 ----------
    "bot_enabled": "1" if os.getenv("BOT_TOKEN", "").strip() else "0",
    "bot_token": os.getenv("BOT_TOKEN", "").strip(),
    "bot_username": "",
    "bot_trigger": "mention",
    "bot_update_offset": "0",
    "ai_base_url": os.getenv("AI_BASE_URL", "").strip() or "https://api.deepseek.com/v1",
    "ai_api_key": os.getenv("AI_API_KEY", "").strip(),
    "ai_model": os.getenv("AI_MODEL", "").strip() or "deepseek-chat",
    "ai_system_prompt": "你是一个在 Telegram 群里聊天的助手，回答简洁、口语化，用中文回复。不确定的事不要编造。",
    "ai_history_len": "12",
    "ai_max_tokens": "800",
    "ai_temperature": "0.7",
    "ai_min_interval": "3",
    # 出网代理，留空表示直连。接口只对大陆网络开放、而主机在境外时填这里。
    "ai_proxy": os.getenv("AI_PROXY", "").strip(),
    # Telegram Business 绑定信息（JSON），由程序写入，不需要手动配置
    "business_connections": "",
    # 机器人待过的群（JSON），入群事件自动记录，面板可一键填入
    "known_chats": "",
}


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS groups (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                chat       TEXT NOT NULL,
                command    TEXT NOT NULL DEFAULT '签到',
                schedule   TEXT NOT NULL DEFAULT '["0 8 * * *"]',
                enabled    INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id   INTEGER,
                group_name TEXT,
                ts         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                source     TEXT NOT NULL DEFAULT 'auto',
                status     TEXT NOT NULL,
                message    TEXT NOT NULL DEFAULT '',
                duration   INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);
            CREATE TABLE IF NOT EXISTS ai_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    TEXT NOT NULL,
                chat_title TEXT NOT NULL DEFAULT '',
                role       TEXT NOT NULL,
                author     TEXT NOT NULL DEFAULT '',
                content    TEXT NOT NULL,
                ts         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_ai_chat ON ai_messages(chat_id, id DESC);
            """
        )
        for key, value in DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value)
            )
        _bootstrap_from_env(conn)


def _bootstrap_from_env(conn: sqlite3.Connection) -> None:
    """容器没有持久卷时，用 TGCK_CONFIG 环境变量恢复配置和登录会话。

    只在全新空库时执行，避免覆盖运行中的面板改动。
    """
    raw = os.getenv("TGCK_CONFIG", "").strip()
    if not raw:
        return
    session_row = conn.execute(
        "SELECT value FROM settings WHERE key='session_string'"
    ).fetchone()
    group_count = conn.execute("SELECT COUNT(*) AS n FROM groups").fetchone()["n"]
    if (session_row and session_row["value"]) or group_count:
        return
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return

    settings = payload.get("settings", {})
    if isinstance(settings, dict):
        for key, value in settings.items():
            if key in DEFAULTS and value is not None:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )

    groups = payload.get("groups", [])
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict) or not str(group.get("chat", "")).strip():
                continue
            schedule = group.get("schedule", ["0 8 * * *"])
            if isinstance(schedule, list):
                schedule = json.dumps(schedule, ensure_ascii=False)
            else:
                schedule = str(schedule)
            conn.execute(
                "INSERT INTO groups(name, chat, command, schedule, enabled) "
                "VALUES(?,?,?,?,?)",
                (
                    str(group.get("name") or group["chat"]),
                    str(group["chat"]),
                    str(group.get("command") or "签到"),
                    schedule,
                    1 if group.get("enabled", True) else 0,
                ),
            )


def get_setting(key: str, default: str = "") -> str:
    with connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return default if row is None else str(row["value"])


def set_setting(key: str, value: Any) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_settings() -> dict[str, str]:
    with connection() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: str(row["value"]) for row in rows}


# ---------------- groups ----------------

def list_groups(enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM groups"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    with connection() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(row) for row in rows]


def get_group(group_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    return None if row is None else dict(row)


def add_group(data: dict[str, Any]) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO groups(name, chat, command, schedule, enabled) "
            "VALUES(?,?,?,?,?)",
            (
                data["name"],
                data["chat"],
                data["command"],
                data["schedule"],
                1 if data.get("enabled", True) else 0,
            ),
        )
        return int(cur.lastrowid)


def update_group(group_id: int, data: dict[str, Any]) -> bool:
    with connection() as conn:
        cur = conn.execute(
            "UPDATE groups SET name=?, chat=?, command=?, schedule=?, enabled=? "
            "WHERE id=?",
            (
                data["name"],
                data["chat"],
                data["command"],
                data["schedule"],
                1 if data.get("enabled", True) else 0,
                group_id,
            ),
        )
        return cur.rowcount > 0


def delete_group(group_id: int) -> bool:
    with connection() as conn:
        cur = conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
        conn.execute("UPDATE logs SET group_id=NULL WHERE group_id=?", (group_id,))
        return cur.rowcount > 0


# ---------------- logs ----------------

def add_log(
    group_id: int | None,
    group_name: str,
    source: str,
    status: str,
    message: str,
    duration: int,
) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO logs(group_id, group_name, source, status, message, duration) "
            "VALUES(?,?,?,?,?,?)",
            (group_id, group_name, source, status, message[:1000], duration),
        )
        # 只保留最近 2000 条，避免数据库无限增长
        conn.execute(
            "DELETE FROM logs WHERE id NOT IN "
            "(SELECT id FROM logs ORDER BY id DESC LIMIT 2000)"
        )


def list_logs(limit: int = 100, status: str = "") -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 1000))
    sql = "SELECT * FROM logs"
    params: list[Any] = []
    if status in ("success", "failed"):
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def clear_logs() -> None:
    with connection() as conn:
        conn.execute("DELETE FROM logs")


def stats() -> dict[str, int]:
    with connection() as conn:
        groups = conn.execute("SELECT COUNT(*) AS n FROM groups").fetchone()["n"]
        enabled = conn.execute(
            "SELECT COUNT(*) AS n FROM groups WHERE enabled=1"
        ).fetchone()["n"]
        today = conn.execute(
            "SELECT COUNT(*) AS n FROM logs WHERE date(ts)=date('now','localtime') "
            "AND status='success'"
        ).fetchone()["n"]
        failed = conn.execute(
            "SELECT COUNT(*) AS n FROM logs WHERE date(ts)=date('now','localtime') "
            "AND status='failed'"
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"]
    return {
        "groups": int(groups),
        "enabled": int(enabled),
        "today_success": int(today),
        "today_failed": int(failed),
        "total_runs": int(total),
    }


# ---------------- AI 对话记忆 ----------------

def add_ai_message(
    chat_id: str, chat_title: str, role: str, author: str, content: str
) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO ai_messages(chat_id, chat_title, role, author, content) "
            "VALUES(?,?,?,?,?)",
            (str(chat_id), chat_title[:120], role, author[:80], content[:4000]),
        )
        # 每个会话只保留最近 60 条，避免无限增长
        conn.execute(
            "DELETE FROM ai_messages WHERE chat_id=? AND id NOT IN "
            "(SELECT id FROM ai_messages WHERE chat_id=? ORDER BY id DESC LIMIT 60)",
            (str(chat_id), str(chat_id)),
        )


def ai_history(chat_id: str, limit: int = 12) -> list[dict[str, Any]]:
    """取最近若干条对话，按时间正序返回，供拼接上下文。"""
    limit = max(1, min(int(limit), 40))
    with connection() as conn:
        rows = conn.execute(
            "SELECT role, author, content, ts FROM ai_messages "
            "WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (str(chat_id), limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def list_ai_messages(limit: int = 60) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 300))
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ai_messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def clear_ai_messages() -> None:
    with connection() as conn:
        conn.execute("DELETE FROM ai_messages")


def ai_stats() -> dict[str, int]:
    with connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM ai_messages").fetchone()["n"]
        today = conn.execute(
            "SELECT COUNT(*) AS n FROM ai_messages "
            "WHERE role='assistant' AND date(ts)=date('now','localtime')"
        ).fetchone()["n"]
        chats = conn.execute(
            "SELECT COUNT(DISTINCT chat_id) AS n FROM ai_messages"
        ).fetchone()["n"]
    return {"total": int(total), "today": int(today), "chats": int(chats)}
