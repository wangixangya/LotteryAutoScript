# lottery-panel · 抽奖控制台

LotteryAutoScript 的常驻 Web 控制台。后端 `server.py` 只用 Python 标准库，前端 `index.html`
不依赖任何框架与 CDN，图表是手写 SVG。

## 部署

```bash
DOCKER_BUILDKIT=0 docker build -t lottery-panel:2.1.0 .

docker run -d --name lottery-panel --restart unless-stopped --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /root/lottery-docker:/app/lottery \
  -v /root/LotteryAutoScript:/app/src \
  -v /etc/cron.d:/etc/cron.d \
  -v /:/host:ro \
  -v /root/.hermes/scripts:/hscripts:ro \
  -e TZ=Asia/Shanghai \
  -e LOTTERY_HOST=/root/lottery-docker \
  -e HOST_ROOT=/host \
  lottery-panel:2.1.0
```

| 环境变量 | 默认 | 说明 |
|:--|:--|:--|
| `PANEL_PORT` | `5311` | 监听端口 |
| `LOTTERY_DIR` | `/app/lottery` | 面板**容器内**读脚本/配置/日志的路径 |
| `LOTTERY_HOST` | `/root/lottery-docker` | 同上，但必须是**宿主路径**（docker CLI 用） |
| `HOST_ROOT` | `/host` | 宿主根只读挂载，用来读真主机信息 |
| `PANEL_ALLOW_EXTRA` | 空 | 额外放行的 IP，逗号分隔（默认只放内网/本机） |
| `QQ_SEND_SCRIPT` | `/hscripts/qq_ws_send.py` | QQ 推送测试用的发送脚本 |

## 改完代码怎么生效

```bash
bash panel/recreate.sh lottery-panel:2.1.0   # build 成功才替换容器
```

或直接点面板「设置 → 重建面板（自更新）」，效果相同（build 失败时旧容器保持在线）。

> 注意：`docker restart` **不会**更新镜像内容，必须 `docker rm -f` 重建容器。

## 接口

| 方法 | 路径 | 说明 |
|:--|:--|:--|
| POST | `/login` | 密码登录，发 `sid` cookie（HttpOnly / SameSite=Lax / 7 天） |
| GET | `/panel/status` | 容器状态、dyid 数、lottery_info、定时任务、面板版本 |
| GET | `/panel/system` | CPU / 内存 / 磁盘 / 温度（CPU + 主板）/ 主机信息 / 负载 |
| GET | `/panel/stats` | 近 30 天运行、dyid 增长快照、中奖记录 |
| GET | `/panel/samples` | CPU 采样环（每分钟 1 点，最多 120 点） |
| GET | `/panel/logs?file=&lines=` | 日志尾部 |
| GET | `/panel/data` | dyid 计数与样本、lottery_info 明细 |
| GET | `/panel/accounts` | 解析 `env.js` 的账号（Cookie 不外传） |
| GET | `/panel/upstream` | 上游 git 分支 / 最新提交 / 工作区状态 |
| GET | `/panel/audit` | 操作审计（最近 120 条） |
| GET | `/panel/qr` | B站登录二维码 PNG |
| GET | `/panel/download?file=` | 下载 dyid.txt / 配置 / 日志（白名单） |
| POST | `/panel/run` `/stop` `/rebuild` `/sync` `/clear` `/cleanup` `/wcheck` | 运行控制 |
| POST | `/panel/config` | 保存 `my_config.js`（`node --check` 通过才落盘） |
| POST | `/panel/schedule` | 写入 `/etc/cron.d/lottery-panel` |
| POST | `/panel/backup` `/restore` | 配置备份 / 恢复 |
| POST | `/panel/password` | 改面板密码 |
| POST | `/panel/notify-test` | 发一条 QQ 测试消息 |
| POST | `/panel/qr/refresh` | 拉起重登容器拿新二维码 |
| POST | `/panel/selfupdate` | 后台 build + 替换自身容器 |

## 已知坑

1. **`%` 在 cron 里是换行符** —— 面板写入时会把未转义的 `%` 变成 `\%`。
2. **面板容器里挂 docker `-v` 要用宿主路径** —— 容器内是 `/app/lottery`，但 docker CLI 操作宿主，写容器路径会挂空。
3. **`node --check` 认扩展名** —— 临时文件必须叫 `*.check.js`，否则报 `ERR_UNKNOWN_FILE_EXTENSION`。
4. **buildkit 挂载受限时** —— `DOCKER_BUILDKIT=0 docker build` 用 legacy builder 即可。
5. **温度要分两个** —— `coretemp` / `x86_pkg_temp` 是 CPU 封装（60–80℃ 正常），`acpitz` 是主板（~28℃）；只取一个会误导。
6. **中奖关键词不能用「中奖」** —— `winner-check.log` 里 `[中奖检测]` 每轮都有，只认 `可能中奖` / `中奖了` / `恭喜`。
