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
    "panel_password": os.getenv("PANEL_PASSWORD", "admin123"),
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
                command    TEXT NOT NULL DEFAULT '/checkin',
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
                    str(group.get("command") or "/checkin"),
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
