#!/bin/sh
# 容器以 root 启动只为两件事：初始化数据目录、把属主交给非特权用户，然后立刻降权。
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p "${DATA_DIR:-/app/data}"
    # 宿主目录挂进来时属主通常是 root，chown 一次让 app 用户能读写
    chown -R app:app "${DATA_DIR:-/app/data}" 2>/dev/null || true
    # rootfs 里的 setuid 位是镜像自带、跑起来后才丢的，所以直接 exec gosu 会
    # 报 "failed switching to app: operation not permitted"，容器秒退、平台判部署
    # 失败。先探一次，探不通就说明是本机/平台的权限模型不让切（缺 CAP_SETUID），
    # 那就老实以当前用户跑，别为了降权把服务起不来。
    if gosu app true 2>/dev/null; then
        exec gosu app "$@"
    fi
    echo "warn: 当前环境不允许降权到 app 用户（缺 CAP_SETUID），以 uid $(id -u) 运行" >&2
fi

exec "$@"
