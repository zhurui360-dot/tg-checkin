"""备份 / 恢复 / WebDAV 云备份 / GitHub 同步。

设计要点：
- 备份包就是一个 JSON（settings + groups + accounts + bots + logs），
  既可以直接下载、也可以在 WebDAV / GitHub 上存一份。
- 恢复支持「替换」和「合并」两种模式；账号 / 机器人的自增 ID 会重新映射，
  群组里引用的 account_id / bot_id 跟着改写，不会指错人。
- WebDAV（坚果云、Nextcloud、群晖等）和 GitHub 都只是「放文件的地方」，
  上传下载走同一套 urllib，不额外引依赖。
- 免费 PaaS 常常没有持久卷（容器文件每次重新部署都会清空），
  所以支持「启动时如果本地空空如也，就自动从云端拉最新备份恢复」。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, db, security

log = logging.getLogger("tg-checkin.backup")

BACKUP_DIR = db.DATA_DIR / "backups"
FORMAT_VERSION = 3
NAME_RE = re.compile(r"^tgck-\d{8}-\d{6}\.json$")


class BackupError(RuntimeError):
    """备份相关操作失败（网络、鉴权、格式不对等）。

    带上 HTTP 状态码，调用方不用靠字符串匹配去判断「目录不存在」这类情况。
    """

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


# ---------------- 备份包 ----------------

def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def build_bundle(include_logs: bool = True) -> dict[str, Any]:
    """把当前全部数据打成一份可迁移的 JSON。"""
    return {
        "app": "tg-checkin",
        "format": FORMAT_VERSION,
        "version": __version__,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "settings": db.get_settings(),
        "groups": db.list_groups(),
        "accounts": db.list_accounts(),
        "bots": db.list_bots(),
        "ai_agents": db.list_agents(),
        "logs": db.list_logs(limit=1000) if include_logs else [],
    }


def _check_bundle(bundle: Any) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        raise BackupError("备份文件格式不对：应该是一段 JSON 对象")
    if bundle.get("app") not in (None, "tg-checkin"):
        raise BackupError("这不是本程序的备份文件")
    if not any(isinstance(bundle.get(k), list) for k in ("groups", "accounts", "bots")):
        raise BackupError("备份文件里没有群组/账号/机器人数据，无法恢复")
    return bundle


def restore_bundle(
    bundle: dict[str, Any], mode: str = "replace", restore_password: bool = False
) -> dict[str, Any]:
    """把备份写回当前实例。mode=replace 先清空，mode=merge 只补缺。"""
    _check_bundle(bundle)
    if mode not in ("replace", "merge"):
        raise BackupError("恢复模式只能是 replace 或 merge")

    # 1) 设置项：替换模式直接覆盖，合并模式只补空值
    settings = bundle.get("settings") if isinstance(bundle.get("settings"), dict) else {}
    restored_settings = 0
    current = db.get_settings()
    for key, value in settings.items():
        if key in ("panel_password",) and not restore_password:
            continue
        if mode == "merge" and str(current.get(key, "")).strip():
            continue
        db.set_setting(key, value)
        restored_settings += 1

    # 2) 账号 / 智能体 / 机器人：先恢复，拿到新 ID 的映射关系
    if mode == "replace":
        for item in db.list_groups():
            db.delete_group(int(item["id"]))
        for item in db.list_accounts():
            db.delete_account(int(item["id"]))
        for item in db.list_bots():
            db.delete_bot(int(item["id"]))
        for item in db.list_agents():
            db.delete_agent(int(item["id"]))

    account_map = _restore_accounts(bundle.get("accounts") or [], mode)
    agent_map = _restore_agents(bundle.get("ai_agents") or [], mode)
    bot_map = _restore_bots(bundle.get("bots") or [], mode, agent_map)
    groups = _restore_groups(bundle.get("groups") or [], mode, account_map, bot_map)
    logs = _restore_logs(bundle.get("logs") or [], mode)

    # 默认智能体存的是旧 ID，得换成恢复后的新 ID，否则指向一个不存在的记录
    if isinstance(settings.get("ai_agent_id"), (str, int)):
        raw_agent = str(settings.get("ai_agent_id") or "").strip()
        if raw_agent.isdigit():
            mapped = agent_map.get(int(raw_agent))
            db.set_setting("ai_agent_id", str(mapped) if mapped else "")

    return {
        "ok": True,
        "mode": mode,
        "settings": restored_settings,
        "accounts": len(account_map),
        "bots": len(bot_map),
        "ai_agents": len(agent_map),
        "groups": groups,
        "logs": logs,
    }


def _restore_agents(items: list[Any], mode: str) -> dict[int, int]:
    mapping: dict[int, int] = {}
    existing = {
        f"{a.get('name') or ''}|{a.get('model') or ''}|{a.get('base_url') or ''}": int(a["id"])
        for a in db.list_agents()
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        old_id = item.get("id")
        payload = {
            "name": str(item.get("name") or ""),
            "base_url": str(item.get("base_url") or ""),
            "api_key": str(item.get("api_key") or ""),
            "model": str(item.get("model") or ""),
            "system_prompt": str(item.get("system_prompt") or ""),
            "temperature": str(item.get("temperature") or "0.7"),
            "max_tokens": int(item.get("max_tokens") or 800),
            "history_len": int(item.get("history_len") or 12),
            "min_interval": int(item.get("min_interval") or 3),
            "proxy": str(item.get("proxy") or ""),
            "enabled": bool(item.get("enabled", True)),
        }
        key = f"{payload['name']}|{payload['model']}|{payload['base_url']}"
        if mode == "merge" and key in existing:
            if old_id is not None:
                mapping[int(old_id)] = existing[key]
            continue
        new_id = db.add_agent(payload)
        existing[key] = new_id
        if old_id is not None:
            mapping[int(old_id)] = new_id
    return mapping


def _restore_accounts(items: list[Any], mode: str) -> dict[int, int]:
    mapping: dict[int, int] = {}
    existing = {
        f"{a.get('phone') or ''}|{a.get('api_id') or ''}": int(a["id"])
        for a in db.list_accounts()
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        old_id = item.get("id")
        payload = {
            "name": str(item.get("name") or ""),
            "phone": str(item.get("phone") or ""),
            "api_id": str(item.get("api_id") or ""),
            "api_hash": str(item.get("api_hash") or ""),
            "session_string": str(item.get("session_string") or ""),
            "proxy_url": str(item.get("proxy_url") or ""),
            "enabled": bool(item.get("enabled", True)),
        }
        key = f"{payload['phone']}|{payload['api_id']}"
        if mode == "merge" and key in existing:
            if old_id is not None:
                mapping[int(old_id)] = existing[key]
            continue
        new_id = db.add_account(payload)
        existing[key] = new_id
        if old_id is not None:
            mapping[int(old_id)] = new_id
    return mapping


def _restore_bots(
    items: list[Any], mode: str, agent_map: dict[int, int] | None = None
) -> dict[int, int]:
    mapping: dict[int, int] = {}
    existing = {str(b.get("token") or ""): int(b["id"]) for b in db.list_bots()}
    for item in items:
        if not isinstance(item, dict):
            continue
        old_id = item.get("id")
        raw_agent = item.get("ai_agent_id")
        bound_agent = None
        if raw_agent not in (None, "", 0, "0"):
            try:
                bound_agent = (agent_map or {}).get(int(raw_agent))
            except (TypeError, ValueError):
                bound_agent = None
        payload = {
            "name": str(item.get("name") or ""),
            "token": str(item.get("token") or ""),
            "username": str(item.get("username") or ""),
            "enabled": bool(item.get("enabled", True)),
            "trigger": str(item.get("trigger") or "mention"),
            "business_connections": str(item.get("business_connections") or ""),
            "known_chats": str(item.get("known_chats") or ""),
            "ai_agent_id": bound_agent,
        }
        key = payload["token"]
        if mode == "merge" and key in existing:
            if old_id is not None:
                mapping[int(old_id)] = existing[key]
            continue
        new_id = db.add_bot(payload)
        # add_bot 只写基础字段，秘书模式绑定和群列表得再补一次
        db.update_bot(
            new_id,
            {
                "update_offset": str(item.get("update_offset") or "0"),
                "business_connections": payload["business_connections"],
                "known_chats": payload["known_chats"],
            },
        )
        existing[key] = new_id
        if old_id is not None:
            mapping[int(old_id)] = new_id
    return mapping


def _restore_groups(
    items: list[Any],
    mode: str,
    account_map: dict[int, int],
    bot_map: dict[int, int],
) -> int:
    existing_chats = {str(g.get("chat") or "") for g in db.list_groups()}
    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        chat = str(item.get("chat") or "").strip()
        if not chat:
            continue
        if mode == "merge" and chat in existing_chats:
            continue
        old_account = item.get("account_id")
        old_bot = item.get("bot_id")
        payload = {
            "name": str(item.get("name") or chat),
            "chat": chat,
            "command": str(item.get("command") or "签到"),
            "schedule": item.get("schedule") or '["0 8 * * *"]',
            "enabled": bool(item.get("enabled", True)),
            "account_id": account_map.get(int(old_account)) if old_account else None,
            "bot_id": bot_map.get(int(old_bot)) if old_bot else None,
            "mode": str(item.get("mode") or ""),
        }
        if not isinstance(payload["schedule"], str):
            payload["schedule"] = json.dumps(payload["schedule"], ensure_ascii=False)
        db.add_group(payload)
        existing_chats.add(chat)
        count += 1
    return count


def _restore_logs(items: list[Any], mode: str) -> int:
    if mode == "merge" or not items:
        return 0
    count = 0
    with db.connection() as conn:
        for item in items:
            if not isinstance(item, dict):
                continue
            conn.execute(
                "INSERT INTO logs(group_id, group_name, ts, source, status, message, duration) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    item.get("group_id"),
                    str(item.get("group_name") or ""),
                    str(item.get("ts") or ""),
                    str(item.get("source") or "auto"),
                    str(item.get("status") or "failed"),
                    str(item.get("message") or "")[:1000],
                    int(item.get("duration") or 0),
                ),
            )
            count += 1
    return count


# ---------------- 本地备份文件 ----------------

def _ensure_dir() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    # 备份包里是会话字符串和全部 Key，目录和文件都不该让同机其他用户读到
    security.harden_permissions(BACKUP_DIR)
    return BACKUP_DIR


def _safe_name(name: str) -> str:
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise BackupError("备份文件名不合法")
    return name


def save_local(bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    bundle = bundle or build_bundle()
    path = _ensure_dir() / f"tgck-{_stamp()}.json"
    text = json.dumps(bundle, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    security.harden_permissions(path)
    prune_local()
    return {"name": path.name, "size": len(text.encode("utf-8")), "path": str(path)}


def list_local() -> list[dict[str, Any]]:
    if not BACKUP_DIR.exists():
        return []
    items = []
    for path in sorted(BACKUP_DIR.glob("tgck-*.json"), reverse=True):
        if not NAME_RE.match(path.name):
            continue
        stat = path.stat()
        items.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return items


def load_local(name: str) -> dict[str, Any]:
    path = BACKUP_DIR / _safe_name(name)
    if not path.exists():
        raise BackupError("这个备份文件不存在了")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise BackupError(f"备份文件读不出来：{exc}")


def delete_local(name: str) -> bool:
    path = BACKUP_DIR / _safe_name(name)
    if not path.exists():
        return False
    path.unlink()
    return True


def prune_local(keep: int | None = None) -> None:
    try:
        keep = int(keep if keep is not None else db.get_setting("backup_keep", "14"))
    except ValueError:
        keep = 14
    keep = max(1, min(keep, 200))
    files = sorted(
        [p for p in _ensure_dir().glob("tgck-*.json") if NAME_RE.match(p.name)], reverse=True
    )
    for path in files[keep:]:
        try:
            path.unlink()
        except OSError:
            pass


# ---------------- HTTP 小工具（WebDAV / GitHub 共用） ----------------

def _explain_http(code: int, body: str, kind: str) -> str:
    """把服务端返回的原始报文翻成一句人话，避免面板上显示一大坨 JSON。"""
    if kind == "github":
        if code == 401:
            return "GitHub 不认这个 Token（可能已过期或被撤销），重新生成一个带 repo 权限的"
        if code == 403:
            return "GitHub 拒绝访问：Token 没有 repo 权限，或触发了速率限制"
        if code == 404:
            return "GitHub 找不到这个仓库，检查 owner/repo 是否写对、Token 是否有该仓库权限"
        if code == 409 or code == 422:
            return "GitHub 写入冲突，稍后重试即可"
    if kind == "webdav":
        if code == 401:
            return "WebDAV 认证失败：用户名或应用密码不对"
        if code == 403:
            return "WebDAV 拒绝访问：这个账号没有写入该目录的权限"
        if code == 404:
            return "WebDAV 目录不存在：检查地址是否少写了层级（填了文件夹的话程序会自动创建，失败多半是上级目录没写权限）"
        if code == 405:
            return "这个地址不支持 WebDAV 的写入方法，确认填的是 WebDAV 入口（不是网页首页）"
    # 兜底：剥掉 JSON 的引号和换行，只留一句话
    text = " ".join(body.split())
    for key in ("message", "error", "detail"):
        found = re.search(rf'"{key}"\s*:\s*"([^"]{{1,160}})"', text)
        if found:
            text = found.group(1)
            break
    return f"HTTP {code}：{text[:160]}" if text else f"HTTP {code}"


def _http(
    method: str,
    url: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
    timeout: float = 30.0,
    kind: str = "",
) -> tuple[int, bytes]:
    try:
        url = security.guard_outbound(url)
    except security.SecurityError as exc:
        raise BackupError(str(exc))
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", "Basic " + token)
    request.add_header("User-Agent", f"tg-checkin/{__version__}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()[:600].decode("utf-8", "ignore")
        raise BackupError(_explain_http(int(exc.code), body, kind), code=int(exc.code))
    except urllib.error.URLError as exc:
        raise BackupError(f"连不上服务：{exc.reason}")
    except TimeoutError:
        raise BackupError("连接超时，检查地址是否可达")


# ---------------- WebDAV ----------------

def _webdav_conf() -> dict[str, str]:
    return {
        "url": db.get_setting("webdav_url", "").strip(),
        "username": db.get_setting("webdav_username", "").strip(),
        "password": db.get_setting("webdav_password", ""),
        "dir": db.get_setting("webdav_dir", "").strip(),
    }


def _webdav_auth(conf: dict[str, str]) -> tuple[str, str] | None:
    return (conf["username"], conf["password"]) if conf["username"] else None


def _clean_subdir(raw: str) -> str:
    """把用户填的文件夹名规整成 a/b 这种相对路径；不合法就报错。"""
    text = str(raw or "").strip().replace("\\", "/")
    parts = [part.strip() for part in text.split("/") if part.strip()]
    if any(part in (".", "..") for part in parts):
        raise BackupError("备份文件夹不能写 . 或 .. 这种相对路径")
    for part in parts:
        if not re.match(r"^[\w\-.@ ]+$", part):
            raise BackupError(f"备份文件夹「{part}」有不能用的字符，只用字母、数字、下划线、横线、点和空格")
    return "/".join(parts)


def normalize_webdav_dir(raw: str) -> str:
    """把面板上填的备份文件夹规整成 a/b；保存设置时用它提前发现非法字符。"""
    return _clean_subdir(raw)


def _webdav_dir(conf: dict[str, str]) -> str:
    url = conf["url"]
    if not url:
        raise BackupError("还没填 WebDAV 地址（在「WebDAV 云备份」里填好后保存）")
    if not url.startswith(("http://", "https://")):
        raise BackupError("WebDAV 地址要以 http:// 或 https:// 开头")
    base = url.rstrip("/") + "/"
    sub = _clean_subdir(conf.get("dir") or "")
    return base + sub + "/" if sub else base


def _webdav_url_at(base: str, depth: int) -> str:
    """取 base 的第 depth 级祖先地址（depth=0 就是站点根）。"""
    parts = urllib.parse.urlsplit(base)
    segments = [seg for seg in parts.path.split("/") if seg]
    path = "/" + "/".join(segments[:depth])
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path + "/", "", ""))


def _webdav_mkdirs(base: str, auth: tuple[str, str] | None) -> list[str]:
    """保证 base 这一串目录都存在，缺哪级建哪级，返回本次新建的目录。

    先自下而上找一个「已存在」的祖先，再自上而下逐级 MKCOL：
    坚果云 / Nextcloud 这类服务对不存在的父目录直接 PUT 会 409，
    而 WebDAV 没有 mkdir -p，只能一级一级来。
    """
    parts = urllib.parse.urlsplit(base)
    depth = len([seg for seg in parts.path.split("/") if seg])
    existing = 0
    for level in range(depth, -1, -1):
        try:
            _http(
                "PROPFIND",
                _webdav_url_at(base, level),
                headers={"Depth": "0"},
                auth=auth,
                timeout=15,
                kind="webdav",
            )
            existing = level
            break
        except BackupError as exc:
            if exc.code in (404, 409):
                continue
            # 401/403 之类不该当成「目录不存在」，交给后面的 MKCOL 抛出真实原因
            existing = level
            break
    created: list[str] = []
    for level in range(existing + 1, depth + 1):
        target = _webdav_url_at(base, level)
        try:
            _http("MKCOL", target, auth=auth, timeout=20, kind="webdav")
            created.append(target)
        except BackupError as exc:
            if exc.code in (200, 201, 301, 302, 405):
                continue  # 已经存在
            raise
    return created


def webdav_ensure_dir() -> dict[str, Any]:
    """按当前设置把云端的备份文件夹建出来（幂等）。"""
    conf = _webdav_conf()
    base = _webdav_dir(conf)
    created = _webdav_mkdirs(base, _webdav_auth(conf))
    return {"ok": True, "dir": base, "created": created}


def webdav_test() -> dict[str, Any]:
    conf = _webdav_conf()
    if not conf["url"]:
        raise BackupError("还没填 WebDAV 地址")
    base = _webdav_dir(conf)
    auth = _webdav_auth(conf)
    # 地址或文件夹是刚填的，先把它建出来；连不上会在这里就抛出真实原因
    created = _webdav_mkdirs(base, auth)
    status, _ = _http(
        "PROPFIND", base, headers={"Depth": "0"}, auth=auth, timeout=20, kind="webdav"
    )
    files = webdav_list()
    sub = _clean_subdir(conf.get("dir") or "")
    if created and sub:
        hint = f"连接成功，已自动创建文件夹「{sub}」，里面现有 {len(files)} 个备份"
    elif sub:
        hint = f"连接成功，文件夹「{sub}」已就绪，里面现有 {len(files)} 个备份"
    else:
        hint = f"连接成功，目录里已有 {len(files)} 个备份"
    return {
        "ok": True,
        "status": status,
        "dir": base,
        "subdir": sub,
        "created": created,
        "count": len(files),
        "files": files[:5],
        "message": hint,
    }


def webdav_list() -> list[dict[str, Any]]:
    conf = _webdav_conf()
    base = _webdav_dir(conf)
    try:
        _, body = _http(
            "PROPFIND",
            base,
            data=b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength/></d:prop></d:propfind>',
            headers={"Depth": "1", "Content-Type": "application/xml"},
            auth=_webdav_auth(conf),
            timeout=20,
            kind="webdav",
        )
    except BackupError as exc:
        if exc.code == 404:
            return []
        raise
    text = body.decode("utf-8", "ignore")
    names: list[str] = []
    for href in re.findall(r"<[^>]*href[^>]*>([^<]+)<", text, flags=re.I):
        name = urllib.parse.unquote(href.rstrip("/").split("/")[-1])
        if NAME_RE.match(name) and name not in names:
            names.append(name)
    names.sort(reverse=True)
    return [{"name": name} for name in names]


def webdav_upload(bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    conf = _webdav_conf()
    base = _webdav_dir(conf)
    auth = _webdav_auth(conf)
    bundle = bundle or build_bundle()
    name = f"tgck-{_stamp()}.json"
    payload = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        _http(
            "PUT",
            base + name,
            data=payload,
            headers={"Content-Type": "application/json"},
            auth=auth,
            timeout=60,
            kind="webdav",
        )
    except BackupError as exc:
        if exc.code not in (404, 409):
            raise
        # 目录不存在就把整串目录补齐，再重试一次
        _webdav_mkdirs(base, auth)
        _http(
            "PUT",
            base + name,
            data=payload,
            headers={"Content-Type": "application/json"},
            auth=auth,
            timeout=60,
            kind="webdav",
        )
    prune_webdav()
    return {"ok": True, "name": name, "size": len(payload), "dir": base}


def webdav_download(name: str) -> dict[str, Any]:
    conf = _webdav_conf()
    base = _webdav_dir(conf)
    _, body = _http(
        "GET", base + _safe_name(name), auth=_webdav_auth(conf), timeout=60, kind="webdav"
    )
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise BackupError(f"云端文件不是合法 JSON：{exc}")


def prune_webdav() -> None:
    try:
        keep = int(db.get_setting("webdav_keep", "14"))
    except ValueError:
        keep = 14
    keep = max(1, min(keep, 200))
    try:
        files = webdav_list()
    except BackupError:
        return
    conf = _webdav_conf()
    for item in files[keep:]:
        try:
            _http(
                "DELETE", _webdav_dir(conf) + item["name"],
                auth=_webdav_auth(conf), timeout=20, kind="webdav",
            )
        except BackupError:
            pass


# ---------------- GitHub 同步 ----------------

GITHUB_API = "https://api.github.com"


def _github_conf() -> dict[str, str]:
    return {
        "token": db.get_setting("github_token", "").strip(),
        "repo": db.get_setting("github_repo", "").strip(),
        "branch": db.get_setting("github_branch", "").strip() or "main",
        "path": db.get_setting("github_path", "").strip().strip("/") or "tgck-backups",
    }


def _github_headers(conf: dict[str, str]) -> dict[str, str]:
    if not conf["token"]:
        raise BackupError("还没填 GitHub Token（需要 repo 权限）")
    return {
        "Authorization": f"Bearer {conf['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


def _normalize_repo(raw: str) -> str:
    """把各种写法统一成 owner/repo。

    允许直接粘贴网页地址（https://github.com/me/data）、git 地址
    （git@github.com:me/data.git）或末尾带子目录的地址，
    免得用户照着浏览器地址栏抄一份就报「找不到仓库」。
    """
    text = str(raw or "").strip()
    if not text:
        raise BackupError("还没填 GitHub 仓库（写成 owner/repo）")
    text = re.sub(r"^[a-z]+://[^/]+/", "", text, flags=re.I)  # 去掉 https://github.com/
    text = re.sub(r"^[^@/]+@[^:]+:", "", text)  # 去掉 git@github.com:
    text = text.split("?")[0].split("#")[0].strip("/")
    if text.lower().endswith(".git"):
        text = text[:-4]
    parts = [part for part in text.split("/") if part]
    if parts and re.match(r"^[\w\-]+\.[a-z]{2,}$", parts[0], re.I):
        parts = parts[1:]  # 用户只写了 github.com/owner/repo
    if len(parts) >= 2:
        text = f"{parts[0]}/{parts[1]}"
    elif parts:
        text = parts[0]
    if not re.match(r"^[\w.\-]+/[\w.\-]+$", text):
        raise BackupError("GitHub 仓库要写成 owner/repo，例如 yourname/tg-checkin-data")
    return text


def _github_repo(conf: dict[str, str]) -> str:
    return _normalize_repo(conf["repo"])


def _github_login(conf: dict[str, str]) -> str:
    """这个 Token 属于谁——排查 owner 写错时很有用。"""
    try:
        _, body = _http(
            "GET", f"{GITHUB_API}/user", headers=_github_headers(conf), timeout=15, kind="github"
        )
        return str(json.loads(body.decode("utf-8") or "{}").get("login") or "")
    except (BackupError, ValueError):
        return ""


def _github_accessible(conf: dict[str, str]) -> list[str]:
    """Token 实际能看到的仓库（细粒度 Token 只会列出被授权的那些）。"""
    url = (
        f"{GITHUB_API}/user/repos?per_page=100&sort=updated"
        "&affiliation=owner,collaborator,organization_member"
    )
    try:
        _, body = _http("GET", url, headers=_github_headers(conf), timeout=20, kind="github")
        data = json.loads(body.decode("utf-8") or "[]")
    except (BackupError, ValueError):
        return []
    return [
        str(item.get("full_name") or "")
        for item in data
        if isinstance(item, dict) and item.get("full_name")
    ]


def _github_404_detail(repo: str, login: str, conf: dict[str, str]) -> str:
    """GitHub 对「不存在」和「没权限」都回 404，这里替用户分辨清楚。"""
    _, _, name = repo.partition("/")
    visible = _github_accessible(conf)
    for full in visible:
        if full.lower() == repo.lower():
            return f"仓库名大小写不对：GitHub 上正确的是「{full}」，改成它就行"
    same_name = [item for item in visible if item.split("/")[-1].lower() == name.lower()]
    if same_name:
        return f"找不到「{repo}」，但这个 Token 能访问「{same_name[0]}」——owner 写错了，改成它"
    who = f"（Token 属于 {login}）" if login else ""
    if not visible:
        return (
            f"找不到「{repo}」{who}，而且这个 Token 连一个仓库都列不出来。"
            "如果是细粒度 Token，需要在创建时把 Repository access 设为 "
            "Only select repositories 并勾上这个仓库（权限至少 Contents: Read and write）；"
            "私有仓库对没授权的 Token 一律返回「找不到」。"
        )
    sample = "、".join(visible[:5]) + ("…" if len(visible) > 5 else "")
    return (
        f"找不到「{repo}」{who}。这个 Token 现在能看到：{sample}。"
        "先在 GitHub 上把仓库建好（空仓库也可以），再确认 Token 的 Repository access 勾选了它。"
    )


def _github_branch_exists(conf: dict[str, str], repo: str) -> bool:
    branch = urllib.parse.quote(conf["branch"], safe="")
    try:
        _http(
            "GET",
            f"{GITHUB_API}/repos/{repo}/branches/{branch}",
            headers=_github_headers(conf),
            timeout=15,
            kind="github",
        )
        return True
    except BackupError as exc:
        if exc.code == 404:
            return False
        raise


def github_test() -> dict[str, Any]:
    conf = _github_conf()
    repo = _github_repo(conf)
    login = _github_login(conf)
    try:
        status, body = _http(
            "GET", f"{GITHUB_API}/repos/{repo}", headers=_github_headers(conf), timeout=20,
            kind="github",
        )
    except BackupError as exc:
        if exc.code == 404:
            raise BackupError(_github_404_detail(repo, login, conf), code=404)
        raise
    info = json.loads(body.decode("utf-8") or "{}")
    default_branch = str(info.get("default_branch") or "main")
    if not _github_branch_exists(conf, repo):
        raise BackupError(
            f"仓库没问题，但分支「{conf['branch']}」不存在——这个仓库的默认分支是"
            f"「{default_branch}」，把「分支」改成它再保存",
            code=404,
        )
    files = github_list()
    return {
        "ok": True,
        "status": status,
        "repo": repo,
        "login": login,
        "branch": conf["branch"],
        "default_branch": default_branch,
        "private": bool(info.get("private")),
        "count": len(files),
        "message": f"连接成功，{conf['path']} 里已有 {len(files)} 个备份",
    }


def github_list() -> list[dict[str, Any]]:
    conf = _github_conf()
    repo = _github_repo(conf)
    url = f"{GITHUB_API}/repos/{repo}/contents/{urllib.parse.quote(conf['path'])}?ref={conf['branch']}"
    try:
        _, body = _http("GET", url, headers=_github_headers(conf), timeout=20, kind="github")
    except BackupError as exc:
        if exc.code == 404:
            return []
        raise
    data = json.loads(body.decode("utf-8") or "[]")
    files = [
        {"name": item.get("name"), "size": item.get("size"), "sha": item.get("sha")}
        for item in data
        if isinstance(item, dict) and NAME_RE.match(str(item.get("name") or ""))
    ]
    files.sort(key=lambda i: str(i["name"]), reverse=True)
    return files


def _github_put(conf: dict[str, str], path: str, text: str, message: str) -> None:
    repo = _github_repo(conf)
    url = f"{GITHUB_API}/repos/{repo}/contents/{urllib.parse.quote(path)}"
    sha = ""
    try:
        _, body = _http(
            "GET", f"{url}?ref={conf['branch']}", headers=_github_headers(conf), timeout=20,
            kind="github",
        )
        sha = str(json.loads(body.decode("utf-8") or "{}").get("sha") or "")
    except BackupError as exc:
        if exc.code != 404:
            raise
    payload: dict[str, Any] = {
        "message": message,
        "branch": conf["branch"],
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    _http(
        "PUT",
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=_github_headers(conf),
        timeout=60,
        kind="github",
    )


def github_upload(bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    conf = _github_conf()
    bundle = bundle or build_bundle()
    name = f"tgck-{_stamp()}.json"
    text = json.dumps(bundle, ensure_ascii=False, indent=2)
    _github_put(conf, f"{conf['path']}/{name}", text, f"backup: {name}")
    prune_github()
    return {"ok": True, "name": name, "size": len(text.encode("utf-8"))}


def github_download(name: str) -> dict[str, Any]:
    conf = _github_conf()
    repo = _github_repo(conf)
    path = f"{conf['path']}/{_safe_name(name)}"
    url = f"{GITHUB_API}/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={conf['branch']}"
    _, body = _http("GET", url, headers=_github_headers(conf), timeout=60, kind="github")
    data = json.loads(body.decode("utf-8") or "{}")
    content = str(data.get("content") or "").replace("\n", "")
    try:
        raw = base64.b64decode(content).decode("utf-8")
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupError(f"云端文件解析失败：{exc}")


def prune_github() -> None:
    try:
        keep = int(db.get_setting("github_keep", "14"))
    except ValueError:
        keep = 14
    keep = max(1, min(keep, 200))
    try:
        files = github_list()
    except BackupError:
        return
    conf = _github_conf()
    repo = _github_repo(conf)
    for item in files[keep:]:
        path = f"{conf['path']}/{item['name']}"
        url = f"{GITHUB_API}/repos/{repo}/contents/{urllib.parse.quote(path)}"
        try:
            _http(
                "DELETE",
                url,
                data=json.dumps(
                    {
                        "message": f"prune: {item['name']}",
                        "sha": item.get("sha", ""),
                        "branch": conf["branch"],
                    }
                ).encode("utf-8"),
                headers=_github_headers(conf),
                timeout=20,
                kind="github",
            )
        except BackupError:
            pass


# ---------------- 自动备份 ----------------

_thread: threading.Thread | None = None
_stop: threading.Event | None = None
_lock = threading.RLock()


def _mark(kind: str, ok: bool, message: str = "") -> None:
    db.set_setting("backup_last_run", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    db.set_setting("backup_last_target", kind)
    db.set_setting("backup_last_error", "" if ok else message[:300])


def run_auto() -> dict[str, Any]:
    """跑一次自动备份：本地必存，云端按各自的开关上传。"""
    bundle = build_bundle(include_logs=False)
    result: dict[str, Any] = {"ok": True, "local": "", "webdav": "", "github": "", "errors": []}
    try:
        result["local"] = save_local(bundle)["name"]
    except Exception as exc:  # 本地磁盘满 / 权限问题
        result["ok"] = False
        result["errors"].append(f"本地备份失败：{exc}")

    if db.get_setting("webdav_auto", "0") == "1" and db.get_setting("webdav_url", "").strip():
        try:
            result["webdav"] = webdav_upload(bundle)["name"]
        except Exception as exc:
            result["ok"] = False
            result["errors"].append(f"WebDAV 上传失败：{exc}")
            log.warning("WebDAV 自动备份失败：%s", exc)

    if db.get_setting("github_auto", "0") == "1" and db.get_setting("github_repo", "").strip():
        try:
            result["github"] = github_upload(bundle)["name"]
        except Exception as exc:
            result["ok"] = False
            result["errors"].append(f"GitHub 同步失败：{exc}")
            log.warning("GitHub 自动同步失败：%s", exc)

    targets = [k for k in ("webdav", "github") if result[k]]
    _mark("+".join(targets) or "local", result["ok"], "；".join(result["errors"]))
    log.info("自动备份完成：本地 %s，云端 %s", result["local"], targets or "无")
    return result


def _auto_loop(stop: threading.Event) -> None:
    stop.wait(90)  # 等容器和网络就绪，别一开机就抢带宽
    while not stop.is_set():
        try:
            if db.get_setting("backup_auto", "0") == "1":
                run_auto()
        except Exception as exc:
            log.warning("自动备份检查失败：%s", exc)
        try:
            hours = max(1, min(int(float(db.get_setting("backup_interval", "12") or 12)), 168))
        except ValueError:
            hours = 12
        stop.wait(hours * 3600)


def start_auto() -> None:
    global _thread, _stop
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop = threading.Event()
        _thread = threading.Thread(target=_auto_loop, args=(_stop,), name="backup", daemon=True)
    _thread.start()
    log.info("自动备份线程已启动")


def stop_auto() -> None:
    global _thread, _stop
    with _lock:
        if _stop is not None:
            _stop.set()
        _thread = None
        _stop = None


def auto_status() -> dict[str, Any]:
    with _lock:
        alive = _thread is not None and _thread.is_alive()
    return {
        "running": alive,
        "enabled": db.get_setting("backup_auto", "0") == "1",
        "interval": db.get_setting("backup_interval", "12"),
        "keep": db.get_setting("backup_keep", "14"),
        "restore_on_boot": db.get_setting("backup_restore_on_boot", "1") == "1",
        "last_run": db.get_setting("backup_last_run", ""),
        "last_target": db.get_setting("backup_last_target", ""),
        "last_error": db.get_setting("backup_last_error", ""),
        "local_count": len(list_local()),
    }


# ---------------- 开机自动恢复（应对无持久卷的平台） ----------------

def looks_empty() -> bool:
    return not db.list_groups() and not db.list_accounts() and not db.list_bots()


def auto_restore_on_boot() -> None:
    """本地库空空如也（比如新容器）时，从云端拉最新备份恢复一把。"""
    if db.get_setting("backup_restore_on_boot", "1") != "1":
        return
    if not looks_empty():
        return

    candidates: list[tuple[str, str]] = []
    if db.get_setting("webdav_auto", "0") == "1" and db.get_setting("webdav_url", "").strip():
        candidates.append(("webdav", "WebDAV"))
    if db.get_setting("github_auto", "0") == "1" and db.get_setting("github_repo", "").strip():
        candidates.append(("github", "GitHub"))

    for kind, label in candidates:
        try:
            files = webdav_list() if kind == "webdav" else github_list()
        except Exception as exc:
            log.warning("%s 备份列表读取失败：%s", label, exc)
            continue
        if not files:
            continue
        name = str(files[0]["name"])
        try:
            bundle = webdav_download(name) if kind == "webdav" else github_download(name)
            result = restore_bundle(bundle, mode="replace", restore_password=True)
            log.info("已从 %s 的 %s 自动恢复：%s", label, name, result)
            return
        except Exception as exc:
            log.warning("从 %s 恢复 %s 失败：%s", label, name, exc)


def _status_repo(raw: str) -> str:
    try:
        return _normalize_repo(raw) if raw else ""
    except BackupError:
        return raw


def status() -> dict[str, Any]:
    conf_dav = _webdav_conf()
    conf_gh = _github_conf()
    try:
        dav_dir = _webdav_dir(conf_dav) if conf_dav["url"] else ""
    except BackupError:
        dav_dir = ""
    return {
        "auto": auto_status(),
        "local": list_local()[:20],
        "webdav": {
            "configured": bool(conf_dav["url"]),
            "url": conf_dav["url"],
            "dir": conf_dav["dir"],
            "full_dir": dav_dir,
            "username": conf_dav["username"],
            "password_set": bool(conf_dav["password"]),
            "auto": db.get_setting("webdav_auto", "0") == "1",
            "keep": db.get_setting("webdav_keep", "14"),
        },
        "github": {
            "configured": bool(conf_gh["repo"] and conf_gh["token"]),
            "repo": _status_repo(conf_gh["repo"]),
            "branch": conf_gh["branch"],
            "path": conf_gh["path"],
            "token_set": bool(conf_gh["token"]),
            "auto": db.get_setting("github_auto", "0") == "1",
            "keep": db.get_setting("github_keep", "14"),
        },
    }
