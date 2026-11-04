#!/bin/sh
# 入口脚本：按 PUID/PGID 修正 /data 属主后降权运行。
# 解决绑定挂载目录属主与容器用户不一致导致的 Permission denied
# （Docker 自动创建的挂载目录属主是 root；NAS 上属主通常是宿主机用户）。
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    if ! chown -R "$PUID:$PGID" /data 2>/dev/null; then
        echo "[entrypoint] 警告: 无法修改 /data 属主（网络文件系统不支持 chown?），"
        echo "[entrypoint] 若仍报 Permission denied，请在宿主机执行: sudo chown -R $PUID:$PGID <宿主机data目录>"
    fi
    exec gosu "$PUID:$PGID" "$@"
fi

# 非 root 启动（如用户自行指定 user:），直接运行
exec "$@"
