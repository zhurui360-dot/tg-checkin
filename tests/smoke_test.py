"""不依赖 Telegram 网络的面板冒烟测试：python tests/smoke_test.py"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="tgck-test-")
os.environ.setdefault("PANEL_PASSWORD", "admin123")
# 测试里会连本地假 WebDAV，别让系统代理插一脚
os.environ["no_proxy"] = "localhost,127.0.0.1"
os.environ["NO_PROXY"] = "localhost,127.0.0.1"

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
    assert boot.json()["standby"] is False  # 没设 STANDBY 时是正常模式

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

    # 保活自检功能已移除：接口不再存在，设置里也不再回显这三个键
    assert client.get("/api/keepalive").status_code == 404
    assert client.post("/api/keepalive/ping").status_code == 404
    settings_now = client.get("/api/bootstrap").json()["settings"]
    assert not any(k.startswith("keepalive") for k in settings_now), settings_now
    # 老配置里残留的键即使被提交也只当未知字段忽略，不会写回去
    assert client.put("/api/settings", json={"keepalive_enabled": "1"}).status_code == 200
    assert "keepalive_enabled" not in client.get("/api/bootstrap").json()["settings"]

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

    # 面板留空的数值项算「用默认值」，不能报「必须是数字」
    plain = client.post("/api/ai/agents", json={
        "name": "全默认参数", "base_url": "https://api.example.com/v1",
        "model": "m-1", "api_key": "sk-plain",
        "temperature": "", "max_tokens": "", "history_len": "", "min_interval": "",
    })
    assert plain.status_code == 200, plain.text
    plain_id = plain.json()["id"]
    plain_row = [a for a in client.get("/api/ai/agents").json()["agents"] if a["id"] == plain_id][0]
    assert plain_row["model"] == "m-1" and int(plain_row["max_tokens"]) == 800, plain_row
    assert client.delete(f"/api/ai/agents/{plain_id}").status_code == 200

    # ---------- 读取模型：填了地址 + Key 就自动列模型 ----------
    import threading
    from http.server import BaseHTTPRequestHandler as _BaseHandler, ThreadingHTTPServer as _ModelServer

    class ModelsHandler(_BaseHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def _reply(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.rstrip("/")
            if not path.endswith("/models"):
                self._reply(404, b'{"error":"not found"}')
                return
            if self.headers.get("Authorization") != "Bearer sk-models":
                self._reply(401, b'{"error":{"message":"invalid api key"}}')
                return
            if path == "/v1/models":
                self._reply(200, b'{"object":"list","data":[{"id":"model-b"},{"id":"model-a"},{"id":"model-a"}]}')
            elif path == "/arr/v1/models":
                # 有些中转直接给一个数组
                self._reply(200, b'["arr-1","arr-2"]')
            elif path == "/nested/v1/models":
                # 还有套两层的
                self._reply(200, b'{"data":{"data":[{"name":"nested-1"}]}}')
            elif path == "/limit/v1/models":
                self._reply(429, b'{"error":{"message":"exceeds rpm limit"}}')
            else:
                self._reply(404, b'{"error":"not found"}')

    model_server = _ModelServer(("127.0.0.1", 0), ModelsHandler)
    threading.Thread(target=model_server.serve_forever, daemon=True).start()
    root_url = f"http://127.0.0.1:{model_server.server_address[1]}"
    models_url = f"{root_url}/v1"
    try:
        probed = client.post("/api/ai/models", json={"base_url": models_url, "api_key": "sk-models"})
        assert probed.status_code == 200, probed.text
        payload = probed.json()
        assert payload["models"] == ["model-a", "model-b"], payload  # 去重且排序
        assert payload["count"] == 2 and payload["endpoint"].endswith("/v1/models"), payload
        # Key 不对要说人话，而不是甩 HTTP 401
        unauth = client.post("/api/ai/models", json={"base_url": models_url, "api_key": "bad"})
        assert unauth.status_code == 400 and "鉴权" in unauth.json()["detail"], unauth.text
        # 地址 / Key 缺一个都直接拒
        assert client.post("/api/ai/models", json={"api_key": "x"}).status_code == 400
        assert client.post("/api/ai/models", json={"base_url": models_url}).status_code == 400
        # 把整条 /chat/completions 粘进 base_url 也要能纠正过来
        pasted = client.post("/api/ai/models", json={
            "base_url": f"{models_url}/chat/completions", "api_key": "sk-models",
        })
        assert pasted.status_code == 200, pasted.text
        assert pasted.json()["endpoint"].endswith("/v1/models"), pasted.text
        # 数组 / 嵌套两种返回形状都要认得
        arr = client.post("/api/ai/models", json={"base_url": f"{root_url}/arr/v1", "api_key": "sk-models"})
        assert arr.status_code == 200 and arr.json()["models"] == ["arr-1", "arr-2"], arr.text
        nested = client.post("/api/ai/models", json={"base_url": f"{root_url}/nested/v1", "api_key": "sk-models"})
        assert nested.status_code == 200 and nested.json()["models"] == ["nested-1"], nested.text
        # 429 重试一次后仍限流，要给人话
        limited = client.post("/api/ai/models", json={"base_url": f"{root_url}/limit/v1", "api_key": "sk-models"})
        assert limited.status_code == 400 and "限流" in limited.json()["detail"], limited.text
        # 同一家接口建两个模型：一个 key 复用
        local = client.post("/api/ai/agents", json={
            "name": "本地接口 · model-a", "base_url": models_url,
            "api_key": "sk-models", "model": "model-a",
        })
        assert local.status_code == 200, local.text
        local_id = local.json()["id"]
        # 编辑时 Key 留空，用存下来的那把
        reused = client.post("/api/ai/models", json={"base_url": models_url, "agent_id": local_id})
        assert reused.status_code == 200 and reused.json()["count"] == 2, reused.text
        # 保存时也会把地址收拾干净
        stored = [a for a in client.get("/api/ai/agents").json()["agents"] if a["id"] == local_id][0]
        assert stored["base_url"] == models_url, stored
        assert client.delete(f"/api/ai/agents/{local_id}").status_code == 200
    finally:
        model_server.shutdown()

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

    # ---------- 群签到：一律用已登录的账号，机器人不再参与 ----------
    from app import bot as appbot  # noqa: E402
    from app import telegram as apptg  # noqa: E402
    assert appdb.get_setting("checkin_mode") == "user"
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
    # 老页面还传 bot 也不会生效，一律收敛成 user
    assert client.put("/api/settings", json={"checkin_mode": "bot"}).status_code == 200
    assert appdb.get_setting("checkin_mode") == "user"

    # 没有登录账号时点「立即签到」应写一条失败日志，而不是把面板打 500 崩掉
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
    assert "账号" in test_run.json()["detail"], test_run.text
    assert client.delete(f"/api/groups/{g2}").status_code == 200

    # 群组不再接受机器人签到选项，但老数据传 bot 会被纠正成 user
    g3 = client.post("/api/groups", json={
        "name": "账号签到群",
        "chat": "@user_checkin_group",
        "command": "/checkin",
        "schedule": ["0 8 * * *"],
        "enabled": True,
        "mode": "bot",
    }).json()["id"]
    assert [g for g in client.get("/api/groups").json()["groups"] if g["id"] == g3][0]["mode"] == "user"
    user_run = client.post(f"/api/groups/{g3}/test")
    assert user_run.status_code == 500, user_run.text
    assert "账号" in user_run.json()["detail"], user_run.text
    assert client.delete(f"/api/groups/{g3}").status_code == 200

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
    assert client.post("/api/bots/9999/toggle").status_code == 404

    # 启用 / 停用开关：只翻 enabled，token、名字、绑定关系都不动
    from app import db as _bdb
    sw_id = _bdb.add_bot({"name": "开关测试", "token": "123:fake", "username": "sw", "enabled": True})
    off = client.post(f"/api/bots/{sw_id}/toggle")
    assert off.status_code == 200 and off.json()["enabled"] is False, off.text
    assert off.json()["running"] is False, off.text
    kept = [b for b in client.get("/api/bots").json()["bots"] if b["id"] == sw_id][0]
    assert kept["name"] == "开关测试" and kept["token_set"] is True, kept
    # 显式指定目标值，且重复设置是幂等的
    again = client.post(f"/api/bots/{sw_id}/toggle", json={"enabled": False})
    assert again.status_code == 200 and again.json()["enabled"] is False
    on = client.post(f"/api/bots/{sw_id}/toggle", json={"enabled": True})
    assert on.status_code == 200 and on.json()["enabled"] is True, on.text
    assert client.post(f"/api/bots/{sw_id}/toggle").json()["enabled"] is False
    assert client.delete(f"/api/bots/{sw_id}").status_code == 200
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

    # ---------- WebDAV：设了文件夹就自动建出来 ----------
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    dav = {"dirs": {"/dav/"}, "files": {}, "mkcol": []}

    class DavHandler(BaseHTTPRequestHandler):
        """够用就好的假 WebDAV：PROPFIND / MKCOL / PUT / GET / DELETE。"""

        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def _path(self):
            return self.path.split("?", 1)[0]

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _send(self, code, body=b"", ctype="text/xml; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_PROPFIND(self):
            self._body()
            path = self._path()
            if not path.endswith("/"):
                path += "/"
            if path not in dav["dirs"]:
                return self._send(404, b"missing")
            children = b"".join(
                f"<d:response><d:href>{name}</d:href></d:response>".encode()
                for name in sorted(dav["files"])
                if name.rsplit("/", 1)[0] + "/" == path
            )
            body = (
                b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
                + f"<d:response><d:href>{path}</d:href></d:response>".encode()
                + children
                + b"</d:multistatus>"
            )
            self._send(207, body)

        def do_MKCOL(self):
            path = self._path()
            if not path.endswith("/"):
                path += "/"
            parent = path.rstrip("/").rsplit("/", 1)[0] + "/"
            if path in dav["dirs"]:
                return self._send(405, b"exists")
            if parent not in dav["dirs"]:
                return self._send(409, b"no parent")
            dav["dirs"].add(path)
            dav["mkcol"].append(path)
            self._send(201, b"created")

        def do_PUT(self):
            data = self._body()
            path = self._path()
            if path.rsplit("/", 1)[0] + "/" not in dav["dirs"]:
                return self._send(409, b"no parent")
            dav["files"][path] = data
            self._send(201, b"stored")

        def do_GET(self):
            path = self._path()
            if path not in dav["files"]:
                return self._send(404, b"missing")
            self._send(200, dav["files"][path], "application/json")

        def do_DELETE(self):
            dav["files"].pop(self._path(), None)
            self._send(204)

    dav_server = ThreadingHTTPServer(("127.0.0.1", 0), DavHandler)
    dav_port = dav_server.server_address[1]
    threading.Thread(target=dav_server.serve_forever, daemon=True).start()
    dav_url = f"http://127.0.0.1:{dav_port}/dav"
    try:
        saved = client.put("/api/settings", json={
            "webdav_url": dav_url,
            "webdav_dir": "backup/tgck/",
            "webdav_username": "davuser",
            "webdav_password": "davpass",
        })
        assert saved.status_code == 200, saved.text
        info = saved.json().get("webdav") or {}
        assert info.get("ok") is True, saved.json()
        # 保存设置这一步就把多级目录建好了
        assert "/dav/backup/" in dav["dirs"], dav["dirs"]
        assert "/dav/backup/tgck/" in dav["dirs"], dav["dirs"]
        assert _db.get_setting("webdav_dir") == "backup/tgck"  # 结尾斜杠被规整掉

        tested = client.post("/api/backup/webdav/test")
        assert tested.status_code == 200, tested.text
        assert tested.json()["dir"] == dav_url + "/backup/tgck/", tested.json()
        assert tested.json()["subdir"] == "backup/tgck"

        uploaded = client.post("/api/backup/webdav/upload")
        assert uploaded.status_code == 200, uploaded.text
        dav_name = uploaded.json()["name"]
        assert f"/dav/backup/tgck/{dav_name}" in dav["files"], dav["files"].keys()
        listed = client.get("/api/backup/webdav").json()["files"]
        assert any(item["name"] == dav_name for item in listed), listed

        # 非法文件夹名在保存阶段就要挡住，不能等上传才炸
        assert client.put("/api/settings", json={"webdav_dir": "../etc"}).status_code == 400
        assert client.put("/api/settings", json={"webdav_dir": "a$b"}).status_code == 400

        client.put("/api/settings", json={"webdav_url": "", "webdav_dir": "", "webdav_auto": "0"})
    finally:
        dav_server.shutdown()
        dav_server.server_close()

    # ---------- GitHub：仓库写法容错 + 404 说清原因 ----------
    from app import backup as _bk

    assert _bk._normalize_repo("https://github.com/alice/tg-checkin-data") == "alice/tg-checkin-data"
    assert _bk._normalize_repo("git@github.com:alice/tg-checkin-data.git") == "alice/tg-checkin-data"
    assert _bk._normalize_repo("github.com/alice/tg-checkin-data/tree/main") == "alice/tg-checkin-data"
    assert _bk._normalize_repo(" alice/tg-checkin-data/ ") == "alice/tg-checkin-data"
    try:
        _bk._normalize_repo("只有一个名字")
    except _bk.BackupError:
        pass
    else:
        raise AssertionError("仓库名写成一段时应该报错")

    real_http = _bk._http

    def fake_http(method, url, *args, **kwargs):
        """假装 token 属于 alice，能访问 alice/tg-checkin-data。"""
        if "/user/repos" in url:
            return 200, b'[{"full_name": "alice/tg-checkin-data"}]'
        if url.rstrip("/").endswith("/user"):
            return 200, b'{"login": "alice"}'
        raise _bk.BackupError("not found", code=404)

    _bk._http = fake_http
    try:
        client.put("/api/settings", json={
            "github_token": "github_pat_smoke", "github_repo": "alice/tg-checkin-dat",
        })
        wrong = client.post("/api/backup/github/test")
        assert wrong.status_code == 400, wrong.text
        detail = wrong.json()["detail"]
        assert "alice/tg-checkin-data" in detail, detail
        assert "Token" in detail, detail
    finally:
        _bk._http = real_http
        _db.set_setting("github_token", "")
        _db.set_setting("github_repo", "")

    # ---------------- 安全加固 ----------------
    from app import security as _sec
    from app import db as _sdb

    page = client.get("/")
    assert page.headers.get("x-frame-options") == "DENY"
    assert page.headers.get("x-content-type-options") == "nosniff"
    assert "frame-ancestors 'none'" in page.headers.get("content-security-policy", "")
    assert page.headers.get("cache-control") == "no-store"
    assert client.get("/health").headers.get("referrer-policy") == "no-referrer"
    # 首页把静态资源版本戳填进去了，资源改了要让浏览器重新取
    body = page.text
    assert "__ASSET_V__" not in body, "首页的版本占位符没被替换"
    assert "/static/app.js?v=" in body and "/static/style.css?v=" in body, body[:200]
    asset = client.get("/static/app.js")
    assert asset.status_code == 200
    assert "no-cache" in asset.headers.get("cache-control", ""), asset.headers

    # 会话 token 被改一个字符就不该再通过
    good = client.cookies.get("tgck_session")
    assert good and _sec.verify_session(good, "admin123")
    client.cookies.set("tgck_session", good[:-1] + ("0" if good[-1] != "0" else "1"))
    assert client.get("/api/bootstrap").status_code == 401
    client.cookies.set("tgck_session", good)
    assert client.get("/api/bootstrap").status_code == 200

    # 出网护栏：云元数据地址必须被拦住，普通地址放行
    for bad in ("http://169.254.169.254/latest/meta-data/", "http://100.100.100.200/",
                "http://metadata.google.internal/"):
        try:
            _sec.guard_outbound(bad)
        except _sec.SecurityError:
            pass
        else:
            raise AssertionError(f"应该拦住 {bad}")
    assert _sec.guard_outbound("https://api.deepseek.com/v1")
    assert _sec.guard_outbound("http://127.0.0.1:8080/dav")  # 本机 / 局域网服务照常可用

    # 连续输错密码会进入冷却，避免在线爆破（测完清掉计数）
    for _ in range(5):
        assert client.post("/api/panel/login", json={"password": "wrong"}).status_code == 401
    blocked = client.post("/api/panel/login", json={"password": "admin123"})
    assert blocked.status_code == 429, blocked.text
    assert blocked.headers.get("retry-after")
    _sec.login_throttle.reset("testclient")
    assert client.post("/api/panel/login", json={"password": "admin123"}).status_code == 200

    # 改密码后旧会话立即失效
    assert client.put("/api/settings", json={"panel_password": "n3w-Passw0rd"}).status_code == 200
    assert client.get("/api/bootstrap").status_code == 401
    client.cookies.clear()
    assert client.post("/api/panel/login", json={"password": "n3w-Passw0rd"}).status_code == 200
    assert client.get("/api/bootstrap").status_code == 200
    assert _sdb.get_setting("panel_password") == "n3w-Passw0rd"
    # 留回原始密码，后续断言不受影响
    assert client.put("/api/settings", json={"panel_password": "admin123"}).status_code == 200

    # ---------------- 智能体长期记忆 ----------------
    from app import memory as _mem

    # 上面的改密码用例把会话签名换掉了，这里重新登录一次
    client.cookies.clear()
    assert client.post("/api/panel/login", json={"password": "admin123"}).status_code == 200

    agent_id = client.get("/api/ai/agents").json()["default_id"]
    assert agent_id, "应当有默认智能体"

    assert client.post("/api/memory", json={"agent_id": agent_id, "content": "他叫小王，在杭州做后端"}).status_code == 200
    assert client.post("/api/memory", json={"agent_id": agent_id, "content": "  "}).status_code == 400
    assert client.post("/api/memory", json={"agent_id": agent_id, "content": "x", "scope": "user"}).status_code == 400
    assert client.post("/api/memory", json={"agent_id": agent_id, "content": "x", "scope": "bogus"}).status_code == 400

    # 手动 + 自动两种来源都能记；按人记的只在对应人身上出现
    client.post("/api/memory", json={"agent_id": agent_id, "content": "小王讨厌被叫全名",
                                     "scope": "user", "scope_key": "小王"})
    listed_notes = client.get(f"/api/memory?agent_id={agent_id}").json()
    assert listed_notes["stats"]["total"] == 2, listed_notes["stats"]
    assert listed_notes["stats"]["user"] == 1 and listed_notes["stats"]["global"] == 1

    block = _mem.context(agent_id, "-100123", "小王", "这个接口帮我看看设计得对不对")
    assert "杭州" in block, block
    assert "讨厌被叫全名" in block, block
    # 别人来聊，只该看到全局那条
    other = _mem.context(agent_id, "-100123", "李四", "在吗")
    assert "讨厌被叫全名" not in other and "杭州" in other

    # 去重：换一种说法不该重复入库
    _sdb.add_note(agent_id, "global", "", "重复测试：他住在杭州", "manual")
    assert _mem._similar("小王在杭州做后端", "小王 在杭州 做后端。")
    assert not _mem._similar("小王在杭州做后端", "他养了两只猫")

    note_id = [n for n in listed_notes["notes"] if n["scope"] == "global"][0]["id"]
    assert client.delete(f"/api/memory/{note_id}").status_code == 200
    assert client.delete(f"/api/memory/{note_id}").status_code == 404
    removed = client.delete(f"/api/memory?agent_id={agent_id}").json()
    assert removed["removed"] >= 1 and removed["stats"]["total"] == 0

    # 关掉注入后不再拼记忆块
    assert client.put("/api/settings", json={"memory_enabled": "0"}).status_code == 200
    _sdb.add_note(agent_id, "global", "", "开关测试：记得打招呼", "manual")
    assert _mem.context(agent_id, "-100123", "小王", "你好") == ""
    assert client.put("/api/settings", json={"memory_enabled": "1"}).status_code == 200
    assert "打招呼" in _mem.context(agent_id, "-100123", "小王", "你好")
    assert client.put("/api/settings", json={"memory_max_notes": "99"}).status_code == 200
    assert _sdb.get_setting("memory_max_notes") == "20"

    # ---------------- 用自己账号自动回复 ----------------
    st = client.get("/api/assistant").json()
    assert st["enabled"] is False and st["running"] is False
    assert client.post("/api/assistant/restart").json()["ok"] is False

    assert client.put("/api/settings", json={"assistant_trigger": "sometimes"}).status_code == 400
    assert client.put("/api/settings", json={"assistant_quiet": "晚上到早上"}).status_code == 400
    assert client.put("/api/settings", json={"assistant_quiet": "23:30-08:00"}).status_code == 200
    assert client.put("/api/settings", json={"assistant_cooldown": "999"}).status_code == 200
    assert _sdb.get_setting("assistant_cooldown") == "120"
    assert client.put("/api/settings", json={"assistant_max_per_hour": "0"}).status_code == 200
    assert _sdb.get_setting("assistant_max_per_hour") == "1"
    assert client.put("/api/settings", json={"assistant_agent_id": "9999"}).status_code == 400

    # 免打扰时段与名单匹配的逻辑（不启动线程，纯函数）
    from app import assistant as _as
    assert _as._quiet_now("") is False
    assert _as._quiet_now("00:00-23:59") is True
    assert _as._in_list({"alice", "-1001"}, "@alice, -1002") is True
    assert _as._in_list({"@alice"}, "@alice") is True
    assert _as._in_list({"@bob"}, "@alice") is False

    # 处理记录：每条消息都要留痕，否则「没回复」查不出原因
    assert _sdb.assistant_log_stats()["total"] == 0
    _sdb.add_assistant_log("-1001", "测试群", "alice", "在吗", "skipped", "不在白名单")
    _sdb.add_assistant_log("-1001", "测试群", "alice", "在吗", "replied", "已回复", "在的")
    _sdb.add_assistant_log("-1001", "测试群", "alice", "在吗", "error", "AI 调用失败：超时")
    stats = _sdb.assistant_log_stats()
    assert stats["total"] == 3 and stats["replied"] == 1 and stats["skipped"] == 1 and stats["error"] == 1, stats

    log_api = client.get("/api/assistant/log").json()
    assert len(log_api["items"]) == 3, log_api
    assert log_api["items"][0]["action"] == "error"  # 倒序，最新在前
    assert len(client.get("/api/assistant/log?action=skipped").json()["items"]) == 1
    assert client.get("/api/assistant/log?limit=2").json()["items"].__len__() == 2

    # 流水只保留最近 500 条，跑久了不会撑库
    for i in range(520):
        _sdb.add_assistant_log("-1001", "群", "bob", f"第{i}条", "skipped", "测试")
    assert _sdb.assistant_log_stats()["total"] == 500, _sdb.assistant_log_stats()

    # 面板里「清空记录」
    assert client.delete("/api/assistant/log").status_code == 200
    assert _sdb.assistant_log_stats()["total"] == 0

    # 自检接口：没有可用智能体时要给明确提示，而不是 500
    assert client.post("/api/assistant/selftest", json={}).status_code in (200, 400)

    # ---------------- 大模型接入：地址纠错与限流 ----------------
    from app import bot as _ab
    assert _ab.normalize_base("https://api.deepseek.com/v1/") == "https://api.deepseek.com/v1"
    assert _ab.normalize_base("https://api.deepseek.com/v1/chat/completions") == "https://api.deepseek.com/v1"
    assert _ab.normalize_base("https://api.deepseek.com/v1/models") == "https://api.deepseek.com/v1"
    assert _ab.normalize_base("  https://a.com/v1//  ") == "https://a.com/v1"
    # 各种 /models 返回形状
    assert _ab._model_names({"data": [{"id": "a"}, {"id": "a"}, {"name": "b"}]}) == ["a", "b"]
    assert _ab._model_names(["x", "y"]) == ["x", "y"]
    assert _ab._model_names({"models": [{"model": "m1"}]}) == ["m1"]
    assert _ab._model_names({"result": ["r1"]}) == ["r1"]
    assert _ab._model_names({"data": {"data": [{"id": "n1"}]}}) == ["n1"]
    assert _ab._model_names({"nope": []}) == []
    # 限流报文翻译成人话
    assert "token" in _ab._rate_limit_hint('{"message":"inference exceeds tpm/rpm limit"}')
    assert "请求次数" in _ab._rate_limit_hint('{"message":"exceeds rpm limit"}')
    assert "429" in _ab._rate_limit_hint("slow down")
    assert _ab._is_retryable(429) and _ab._is_retryable(503) and not _ab._is_retryable(401)

print("冒烟测试通过：健康检查、鉴权、群组 CRUD、cron 校验、设置保存、AI 机器人、多智能体、机器人签到、扫码登录、多账号、多机器人、补签、备份恢复、WebDAV 自动建目录、GitHub 仓库诊断、安全加固（会话签名/限速/响应头/出网护栏）、长期记忆（注入/去重/作用域）、账号自动回复配置、大模型接入（地址纠错/返回格式兼容/限流提示）均正常")
