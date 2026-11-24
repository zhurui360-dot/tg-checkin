#!/bin/sh
# 容器以 root 启动只为两件事：初始化数据目录、把属主交给非特权用户，然后立刻降权。
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p "${DATA_DIR:-/app/data}"
    # 宿主目录挂进来时属主通常是 root，chown 一次让 app 用户能读写
    chown -R app:app "${DATA_DIR:-/app/data}" 2>/dev/null || true
    exec gosu app "$@"
fi

exec "$@"
