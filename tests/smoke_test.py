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

print("冒烟测试通过：健康检查、鉴权、群组 CRUD、cron 校验、设置保存均正常")
