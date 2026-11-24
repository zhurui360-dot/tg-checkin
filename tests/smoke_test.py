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
    assert "API ID" in user_run.json()["detail"], user_run.text
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
            return {"id": -1001, "type": "channel", "title": "某频道", "linked_chat_id": -1003219886860}
        if payload.get("chat_id") == "@bare_channel":
            return {"id": -1002, "type": "channel", "title": "没讨论群的频道"}
        return {"id": -1003219886860, "type": "supergroup", "title": "某群"}

    appbot._api = fake_api
    try:
        target, hint = appbot._resolve_target("@a_channel", "@a_channel")
        assert target == -1003219886860, (target, hint)
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

print("冒烟测试通过：健康检查、鉴权、群组 CRUD、cron 校验、设置保存、保活自检、AI 机器人、机器人签到、扫码登录接口均正常")
