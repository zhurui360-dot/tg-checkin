#!/bin/sh
# 容器以 root 启动只为两件事：初始化数据目录、把属主交给非特权用户，然后立刻降权。
# 注意：这里不能写 `set -e`。降权是「尽力而为」的增强项，任何一步失败都不该让
# 整个服务起不来 —— 之前用 set -e 时，gosu 在受限权限模型下返回非 0，脚本立刻
# 退出，容器表现为 exit 255 无限重启，而日志还抓不到，排查成本极高。
set -u

DATA="${DATA_DIR:-/app/data}"
mkdir -p "$DATA" 2>/dev/null || true
# 宿主目录挂进来时属主通常是 root，chown 一次让 app 用户能读写
chown -R app:app "$DATA" 2>/dev/null || true

if [ "$(id -u)" = "0" ]; then
    # rootfs 里的 setuid 位是镜像自带、跑起来后才丢的，所以直接 exec gosu 会
    # 报 "failed switching to app: operation not permitted"。先探一次，探不通
    # 就说明是本机/平台的权限模型不让切（缺 CAP_SETUID），那就老实以当前用户
    # 跑，别为了降权把服务起不来。
    if command -v gosu >/dev/null 2>&1 && gosu app true 2>/dev/null; then
        exec gosu app "$@"
    fi
    echo "warn: 无法降权到 app 用户，以 uid $(id -u) 运行" >&2
fi

exec "$@"
