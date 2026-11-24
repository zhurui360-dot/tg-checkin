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

print("冒烟测试通过：健康检查、鉴权、群组 CRUD、cron 校验、设置保存、保活自检、AI 机器人配置均正常")
