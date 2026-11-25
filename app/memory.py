"""智能体的长期记忆：自动提炼 + 按相关度注入。

和「上下文条数」的区别：上下文是最近 N 条对话，滚出窗口就忘了；这里是主动提炼
出来的稳定事实（称呼、偏好、身份、约定、正在做的事），存在 SQLite 里，跨天、
跨会话都还在。

没有引入向量库：中文用字符二元组做重叠打分，配合新近度，几十条笔记的规模足够，
且不增加任何依赖。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

from . import db

log = logging.getLogger("tg-checkin.memory")

# 每个会话的提炼节流：至少隔 3 轮对话、90 秒
_learn_seen: dict[str, int] = {}
_learn_at: dict[str, float] = {}
_learn_lock = threading.Lock()

MAX_NOTES_PER_SCOPE = 12
MAX_NOTES_PER_AGENT = 120

_EXTRACT_SYSTEM = (
    "你是记忆整理器。从给定的一轮对话里抽取值得长期记住的稳定事实："
    "对方的名字或称呼、身份职业、长期偏好与忌讳、明确说过的约定、正在做的事。"
    "不要记录寒暄、一次性问题、模型自己的猜测。"
    "只输出 JSON 数组，元素形如 {\"scope\":\"user\",\"content\":\"...\"}；"
    "scope 取 user（关于这个人）、chat（关于这个群或这个会话）、global（跟谁聊都适用）。"
    "每条不超过 50 个字，用陈述句，不要带「用户说」这类前缀。没有值得记的就输出 []。"
)


def _normalize(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()\[\]【】]+", "", text or "").lower()


def _bigrams(text: str) -> set[str]:
    clean = _normalize(text)
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def _similar(a: str, b: str) -> bool:
    """判断两条记忆是不是同一件事，避免重复入库。"""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return False
    return len(ba & bb) / len(ba | bb) >= 0.72


def _score(query: str, content: str, note_id: int) -> float:
    """相关度：二元组重叠为主，新近度做小幅加权。"""
    q, c = _bigrams(query), _bigrams(content)
    overlap = len(q & c) / max(1, len(q)) if q else 0.0
    return overlap * 10 + min(note_id, 100000) / 100000.0


def context(
    agent_id: int,
    chat_id: str,
    author: str,
    query: str,
    limit: int = 8,
    budget: int = 700,
) -> str:
    """挑出和当前这句话最相关的几条记忆，拼成一段可注入的系统提示。"""
    if db.get_setting("memory_enabled", "1") != "1":
        return ""
    pool = db.note_pool(agent_id, chat_id, author)
    if not pool:
        return ""
    ranked = sorted(pool, key=lambda n: _score(query, str(n.get("content") or ""), int(n["id"])), reverse=True)
    picked: list[dict[str, Any]] = []
    used = 0
    for note in ranked:
        content = str(note.get("content") or "").strip()
        if not content or content in [p["content"] for p in picked]:
            continue
        if used + len(content) > budget:
            continue
        picked.append(note)
        used += len(content)
        if len(picked) >= max(1, min(int(limit), 20)):
            break
    if not picked:
        return ""
    labels = {"user": "关于对方", "chat": "关于这个会话", "global": "通用"}
    lines = [f"- （{labels.get(str(n['scope']), '通用')}）{n['content']}" for n in picked]
    db.touch_notes([int(n["id"]) for n in picked])
    return "以下是你已经记住的信息，自然使用即可：不要逐条复述，也不要编造没记过的事。\n" + "\n".join(lines)


def _parse(raw: str) -> list[dict[str, str]]:
    text = (raw or "").strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    result: list[dict[str, str]] = []
    for item in data[:4]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        scope = str(item.get("scope") or "user").strip()
        if scope not in ("user", "chat", "global"):
            scope = "user"
        if not content or len(content) > 80:
            continue
        result.append({"scope": scope, "content": content})
    return result


def _trim(agent_id: int, chat_id: str, author: str) -> None:
    """每个作用域只留最近的若干条，避免记忆无限膨胀。"""
    for scope_name, key, keep in (
        ("global", "", MAX_NOTES_PER_SCOPE),
        ("chat", chat_id, MAX_NOTES_PER_SCOPE),
        ("user", str(author or "")[:120], MAX_NOTES_PER_SCOPE),
    ):
        rows = db.list_notes(agent_id, scope_name, key, limit=keep + 20)
        for row in rows[keep:]:
            db.delete_note(int(row["id"]))
    rows = db.list_notes(agent_id, limit=MAX_NOTES_PER_AGENT + 50)
    for row in rows[MAX_NOTES_PER_AGENT:]:
        db.delete_note(int(row["id"]))


def learn(
    agent_id: int,
    chat_id: str,
    chat_title: str,
    author: str,
    user_text: str,
    answer: str,
    agent: dict[str, Any] | None = None,
) -> list[str]:
    """让模型从这一轮对话里提炼记忆并入库，返回新加的条目。"""
    if db.get_setting("memory_learn", "1") != "1":
        return []
    if len((user_text or "").strip()) < 6:
        return []
    prompt = (
        f"会话：{chat_title or chat_id}\n"
        f"对方：{author or '未知'}\n"
        f"对方说：{user_text[:800]}\n"
        f"你回答：{answer[:800]}"
    )
    # 延迟导入：bot 依赖 memory，这里再反向依赖一次会成环
    from . import bot as bot_mod

    try:
        raw = bot_mod.call_ai(
            [
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            agent=agent,
        )
    except Exception as exc:  # 记忆提炼失败不能影响正常回复
        log.info("记忆提炼失败：%s", exc)
        return []

    added: list[str] = []
    existing = db.note_pool(agent_id, chat_id, author)
    for item in _parse(raw):
        scope = item["scope"]
        content = item["content"]
        key = "" if scope == "global" else (chat_id if scope == "chat" else str(author or "")[:120])
        if any(_similar(content, str(n.get("content") or "")) for n in existing):
            continue
        db.add_note(agent_id, scope, key, content, source="auto")
        existing.append({"content": content, "scope": scope, "id": 10**9})
        added.append(content)
    if added:
        _trim(agent_id, chat_id, author)
        log.info("记住 %d 条：%s", len(added), "；".join(added)[:200])
    return added


def learn_async(
    agent_id: int,
    chat_id: str,
    chat_title: str,
    author: str,
    user_text: str,
    answer: str,
    agent: dict[str, Any] | None = None,
) -> None:
    """节流后丢到后台线程，别拖慢回复。"""
    with _learn_lock:
        seen = _learn_seen.get(chat_id, 0) + 1
        _learn_seen[chat_id] = seen
        last = _learn_at.get(chat_id, 0.0)
        if seen % 3 != 0 or time.time() - last < 90:
            return
        _learn_at[chat_id] = time.time()

    def worker() -> None:
        try:
            learn(agent_id, chat_id, chat_title, author, user_text, answer, agent=agent)
        except Exception as exc:
            log.info("记忆线程异常：%s", exc)

    threading.Thread(target=worker, name=f"mem-{chat_id}", daemon=True).start()
