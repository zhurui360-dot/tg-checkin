"""面板安全基线：会话签名、登录限速、响应头、出网目标校验。

集中放一处，main / backup / bot 共用，避免每处各写一套规则。
原则：只做「不改变使用方式」的加固，不引入需要用户额外配置的步骤。
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import db

log = logging.getLogger("tg-checkin.security")

SESSION_COOKIE = "tgck_session"
SESSION_TTL = 7 * 24 * 3600  # 面板里全是账号凭据，会话不宜太久
MAX_BODY = 32 * 1024 * 1024  # 备份恢复走 JSON body，超过这个体积一定是异常请求
SECRET_KEY = "panel_secret"


class SecurityError(RuntimeError):
    """出网目标不安全（云元数据、非法 scheme 等）。"""


# ---------------- 会话签名 ----------------

_secret_cache: str | None = None
_secret_lock = threading.Lock()


def _secret() -> str:
    """面板会话签名密钥：首次启动随机生成并落库，重启后会话仍然有效。"""
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    with _secret_lock:
        if _secret_cache:
            return _secret_cache
        value = db.get_setting(SECRET_KEY) or ""
        if len(value) < 32:
            value = secrets.token_hex(32)
            db.set_setting(SECRET_KEY, value)
        _secret_cache = value
        return value


def _fingerprint(password: str, secret: str) -> str:
    """把密码折成指纹放进 token：改密码后所有旧会话立即失效。"""
    return hmac.new(secret.encode(), f"pw|{password}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def issue_session(password: str, ttl: int = SESSION_TTL) -> str:
    """签一个会话 token：v1.<过期时间>.<随机串>.<密码指纹>.<签名>。"""
    secret = _secret()
    body = f"v1.{int(time.time()) + ttl}.{secrets.token_urlsafe(9)}.{_fingerprint(password, secret)}"
    sig = hmac.new(secret.encode(), body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def verify_session(token: str, password: str) -> bool:
    """校验 token：签名、有效期、密码指纹三者都对才算通过。"""
    if not token or not password:
        return False
    parts = str(token).split(".")
    if len(parts) != 5:
        return False
    version, exp, sid, fp, sig = parts
    if version != "v1" or not exp.isdigit() or int(exp) < time.time():
        return False
    secret = _secret()
    body = f"{version}.{exp}.{sid}.{fp}"
    expect = hmac.new(secret.encode(), body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expect):
        return False
    return hmac.compare_digest(fp, _fingerprint(password, secret))


def password_ok(given: str, expected: str) -> bool:
    """常量时间比较，别让响应时间泄露密码前缀。"""
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


# ---------------- 登录限速 ----------------

class LoginThrottle:
    """同一个 IP 连续输错就逐步拉长等待，挡住在线爆破。

    只记内存，重启即清空——面板本来就跑在单进程里，够用且不占磁盘。
    """

    def __init__(self, limit: int = 5, window: float = 600.0, max_wait: float = 900.0) -> None:
        self.limit = limit
        self.window = window
        self.max_wait = max_wait
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def wait_seconds(self, key: str) -> int:
        """还要等多久才能再试，0 表示可以试。"""
        now = time.time()
        with self._lock:
            hits = self._recent(key, now)
        if len(hits) < self.limit:
            return 0
        over = len(hits) - self.limit + 1
        wait = min(self.max_wait, 30.0 * (2 ** min(over - 1, 6)))
        return max(0, int(wait - (now - hits[-1])))

    def fail(self, key: str) -> None:
        now = time.time()
        with self._lock:
            hits = self._recent(key, now)
            hits.append(now)
            self._hits[key] = hits[-40:]

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


login_throttle = LoginThrottle()


# ---------------- 请求来源校验 ----------------

def same_origin(headers) -> bool:
    """非 GET 请求带 Origin/Referer 时，必须和当前 Host 一致（防 CSRF 兜底）。"""
    origin = (headers.get("origin") or "").strip()
    if not origin:
        referer = (headers.get("referer") or "").strip()
        origin = referer
    if not origin:
        return True  # curl / 脚本调用没有这个头，交给 cookie 鉴权
    if origin == "null":
        return False
    try:
        host = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    current = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",")[0].strip().lower()
    return bool(host) and host == current


# ---------------- 响应头 ----------------

CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "object-src 'none'"
)


def apply_headers(response, path: str, https: bool) -> None:
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    headers.setdefault("Content-Security-Policy", CSP)
    if https:
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if path.startswith("/api/") or path in ("/", "/index.html"):
        # 面板返回的是账号、会话、Key 明文，禁止任何中间层缓存
        headers.setdefault("Cache-Control", "no-store")


# ---------------- 出网目标校验 ----------------

_BLOCKED_HOSTS = {
    "metadata.google.internal",
    "metadata",
    "instance-data",
    "metadata.tencentyun.com",
}
_BLOCKED_IPS = {
    "100.100.100.200",  # 阿里云元数据
    "169.254.169.254",  # AWS / GCP / Azure 元数据
}


def guard_outbound(url: str) -> str:
    """拦住云厂商元数据地址，防止「填个 URL」就把容器身份凭据读走。

    只挡链路本地与元数据地址，局域网 NAS / 本地代理照常可用。
    """
    text = str(url or "").strip()
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise SecurityError("地址要以 http:// 或 https:// 开头")
    host = (parts.hostname or "").lower()
    if not host:
        raise SecurityError("地址里缺少主机名")
    if host in _BLOCKED_HOSTS or host in _BLOCKED_IPS:
        raise SecurityError("这个地址指向云平台元数据服务，出于安全考虑禁止访问")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return text
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast or str(ip) in _BLOCKED_IPS:
        raise SecurityError("不允许访问链路本地 / 保留地址")
    return text


# ---------------- 文件权限 ----------------

def harden_permissions(*paths: Path) -> None:
    """数据目录里是 bot token、登录会话、AI Key：目录 700、文件 600。

    Windows 上 chmod 语义有限，失败就跳过，不影响运行。
    """
    for path in paths:
        try:
            if not Path(path).exists():
                continue
            os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)
        except OSError:
            pass


def default_password_in_use() -> bool:
    return (db.get_setting("panel_password") or "") == "admin123"
