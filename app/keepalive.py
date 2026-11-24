"""保活自检：定时请求自身公网地址，确认服务可被外部访问。

重要限制：进程内线程无法阻止实例休眠。
平台判断是否休眠看的是「有没有外部入站流量」，而实例一旦被停止，
本模块的线程也会随之消失，所以它无法唤醒已经睡下去的自己。
真正防止休眠必须靠外部定时器（仓库自带的 .github/workflows/keepalive.yml）。

这里的作用是：
1. 主动制造入站流量，在实例仍然存活时延长活跃窗口；
2. 提前暴露域名解析、代理、鉴权等外部可达性问题，而不是等到触发时刻才发现。
"""
from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from . import db, security

log = logging.getLogger("tg-checkin.keepalive")

_state_lock = threading.Lock()
_learn_lock = threading.Lock()

# 每一代保活线程持有自己的停止信号，避免重启时新旧线程并存
_generation = 0
_threads: dict[int, threading.Thread] = {}
_stops: dict[int, threading.Event] = {}

# 绕过环境里的 http_proxy，否则自检请求可能被代理拦掉
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_state: dict[str, Any] = {
    "learned_base": "",
    "last_ping": "",
    "last_ok": None,
    "last_status": "",
    "fail_streak": 0,
    "ok_count": 0,
}

MIN_INTERVAL = 60
MAX_INTERVAL = 3600


def enabled() -> bool:
    return db.get_setting("keepalive_enabled", "0") == "1"


def interval() -> int:
    raw = db.get_setting("keepalive_interval", "300")
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = 300
    return max(MIN_INTERVAL, min(value, MAX_INTERVAL))


def target_url() -> str:
    """优先用面板里填的地址，其次是自动学到的公网地址。"""
    configured = (db.get_setting("keepalive_url", "") or "").strip()
    if configured:
        base = configured if "://" in configured else f"https://{configured}"
        base = base.rstrip("/")
        # 只填了域名时补上 /health，避免为了自检去拉首页 HTML
        if not urlsplit(base).path:
            return f"{base}/health"
        return base
    with _state_lock:
        base = _state["learned_base"]
    if base:
        return f"{base}/health"
    env = (os.getenv("PUBLIC_URL", "") or "").strip()
    if env:
        return env.rstrip("/") + "/health"
    return ""


def _is_local(host: str) -> bool:
    plain = host.split(":")[0].strip("[]").lower()
    if plain in ("", "localhost", "127.0.0.1", "0.0.0.0", "::1", "testserver"):
        return True
    return plain.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19."))


def learn_from_request(scheme: str, host: str) -> None:
    """从真实外部请求里学出公网地址，只认第一次看到的非本地 host。"""
    if not host or _is_local(host):
        return
    base = f"{scheme or 'https'}://{host}".rstrip("/")
    with _learn_lock:
        with _state_lock:
            if _state["learned_base"]:
                return
            _state["learned_base"] = base
    log.info("保活目标地址已确定为 %s/health", base)


def ping_now() -> dict[str, Any]:
    """执行一次自检，返回最新状态。失败只记录，不抛异常。"""
    url = target_url()
    if not url:
        return status()

    started = time.time()
    ok = False
    status_text = ""
    try:
        url = security.guard_outbound(url)
        request = urllib.request.Request(
            url, headers={"User-Agent": "tg-checkin-keepalive/1.0"}
        )
        with _opener.open(request, timeout=20) as response:
            status_text = str(response.status)
            ok = 200 <= int(response.status) < 400
    except urllib.error.HTTPError as exc:
        status_text = str(exc.code)
    except Exception as exc:  # 网络异常不抛出，只记录
        status_text = f"error: {exc}"

    with _state_lock:
        _state["last_ping"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
        _state["last_ok"] = ok
        _state["last_status"] = status_text
        if ok:
            _state["ok_count"] += 1
            _state["fail_streak"] = 0
            streak = 0
        else:
            _state["fail_streak"] += 1
            streak = _state["fail_streak"]

    if not ok:
        log.warning("保活请求失败（连续 %s 次）：%s -> %s", streak, url, status_text)
        # 只在第 1 次和每 10 次记一条，避免刷爆日志
        if streak == 1 or streak % 10 == 0:
            db.add_log(
                None, "保活自检", "keepalive", "failed",
                f"请求 {url} 失败：{status_text}",
                int((time.time() - started) * 1000),
            )
    return status()


def _loop(gen: int, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            if enabled():
                ping_now()
        except Exception as exc:  # 保活线程不能因异常退出
            log.exception("保活循环异常: %s", exc)
        stop_event.wait(interval())
    with _state_lock:
        _threads.pop(gen, None)
        _stops.pop(gen, None)


def _stop_all() -> None:
    for event in list(_stops.values()):
        event.set()


def start() -> None:
    global _generation
    with _state_lock:
        alive = [t for t in _threads.values() if t.is_alive()]
        if alive:
            return
        _generation += 1
        gen = _generation
        stop_event = threading.Event()
        _stops[gen] = stop_event
        thread = threading.Thread(target=_loop, args=(gen, stop_event), name=f"keepalive-{gen}", daemon=True)
        _threads[gen] = thread
    thread.start()
    log.info("保活线程已启动，间隔 %s 秒，启用=%s", interval(), enabled())


def stop() -> None:
    with _state_lock:
        _stop_all()
        _threads.clear()
        _stops.clear()


def restart() -> None:
    """设置改动后让新间隔尽快生效。"""
    stop()
    start()


def status() -> dict[str, Any]:
    with _state_lock:
        snapshot = dict(_state)
    url = target_url()
    return {
        "enabled": enabled(),
        "interval": interval(),
        "url": url,
        "detected": bool(url),
        "last_ping": snapshot["last_ping"],
        "last_ok": snapshot["last_ok"],
        "last_status": snapshot["last_status"],
        "fail_streak": snapshot["fail_streak"],
        "ok_count": snapshot["ok_count"],
    }
