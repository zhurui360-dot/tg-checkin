"""基于 APScheduler 的动态 cron 调度：配置改动后热重载任务。"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db, telegram

log = logging.getLogger("tg-checkin.scheduler")

_scheduler: BackgroundScheduler | None = None
_lock = threading.RLock()


def _timezone() -> ZoneInfo:
    name = db.get_setting("timezone", "Asia/Shanghai") or "Asia/Shanghai"
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("无效时区 %s，回退到 Asia/Shanghai", name)
        return ZoneInfo("Asia/Shanghai")


def parse_expression(expr: str, tz: ZoneInfo | None = None) -> CronTrigger:
    return CronTrigger.from_crontab(str(expr).strip(), timezone=tz or _timezone())


def _run(group_id: int, expression: str) -> None:
    try:
        result = telegram.execute_checkin(group_id, source="auto")
        log.info("群组 %s [%s] 自动签到：%s", group_id, expression, result["message"][:120])
    except Exception as exc:
        log.exception("群组 %s 自动签到异常: %s", group_id, exc)
        db.add_log(group_id, "自动任务", "auto", "failed", f"调度异常: {exc}", 0)


def start() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return
        _scheduler = BackgroundScheduler(timezone=_timezone())
        _scheduler.start()
        reload_jobs()
        log.info("调度器已启动")


def shutdown() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None


def reload_jobs() -> None:
    """按数据库里的群组配置重建全部任务。"""
    with _lock:
        if _scheduler is None:
            return
        for job in _scheduler.get_jobs():
            job.remove()

        tz = _timezone()
        for group in db.list_groups(enabled_only=True):
            try:
                expressions: list[str] = json.loads(group["schedule"] or "[]")
            except (ValueError, TypeError):
                log.warning("群组 %s 的 schedule 格式错误，跳过", group["id"])
                continue
            for index, expression in enumerate(expressions):
                try:
                    trigger = parse_expression(expression, tz)
                except Exception as exc:
                    log.warning("群组 %s cron 表达式 %r 无效: %s", group["id"], expression, exc)
                    continue
                _scheduler.add_job(
                    _run,
                    trigger=trigger,
                    args=[group["id"], expression],
                    id=f"group-{group['id']}-{index}",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=600,
                    replace_existing=True,
                )
        log.info("调度任务已重载，当前任务数：%s", len(_scheduler.get_jobs()))


def next_runs() -> dict[str, list[dict[str, str]]]:
    """给面板展示每个群组的下次执行时间。"""
    result: dict[str, list[dict[str, str]]] = {}
    with _lock:
        if _scheduler is None:
            return result
        for job in _scheduler.get_jobs():
            parts = job.id.split("-")
            if len(parts) < 3:
                continue
            group_id = parts[1]
            result.setdefault(group_id, []).append(
                {
                    "expression": str(job.trigger),
                    "next": job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
                    if isinstance(job.next_run_time, datetime)
                    else "",
                }
            )
    return result


def job_count() -> int:
    with _lock:
        return 0 if _scheduler is None else len(_scheduler.get_jobs())


# ---------------- 补签：定时点没在线，就在上线后补一次 ----------------
#
# 容器被平台休眠 / 重启时，APScheduler 那些到点的任务不会真的执行，
# 于是「每天签到一次」就漏了。这里用一个独立线程定时核对：
# 只要今天已经有计划时间过去了、而日志里还没有成功记录，就立刻补一次。
# 补签同样会写日志（source=catchup），所以不会重复补；失败后按冷却时间再试。

_catchup_thread: threading.Thread | None = None
_catchup_stop: threading.Event | None = None


def _scheduled_times_passed_today(group: dict[str, Any], now: datetime) -> list[datetime]:
    """列出今天已经到点的计划时间。"""
    try:
        expressions = json.loads(group.get("schedule") or "[]")
    except (ValueError, TypeError):
        return []
    tz = now.tzinfo
    passed: list[datetime] = []
    for expression in expressions if isinstance(expressions, list) else []:
        try:
            trigger = parse_expression(str(expression), tz)
        except Exception:
            continue
        cursor = now.replace(hour=0, minute=0, second=0, microsecond=0)
        fire = trigger.get_next_fire_time(None, cursor)
        guard = 0
        while fire is not None and fire <= now and guard < 100:
            passed.append(fire)
            fire = trigger.get_next_fire_time(fire, fire + timedelta(seconds=1))
            guard += 1
    return passed


def catch_up(reason: str = "上线") -> list[dict[str, Any]]:
    """给今天还没签到成功的群组补一次，返回本次补签结果。"""
    cfg = db.get_settings()
    if str(cfg.get("catchup_enabled", "1")) != "1":
        return []
    try:
        cooldown = max(0, int(float(cfg.get("catchup_cooldown", "1800"))))
    except ValueError:
        cooldown = 1800

    now = datetime.now(_timezone())
    results: list[dict[str, Any]] = []
    for group in db.list_groups(enabled_only=True):
        if db.success_today(group["id"]):
            continue
        if not _scheduled_times_passed_today(group, now):
            continue  # 今天还没到点，等正常调度
        last = db.last_attempt_epoch(group["id"])
        if cooldown and last and (now.timestamp() - last) < cooldown:
            continue  # 刚试过还没到冷却时间，别把群刷屏
        log.info("群组 %s 今天还没签到成功（%s），开始补签", group["id"], reason)
        try:
            result = telegram.execute_checkin(group["id"], source="catchup")
            results.append(result)
            log.info("群组 %s 补签结果：%s", group["id"], str(result.get("message"))[:120])
        except Exception as exc:
            log.exception("群组 %s 补签异常: %s", group["id"], exc)
            db.add_log(group["id"], group.get("name", ""), "catchup", "failed", f"补签异常: {exc}", 0)
    return results


def _catchup_loop(stop: threading.Event) -> None:
    # 启动后先等一会：让容器和网络、Bot API 都就绪，避免一开机就报错
    stop.wait(25)
    while not stop.is_set():
        try:
            catch_up("开机补签")
        except Exception as exc:
            log.warning("补签检查失败：%s", exc)
        interval = 600
        try:
            interval = max(120, min(int(float(db.get_setting("catchup_interval", "600") or 600)), 3600))
        except ValueError:
            pass
        stop.wait(interval)


def start_catchup() -> None:
    global _catchup_thread, _catchup_stop
    with _lock:
        if _catchup_thread is not None and _catchup_thread.is_alive():
            return
        _catchup_stop = threading.Event()
        _catchup_thread = threading.Thread(
            target=_catchup_loop, args=(_catchup_stop,), name="catchup", daemon=True
        )
    _catchup_thread.start()
    log.info("补签检查线程已启动")


def stop_catchup() -> None:
    global _catchup_thread, _catchup_stop
    with _lock:
        if _catchup_stop is not None:
            _catchup_stop.set()
        _catchup_thread = None
        _catchup_stop = None


def catchup_status() -> dict[str, Any]:
    with _lock:
        alive = _catchup_thread is not None and _catchup_thread.is_alive()
    return {
        "running": alive,
        "enabled": db.get_setting("catchup_enabled", "1") == "1",
        "interval": db.get_setting("catchup_interval", "600"),
        "cooldown": db.get_setting("catchup_cooldown", "1800"),
    }
