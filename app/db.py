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
    # 补签：定时点没在线（容器被休眠/重启）时，上线后补一次，保证每天成功一次
    "catchup_enabled": "1",
    "catchup_interval": "600",
    "catchup_cooldown": "1800",
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
    # 当前使用的智能体（ai_agents 表的主键）。空表示还没选，回退到下面这组老字段
    "ai_agent_id": "",
    "ai_system_prompt": "你是一个在 Telegram 群里聊天的助手，回答简洁、口语化，用中文回复。不确定的事不要编造。",
    "ai_history_len": "12",
    "ai_max_tokens": "800",
    "ai_temperature": "0.7",
    "ai_min_interval": "3",
    # 出网代理，留空表示直连。接口只对大陆网络开放、而主机在境外时填这里。
    "ai_proxy": os.getenv("AI_PROXY", "").strip(),
    # ---------- 智能体记忆 ----------
    "memory_enabled": "1",        # 把长期记忆注入上下文
    "memory_learn": "1",          # 聊完自动提炼值得记住的事
    "memory_max_notes": "8",      # 每次最多注入几条
    # ---------- 用自己账号自动回复（Userbot） ----------
    "assistant_enabled": "0",     # 默认关：自动回复会真的以你的身份发消息
    "assistant_account_id": "",   # 留空用第一个可用账号
    "assistant_agent_id": "",     # 留空跟随默认智能体
    "assistant_private": "1",     # 私聊
    "assistant_groups": "0",      # 群聊
    "assistant_trigger": "mention",  # mention / reply / all
    "assistant_whitelist": "",    # 只回这些会话，逗号分隔
    "assistant_blacklist": "",
    "assistant_ignore_bots": "1",
    "assistant_cooldown": "5",    # 同会话最短间隔（秒）
    "assistant_max_per_hour": "30",
    "assistant_quiet": "",        # 免打扰时段，例如 23:30-08:00
    # Telegram Business 绑定信息（JSON），由程序写入，不需要手动配置
    "business_connections": "",
    # 机器人待过的群（JSON），入群事件自动记录，面板可一键填入
    "known_chats": "",
    # ---------- 备份 / 恢复 ----------
    # 自动备份：本地必存一份，WebDAV / GitHub 按各自开关上传
    "backup_auto": "0",
    "backup_interval": "12",          # 小时
    "backup_keep": "14",              # 本地保留份数
    "backup_restore_on_boot": "1",    # 新容器数据为空时，开机自动从云端恢复
    "backup_last_run": "",
    "backup_last_target": "",
    "backup_last_error": "",
    "webdav_url": "",
    "webdav_dir": "",
    "webdav_username": "",
    "webdav_password": "",
    "webdav_auto": "0",
    "webdav_keep": "14",
    "github_token": "",
    "github_repo": "",
    "github_branch": "main",
    "github_path": "tgck-backups",
    "github_auto": "0",
    "github_keep": "14",
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
                account_id INTEGER,
                bot_id     INTEGER,
                mode       TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS accounts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL DEFAULT '',
                phone          TEXT NOT NULL DEFAULT '',
                api_id         TEXT NOT NULL DEFAULT '',
                api_hash       TEXT NOT NULL DEFAULT '',
                session_string TEXT NOT NULL DEFAULT '',
                proxy_url      TEXT NOT NULL DEFAULT '',
                enabled        INTEGER NOT NULL DEFAULT 1,
                created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bots (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                name                 TEXT NOT NULL DEFAULT '',
                token                TEXT NOT NULL DEFAULT '',
                username             TEXT NOT NULL DEFAULT '',
                enabled              INTEGER NOT NULL DEFAULT 1,
                trigger              TEXT NOT NULL DEFAULT 'mention',
                update_offset        TEXT NOT NULL DEFAULT '0',
                business_connections TEXT NOT NULL DEFAULT '',
                known_chats          TEXT NOT NULL DEFAULT '',
                ai_agent_id          INTEGER,
                created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ai_agents (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL DEFAULT '',
                base_url      TEXT NOT NULL DEFAULT '',
                api_key       TEXT NOT NULL DEFAULT '',
                model         TEXT NOT NULL DEFAULT '',
                system_prompt TEXT NOT NULL DEFAULT '',
                temperature   TEXT NOT NULL DEFAULT '0.7',
                max_tokens    INTEGER NOT NULL DEFAULT 800,
                history_len   INTEGER NOT NULL DEFAULT 12,
                min_interval  INTEGER NOT NULL DEFAULT 3,
                proxy         TEXT NOT NULL DEFAULT '',
                enabled       INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                bot_id     INTEGER NOT NULL DEFAULT 0,
                chat_id    TEXT NOT NULL,
                chat_title TEXT NOT NULL DEFAULT '',
                role       TEXT NOT NULL,
                author     TEXT NOT NULL DEFAULT '',
                content    TEXT NOT NULL,
                ts         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_ai_chat ON ai_messages(chat_id, id DESC);
            CREATE TABLE IF NOT EXISTS ai_notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id   INTEGER NOT NULL DEFAULT 0,
                scope      TEXT NOT NULL DEFAULT 'global',
                scope_key  TEXT NOT NULL DEFAULT '',
                content    TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT 'auto',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                used_at    TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_notes_agent ON ai_notes(agent_id, scope, scope_key);
            """
        )
        for key, value in DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value)
            )
        _bootstrap_from_env(conn)
        _migrate_columns(conn)
        _seed_identities(conn)
        _seed_agents(conn)
    _tighten_permissions()


def _tighten_permissions() -> None:
    """库里存着 bot token、登录会话、AI Key：权限收到 600，目录收到 700。

    Windows 上 chmod 语义有限，失败忽略即可，不影响运行。
    """
    for path, mode in ((DATA_DIR, 0o700), (DB_PATH, 0o600)):
        try:
            if path.exists():
                os.chmod(path, mode)
        except OSError:
            pass


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """老库补列：groups 增加「用哪个账号/机器人签到」，ai_messages 增加 bot_id。"""
    existing = _columns(conn, "groups")
    for column, ddl in (
        ("account_id", "INTEGER"),
        ("bot_id", "INTEGER"),
        ("mode", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE groups ADD COLUMN {column} {ddl}")

    if "bot_id" not in _columns(conn, "ai_messages"):
        conn.execute("ALTER TABLE ai_messages ADD COLUMN bot_id INTEGER NOT NULL DEFAULT 0")
        # 老对话记录归到第一个机器人名下，升级后上下文不至于全丢
        first = conn.execute("SELECT MIN(id) AS n FROM bots").fetchone()["n"]
        if first:
            conn.execute("UPDATE ai_messages SET bot_id=? WHERE bot_id=0", (int(first),))

    if "ai_agent_id" not in _columns(conn, "bots"):
        conn.execute("ALTER TABLE bots ADD COLUMN ai_agent_id INTEGER")


def _seed_identities(conn: sqlite3.Connection) -> None:
    """把老的「单账号 / 单机器人」配置迁移成列表里的第一条记录。

    之后账号、机器人各自独立管理；settings 里的同名字段只作为兼容保留。
    """
    if not conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('account_name','phone','api_id','api_hash','session_string','proxy_url')"
        ).fetchall()
        data = {row["key"]: str(row["value"]) for row in rows}
        if data.get("session_string"):
            conn.execute(
                "INSERT INTO accounts(name, phone, api_id, api_hash, session_string, proxy_url, enabled) "
                "VALUES(?,?,?,?,?,?,1)",
                (
                    data.get("account_name") or data.get("phone") or "默认账号",
                    data.get("phone", ""),
                    data.get("api_id", ""),
                    data.get("api_hash", ""),
                    data["session_string"],
                    data.get("proxy_url", ""),
                ),
            )

    if not conn.execute("SELECT COUNT(*) AS n FROM bots").fetchone()["n"]:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('bot_token','bot_username','bot_trigger','bot_enabled','bot_update_offset',"
            "'known_chats','business_connections')"
        ).fetchall()
        data = {row["key"]: str(row["value"]) for row in rows}
        if data.get("bot_token"):
            conn.execute(
                "INSERT INTO bots(name, token, username, enabled, trigger, update_offset, "
                "known_chats, business_connections) VALUES(?,?,?,?,?,?,?,?)",
                (
                    data.get("bot_username") or "默认机器人",
                    data["bot_token"],
                    data.get("bot_username", ""),
                    1 if data.get("bot_enabled", "1") == "1" else 0,
                    data.get("bot_trigger", "mention") or "mention",
                    data.get("bot_update_offset", "0") or "0",
                    data.get("known_chats", ""),
                    data.get("business_connections", ""),
                ),
            )


def _seed_agents(conn: sqlite3.Connection) -> None:
    """把老的单份 AI 配置提升成第一个智能体。

    升级后不会丢配置：接口地址、Key、模型、提示词全部搬进来，
    并把 settings.ai_agent_id 指向它，机器人默认就用它说话。
    """
    if conn.execute("SELECT COUNT(*) AS n FROM ai_agents").fetchone()["n"]:
        return
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key IN "
        "('ai_base_url','ai_api_key','ai_model','ai_system_prompt','ai_temperature',"
        "'ai_max_tokens','ai_history_len','ai_min_interval','ai_proxy')"
    ).fetchall()
    data = {row["key"]: str(row["value"]) for row in rows}
    # 完全没配过就别造一个空壳出来，等用户自己新建
    if not (data.get("ai_api_key") or "").strip() and not (data.get("ai_model") or "").strip():
        return
    cur = conn.execute(
        "INSERT INTO ai_agents(name, base_url, api_key, model, system_prompt, "
        "temperature, max_tokens, history_len, min_interval, proxy, enabled) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,1)",
        (
            "默认智能体",
            data.get("ai_base_url", ""),
            data.get("ai_api_key", ""),
            data.get("ai_model", ""),
            data.get("ai_system_prompt", ""),
            data.get("ai_temperature", "0.7") or "0.7",
            int(float(data.get("ai_max_tokens", "800") or 800)),
            int(float(data.get("ai_history_len", "12") or 12)),
            int(float(data.get("ai_min_interval", "3") or 3)),
            data.get("ai_proxy", ""),
        ),
    )
    agent_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('ai_agent_id', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(agent_id),),
    )


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
                "INSERT INTO groups(name, chat, command, schedule, enabled, account_id, bot_id, mode) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    str(group.get("name") or group["chat"]),
                    str(group["chat"]),
                    str(group.get("command") or "签到"),
                    schedule,
                    1 if group.get("enabled", True) else 0,
                    group.get("account_id"),
                    group.get("bot_id"),
                    str(group.get("mode") or ""),
                ),
            )

    accounts = payload.get("accounts", [])
    if isinstance(accounts, list) and not conn.execute(
        "SELECT COUNT(*) AS n FROM accounts"
    ).fetchone()["n"]:
        for account in accounts:
            if not isinstance(account, dict) or not str(account.get("session_string", "")).strip():
                continue
            conn.execute(
                "INSERT INTO accounts(name, phone, api_id, api_hash, session_string, proxy_url, enabled) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    str(account.get("name") or "账号"),
                    str(account.get("phone", "")),
                    str(account.get("api_id", "")),
                    str(account.get("api_hash", "")),
                    str(account.get("session_string", "")),
                    str(account.get("proxy_url", "")),
                    1 if account.get("enabled", True) else 0,
                ),
            )

    bots = payload.get("bots", [])
    if isinstance(bots, list) and not conn.execute(
        "SELECT COUNT(*) AS n FROM bots"
    ).fetchone()["n"]:
        for item in bots:
            if not isinstance(item, dict) or not str(item.get("token", "")).strip():
                continue
            conn.execute(
                "INSERT INTO bots(name, token, username, enabled, trigger, update_offset, "
                "known_chats, business_connections) VALUES(?,?,?,?,?,?,?,?)",
                (
                    str(item.get("name") or item.get("username") or "机器人"),
                    str(item.get("token", "")),
                    str(item.get("username", "")),
                    1 if item.get("enabled", True) else 0,
                    str(item.get("trigger") or "mention"),
                    str(item.get("update_offset") or "0"),
                    str(item.get("known_chats") or ""),
                    str(item.get("business_connections") or ""),
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
            "INSERT INTO groups(name, chat, command, schedule, enabled, account_id, bot_id, mode) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                data["name"],
                data["chat"],
                data["command"],
                data["schedule"],
                1 if data.get("enabled", True) else 0,
                data.get("account_id"),
                data.get("bot_id"),
                str(data.get("mode") or ""),
            ),
        )
        return int(cur.lastrowid)


def update_group(group_id: int, data: dict[str, Any]) -> bool:
    with connection() as conn:
        cur = conn.execute(
            "UPDATE groups SET name=?, chat=?, command=?, schedule=?, enabled=?, "
            "account_id=?, bot_id=?, mode=? WHERE id=?",
            (
                data["name"],
                data["chat"],
                data["command"],
                data["schedule"],
                1 if data.get("enabled", True) else 0,
                data.get("account_id"),
                data.get("bot_id"),
                str(data.get("mode") or ""),
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
    chat_id: str,
    chat_title: str,
    role: str,
    author: str,
    content: str,
    bot_id: int = 0,
) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO ai_messages(bot_id, chat_id, chat_title, role, author, content) "
            "VALUES(?,?,?,?,?,?)",
            (int(bot_id or 0), str(chat_id), chat_title[:120], role, author[:80], content[:4000]),
        )
        # 每个会话只保留最近 60 条，避免无限增长（按机器人分开算）
        conn.execute(
            "DELETE FROM ai_messages WHERE bot_id=? AND chat_id=? AND id NOT IN "
            "(SELECT id FROM ai_messages WHERE bot_id=? AND chat_id=? ORDER BY id DESC LIMIT 60)",
            (int(bot_id or 0), str(chat_id), int(bot_id or 0), str(chat_id)),
        )


def ai_history(chat_id: str, limit: int = 12, bot_id: int = 0) -> list[dict[str, Any]]:
    """取最近若干条对话，按时间正序返回，供拼接上下文。"""
    limit = max(1, min(int(limit), 40))
    with connection() as conn:
        rows = conn.execute(
            "SELECT role, author, content, ts FROM ai_messages "
            "WHERE bot_id=? AND chat_id=? ORDER BY id DESC LIMIT ?",
            (int(bot_id or 0), str(chat_id), limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def list_ai_messages(limit: int = 60, bot_id: int | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 300))
    sql = "SELECT * FROM ai_messages"
    params: list[Any] = []
    if bot_id is not None:
        sql += " WHERE bot_id=?"
        params.append(int(bot_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def clear_ai_messages(bot_id: int | None = None) -> None:
    with connection() as conn:
        if bot_id is None:
            conn.execute("DELETE FROM ai_messages")
        else:
            conn.execute("DELETE FROM ai_messages WHERE bot_id=?", (int(bot_id),))


# ---------------- 长期记忆（ai_notes） ----------------
# scope: global 全局 / chat 某个会话 / user 某个人。智能体之间互不串味（agent_id 区分）。

def add_note(
    agent_id: int,
    scope: str,
    scope_key: str,
    content: str,
    source: str = "auto",
) -> int:
    with connection() as conn:
        cursor = conn.execute(
            "INSERT INTO ai_notes(agent_id, scope, scope_key, content, source) VALUES(?,?,?,?,?)",
            (int(agent_id or 0), scope, str(scope_key or "")[:120], content[:400], source),
        )
        return int(cursor.lastrowid or 0)


def list_notes(
    agent_id: int | None = None,
    scope: str | None = None,
    scope_key: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM ai_notes WHERE 1=1"
    params: list[Any] = []
    if agent_id is not None:
        sql += " AND agent_id=?"
        params.append(int(agent_id))
    if scope:
        sql += " AND scope=?"
        params.append(scope)
    if scope_key is not None:
        sql += " AND scope_key=?"
        params.append(str(scope_key))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with connection() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def note_pool(agent_id: int, chat_id: str, author: str) -> list[dict[str, Any]]:
    """拼上下文时用的候选池：全局 + 当前会话 + 当前这个人。"""
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ai_notes WHERE agent_id=? AND ("
            "(scope='global') OR (scope='chat' AND scope_key=?) OR (scope='user' AND scope_key=?)"
            ") ORDER BY id DESC LIMIT 300",
            (int(agent_id or 0), str(chat_id), str(author or "")[:120]),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_note(note_id: int) -> bool:
    with connection() as conn:
        cursor = conn.execute("DELETE FROM ai_notes WHERE id=?", (int(note_id),))
        return cursor.rowcount > 0


def clear_notes(agent_id: int, scope: str = "", scope_key: str = "") -> int:
    sql = "DELETE FROM ai_notes WHERE agent_id=?"
    params: list[Any] = [int(agent_id)]
    if scope:
        sql += " AND scope=?"
        params.append(scope)
    if scope_key:
        sql += " AND scope_key=?"
        params.append(scope_key)
    with connection() as conn:
        cursor = conn.execute(sql, params)
        return int(cursor.rowcount or 0)


def touch_notes(note_ids: list[int]) -> None:
    if not note_ids:
        return
    marks = ",".join("?" for _ in note_ids)
    with connection() as conn:
        conn.execute(
            f"UPDATE ai_notes SET used_at=CURRENT_TIMESTAMP WHERE id IN ({marks})",
            [int(i) for i in note_ids],
        )


def note_stats(agent_id: int) -> dict[str, int]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT scope, COUNT(*) AS n FROM ai_notes WHERE agent_id=? GROUP BY scope",
            (int(agent_id),),
        ).fetchall()
    data = {"total": 0, "global": 0, "chat": 0, "user": 0}
    for row in rows:
        data[str(row["scope"])] = int(row["n"])
        data["total"] += int(row["n"])
    return data


def ai_stats(bot_id: int | None = None) -> dict[str, int]:
    where = "" if bot_id is None else " WHERE bot_id=?"
    params: tuple[Any, ...] = () if bot_id is None else (int(bot_id),)
    with connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM ai_messages{where}", params).fetchone()["n"]
        today_row = conn.execute(
            "SELECT COUNT(*) AS n FROM ai_messages "
            f"WHERE role='assistant' AND date(ts)=date('now','localtime')"
            + ("" if bot_id is None else " AND bot_id=?"),
            params,
        ).fetchone()
        chats = conn.execute(
            f"SELECT COUNT(DISTINCT chat_id) AS n FROM ai_messages{where}", params
        ).fetchone()["n"]
    return {"total": int(total), "today": int(today_row["n"]), "chats": int(chats)}


# ---------------- 账号列表 ----------------

ACCOUNT_FIELDS = ("name", "phone", "api_id", "api_hash", "session_string", "proxy_url", "enabled")


def _account_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    data["authorized"] = bool(str(data.get("session_string") or "").strip())
    return data


def list_accounts() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return [_account_row(row) for row in rows]


def get_account(account_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return None if row is None else _account_row(row)


def default_account() -> dict[str, Any] | None:
    """没指定账号时的兜底：优先已登录且启用的，其次任意一条。"""
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE enabled=1 AND session_string<>'' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM accounts ORDER BY id LIMIT 1").fetchone()
    return None if row is None else _account_row(row)


def add_account(data: dict[str, Any]) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO accounts(name, phone, api_id, api_hash, session_string, proxy_url, enabled) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                data.get("name") or data.get("phone") or "账号",
                data.get("phone", ""),
                data.get("api_id", ""),
                data.get("api_hash", ""),
                data.get("session_string", ""),
                data.get("proxy_url", ""),
                1 if data.get("enabled", True) else 0,
            ),
        )
        return int(cur.lastrowid)


def update_account(account_id: int, data: dict[str, Any]) -> bool:
    current = get_account(account_id)
    if current is None:
        return False
    merged = {**current, **{k: v for k, v in data.items() if k in ACCOUNT_FIELDS}}
    with connection() as conn:
        cur = conn.execute(
            "UPDATE accounts SET name=?, phone=?, api_id=?, api_hash=?, session_string=?, "
            "proxy_url=?, enabled=? WHERE id=?",
            (
                merged.get("name", ""),
                merged.get("phone", ""),
                merged.get("api_id", ""),
                merged.get("api_hash", ""),
                merged.get("session_string", ""),
                merged.get("proxy_url", ""),
                1 if merged.get("enabled", True) else 0,
                account_id,
            ),
        )
        return cur.rowcount > 0


def delete_account(account_id: int) -> bool:
    with connection() as conn:
        cur = conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        conn.execute("UPDATE groups SET account_id=NULL WHERE account_id=?", (account_id,))
        return cur.rowcount > 0


# ---------------- AI 智能体 ----------------

AGENT_FIELDS = (
    "name", "base_url", "api_key", "model", "system_prompt",
    "temperature", "max_tokens", "history_len", "min_interval", "proxy", "enabled",
)


def _agent_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    data["key_set"] = bool(str(data.get("api_key") or "").strip())
    return data


def list_agents() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM ai_agents ORDER BY id").fetchall()
    return [_agent_row(row) for row in rows]


def get_agent(agent_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM ai_agents WHERE id=?", (agent_id,)).fetchone()
    return None if row is None else _agent_row(row)


def add_agent(data: dict[str, Any]) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO ai_agents(name, base_url, api_key, model, system_prompt, "
            "temperature, max_tokens, history_len, min_interval, proxy, enabled) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                data.get("name") or "未命名智能体",
                data.get("base_url", ""),
                data.get("api_key", ""),
                data.get("model", ""),
                data.get("system_prompt", ""),
                str(data.get("temperature", "0.7")),
                int(data.get("max_tokens", 800)),
                int(data.get("history_len", 12)),
                int(data.get("min_interval", 3)),
                data.get("proxy", ""),
                1 if data.get("enabled", True) else 0,
            ),
        )
        return int(cur.lastrowid)


def update_agent(agent_id: int, data: dict[str, Any]) -> bool:
    current = get_agent(agent_id)
    if current is None:
        return False
    merged = {**current, **{k: v for k, v in data.items() if k in AGENT_FIELDS}}
    with connection() as conn:
        cur = conn.execute(
            "UPDATE ai_agents SET name=?, base_url=?, api_key=?, model=?, system_prompt=?, "
            "temperature=?, max_tokens=?, history_len=?, min_interval=?, proxy=?, enabled=? "
            "WHERE id=?",
            (
                merged.get("name") or "未命名智能体",
                merged.get("base_url", ""),
                merged.get("api_key", ""),
                merged.get("model", ""),
                merged.get("system_prompt", ""),
                str(merged.get("temperature", "0.7")),
                int(merged.get("max_tokens", 800)),
                int(merged.get("history_len", 12)),
                int(merged.get("min_interval", 3)),
                merged.get("proxy", ""),
                1 if merged.get("enabled", True) else 0,
                agent_id,
            ),
        )
        return cur.rowcount > 0


def delete_agent(agent_id: int) -> bool:
    with connection() as conn:
        cur = conn.execute("DELETE FROM ai_agents WHERE id=?", (agent_id,))
        # 引用了它的机器人退回「跟随默认」，避免留下悬空 ID
        conn.execute("UPDATE bots SET ai_agent_id=NULL WHERE ai_agent_id=?", (agent_id,))
        return cur.rowcount > 0


def default_agent_id() -> int | None:
    raw = get_setting("ai_agent_id", "").strip()
    if raw.isdigit() and get_agent(int(raw)):
        return int(raw)
    enabled = [a for a in list_agents() if a["enabled"]]
    if enabled:
        return int(enabled[0]["id"])
    return None


def agent_for_bot(bot_id: int | None) -> dict[str, Any] | None:
    """解析某个机器人实际该用哪个智能体。

    优先级：机器人自己指定的 → 全局默认 → 第一个启用的 → 空（回退老配置）。
    """
    if bot_id:
        record = get_bot(int(bot_id))
        if record and record.get("ai_agent_id"):
            agent = get_agent(int(record["ai_agent_id"]))
            if agent:
                return agent
    agent_id = default_agent_id()
    return get_agent(agent_id) if agent_id else None


# ---------------- 机器人列表 ----------------

BOT_FIELDS = (
    "name", "token", "username", "enabled", "trigger",
    "update_offset", "business_connections", "known_chats", "ai_agent_id",
)


def _bot_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    data["token_set"] = bool(str(data.get("token") or "").strip())
    return data


def list_bots() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM bots ORDER BY id").fetchall()
    return [_bot_row(row) for row in rows]


def get_bot(bot_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    return None if row is None else _bot_row(row)


def default_bot() -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM bots WHERE enabled=1 AND token<>'' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM bots ORDER BY id LIMIT 1").fetchone()
    return None if row is None else _bot_row(row)


def add_bot(data: dict[str, Any]) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO bots(name, token, username, enabled, trigger, ai_agent_id) "
            "VALUES(?,?,?,?,?,?)",
            (
                data.get("name") or data.get("username") or "机器人",
                data.get("token", ""),
                data.get("username", ""),
                1 if data.get("enabled", True) else 0,
                data.get("trigger") or "mention",
                data.get("ai_agent_id") or None,
            ),
        )
        return int(cur.lastrowid)


def update_bot(bot_id: int, data: dict[str, Any]) -> bool:
    current = get_bot(bot_id)
    if current is None:
        return False
    merged = {**current, **{k: v for k, v in data.items() if k in BOT_FIELDS}}
    with connection() as conn:
        cur = conn.execute(
            "UPDATE bots SET name=?, token=?, username=?, enabled=?, trigger=?, "
            "update_offset=?, business_connections=?, known_chats=?, ai_agent_id=? WHERE id=?",
            (
                merged.get("name", ""),
                merged.get("token", ""),
                merged.get("username", ""),
                1 if merged.get("enabled", True) else 0,
                merged.get("trigger") or "mention",
                str(merged.get("update_offset") or "0"),
                merged.get("business_connections") or "",
                merged.get("known_chats") or "",
                merged.get("ai_agent_id") or None,
                bot_id,
            ),
        )
        return cur.rowcount > 0


def delete_bot(bot_id: int) -> bool:
    with connection() as conn:
        cur = conn.execute("DELETE FROM bots WHERE id=?", (bot_id,))
        conn.execute("UPDATE groups SET bot_id=NULL WHERE bot_id=?", (bot_id,))
        return cur.rowcount > 0


# ---------------- 补签用：今天是否已成功 ----------------

def success_today(group_id: int) -> bool:
    with connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM logs WHERE group_id=? AND status='success' "
            "AND date(ts, 'localtime')=date('now', 'localtime')",
            (group_id,),
        ).fetchone()
    return bool(row["n"])


def last_attempt_epoch(group_id: int) -> float:
    """最近一次尝试的时间戳（UTC epoch 秒），用于补签冷却判断。"""
    with connection() as conn:
        row = conn.execute(
            "SELECT CAST(strftime('%s', ts) AS INTEGER) AS ts FROM logs "
            "WHERE group_id=? ORDER BY id DESC LIMIT 1",
            (group_id,),
        ).fetchone()
    if row is None or row["ts"] is None:
        return 0.0
    return float(row["ts"])
