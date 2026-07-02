#!/usr/bin/env bash
#
# hima-realtime.sh — 宿主机 cron 脚本。
# 每次执行:进 docker 容器跑一次「葵花 realtime 下载 + 增量 Zarr 裁剪」
#   (hima-download realtime --once --restore)。
#
# 注意:此脚本运行在【宿主机】,通过 docker exec 进容器,不在容器里跑。
#
# crontab 用法(每 10 分钟一次):
#   */10 * * * * /bin/bash /usr/local/bin/hima-realtime.sh
#
set -uo pipefail

# ==================== 配置(按环境改) ====================
CONTAINER="zhangmy-dev"                    # docker 容器名
PROJECT_DIR="/workspace/hima-download"     # 容器内项目路径
RESTORE_CFG="restore.yaml"                 # 容器内、相对 PROJECT_DIR 的裁剪配置
DOCKER="$(command -v docker || echo /usr/bin/docker)"
LOCK="/tmp/hima_realtime.lock"             # 宿主机自锁文件(防重叠)
LOG="/var/log/hima-realtime.cron.log"      # 宿主机 cron 层日志
AUTO_START=1                               # 容器未运行时是否尝试 docker start
# ========================================================

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
exec >>"$LOG" 2>&1
ts() { date '+%F %T'; }

# --- 自锁:上一轮没跑完(网络慢)就跳过本轮,避免多个 exec 叠加 ---
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(ts)] 上一轮仍在运行,跳过"
  exit 0
fi

# --- 确认容器在运行 ---
running="$("$DOCKER" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo missing)"
if [ "$running" != "true" ]; then
  if [ "$AUTO_START" = "1" ] && [ "$running" = "false" ]; then
    echo "[$(ts)] 容器未运行,尝试 docker start $CONTAINER"
    "$DOCKER" start "$CONTAINER" >/dev/null 2>&1 || { echo "[$(ts)] 启动失败,退出"; exit 1; }
  else
    echo "[$(ts)] 容器 $CONTAINER 不可用(状态=$running),退出"; exit 1
  fi
fi

# --- 执行:下载新帧,并把新帧增量并入当月 Zarr ---
echo "[$(ts)] START realtime --once --restore"
if "$DOCKER" exec "$CONTAINER" sh -c \
     "cd '$PROJECT_DIR' && ./.venv/bin/hima-download realtime --once --restore --restore-config '$RESTORE_CFG'"; then
  echo "[$(ts)] DONE ok"
else
  rc=$?; echo "[$(ts)] DONE rc=$rc"; exit "$rc"
fi
