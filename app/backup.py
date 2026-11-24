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

from . import __version__, db

log = logging.getLogger("tg-checkin.backup")

BACKUP_DIR = db.DATA_DIR / "backups"
FORMAT_VERSION = 2
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

    # 2) 账号 / 机器人：先恢复，拿到新 ID 的映射关系
    if mode == "replace":
        for item in db.list_groups():
            db.delete_group(int(item["id"]))
        for item in db.list_accounts():
            db.delete_account(int(item["id"]))
        for item in db.list_bots():
            db.delete_bot(int(item["id"]))

    account_map = _restore_accounts(bundle.get("accounts") or [], mode)
    bot_map = _restore_bots(bundle.get("bots") or [], mode)
    groups = _restore_groups(bundle.get("groups") or [], mode, account_map, bot_map)
    logs = _restore_logs(bundle.get("logs") or [], mode)

    return {
        "ok": True,
        "mode": mode,
        "settings": restored_settings,
        "accounts": len(account_map),
        "bots": len(bot_map),
        "groups": groups,
        "logs": logs,
    }


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


def _restore_bots(items: list[Any], mode: str) -> dict[int, int]:
    mapping: dict[int, int] = {}
    existing = {str(b.get("token") or ""): int(b["id"]) for b in db.list_bots()}
    for item in items:
        if not isinstance(item, dict):
            continue
        old_id = item.get("id")
        payload = {
            "name": str(item.get("name") or ""),
            "token": str(item.get("token") or ""),
            "username": str(item.get("username") or ""),
            "enabled": bool(item.get("enabled", True)),
            "trigger": str(item.get("trigger") or "mention"),
            "business_connections": str(item.get("business_connections") or ""),
            "known_chats": str(item.get("known_chats") or ""),
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
            return "WebDAV 目录不存在，检查地址是否少写了层级"
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
    }


def _webdav_auth(conf: dict[str, str]) -> tuple[str, str] | None:
    return (conf["username"], conf["password"]) if conf["username"] else None


def _webdav_dir(conf: dict[str, str]) -> str:
    url = conf["url"]
    if not url:
        raise BackupError("还没填 WebDAV 地址（在「WebDAV 云备份」里填好后保存）")
    if not url.startswith(("http://", "https://")):
        raise BackupError("WebDAV 地址要以 http:// 或 https:// 开头")
    return url.rstrip("/") + "/"


def webdav_test() -> dict[str, Any]:
    conf = _webdav_conf()
    if not conf["url"]:
        raise BackupError("还没填 WebDAV 地址")
    base = _webdav_dir(conf)
    auth = _webdav_auth(conf)
    status, _ = _http(
        "PROPFIND", base, headers={"Depth": "0"}, auth=auth, timeout=20, kind="webdav"
    )
    files = webdav_list()
    return {
        "ok": True,
        "status": status,
        "dir": base,
        "count": len(files),
        "files": files[:5],
        "message": f"连接成功，目录里已有 {len(files)} 个备份",
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
        # 目录不存在就建一层，再重试一次
        try:
            _http("MKCOL", base, auth=auth, timeout=20, kind="webdav")
        except BackupError as mk:
            if mk.code != 405:  # 405 = 目录已存在，可以忽略
                raise
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
    return {"ok": True, "name": name, "size": len(payload)}


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


def _github_repo(conf: dict[str, str]) -> str:
    repo = conf["repo"]
    if not repo:
        raise BackupError("还没填 GitHub 仓库（写成 owner/repo）")
    if not re.match(r"^[\w.\-]+/[\w.\-]+$", repo):
        raise BackupError("GitHub 仓库要写成 owner/repo，例如 yourname/tg-checkin-data")
    return repo


def github_test() -> dict[str, Any]:
    conf = _github_conf()
    repo = _github_repo(conf)
    status, body = _http(
        "GET", f"{GITHUB_API}/repos/{repo}", headers=_github_headers(conf), timeout=20,
        kind="github",
    )
    info = json.loads(body.decode("utf-8") or "{}")
    files = github_list()
    return {
        "ok": True,
        "status": status,
        "repo": repo,
        "branch": conf["branch"],
        "default_branch": info.get("default_branch", ""),
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


def status() -> dict[str, Any]:
    conf_dav = _webdav_conf()
    conf_gh = _github_conf()
    return {
        "auto": auto_status(),
        "local": list_local()[:20],
        "webdav": {
            "configured": bool(conf_dav["url"]),
            "url": conf_dav["url"],
            "username": conf_dav["username"],
            "password_set": bool(conf_dav["password"]),
            "auto": db.get_setting("webdav_auto", "0") == "1",
            "keep": db.get_setting("webdav_keep", "14"),
        },
        "github": {
            "configured": bool(conf_gh["repo"] and conf_gh["token"]),
            "repo": conf_gh["repo"],
            "branch": conf_gh["branch"],
            "path": conf_gh["path"],
            "token_set": bool(conf_gh["token"]),
            "auto": db.get_setting("github_auto", "0") == "1",
            "keep": db.get_setting("github_keep", "14"),
        },
    }
