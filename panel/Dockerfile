# LotteryAutoScript 控制台 —— 常驻容器，通过挂载的 docker.sock 控制宿主上的抽奖容器
FROM alpine:3.20

RUN apk add --no-cache python3 bash docker-cli tzdata tini git \
 && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
 && echo "Asia/Shanghai" > /etc/timezone

WORKDIR /app
COPY server.py index.html panel.conf recreate.sh /app/

ENV PANEL_PORT=5311 \
    LOTTERY_DIR=/app/lottery \
    LOTTERY_SRC=/app/src \
    CROND_DIR=/etc/cron.d \
    PYTHONUNBUFFERED=1

EXPOSE 5311
ENTRYPOINT ["/sbin/tini","--"]
CMD ["python3","/app/server.py"]
