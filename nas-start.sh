#!/bin/sh
# fnOS 上直接跑源码用的启动脚本（不构建镜像，改代码重启即生效）
set -e

cd /app

# 依赖判断不能靠标记文件：依赖装在容器内，标记文件却落在挂载卷上。
# 换一个新容器复用旧卷时会跳过安装，然后因为找不到 fastapi 直接崩溃。
# 这里改成「真的能 import 才算装好」，跨容器、跨版本都安全。
if ! python -c "import fastapi, uvicorn, socks" >/dev/null 2>&1; then
    echo "[nas-start] 安装依赖..."
    pip install --no-cache-dir -r /app/requirements.txt
fi

# 数据目录：容器以 root 启动，装完依赖后降权到 1000 运行，
# 这样面板写出来的 sqlite 文件在 NAS 上归 fnOS 用户所有，方便备份
mkdir -p /app/data
chown -R 1000:1000 /app/data 2>/dev/null || true

exec setpriv --reuid=1000 --regid=1000 --clear-groups \
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
