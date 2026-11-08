#!/bin/bash
# 重建并替换 lottery-panel 容器（可在面板里一键触发，也可直接 bash 跑）
#   docker run -d --rm --name panel-updater -v /var/run/docker.sock:/var/run/docker.sock \
#     -v /root/lottery-panel:/src lottery-panel:2.1.0 bash /src/recreate.sh
# 说明：build 成功才删旧容器；日志写脚本同级 selfupdate.log（容器里就是宿主同一份）。
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMG="${1:-lottery-panel:2.1.0}"
LOG="$DIR/selfupdate.log"
{
  echo "[$(date '+%F %T')] build $IMG (context=$DIR)"
  if DOCKER_BUILDKIT=0 docker build -t "$IMG" "$DIR"; then
    echo "[$(date '+%F %T')] build ok, recreating container"
    docker rm -f lottery-panel >/dev/null 2>&1 || true
    docker run -d --name lottery-panel --restart unless-stopped --network host \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v /root/lottery-docker:/app/lottery \
      -v /root/LotteryAutoScript:/app/src \
      -v /etc/cron.d:/etc/cron.d \
      -v /:/host:ro \
      -v /root/.hermes/scripts:/hscripts:ro -v /root/.hermes/.env:/secrets/hermes.env:ro \
      -e TZ=Asia/Shanghai -e LOTTERY_HOST=/root/lottery-docker -e HOST_ROOT=/host \
      "$IMG"
    echo "[$(date '+%F %T')] done"
  else
    echo "[$(date '+%F %T')] BUILD FAILED, 旧容器保留"
    exit 1
  fi
} >>"$LOG" 2>&1

sleep 3
if [ "$(docker inspect -f '{{.State.Running}}' lottery-panel 2>/dev/null)" != "true" ]; then
  echo "[$(date '+%F %T')] 容器未能启动，最后 20 行日志：" | tee -a "$LOG"
  docker logs --tail 20 lottery-panel 2>&1 | tee -a "$LOG"
  exit 1
fi
echo "[OK] lottery-panel 已启动（$IMG）"
