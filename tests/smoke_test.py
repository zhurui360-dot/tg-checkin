"""不依赖 Telegram 网络的面板冒烟测试：python tests/smoke_test.py"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="tgck-test-")
os.environ.setdefault("PANEL_PASSWORD", "admin123")

try:
    from fastapi.testclient import TestClient  # noqa: E402
except Exception as exc:
    print(f"跳过冒烟测试：TestClient 不可用（{exc}）。安装 httpx 或 httpx2 后可运行。")
    raise SystemExit(0)

from app.main import app  # noqa: E402

with TestClient(app) as client:
    assert client.get("/health").status_code == 200
    assert client.get("/api/bootstrap").status_code == 401

    login = client.post("/api/panel/login", json={"password": "admin123"})
    assert login.status_code == 200, login.text

    boot = client.get("/api/bootstrap")
    assert boot.status_code == 200, boot.text
    assert boot.json()["stats"]["groups"] == 0

    created = client.post("/api/groups", json={
        "name": "测试群",
        "chat": "@test_group",
        "command": "/checkin",
        "schedule": ["0 8 * * *", "0 20 * * *"],
        "enabled": True,
    })
    assert created.status_code == 200, created.text
    group_id = created.json()["id"]

    groups = client.get("/api/groups").json()["groups"]
    assert len(groups) == 1 and groups[0]["chat"] == "@test_group"

    bad = client.post("/api/cron/validate", json={"expression": "not a cron"})
    assert bad.status_code == 400

    assert client.put("/api/settings", json={"dry_run": "1"}).status_code == 200
    assert client.delete(f"/api/groups/{group_id}").status_code == 200
    assert client.get("/api/bootstrap").json()["stats"]["groups"] == 0

    # 保活：默认关闭时应拒绝立即自检；开启后能探测到公网地址并通过校验
    ka = client.get("/api/keepalive").json()
    assert ka["enabled"] in (True, False) and "interval" in ka
    assert client.post("/api/keepalive/ping").status_code == 400

    assert client.put("/api/settings", json={
        "keepalive_enabled": "1",
        "keepalive_interval": "600",
        "keepalive_url": "http://127.0.0.1:9/health",
    }).status_code == 200
    after = client.get("/api/keepalive").json()
    assert after["enabled"] is True and after["interval"] == 600, after
    assert after["url"] == "http://127.0.0.1:9/health", after

    # 地址不可达时应记录失败而不是抛 500
    ping = client.post("/api/keepalive/ping")
    assert ping.status_code == 200, ping.text
    assert ping.json()["last_ok"] is False
    assert ping.json()["fail_streak"] >= 1

    # 非法间隔要落到合法区间（低于 60 秒被抬到 60）
    assert client.put("/api/settings", json={"keepalive_interval": "10"}).status_code == 200
    assert client.get("/api/keepalive").json()["interval"] == 60

    # ---------- AI 机器人（不发真实网络请求，只验证配置与状态流转） ----------
    bst = client.get("/api/bot/status").json()
    assert "enabled" in bst and "running" in bst and "token_set" in bst, bst
    assert bst["token_set"] is False

    # 配置回填接口不应泄露 API Key，只给是否已配置
    cfg = client.get("/api/bot/config").json()
    assert cfg["ai_api_key_set"] is False and "ai_api_key" not in cfg

    # 保存 AI 设置：数值校验与触发方式枚举
    assert client.put("/api/settings", json={
        "ai_base_url": "https://api.example.com/v1",
        "ai_model": "test-model",
        "ai_api_key": "sk-test",
        "ai_history_len": "8",
        "ai_temperature": "0.5",
        "ai_min_interval": "2",
        "bot_trigger": "mention",
    }).status_code == 200
    cfg2 = client.get("/api/bot/config").json()
    assert cfg2["ai_base_url"] == "https://api.example.com/v1"
    assert cfg2["ai_model"] == "test-model"
    assert cfg2["ai_api_key_set"] is True

    # 非法的触发方式与负数应被拒绝
    assert client.put("/api/settings", json={"bot_trigger": "bogus"}).status_code == 400
    assert client.put("/api/settings", json={"ai_history_len": "-1"}).status_code == 400

    # 密钥留空表示不修改，不能被空值覆盖
    assert client.put("/api/settings", json={"ai_api_key": "   "}).status_code == 200
    assert client.get("/api/bot/config").json()["ai_api_key_set"] is True

    # 出网代理可保存、可回显（用于境外主机连只对大陆开放的接口）
    assert client.put("/api/settings", json={
        "ai_proxy": "http://127.0.0.1:1080",
    }).status_code == 200
    assert client.get("/api/bot/config").json()["ai_proxy"] == "http://127.0.0.1:1080"
    # 清空代理回到直连
    assert client.put("/api/settings", json={"ai_proxy": ""}).status_code == 200
    assert client.get("/api/bot/config").json()["ai_proxy"] == ""

    # ---------- AI 智能体：多模型并存 + 各自命名 ----------
    agents = client.get("/api/ai/agents").json()["agents"]
    # 老的单份 AI 配置会被自动迁移成第一个智能体
    assert agents and agents[0]["name"] and agents[0]["is_default"], agents
    assert all("api_key" not in a for a in agents), "列表不能返回 API Key"

    made = client.post("/api/ai/agents", json={
        "name": "闲聊小助手",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": "sk-second",
        "model": "moonshot-v1-8k",
        "system_prompt": "用轻松的语气聊天",
        "temperature": "0.9",
        "max_tokens": "600",
        "history_len": "20",
        "min_interval": "5",
    })
    assert made.status_code == 200, made.text
    second_id = made.json()["id"]

    listed = client.get("/api/ai/agents").json()
    assert len(listed["agents"]) == 2
    second = [a for a in listed["agents"] if a["id"] == second_id][0]
    assert second["name"] == "闲聊小助手" and second["model"] == "moonshot-v1-8k"
    assert second["max_tokens"] == 600 and second["history_len"] == 20

    # 必填与格式校验：名字、地址协议、模型名、Key 都不能糊弄
    assert client.post("/api/ai/agents", json={"base_url": "https://a/v1", "model": "m", "api_key": "k"}).status_code == 400
    assert client.post("/api/ai/agents", json={"name": "x", "base_url": "ftp://a", "model": "m", "api_key": "k"}).status_code == 400
    assert client.post("/api/ai/agents", json={"name": "x", "base_url": "https://a/v1", "api_key": "k"}).status_code == 400
    assert client.post("/api/ai/agents", json={"name": "x", "base_url": "https://a/v1", "model": "m"}).status_code == 400

    # 改名字、改模型；Key 留空表示不修改
    assert client.put(f"/api/ai/agents/{second_id}", json={
        "name": "客服小助手",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k",
        "api_key": "",
        "temperature": "0.3",
    }).status_code == 200
    renamed = [a for a in client.get("/api/ai/agents").json()["agents"] if a["id"] == second_id][0]
    assert renamed["name"] == "客服小助手" and renamed["model"] == "moonshot-v1-32k"
    assert renamed["key_set"] is True, "留空的 Key 不能被清掉"

    # 数值越界自动夹到合法区间
    client.put(f"/api/ai/agents/{second_id}", json={
        "name": "客服小助手", "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k", "temperature": "5", "max_tokens": "99999",
    })
    clamped = [a for a in client.get("/api/ai/agents").json()["agents"] if a["id"] == second_id][0]
    assert float(clamped["temperature"]) <= 2.0 and int(clamped["max_tokens"]) <= 4096

    # 切换默认智能体，bootstrap 要跟着变
    assert client.post(f"/api/ai/agents/{second_id}/default").status_code == 200
    assert str(client.get("/api/bootstrap").json()["ai_agent_id"]) == str(second_id)
    assert client.post("/api/ai/agents/99999/default").status_code == 404

    # 机器人可以单独指定智能体，传不存在的 ID 要拒绝
    assert client.put("/api/settings", json={"ai_agent_id": "99999"}).status_code == 400

    # 删除默认智能体后，默认要让给剩下的，不能留个悬空 ID
    assert client.delete(f"/api/ai/agents/{second_id}").status_code == 200
    remain = client.get("/api/ai/agents").json()
    assert len(remain["agents"]) == 1 and remain["agents"][0]["is_default"]

    # 无 token 时校验应给出明确 400，而不是 500
    bad_verify = client.post("/api/bot/verify", json={"token": ""})
    assert bad_verify.status_code == 400, bad_verify.text

    # 对话记录接口可用，且能写入/清空
    msgs = client.get("/api/bot/messages").json()
    assert msgs["messages"] == [] and "stats" in msgs
    from app import db as appdb  # noqa: E402
    appdb.add_ai_message("1", "测试会话", "user", "我", "你好")
    appdb.add_ai_message("1", "测试会话", "assistant", "bot", "你好呀")
    hist = appdb.ai_history("1", 10)
    assert [m["role"] for m in hist] == ["user", "assistant"], hist
    assert client.get("/api/bot/messages").json()["stats"]["total"] == 2
    assert client.delete("/api/bot/messages").status_code == 200
    assert client.get("/api/bot/messages").json()["stats"]["total"] == 0

    # 开启机器人后状态应反映 enabled，但无 token 时不进入轮询
    assert client.put("/api/settings", json={"bot_enabled": "1"}).status_code == 200
    bst2 = client.get("/api/bot/status").json()
    assert bst2["enabled"] is True
    assert bst2["running"] is False, bst2

    # Business（秘书模式）：状态字段存在，且能识别账号主人自己发的消息。
    # 这条防护很关键——不加过滤会让 AI 对着自己的回复反复回，死循环烧额度。
    assert isinstance(bst2.get("business"), list), bst2
    assert bst2.get("business_replies") == 0, bst2
    from app import bot as appbot  # noqa: E402
    from app import telegram as apptg  # noqa: E402
    appdb.set_setting(
        "business_connections",
        '{"c1": {"owner_id": 999, "owner_name": "Owner", "is_enabled": true}}',
    )
    assert appbot.is_own_business_message(
        {"business_connection_id": "c1", "from": {"id": 999}}
    ) is True
    assert appbot.is_own_business_message(
        {"business_connection_id": "c1", "from": {"id": 111}}
    ) is False
    # 普通消息没有 business_connection_id，永远不算「自己的」
    assert appbot.is_own_business_message({"from": {"id": 999}}) is False
    assert client.get("/api/bot/status").json()["business"][0]["owner_name"] == "Owner"
    appdb.set_setting("business_connections", "")

    # ---------- 群签到：默认走机器人（Bot API），不需要 api_id ----------
    from app import bot as appbot  # noqa: E402
    from app import telegram as apptg  # noqa: E402
    assert appdb.get_setting("checkin_mode") == "bot"
    assert appbot.normalize_chat("-1001234567890") == -1001234567890
    assert appbot.normalize_chat("https://t.me/mygroup") == "@mygroup"
    assert appbot.normalize_chat("@mygroup") == "@mygroup"
    assert appbot.normalize_chat("https://t.me/c/1234567890/45") == -1001234567890
    assert client.get("/api/bot/chats").json()["chats"] == []
    try:
        appbot.normalize_chat("https://t.me/+AbCdEf")
        raise AssertionError("邀请链接应当被拒绝")
    except appbot.BotError:
        pass

    # 只接受 bot / user，别的一律 400
    assert client.put("/api/settings", json={"checkin_mode": "bogus"}).status_code == 400
    assert client.put("/api/settings", json={"checkin_mode": "bot"}).status_code == 200

    # 没有 token 时点「立即签到」应写一条失败日志，而不是把面板打 500
    assert client.put("/api/settings", json={
        "retry_count": "0", "retry_delay": "0", "jitter": "0", "dry_run": "0",
    }).status_code == 200
    g2 = client.post("/api/groups", json={
        "name": "机器人签到群",
        "chat": "@bot_checkin_group",
        "command": "/checkin",
        "schedule": ["0 8 * * *"],
        "enabled": True,
    }).json()["id"]
    test_run = client.post(f"/api/groups/{g2}/test")
    assert test_run.status_code == 500, test_run.text
    assert "Bot Token" in test_run.json()["detail"], test_run.text
    assert client.delete(f"/api/groups/{g2}").status_code == 200

    # 切到账号模式时没有登录会话，同样只写失败日志
    assert client.put("/api/settings", json={"checkin_mode": "user"}).status_code == 200
    g3 = client.post("/api/groups", json={
        "name": "账号签到群",
        "chat": "@user_checkin_group",
        "command": "/checkin",
        "schedule": ["0 8 * * *"],
        "enabled": True,
    }).json()["id"]
    user_run = client.post(f"/api/groups/{g3}/test")
    assert user_run.status_code == 500, user_run.text
    assert "账号" in user_run.json()["detail"], user_run.text
    assert client.delete(f"/api/groups/{g3}").status_code == 200
    assert client.put("/api/settings", json={"checkin_mode": "bot"}).status_code == 200

    # ---------- 扫码登录：接口形状与状态机 ----------
    assert client.get("/api/tg/qr/status").json()["status"] == "idle"
    # 没有 api_id 时不能生成
    assert client.post("/api/tg/qr/start", json={"api_id": "", "api_hash": ""}).status_code == 400
    # api_id 非数字同样拒绝
    assert client.post("/api/tg/qr/start", json={"api_id": "abc", "api_hash": "x" * 32}).status_code == 400
    # 没生成码时刷新/提交密码都要明确报错，而不是 500
    assert client.post("/api/tg/qr/refresh").status_code == 400
    assert client.post("/api/tg/qr/password", json={"password": "x"}).status_code == 400
    assert client.post("/api/tg/qr/cancel").status_code == 200
    # 二维码渲染（不联网，纯本地编码）
    assert len(apptg._qr_svg("tg://login?token=ZA")) > 200

    # ---------- 频道链接：自动改发关联讨论群 ----------
    from app import bot as appbot

    calls: list[dict] = []
    original_api = appbot._api

    def fake_api(method, token, payload):
        calls.append(payload)
        if payload.get("chat_id") == "@a_channel":
            return {"id": -1001, "type": "channel", "title": "某频道", "linked_chat_id": -1001234567890}
        if payload.get("chat_id") == "@bare_channel":
            return {"id": -1002, "type": "channel", "title": "没讨论群的频道"}
        return {"id": -1001234567890, "type": "supergroup", "title": "某群"}

    appbot._api = fake_api
    try:
        target, hint = appbot._resolve_target("@a_channel", "@a_channel")
        assert target == -1001234567890, (target, hint)
        assert "频道" in hint
        target, hint = appbot._resolve_target("@a_group", "@a_group")
        assert target == "@a_group" and hint == ""
        try:
            appbot._resolve_target("@bare_channel", "@bare_channel")
            raise AssertionError("频道没有讨论群时应报错")
        except appbot.BotError as exc:
            assert "频道" in str(exc), str(exc)
    finally:
        appbot._api = original_api

    # 权限类报错要翻成能照着做的中文
    assert "管理员" in appbot._group_error_hint("Bad Request: chat admin privileges are required", "@g")
    assert apptg.is_permission_error(RuntimeError("普通错误")) is False

    # ---------- 多账号 / 多机器人 / 补签 ----------
    # 账号列表初始为空，凭据接口不炸
    assert client.get("/api/accounts").json()["count"] == 0
    assert client.get("/api/bots").json()["count"] == 0
    cred = client.get("/api/tg/credentials").json()
    assert set(cred) == {"api_id", "api_hash", "phone", "proxy_url"}, cred

    # 新建账号：没有手机号/备注要拒绝
    assert client.post("/api/accounts", json={"name": ""}).status_code == 400
    aid = client.post("/api/accounts", json={
        "name": "副账号", "phone": "+8613800138000", "api_id": "123456", "api_hash": "x" * 32,
    }).json()["id"]
    accounts = client.get("/api/accounts").json()
    assert accounts["count"] == 1 and accounts["accounts"][0]["name"] == "副账号"
    # 手机号要打码，不能原样回显
    assert "***" in accounts["accounts"][0]["phone"], accounts["accounts"][0]
    # 还没登录 -> authorized 为 False
    assert accounts["accounts"][0]["authorized"] is False

    assert client.put(f"/api/accounts/{aid}", json={"name": "改过的名字"}).status_code == 200
    assert client.get("/api/accounts").json()["accounts"][0]["name"] == "改过的名字"
    assert client.put("/api/accounts/9999", json={"name": "x"}).status_code == 404
    # 没登录时校验应给出可操作的 400
    verify_acc = client.post(f"/api/accounts/{aid}/verify")
    assert verify_acc.status_code == 400 and "登录" in verify_acc.json()["detail"]

    # 群组可以绑定这个账号；不存在的账号要 400
    assert client.post("/api/groups", json={
        "name": "绑定群", "chat": "@bind_group", "schedule": ["0 8 * * *"],
        "account_id": 9999,
    }).status_code == 400
    gb = client.post("/api/groups", json={
        "name": "绑定群", "chat": "@bind_group", "schedule": ["0 8 * * *"],
        "account_id": aid, "mode": "user",
    })
    assert gb.status_code == 200, gb.text
    gb_id = gb.json()["id"]
    bound = [g for g in client.get("/api/groups").json()["groups"] if g["id"] == gb_id][0]
    assert bound["account_id"] == aid and bound["mode"] == "user", bound
    # 群组方式只允许 默认 / bot / user
    assert client.put(f"/api/groups/{gb_id}", json={
        "name": "绑定群", "chat": "@bind_group", "schedule": ["0 8 * * *"], "mode": "bogus",
    }).status_code == 400
    assert client.delete(f"/api/groups/{gb_id}").status_code == 200

    # 机器人：token 必填，无网络时应给出中文错误而不是 500
    assert client.post("/api/bots", json={"token": ""}).status_code == 400
    assert client.put("/api/bots/9999", json={"name": "x"}).status_code == 404
    assert client.delete("/api/bots/9999").status_code == 404
    assert client.post("/api/bots/9999/verify").status_code == 404
    assert client.post("/api/bots/9999/restart").status_code == 404
    # 不指定 bot_id 时，状态里仍要有兼容字段，不让老前端崩
    legacy = client.get("/api/bot/status").json()
    for key in ("enabled", "running", "token_set", "bots", "replies"):
        assert key in legacy, legacy

    # 删除账号后，引用它的群组要被置空，而不是留一个悬空 id
    db_id = client.post("/api/accounts", json={"name": "临时账号"}).json()["id"]
    client.post("/api/groups", json={
        "name": "临时群", "chat": "@tmp_group", "schedule": ["0 8 * * *"], "account_id": db_id,
    })
    assert client.delete(f"/api/accounts/{db_id}").status_code == 200
    for group in client.get("/api/groups").json()["groups"]:
        assert group.get("account_id") is None, group

    # 补签：条件与开关
    catch = client.get("/api/catchup").json()
    assert catch["enabled"] is True and "running" in catch, catch
    # 今天还没到点（08:00 可能未过），没有群组命中也不能报错
    assert client.post("/api/catchup/run").status_code == 200
    assert client.put("/api/settings", json={"catchup_interval": "10"}).status_code == 200
    assert client.get("/api/catchup").json()["interval"] == "120"  # 低于 120 被抬到 120
    assert client.put("/api/settings", json={"catchup_enabled": "0"}).status_code == 200
    assert client.get("/api/catchup").json()["enabled"] is False
    assert client.put("/api/settings", json={"catchup_enabled": "bogus"}).status_code == 400
    assert client.put("/api/settings", json={"catchup_enabled": "1"}).status_code == 200

    # 补签判断：今天有计划时间且日志无成功记录时才补
    from datetime import datetime as _dt
    from app import scheduler as appsched
    tz = appsched._timezone()
    now = _dt.now(tz).replace(hour=23, minute=59, second=0, microsecond=0)
    fake_group = {"schedule": '["0 8 * * *", "0 20 * * *"]'}
    passed = appsched._scheduled_times_passed_today(fake_group, now)
    assert len(passed) == 2, passed
    early = _dt.now(tz).replace(hour=7, minute=0, second=0, microsecond=0)
    assert appsched._scheduled_times_passed_today(fake_group, early) == []
    assert appsched._scheduled_times_passed_today({"schedule": "bad"}, now) == []

    # 导出配置要带上多身份，否则容器重建后账号/机器人会丢
    exported = client.get("/api/config/export").json()
    assert "accounts" in exported and "bots" in exported, exported.keys()
    assert exported["settings"].get("catchup_enabled") is not None

    # ---------- 备份 / 恢复 ----------
    bstat = client.get("/api/backup/status").json()
    assert "auto" in bstat and "webdav" in bstat and "github" in bstat, bstat
    assert bstat["webdav"]["configured"] is False
    assert bstat["github"]["configured"] is False

    # 备份包要含账号/机器人/群组，且会话字符串不能丢
    acc_b = client.post("/api/accounts", json={
        "name": "备份账号", "phone": "+8613700137000", "api_id": "999888", "api_hash": "abc",
    }).json()["id"]
    assert client.post("/api/groups", json={
        "name": "备份群", "chat": "@backup_group", "schedule": ["0 8 * * *"],
        "mode": "user", "account_id": acc_b,
    }).status_code == 200

    bundle_resp = client.get("/api/backup/export")
    assert bundle_resp.status_code == 200, bundle_resp.text
    assert "attachment" in bundle_resp.headers.get("content-disposition", "")
    bundle = bundle_resp.json()
    assert bundle["app"] == "tg-checkin" and bundle["format"] >= 2
    assert any(a["id"] == acc_b for a in bundle["accounts"]), bundle["accounts"]

    # 恢复：替换模式，账号/机器人 ID 会重排，群组引用要跟着改
    client.delete(f"/api/accounts/{acc_b}")
    assert client.delete(f"/api/groups/{[g['id'] for g in client.get('/api/groups').json()['groups'] if g['chat'] == '@backup_group'][0]}").status_code == 200

    restored = client.post("/api/backup/restore", json={"data": bundle, "mode": "replace"})
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["accounts"] >= 1 and body["groups"] >= 1, body

    groups_now = client.get("/api/groups").json()["groups"]
    backup_group = [g for g in groups_now if g["chat"] == "@backup_group"][0]
    accounts_now = client.get("/api/accounts").json()["accounts"]
    assert backup_group["account_id"] in [a["id"] for a in accounts_now], backup_group

    # 智能体也要跟着回来，且默认值指向的必须是存在的记录
    assert "ai_agents" in bundle and bundle["ai_agents"], bundle.keys()
    agents_now = client.get("/api/ai/agents").json()
    assert agents_now["agents"], "恢复后智能体不能丢"
    assert agents_now["default_id"] in [a["id"] for a in agents_now["agents"]], agents_now

    # 合并模式不能产生重复群组
    again = client.post("/api/backup/restore", json={"data": bundle, "mode": "merge"}).json()
    assert again["groups"] == 0, again
    assert len([g for g in client.get("/api/groups").json()["groups"] if g["chat"] == "@backup_group"]) == 1

    # 上传的备份格式不对时给 400 而不是 500
    assert client.post("/api/backup/restore", json={"data": {"app": "other"}}).status_code == 400
    assert client.post("/api/backup/restore", json={"data": "not a dict"}).status_code == 400
    assert client.post("/api/backup/restore", json={"data": bundle, "mode": "oops"}).status_code == 400

    # 本地备份文件：创建 / 列出 / 删除 / 非法文件名
    made = client.post("/api/backup/local")
    assert made.status_code == 200, made.text
    local_name = made.json()["name"]
    assert local_name.startswith("tgck-")
    files = client.get("/api/backup/local").json()["files"]
    assert any(f["name"] == local_name for f in files), files
    assert client.post(f"/api/backup/local/{local_name}/restore").status_code == 200
    assert client.delete(f"/api/backup/local/{local_name}").status_code == 200
    assert client.delete(f"/api/backup/local/{local_name}").status_code == 404
    assert client.post("/api/backup/local/../etc/passwd/restore").status_code in (400, 404)

    # 自动化开关与数值夹取
    assert client.put("/api/settings", json={"backup_interval": "0"}).status_code == 200
    assert client.get("/api/backup/status").json()["auto"]["interval"] == "1"
    assert client.put("/api/settings", json={"backup_keep": "9999"}).status_code == 200
    assert client.get("/api/backup/status").json()["auto"]["keep"] == "200"
    assert client.put("/api/settings", json={"backup_auto": "bogus"}).status_code == 400
    assert client.put("/api/settings", json={"backup_auto": "1"}).status_code == 200
    assert client.get("/api/backup/status").json()["auto"]["enabled"] is True
    # 没配 WebDAV / GitHub 时，自动备份只落本地，不报错
    ran = client.post("/api/backup/run").json()
    assert ran["ok"] is True and ran["local"], ran
    assert ran["webdav"] == "" and ran["github"] == ""
    assert client.put("/api/settings", json={"backup_auto": "0"}).status_code == 200

    # 没配云端时调测试接口要 400（中文提示），不能 500
    assert client.post("/api/backup/webdav/test").status_code == 400
    assert client.post("/api/backup/github/test").status_code == 400
    assert client.post("/api/backup/webdav/restore", json={"name": "tgck-20260101-000000.json"}).status_code == 400

    # 密钥类字段留空表示不修改
    client.put("/api/settings", json={"webdav_password": "secret123"})
    client.put("/api/settings", json={"webdav_password": ""})
    from app import db as _db
    assert _db.get_setting("webdav_password") == "secret123"
    # 面板回填要打码
    webdav_masked = client.get("/api/bootstrap").json()["settings"]["webdav_password"]
    assert "secret123" not in webdav_masked, webdav_masked

print("冒烟测试通过：健康检查、鉴权、群组 CRUD、cron 校验、设置保存、保活自检、AI 机器人、多智能体、机器人签到、扫码登录、多账号、多机器人、补签、备份恢复均正常")
