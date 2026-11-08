#!/usr/bin/env python3
"""LotteryAutoScript 控制台 v2.1 —— 单文件、零依赖（只用标准库）。

设计要点
- LotteryAutoScript 是一次性 Node 脚本（跑一轮就退出），没有 API；所以全部通过
  docker CLI + 文件读写 + 日志抓取来做，不假设它常驻。
- 面板自己常驻（Docker 容器），定时任务写 /etc/cron.d/lottery-panel（挂载宿主 /etc/cron.d），
  由宿主 cron 执行，面板重启不影响调度。
- 可视化数据来源：日志解析（每轮/每中奖）+ 每日快照（panel-stats.json）+ 内存采样（CPU/内存曲线）。
"""
import glob, http.server, ipaddress, json, os, re, shutil, socketserver, subprocess, urllib.parse, urllib.request
import threading, time, secrets

BASE = os.path.dirname(os.path.abspath(__file__))
LOTTERY = os.environ.get("LOTTERY_DIR", "/app/lottery")                # 容器内路径
LOTTERY_HOST = os.environ.get("LOTTERY_HOST", "/root/lottery-docker")  # docker -v 必须用宿主路径
CROND = os.environ.get("CROND_DIR", "/etc/cron.d")
CRON_FILE = os.path.join(CROND, "lottery-panel")
CONF = os.path.join(LOTTERY, "panel.conf")          # 持久化在挂载目录，重建不丢
LEGACY_CONF = os.path.join(BASE, "panel.conf")      # 老位置，只读兜底
STATS = os.path.join(LOTTERY, "panel-stats.json")
HOST_ROOT = os.environ.get("HOST_ROOT", "/host")   # 宿主根目录只读挂载，用于读主机名/发行版/磁盘
CONTAINER = "lottery-auto-script"
NODE_IMAGE = "node:lts-alpine"
VERSION = "2.2.0"

SEC_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
}

SESSIONS = {}


def _summary_text():
    st = docker_state()
    ids = len(dyid_list())
    info = lottery_info_counts()
    pend = sum(v for v in info.values() if isinstance(v, int))
    al = _alert_state()
    prev = al.get("lastDyids")
    grow = max(0, ids - prev) if isinstance(prev, int) else 0
    return (f"【B站抽奖】本轮完成\n"
            f"退出码 {st.get('exitCode')} · 耗时 {(st.get('startedAt') or '')[11:19]}→{(st.get('finishedAt') or '')[11:19]}\n"
            f"动态ID总数 {ids}（本轮 +{grow}）\n"
            f"待参与抽奖 {pend} 条 · 已知中奖 {len(winners_readonly())} 条")


def _sessions_prune():
    now = time.time()
    with _LOCK:
        dead = [k for k, v in SESSIONS.items() if v < now]
        for k in dead:
            SESSIONS.pop(k, None)
_LOCK = threading.Lock()
START_TS = time.time()

# ---- 访问控制：只允许内网/本机 ----
ALLOW_EXTRA = [x.strip() for x in os.environ.get("PANEL_ALLOW_EXTRA", "").split(",") if x.strip()]
FAILS = {}
MAX_FAILS, FAIL_WINDOW, LOCK_SECS = 5, 600, 600


def ip_allowed(ip):
    if ip in ALLOW_EXTRA:
        return True
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_loopback or a.is_private or a.is_link_local


def login_blocked(ip):
    now = time.time()
    with _LOCK:
        ts = [t for t in FAILS.get(ip, []) if now - t < FAIL_WINDOW]
        FAILS[ip] = ts
        if len(ts) >= MAX_FAILS and now - ts[-1] < LOCK_SECS:
            return int(LOCK_SECS - (now - ts[-1]))
    return 0


def login_fail(ip):
    with _LOCK:
        FAILS.setdefault(ip, []).append(time.time())


def client_ip(h):
    return (h.client_address or ("?",))[0].replace("::ffff:", "")


# ---------- 工具 ----------
def rd(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def wr(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def env_clean():
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(k, None)
    return env


def shq(args, timeout=60, cwd=None):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env_clean())
        return (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return "[超时]"
    except Exception as e:
        return f"[错误] {e}"


def password():
    txt = rd(CONF) if os.path.exists(CONF) else rd(LEGACY_CONF)
    m = re.search(r"^PANEL_PASSWORD=(.*)$", txt, re.M)
    return (m.group(1).strip() if m else "admin") or "admin"


def set_password(new):
    txt = rd(CONF) if os.path.exists(CONF) else rd(LEGACY_CONF, "PANEL_PASSWORD=admin\n")
    if re.search(r"^PANEL_PASSWORD=", txt, re.M):
        txt = re.sub(r"^PANEL_PASSWORD=.*$", f"PANEL_PASSWORD={new}", txt, flags=re.M)
    else:
        txt = txt.rstrip("\n") + f"\nPANEL_PASSWORD={new}\n"
    wr(CONF, txt)


# ---------- 系统信息 ----------
CPU_SAMPLES, MEM_SAMPLES = [], []
_prev_cpu = {"idle": 0, "total": 0}


def cpu_pct():
    try:
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        total = sum(v)
        di, dt = idle - _prev_cpu["idle"], total - _prev_cpu["total"]
        _prev_cpu.update(idle=idle, total=total)
        if dt <= 0:
            return 0.0
        return round(100.0 * (1 - di / dt), 1)
    except Exception:
        return 0.0


def mem_info():
    d = {}
    for ln in rd("/proc/meminfo").splitlines():
        k, _, v = ln.partition(":")
        sp = v.split()
        d[k.strip()] = int(sp[0]) if sp else 0
    total = d.get("MemTotal", 0) / 1048576
    avail = d.get("MemAvailable", 0) / 1048576
    return {"total": round(total, 1), "avail": round(avail, 1), "used": round(total - avail, 1),
            "pct": round(100 * (total - avail) / total, 1) if total else 0}


def disk_info():
    try:
        u = shutil.disk_usage(HOST_ROOT if os.path.isdir(HOST_ROOT) else "/")
        return {"total": round(u.total / 1e9, 1), "used": round(u.used / 1e9, 1),
                "free": round(u.free / 1e9, 1), "pct": round(100 * u.used / u.total, 1)}
    except Exception:
        return {}


def temps():
    """返回 cpu（coretemp/x86_pkg_temp 等封装温度）与 board（acpitz 等主板温度）。"""
    cpu, board, other = [], [], []
    for f in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        t = rd(f[:-5] + "/type").strip().lower()
        try:
            c = int(rd(f).strip() or 0) / 1000.0
        except Exception:
            continue
        if not (1 < c < 130):
            continue
        (cpu if ("x86_pkg" in t or "cpu" in t) else board).append(c)
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        name = rd(os.path.join(d, "name")).strip().lower()
        for f in glob.glob(os.path.join(d, "temp*_input")):
            try:
                c = int(rd(f).strip() or 0) / 1000.0
            except Exception:
                continue
            if not (1 < c < 130):
                continue
            if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
                cpu.append(c)
            elif name == "acpitz":
                board.append(c)
            else:
                other.append(c)
    pick = lambda xs: round(max(xs), 1) if xs else None
    return {"cpu": pick(cpu), "board": pick(board), "max": pick(cpu + board + other)}


def host_info():
    root = HOST_ROOT if os.path.isdir(HOST_ROOT) else ""
    osname = "Linux"
    for ln in rd(root + "/etc/os-release").splitlines():
        if ln.startswith("PRETTY_NAME="):
            osname = ln.split("=", 1)[1].strip().strip('"')
    cpu = "unknown"
    cpuinfo = rd("/proc/cpuinfo")
    if "model name" in cpuinfo:
        cpu = cpuinfo.split("model name", 1)[1].split(":", 1)[1].split("\n")[0].strip()
    hostname = rd(root + "/etc/hostname").strip()
    try:
        import socket
        hostname = hostname or socket.gethostname()
    except Exception:
        hostname = hostname or "host"
    return {"hostname": hostname, "os": osname, "kernel": rd("/proc/sys/kernel/osrelease").strip(),
            "cpu": cpu, "cores": os.cpu_count()}


def sys_payload():
    m = mem_info()
    p = cpu_pct()
    now = int(time.time())
    with _LOCK:
        CPU_SAMPLES.append([now, p])
        MEM_SAMPLES.append([now, m["used"]])
        del CPU_SAMPLES[:-120]
        del MEM_SAMPLES[:-120]
    t = temps()
    return {"cpu": p, "mem": m, "disk": disk_info(), "temp": t.get("cpu"), "tempBoard": t.get("board"),
            "tempMax": t.get("max"), "host": host_info(),
            "load": [float(x) for x in (rd("/proc/loadavg").split()[:3] or [0, 0, 0])],
            "uptime": int(float((rd("/proc/uptime").split() or ["0"])[0]))}


# ---------- 容器 / 数据 ----------
def docker_state():
    raw = shq(["docker", "inspect", CONTAINER], 25)
    if not raw.strip() or raw.startswith("[错误]") or raw.startswith("[超时]"):
        return {"exists": False}
    try:
        d = json.loads(raw)[0]
    except Exception:
        return {"exists": False}
    st, cfg = d.get("State") or {}, d.get("Config") or {}
    return {"exists": True, "running": bool(st.get("Running")), "status": st.get("Status"),
            "exitCode": st.get("ExitCode"),
            "startedAt": (st.get("StartedAt") or "")[:19].replace("T", " "),
            "finishedAt": (st.get("FinishedAt") or "")[:19].replace("T", " "),
            "image": cfg.get("Image"), "restarts": d.get("RestartCount", 0)}


def dyid_list():
    return [x.strip() for x in rd(os.path.join(LOTTERY, "data/dyids/dyid.txt")).split(",") if x.strip()]


def lottery_info_counts():
    out = {}
    d = os.path.join(LOTTERY, "data/lottery_info")
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            try:
                obj = json.loads(rd(os.path.join(d, name)) or "{}")
                out[name] = sum(len(v) for v in obj.values() if isinstance(v, list))
            except Exception:
                out[name] = -1
    return out


def parse_run_log():
    txt = rd(os.path.join(LOTTERY, "logs/lottery-run.log"))
    runs = re.findall(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] run finished", txt)
    skips = re.findall(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] skip:", txt)
    return runs, skips


def _normalize_time(text):
    """把 '11/5/2026, 6:43:00 AM' 这类美式时间转成 '2026-11-05 06:43'。"""
    def rep(m):
        mo, d, y, hh, mm, ss, ap = m.groups()
        h = int(hh) % 12 + (12 if ap.upper() == "PM" else 0)
        return f"{y}-{int(mo):02d}-{int(d):02d} {h:02d}:{int(mm):02d}"
    return re.sub(r"(\d{1,2})/(\d{1,2})/(\d{4}),?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AP]M)", rep, text)


_WIN_CACHE = {"key": None, "val": None}


def winners_readonly():
    try:
        return winners()
    except Exception:
        return []


def winners():
    txt = rd(os.path.join(LOTTERY, "logs/winner-check.log"))
    out, seen = [], set()
    for m in re.finditer(r"\[(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)[^\]]*\]\s*\[(Notice|Info)\]\s*\[([^\]]*)\]\s*\[([^\]]{0,120})", txt):
        date, tm, lvl, tag, msg = m.groups()
        key = (date, tm, msg)
        if key in seen:
            continue
        if ("可能中奖" in tag) or ("可能中奖" in msg) or ("中奖了" in msg) or ("恭喜" in msg):
            seen.add(key)
            msg = _normalize_time(msg)
            out.append({"time": f"{date} {tm}", "level": lvl, "tag": tag, "msg": msg[:160]})
    return out[-60:]


def snapshots_touch():
    try:
        s = json.loads(rd(STATS) or "{}")
    except Exception:
        s = {}
    today = time.strftime("%Y-%m-%d")
    cur = {"dyids": len(dyid_list()), "info": sum(v for v in lottery_info_counts().values() if v > 0)}
    if s.get(today) != cur:
        s[today] = cur
        try:
            wr(STATS, json.dumps({k: s[k] for k in sorted(s)[-90:]}, ensure_ascii=False))
        except Exception:
            pass
    return s


def stats_payload():
    runs, skips = parse_run_log()
    days = {}
    for ts in runs:
        days.setdefault(ts[:10], {"runs": 0, "skips": 0, "wins": 0})["runs"] += 1
    for ts in skips:
        days.setdefault(ts[:10], {"runs": 0, "skips": 0, "wins": 0})["skips"] += 1
    ws = winners()
    for w in ws:
        days.setdefault(w["time"][:10], {"runs": 0, "skips": 0, "wins": 0})["wins"] += 1
    series = [{"date": d, **v} for d, v in sorted(days.items())][-30:]
    snap = snapshots_touch()
    growth = [{"date": d, "dyids": v.get("dyids", 0), "info": v.get("info", 0)} for d, v in sorted(snap.items())][-30:]
    return {"series": series, "growth": growth, "runsTotal": len(runs), "skipsTotal": len(skips),
            "wins": ws, "winsTotal": len(ws), "dyids": len(dyid_list()),
            "info": sum(v for v in lottery_info_counts().values() if v > 0)}


# ---------- cron ----------
def cron_entries():
    out = []
    for ln in rd(CRON_FILE).splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split(None, 6)
        if len(parts) < 7:
            continue
        out.append({"cron": " ".join(parts[:5]), "user": parts[5], "cmd": parts[6], "line": s})
    return out


def cron_write(entries):
    lines = ["# 由 LotteryAutoScript 控制台生成，请勿手工编辑（面板会覆盖）",
             "SHELL=/bin/bash", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", ""]
    for e in entries:
        cron = " ".join(str(e.get("cron") or "").split())
        cmd = str(e.get("cmd") or "").strip()
        if not cron or not cmd:
            continue
        cmd = re.sub(r"(?<!\\)%", r"\\%", cmd)      # cron 里 % = 换行，必须转义
        lines.append(f"{cron} root {cmd}")
    wr(CRON_FILE, "\n".join(lines) + "\n")
    try:
        os.chmod(CRON_FILE, 0o644)
    except Exception:
        pass


def node_check(path):
    return shq(["docker", "run", "--rm", "-v", f"{LOTTERY_HOST}:/mnt:ro", NODE_IMAGE,
                "node", "--check", "/mnt/" + os.path.basename(path)], 120)


def spawn(script, extra=None):
    path = os.path.join(LOTTERY, script)
    if not os.path.exists(path):
        return False
    subprocess.Popen(["/bin/bash", path] + (extra or []), stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, cwd=LOTTERY)
    return True


# ---------- my_config.js 可视化表单 ----------
CFG_BLOCK = "default_config"
FORM_GROUPS = [
    ("关注与关键词", ["UIDs", "TAGs", "Articles", "APIs", "TxT", "key_words", "notice_key_words", "blacklist", "blockword"]),
    ("抽奖行为", ["LotteryOrder", "check_if_duplicated", "maxday", "max_create_time", "disable_reserve_lottery",
                  "is_not_relay_reserve_lottery", "sneaktower", "is_imitator", "only_followed", "minfollower",
                  "save_lottery_info_to_file", "set_lottery_info_url"]),
    ("扫描范围", ["uid_scan_page", "tag_scan_page", "article_scan_page", "article_create_time", "not_check_article",
                  "check_session_pages", "partition_id"]),
    ("延时（毫秒）", ["wait", "search_wait", "get_session_wait", "update_session_wait", "get_partition_wait",
                      "get_dynamic_detail_wait", "filter_wait", "random_dynamic_wait", "reserve_lottery_wait",
                      "lottery_loop_wait", "check_loop_wait", "clear_loop_wait"]),
    ("动态与评论", ["create_dy", "create_dy_type", "create_dy_num", "dy_contents", "create_dy_mode", "at_users",
                    "is_repost_then_chat", "relay", "chat", "is_copy_chat", "copy_blockword", "is_not_create_partition"]),
    ("清理", ["clear_max_day", "clear_remove_dynamic", "clear_remove_attention", "clear_remove_delay",
              "clear_dynamic_type", "clear_white_list", "clear_partition", "clear_quick_remove_attention",
              "clear_quick_remove_attention_fans_number_smallest"]),
    ("AI", ["model", "chatmodel", "prompt", "ai_judge_parm", "providers"]),
]


def _split_comment(raw):
    """把 'value, // 注释' 切成 (value, comment)，引号内的 // 不当注释（如 file://...）。"""
    out, q, i = [], None, 0
    while i < len(raw):
        ch = raw[i]
        if q:
            out.append(ch)
            if ch == q and (i == 0 or raw[i - 1] != "\\"):
                q = None
            i += 1
            continue
        if ch in "'\"":
            q = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(raw) and raw[i + 1] == "/":
            return "".join(out), raw[i:]
        out.append(ch)
        i += 1
    return "".join(out), ""


def _kind(raw):
    if raw in ("true", "false"):
        return "bool"
    if re.fullmatch(r"-?\d+(\.\d+)?", raw):
        return "num"
    if re.fullmatch(r"'.*'|\".*\"", raw, re.S):
        return "str"
    if raw.startswith("[") and raw.endswith("]"):
        return "list"
    return "expr"


# ---------- 字段中文名 ----------
LABELS = {
    "UIDs": "关注的 UP 主 UID", "TAGs": "抽奖 TAG 标签", "Articles": "指定动态 ID",
    "APIs": "抽奖信息来源（文件/接口）", "TxT": "dyid 文本文件", "blacklist": "黑名单 UP 主",
    "blockword": "屏蔽关键词", "LotteryOrder": "抽奖顺序", "check_if_duplicated": "查重开关（1 开）",
    "maxday": "抽奖最大天数", "max_create_time": "动态最大发布时长", "disable_reserve_lottery": "关闭预约抽奖",
    "is_not_relay_reserve_lottery": "预约抽奖不转发", "sneaktower": "隐藏楼层抽奖", "is_imitator": "模仿者模式",
    "only_followed": "只参与已关注", "minfollower": "UP 主最低粉丝数", "save_lottery_info_to_file": "保存抽奖信息到文件",
    "set_lottery_info_url": "抽奖信息接口地址", "uid_scan_page": "关注列表扫描页数", "tag_scan_page": "TAG 扫描页数",
    "article_scan_page": "动态扫描页数", "article_create_time": "动态时限（天）", "not_check_article": "不检查动态",
    "check_session_pages": "会话检查页数", "partition_id": "分区 ID", "wait": "主循环等待（毫秒）",
    "search_wait": "搜索等待", "get_session_wait": "获取会话等待", "update_session_wait": "更新会话等待",
    "get_partition_wait": "获取分区等待", "get_dynamic_detail_wait": "动态详情等待（区间）", "filter_wait": "过滤等待",
    "random_dynamic_wait": "随机动态等待（区间）", "reserve_lottery_wait": "预约抽奖等待",
    "lottery_loop_wait": "抽奖循环等待", "check_loop_wait": "检测循环等待", "clear_loop_wait": "清理循环等待",
    "create_dy": "自动发动态", "create_dy_type": "动态类型", "create_dy_num": "发动态数量",
    "is_repost_then_chat": "转发后评论", "relay": "转发附带文案", "is_copy_chat": "复制他人评论",
    "copy_blockword": "评论复制屏蔽词", "is_not_create_partition": "不新建分区", "clear_max_day": "清理保留天数",
    "clear_remove_dynamic": "删除旧动态", "clear_remove_attention": "同时取关", "clear_remove_delay": "清理间隔（毫秒）",
    "clear_dynamic_type": "清理动态类型", "clear_white_list": "清理白名单", "clear_partition": "清理分区",
    "clear_quick_remove_attention": "快速取关", "clear_quick_remove_attention_fans_number_smallest": "快速取关粉丝数下限",
    "model": "文本模型编号", "chatmodel": "评论模型编号", "block_dynamic_type": "屏蔽动态类型",
    "is_exception": "异常模式", "is_outof_maxfollow": "超出关注上限", "notice_running_state": "推送运行状态",
}

def cfg_fields():
    """解析 default_config 块里的顶层标量/单行数组字段（多行对象/数组不参与表单，走源码模式）。"""
    lines = rd(os.path.join(LOTTERY, "my_config.js")).splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(rf"\s*{CFG_BLOCK}\s*:\s*\{{", ln):
            start = i
            break
    if start is None:
        return {"ok": False, "fields": [], "why": f"没找到 {CFG_BLOCK} 块"}
    fields, seen = [], set()
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if re.match(r"^\s{0,4}\}\s*,?\s*$", ln):
            break
        m = re.match(r"^(\s{6,10})([A-Za-z_][\w]*)\s*:\s*(.*)$", ln)
        if not m:
            continue
        key, raw = m.group(2), m.group(3).strip()
        if raw.startswith("{") or raw.endswith(",") is False and raw == "":
            continue
        # 多行值（[ 或 { 开头但没有闭合）跳过
        if (raw.startswith("[") or raw.startswith("{")) and not re.search(r"[\]\}]\s*,?\s*(//.*)?$", raw):
            continue
        raw = _split_comment(raw)[0].strip().rstrip(",").strip()
        if key in seen:
            continue
        seen.add(key)
        kind = _kind(raw)
        # 嵌套数组/对象（at_users: [[名字, uid], ...]、create_dy_mode: [[0],[0]]）表单改不了，交给源码模式
        if kind == "list" and ("[" in raw[1:] or "{" in raw):
            continue
        fields.append({"key": key, "label": LABELS.get(key, key), "raw": raw, "kind": kind, "line": i})
    return {"ok": True, "fields": fields}


def cfg_grouped(fields):
    got = {f["key"]: f for f in fields}
    used, groups = set(), []
    for name, keys in FORM_GROUPS:
        rows = [got[k] for k in keys if k in got]
        if rows:
            groups.append({"name": name, "fields": rows})
            used.update(k for k in keys if k in got)
    rest = [f for f in fields if f["key"] not in used]
    if rest:
        groups.append({"name": "其他", "fields": rest})
    return groups


def cfg_apply(changes):
    """把 {key: 新值原文} 写回 default_config 块，行级替换，保留缩进与行尾注释。"""
    path = os.path.join(LOTTERY, "my_config.js")
    lines = rd(path).splitlines(True)
    start = next((i for i, ln in enumerate(lines) if re.match(rf"\s*{CFG_BLOCK}\s*:\s*\{{", ln)), None)
    if start is None:
        return False, f"没找到 {CFG_BLOCK} 块", ""
    done = []
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if re.match(r"^\s{0,4}\}\s*,?\s*$", ln):
            break
        m = re.match(r"^(\s{6,10})([A-Za-z_][\w]*)(\s*:\s*)(.*?)(\r?\n)?$", ln)
        if not m or m.group(2) not in changes:
            continue
        indent, key, sep, rest, nl = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5) or "\n"
        _val, cm = _split_comment(rest)
        comment = cm.strip()
        lines[i] = (f"{indent}{key}{sep}{changes[key]},  {comment}" if comment else f"{indent}{key}{sep}{changes[key]},") + nl
        done.append(key)
    if not done:
        return False, "没有匹配到可改的字段", ""
    tmp = path + ".check.js"
    wr(tmp, "".join(lines))
    chk = node_check(tmp)
    if "SyntaxError" in chk or "ERR_" in chk:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False, "语法检查未通过", chk[-600:]
    try:
        shutil.copy2(path, path + ".bak." + time.strftime("%Y%m%d_%H%M%S"))
    except Exception:
        pass
    os.replace(tmp, path)
    return True, "已保存：" + "、".join(done) + "（下一轮生效）", ""

def _trim(paths, keep):
    """保留最新 keep 个，返回删除数。"""
    files = sorted([p for p in paths if os.path.exists(p)], key=lambda p: os.path.getmtime(p), reverse=True)
    n = 0
    for p in files[keep:]:
        try:
            os.remove(p)
            n += 1
        except Exception:
            pass
    return n


def maintain():
    """清理：悬空镜像 / 旧备份 / 超大日志。"""
    out = {"images": "", "backups": 0, "logs": 0, "freed": ""}
    out["images"] = shq(["docker", "image", "prune", "-f"], 120)[-200:]
    out["images"] += " | builder: " + shq(["docker", "builder", "prune", "-f", "--filter", "until=336h"], 120)[-120:]
    bdir = os.path.join(LOTTERY, "backups")
    out["backups"] = _trim(glob.glob(os.path.join(bdir, "*.tar.gz")), 12)
    out["backups"] += _trim(glob.glob(os.path.join(LOTTERY, "my_config.js.bak.*")), 20)
    for f in glob.glob(os.path.join(LOTTERY, "logs", "*.log")) + glob.glob("/var/log/lottery-*.log"):
        try:
            if os.path.getsize(f) > 2_000_000:
                with open(f, "r+", encoding="utf-8", errors="ignore") as fh:
                    lines = fh.readlines()[-2000:]
                    fh.seek(0)
                    fh.truncate()
                    fh.writelines(lines)
                out["logs"] += 1
        except Exception:
            pass
    try:
        out["freed"] = shq(["docker", "system", "df"], 60).replace("\n", " ")[-160:]
    except Exception:
        pass
    return out



# ---------- 通知渠道 ----------
NOTIFY = os.path.join(LOTTERY, "panel-notify.json")

CHANNELS = [
    {"key": "qq_private", "name": "QQ 私聊", "fields": [
        {"k": "user_id", "label": "QQ 号", "ph": "949194446"}]},
    {"key": "qq_group", "name": "QQ 群", "fields": [
        {"k": "group_id", "label": "群号", "ph": "123456789"}]},
    {"key": "telegram", "name": "Telegram", "fields": [
        {"k": "chat_id", "label": "会话 ID", "ph": "1910099858"},
        {"k": "token", "label": "Bot Token", "ph": "留空则用 Hermes 的"}]},
    {"key": "wecom_bot", "name": "企业微信机器人", "fields": [
        {"k": "url", "label": "Webhook 地址", "ph": "https://qyapi.weixin.qq.com/..."}]},
    {"key": "bark", "name": "Bark（iOS）", "fields": [
        {"k": "url", "label": "推送地址", "ph": "https://api.day.app/你的key"}]},
    {"key": "serverchan", "name": "Server酱", "fields": [
        {"k": "key", "label": "SendKey", "ph": "SCT..."}]},
    {"key": "webhook", "name": "通用 Webhook", "fields": [
        {"k": "url", "label": "URL", "ph": "https://.../hook"},
        {"k": "template", "label": "请求体模板", "ph": '{"text":"{msg}"}'}]},
]


def _hermes_env(key, default=""):
    try:
        path = "/secrets/hermes.env" if os.path.exists("/secrets/hermes.env") else "/root/.hermes/.env"
        for ln in open(path, "r", encoding="utf-8", errors="ignore"):
            if ln.startswith(key + "="):
                return ln.split("=", 1)[1].split("#")[0].strip().strip('"').strip("'")
    except Exception:
        pass
    return default


def notify_conf():
    try:
        return json.loads(rd(NOTIFY) or "{}")
    except Exception:
        return {}


def notify_save(conf):
    wr(NOTIFY, json.dumps(conf, ensure_ascii=False, indent=1))


def _mask(v):
    if not v:
        return ""
    v = str(v)
    return v[:4] + "…" + v[-4:] if len(v) > 12 else "已设置"


def notify_view():
    conf = notify_conf()
    out = []
    for ch in CHANNELS:
        cur = conf.get(ch["key"], {})
        fields = []
        for f in ch["fields"]:
            val = cur.get(f["k"], "")
            fields.append({"k": f["k"], "label": f["label"], "ph": f.get("ph", ""),
                           "value": _mask(val), "set": bool(val)})
        hint = ""
        if ch["key"] == "telegram" and not cur.get("token"):
            hint = "已检测到 Hermes 的 Bot Token，可直接用"
        if ch["key"] == "qq_private" and not cur.get("user_id"):
            hint = "走本机 napcat（127.0.0.1:3000）"
        out.append({"key": ch["key"], "name": ch["name"], "enabled": bool(cur.get("enabled")),
                    "fields": fields, "hint": hint})
    return {"channels": out, "enabledCount": sum(1 for c in out if c["enabled"]),
            "summary": bool(conf.get("summary", {}).get("enabled"))}


def _send_one(key, conf, msg):
    c = conf.get(key, {})
    if key == "qq_private":
        uid = c.get("user_id") or ""
        if not uid:
            return False, "未填 QQ 号"
        sender = "/hscripts/qq_ws_send.py"
        if not os.path.exists(sender):
            return False, "缺少 qq_ws_send.py"
        out = shq(["python3", sender, msg, "--user", uid] if "--user" in rd(sender) else ["python3", sender, msg], 60)
        return "OK" in out, out[-160:]
    if key == "qq_group":
        gid = c.get("group_id") or ""
        if not gid:
            return False, "未填群号"
        body = json.dumps({"group_id": int(gid), "message": msg}, ensure_ascii=False)
        return _http_post("http://127.0.0.1:3000/send_group_msg", body, {})
    if key == "telegram":
        tok = c.get("token") or _hermes_env("TELEGRAM_BOT_TOKEN")
        chat = c.get("chat_id") or ""
        if not tok or not chat:
            return False, "缺 token 或 chat_id"
        proxy = _hermes_env("TELEGRAM_PROXY")
        try:
            body = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=body)
            if proxy:
                op = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
                r = op.open(req, timeout=20)
            else:
                r = urllib.request.urlopen(req, timeout=20)
            txt = r.read().decode("utf-8", "ignore")
            return '"ok":true' in txt.replace(" ", ""), txt[-160:]
        except Exception as e:
            return False, str(e)[:160]
    if key == "wecom_bot":
        url = c.get("url") or ""
        if not url:
            return False, "未填 Webhook"
        return _http_post(url, json.dumps({"msgtype": "text", "text": {"content": msg}}, ensure_ascii=False), {})
    if key == "bark":
        url = (c.get("url") or "").rstrip("/")
        if not url:
            return False, "未填推送地址"
        return _http_post(url, json.dumps({"title": "B站抽奖", "body": msg}, ensure_ascii=False), {})
    if key == "serverchan":
        k = c.get("key") or ""
        if not k:
            return False, "未填 SendKey"
        return _http_post(f"https://sctapi.ftqq.com/{k}.send",
                          urllib.parse.urlencode({"title": "B站抽奖", "desp": msg}).encode(), {})
    if key == "webhook":
        url = c.get("url") or ""
        if not url:
            return False, "未填 URL"
        tpl = c.get("template") or '{"text":"{msg}"}'
        body = tpl.replace("{msg}", msg.replace('"', '\"').replace("\n", "\\n"))
        return _http_post(url, body, {"Content-Type": "application/json"})
    return False, "未知渠道"


def _http_post(url, body, headers):
    try:
        data = body.encode() if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            txt = r.read().decode("utf-8", "ignore")
            ok = r.status < 400
            if '"errcode":0' in txt.replace(" ", "") or '"code":200' in txt.replace(" ", ""):
                ok = True
            return ok, txt[-160:]
    except Exception as e:
        return False, str(e)[:160]


def send_notify(msg, only=None):
    conf = notify_conf()
    res = []
    for ch in CHANNELS:
        k = ch["key"]
        if only and k != only:
            continue
        c = conf.get(k, {})
        if not only and not c.get("enabled"):
            continue
        if only and not c.get("enabled") and not c:
            pass
        ok, out = _send_one(k, conf, msg)
        res.append({"key": k, "name": ch["name"], "ok": ok, "out": out})
    return res



# ---------- 连续失败 / 零增长告警 ----------
ALERT = os.path.join(LOTTERY, "panel-alert.json")


def _alert_state():
    try:
        return json.loads(rd(ALERT) or "{}")
    except Exception:
        return {}


def _qq_send(msg):
    sender = os.environ.get("QQ_SEND_SCRIPT", "/hscripts/qq_ws_send.py")
    if not os.path.exists(sender):
        return False, "缺少 " + sender
    out = shq(["python3", sender, msg], 60)
    return "OK" in out, out[-200:]


def alert_tick():
    """每轮跑完检查一次：连续 2 轮失败 / 连续 2 轮 dyid 零增长 → 推 QQ（6 小时冷却）。"""
    st = docker_state()
    if not st.get("exists") or st.get("running"):
        return None
    key = f"{st.get('startedAt')}|{st.get('finishedAt')}"
    s0 = _alert_state()
    if s0.get("lastKey") == key:
        return None
    ids = len(dyid_list())
    prev_ids = s0.get("lastDyids")
    ok = (st.get("exitCode") == 0)
    grew = (prev_ids is None) or (ids > prev_ids)
    s0["lastKey"] = key
    s0["lastDyids"] = ids
    s0["failStreak"] = 0 if ok else int(s0.get("failStreak", 0)) + 1
    s0["zeroStreak"] = 0 if grew else int(s0.get("zeroStreak", 0)) + 1
    s0["lastRun"] = st.get("finishedAt")
    msg = None
    if s0["failStreak"] >= 2:
        msg = f"【B站抽奖】连续 {s0['failStreak']} 轮异常退出\n退出码: {st.get('exitCode')}\n时间: {st.get('finishedAt')}\n建议到面板「运行 → 一键体检」看看"
    elif s0["zeroStreak"] >= 2:
        msg = f"【B站抽奖】连续 {s0['zeroStreak']} 轮没有新增参与\n动态ID总数: {ids}\n时间: {st.get('finishedAt')}\n可能是风控/关键词太窄/Cookie 失效，建议体检"
    # 每轮跑完推摘要（可在设置页开关）
    conf = notify_conf()
    if conf.get("summary", {}).get("enabled") and ok:
        info = lottery_info_counts()
        pend = sum(v for v in info.values() if isinstance(v, int))
        summ = (f"【B站抽奖】本轮完成\n退出码 {st.get('exitCode')} · 耗时 {st.get('startedAt','')[11:19]}→{st.get('finishedAt','')[11:19]}\n"
                f"动态ID总数 {ids}（本轮 +{max(0, ids - (prev_ids or ids))}）\n待参与抽奖 {pend} 条 · 已知中奖 {len(winners_readonly())} 条")
        rr = send_notify(summ)
        s0["summaryAt"] = time.time()
        s0["summaryResult"] = ";".join(f"{r['name']}:{'ok' if r['ok'] else 'fail'}" for r in rr)
    if msg:
        last = float(s0.get("notifiedAt") or 0)
        if time.time() - last > 6 * 3600:
            rr = send_notify(msg)
            s0["notifiedAt"] = time.time()
            s0["notifyResult"] = "; ".join(f"{r['name']}:{'ok' if r['ok'] else 'fail'}" for r in rr) or "无可用渠道"
    try:
        wr(ALERT, json.dumps(s0, ensure_ascii=False))
    except Exception:
        pass
    return s0



# ---------- Cookie 有效期 ----------
def _bili_cookie_info(cookie):
    """SESSDATA 第二段是过期时间戳，能算出到期日。"""
    out = {"expiresAt": None, "daysLeft": None, "expired": None}
    m = re.search(r"SESSDATA=([^;]+)", cookie)
    if not m:
        return out
    raw = urllib.parse.unquote(m.group(1))
    parts = raw.split(",")
    if len(parts) < 2 or not parts[1].isdigit():
        return out
    ts = int(parts[1])
    out["expiresAt"] = time.strftime("%Y-%m-%d", time.localtime(ts))
    out["daysLeft"] = round((ts - time.time()) / 86400, 1)
    out["expired"] = ts < time.time()
    return out


# ---------- cron 下次运行 ----------
def _cron_next(expr, base=None):
    """只支持常用写法：分 时 日 月 周（支持 *、固定数字、*/N）。解析不了返回 None。"""
    base = base or time.time()
    f = expr.split()
    if len(f) < 5:
        return None
    t = time.localtime(base)
    def hit(field, val, lo, hi):
        if field == "*":
            return True
        if field.startswith("*/"):
            step = int(field[2:] or 1)
            return (val - lo) % step == 0
        if "," in field:
            return str(val) in field.split(",")
        if "-" in field:
            a, b = field.split("-")[:2]
            return int(a) <= val <= int(b)
        return field.isdigit() and int(field) == val
    cur = int(base)
    for _ in range(60 * 24 * 8):      # 最多找 8 天
        tm = time.localtime(cur)
        if (hit(f[0], tm.tm_min, 0, 59) and hit(f[1], tm.tm_hour, 0, 23)
                and hit(f[2], tm.tm_mday, 1, 31) and hit(f[3], tm.tm_mon, 1, 12)
                and hit(f[4], (tm.tm_wday + 1) % 7, 0, 6)):
            return cur
        cur += 60
        cur -= cur % 60
    return None


def _next_runs(entries):
    out = []
    for e in entries:
        if "lottery-watchdog" in e["cmd"] or "curl -sf" in e["cmd"]:
            continue          # 看门狗不算抽奖排班
        ts = _cron_next(e["cron"])
        if ts:
            out.append({"cron": e["cron"], "cmd": e["cmd"], "next": ts,
                        "nextText": time.strftime("%m-%d %H:%M", time.localtime(ts)),
                        "inMin": max(0, round((ts - time.time()) / 60))})
    return sorted(out, key=lambda x: x["next"])


# ---------- 一键体检 ----------
def checkup():
    items = []
    st = docker_state()
    items.append({"name": "抽奖容器", "level": "ok" if st.get("exists") else "fail",
                  "text": ("运行中" if st.get("running") else f"空闲（上次退出码 {st.get('exitCode')}）") if st.get("exists") else "未创建",
                  "hint": "空闲属正常，跑完会自动停" if st.get("exists") else "执行 start.sh 建容器"})
    acc = accounts()
    for a in acc.get("accounts", []):
        exp = a.get("expiresAt")
        if a.get("expired"):
            items.append({"name": f"Cookie 账号{a.get('number') or ''}", "level": "fail",
                          "text": "已过期", "hint": "扫二维码重新登录（账号页）"})
        elif a.get("daysLeft") is not None and a["daysLeft"] < 7:
            items.append({"name": f"Cookie 账号{a.get('number') or ''}", "level": "warn",
                          "text": f"仅剩 {a['daysLeft']} 天", "hint": "尽快重新登录"})
        elif exp:
            items.append({"name": f"Cookie 账号{a.get('number') or ''}", "level": "ok",
                          "text": f"有效至 {exp}（{a['daysLeft']} 天）", "hint": ""})
        else:
            items.append({"name": f"Cookie 账号{a.get('number') or ''}", "level": "warn",
                          "text": "解析不到有效期", "hint": "确认 COOKIE 含 SESSDATA"})
    ent = cron_entries()
    items.append({"name": "定时任务", "level": "ok" if ent else "fail",
                  "text": f"{len(ent)} 条" if ent else "未配置", "hint": "设置页可编辑" if not ent else ""})
    nxt = _next_runs(ent)
    if nxt:
        items.append({"name": "下一轮", "level": "ok", "text": f"{nxt[0]['nextText']}（{nxt[0]['inMin']} 分钟后）", "hint": ""})
    d = disk_info()
    if d.get("pct", 0) > 90:
        items.append({"name": "磁盘", "level": "fail", "text": f"已用 {d['pct']}%", "hint": "清理镜像/日志"})
    elif d.get("pct", 0) > 80:
        items.append({"name": "磁盘", "level": "warn", "text": f"已用 {d['pct']}%", "hint": ""})
    ids = dyid_list()
    if not ids:
        items.append({"name": "动态 ID 库", "level": "warn", "text": "空", "hint": "首轮会自行建立"})
    if password() in ("admin", "admin1", "123456", ""):
        items.append({"name": "面板密码", "level": "warn", "text": "仍是默认密码", "hint": "设置页「安全」里改一个"})
    txt = rd(os.path.join(LOTTERY, "my_config.js"))
    if not txt.strip():
        items.append({"name": "my_config.js", "level": "fail", "text": "缺失或为空", "hint": "用源码模式恢复配置"})
    else:
        items.append({"name": "my_config.js", "level": "ok", "text": f"{len(txt.splitlines())} 行", "hint": ""})
    al = _alert_state()
    if int(al.get("failStreak", 0)) >= 2 or int(al.get("zeroStreak", 0)) >= 2:
        items.append({"name": "运行告警", "level": "fail",
                      "text": f"连续失败 {al.get('failStreak',0)} 轮 / 零增长 {al.get('zeroStreak',0)} 轮",
                      "hint": al.get("notifyResult", "")})
    worst = "fail" if any(i["level"] == "fail" for i in items) else ("warn" if any(i["level"] == "warn" for i in items) else "ok")
    return {"level": worst, "items": items}



# ---------- 账号 / 二维码 / 审计 / 上游 ----------
AUDIT = os.path.join(LOTTERY, "logs/panel-audit.log")
QR = os.path.join(LOTTERY, "bilibili-login-qr.png")


def audit(ip, action, extra=""):
    try:
        os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
        try:
            if os.path.getsize(AUDIT) > 1_000_000:
                os.replace(AUDIT, AUDIT + ".1")
        except Exception:
            pass
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{ip}\t{action}\t{extra}\n")
    except Exception:
        pass


def accounts():
    """解析 env.js 的 account_parm：账号数、备注、UID、开关。Cookie 一律不外传。"""
    txt = rd(os.path.join(LOTTERY, "env.js"))
    out = []
    for m in re.finditer(r"COOKIE:\s*['\"](.+?)['\"]", txt, re.S):
        cookie = m.group(1)
        tail = txt[m.end():m.end() + 900]
        note = (re.search(r"NOTE:\s*['\"]([^'\"]*)['\"]", tail) or [None, ""])[1]
        num = (re.search(r"NUMBER:\s*(\d+)", tail) or [None, ""])[1]
        clear = (re.search(r"CLEAR:\s*(true|false)", tail) or [None, ""])[1]
        if len(cookie) < 50:      # MULTIPLE_ACCOUNT_PARM 里的示例是空串，跳过
            continue
        uid = (re.search(r"DedeUserID=(\d+)", cookie) or [None, ""])[1]
        info = _bili_cookie_info(cookie)
        out.append({"number": num, "note": note, "uid": uid, "clear": clear,
                    "cookieLen": len(cookie), "hasBiliJct": "bili_jct=" in cookie, **info})
    flags = {}
    for k in ("ENABLE_MULTIPLE_ACCOUNT", "ENABLE_AI_JUDGE", "ENABLE_AI_COMMENTS",
              "ENABLE_CHAT_CAPTCHA_OCR", "LOTTERY_LOG_LEVEL", "NOT_GO_LOTTERY"):
        m = re.search(rf"{k}:\s*([^,\n]+)", txt)
        flags[k] = (m.group(1).strip() if m else "")
    return {"accounts": out, "flags": flags, "multiple": "true" in flags.get("ENABLE_MULTIPLE_ACCOUNT", "")}


def upstream_info():
    src = "/app/src"
    if not os.path.isdir(os.path.join(src, ".git")):
        return {"ok": False}
    def g(args):
        return shq(["git", "-C", src] + args, 20).strip()
    return {"ok": True, "head": g(["log", "-1", "--format=%h %ad %s", "--date=short"]),
            "branch": g(["branch", "--show-current"]),
            "remote": g(["remote", "get-url", "origin"]),
            "dirty": bool(g(["status", "--porcelain"]))}

# ---------- HTTP ----------
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "LotteryPanel/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in SEC_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def j(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def authed(self):
        _sessions_prune()
        m = re.search(r"sid=([0-9a-f]+)", self.headers.get("Cookie") or "")
        if not m:
            return False
        with _LOCK:
            exp = SESSIONS.get(m.group(1))
        return bool(exp and exp > time.time())

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def deny(self):
        if not ip_allowed(client_ip(self)):
            self._send(403, "403 只允许内网访问", "text/plain; charset=utf-8")
            return True
        return False

    # ---- GET ----
    def do_GET(self):
        p = self.path.split("?")[0]
        if self.deny():
            return
        if p in ("/", "/index.html", "/login"):
            return self._send(200, rd(os.path.join(BASE, "index.html"), "<h1>missing index.html</h1>"),
                              "text/html; charset=utf-8")
        if not self.authed():
            return self.j(401, {"error": "未登录"})

        if p == "/panel/status":
            ent = cron_entries()
            return self.j(200, {"container": docker_state(), "dyids": len(dyid_list()),
                                "info": lottery_info_counts(), "schedule": ent, "next": _next_runs(ent),
                                "version": VERSION, "uptime": int(time.time() - START_TS),
                                "now": time.strftime("%Y-%m-%d %H:%M:%S")})
        if p == "/panel/system":
            return self.j(200, sys_payload())
        if p == "/panel/stats":
            return self.j(200, stats_payload())
        if p == "/panel/samples":
            with _LOCK:
                return self.j(200, {"cpu": CPU_SAMPLES[-60:], "mem": MEM_SAMPLES[-60:]})
        if p == "/panel/logs":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            name = q.get("file", "lottery-run.log")
            if "/" in name or ".." in name:
                return self.j(400, {"error": "非法文件名"})
            try:
                lines = max(1, min(int(q.get("lines", "400") or 400), 5000))
            except ValueError:
                lines = 400
            txt = rd(os.path.join(LOTTERY, "logs", name))
            grep = (q.get("grep") or "").strip()
            if grep:
                hits = [ln for ln in txt.splitlines() if grep in ln]
                return self.j(200, {"file": name, "text": "\n".join(hits[-lines:]), "size": len(txt),
                                    "hits": len(hits), "files": []})
            logdir = os.path.join(LOTTERY, "logs")
            return self.j(200, {"file": name, "text": "\n".join(txt.splitlines()[-lines:]),
                                "size": len(txt), "mtime": time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(os.path.join(logdir, name)))) if os.path.exists(os.path.join(logdir, name)) else "",
                                "files": sorted([f for f in os.listdir(logdir) if f.endswith(".log")]) if os.path.isdir(logdir) else []})
        if p == "/panel/config":
            return self.j(200, {"my_config": rd(os.path.join(LOTTERY, "my_config.js")),
                                "env": re.sub(r"(?i)((?:cookie|token|key|pass|secret)\w*)(\s*[:=]\s*)(['\"])([^'\"]{0,400})\3",
                                              lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}***{m.group(3)}",
                                              rd(os.path.join(LOTTERY, "env.js")))})
        if p == "/panel/data":
            ids = dyid_list()
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            qq = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            needle = urllib.parse.unquote(qq.get("q", "")).strip()
            if needle:
                hit = [x for x in ids if needle in x]
                return self.j(200, {"q": needle, "hits": len(hit), "sample": hit[-40:][::-1]})
            return self.j(200, {"dyids": len(ids), "sample": ids[-20:][::-1], "info": lottery_info_counts(),
                                "size": len(rd(os.path.join(LOTTERY, "data/dyids/dyid.txt")))})
        if p == "/panel/backups":
            d = os.path.join(LOTTERY, "backups")
            os.makedirs(d, exist_ok=True)
            files = []
            for n in sorted(os.listdir(d), reverse=True):
                fp = os.path.join(d, n)
                files.append({"name": n, "size": os.path.getsize(fp),
                              "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(fp)))})
            return self.j(200, {"backups": files[:60]})
        if p == "/panel/config/form":
            f = cfg_fields()
            return self.j(200, {"ok": f.get("ok", False), "why": f.get("why", ""),
                                "groups": cfg_grouped(f.get("fields", [])) if f.get("ok") else [],
                                "count": len(f.get("fields", []))})
        if p == "/panel/notify":
            return self.j(200, notify_view())
        if p == "/panel/wins.csv":
            rows = [["时间", "级别", "标签", "内容", "动态链接"]]
            for w in winners():
                mid = re.search(r"(\d{18,20})", w["msg"] or "")
                rows.append([w["time"], w["level"], w["tag"], (w["msg"] or "").replace("\n", " ").replace("\r", ""),
                             ("https://t.bilibili.com/" + mid.group(1)) if mid else ""])
            buf = "\ufeff" + "\r\n".join(",".join('"' + str(c).replace('"', '""') + '"' for c in r) for r in rows)
            data = buf.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="wins.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/panel/checkup":
            return self.j(200, checkup())
        if p == "/panel/accounts":
            return self.j(200, accounts())
        if p == "/panel/upstream":
            return self.j(200, upstream_info())
        if p == "/panel/audit":
            lines = rd(AUDIT).splitlines()[-120:][::-1]
            return self.j(200, {"lines": lines})
        if p == "/panel/qr":
            if not os.path.exists(QR):
                return self.j(404, {"error": "还没有二维码，先点「获取登录二维码」"})
            return self._send(200, open(QR, "rb").read(), "image/png")
        if p == "/panel/download":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            rel = q.get("file", "")
            allowed = ["data/dyids/dyid.txt", "my_config.js"]
            logdir = os.path.join(LOTTERY, "logs")
            if os.path.isdir(logdir):
                allowed += ["logs/" + f for f in os.listdir(logdir) if f.endswith(".log")]
            if rel not in allowed:
                return self.j(400, {"error": "不允许的文件"})
            fp = os.path.join(LOTTERY, rel)
            if not os.path.exists(fp):
                return self.j(404, {"error": "文件不存在"})
            data = open(fp, "rb").read()
            name = os.path.basename(rel)
            return self._send(200, data, "application/octet-stream",
                              [("Content-Disposition", f'attachment; filename="{name}"')])
        return self.j(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self):
        p = self.path.split("?")[0]
        ip = client_ip(self)
        if self.deny():
            return
        if p == "/login":
            wait = login_blocked(ip)
            if wait:
                return self.j(429, {"error": f"失败次数过多，请 {wait} 秒后再试"})
            d = self.body_json()
            if str(d.get("password") or "") != password():
                login_fail(ip)
                left = MAX_FAILS - len([t for t in FAILS.get(ip, []) if time.time() - t < FAIL_WINDOW])
                return self.j(401, {"error": "密码错误", "left": max(left, 0)})
            with _LOCK:
                FAILS.pop(ip, None)
            sid = secrets.token_hex(16)
            with _LOCK:
                SESSIONS[sid] = time.time() + 7 * 86400
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8",
                              [("Set-Cookie", f"sid={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={7*86400}")])

        if not self.authed():
            return self.j(401, {"error": "未登录"})
        d = self.body_json()
        audit(ip, p.replace("/panel/", ""), json.dumps(d, ensure_ascii=False)[:120])

        if p == "/panel/run":
            return self.j(200, {"ok": True, "msg": "已触发一轮抽奖（后台运行）"}) if spawn("run.sh") else self.j(400, {"error": "缺少 run.sh"})
        if p == "/panel/stop":
            return self.j(200, {"ok": True, "msg": "已停止", "out": shq(["docker", "stop", CONTAINER], 120)[-200:]})
        if p == "/panel/rebuild":
            return self.j(200, {"ok": True, "msg": "已开始重建（3–5 分钟）"}) if spawn("start.sh") else self.j(400, {"error": "缺少 start.sh"})
        if p == "/panel/sync":
            return self.j(200, {"ok": True, "msg": "已开始同步上游（后台，3–10 分钟）"}) if spawn("sync-and-update.sh") else self.j(400, {"error": "缺少 sync-and-update.sh"})
        if p == "/panel/clear":
            days = str(d.get("days", "")).strip()
            ok = spawn("clear.sh", ([days] if days else []) + ["--no-wait"])
            return self.j(200, {"ok": True, "msg": f"已开始清理动态（{days or '默认'} 天，--no-wait）"}) if ok else self.j(400, {"error": "缺少 clear.sh"})
        if p == "/panel/notify":
            conf = notify_conf()
            for k, v in (d.get("channels") or {}).items():
                cur = conf.get(k, {})
                cur["enabled"] = bool(v.get("enabled"))
                for fk, fv in (v.get("fields") or {}).items():
                    if str(fv).strip():
                        cur[fk] = str(fv).strip()
                conf[k] = cur
            notify_save(conf)
            return self.j(200, {"ok": True, "msg": "通知设置已保存", **notify_view()})
        if p == "/panel/maintain":
            if not d.get("confirm"):
                return self.j(400, {"error": "需要 confirm"})
            m = maintain()
            return self.j(200, {"ok": True, "msg": f"已清理：镜像悬空层、备份 {m['backups']} 个、日志 {m['logs']} 个",
                                **m})
        if p == "/panel/summary/preview":
            rr = send_notify(_summary_text())
            if not rr:
                return self.j(200, {"ok": False, "error": "没有启用的通知渠道"})
            ok = any(r["ok"] for r in rr)
            detail = "; ".join(f"{r['name']}:{'ok' if r['ok'] else 'fail'}" for r in rr)
            return self.j(200, {"ok": ok, "msg": ("摘要已发送 —— " + detail) if ok else ("发送失败 —— " + detail),
                                "text": _summary_text()})
        if p == "/panel/summary":
            conf = notify_conf()
            conf.setdefault("summary", {})["enabled"] = bool(d.get("enabled"))
            notify_save(conf)
            return self.j(200, {"ok": True, "msg": "已" + ("开启" if d.get("enabled") else "关闭") + "每轮摘要推送"})
        if p == "/panel/notify/test":
            key = d.get("key")
            res = send_notify(f"【B站抽奖】通知测试 · {time.strftime('%m-%d %H:%M')} · 面板 v{VERSION}", only=key)
            if not res:
                return self.j(200, {"ok": False, "error": "该渠道未配置"})
            r = res[0]
            return self.j(200, {"ok": r["ok"], "msg": ("已发送（" + r["name"] + "）") if r["ok"] else ("失败：" + r["out"])})
        if p == "/panel/cleanup":
            return self.j(200, {"ok": True, "msg": "已开始清理过期数据"}) if spawn("cleanup-expired.sh") else self.j(400, {"error": "缺少 cleanup-expired.sh"})
        if p == "/panel/wcheck":
            return self.j(200, {"ok": True, "msg": "已触发中奖检测（后台）"}) if spawn("winner-check.sh") else self.j(400, {"error": "缺少 winner-check.sh"})

        if p == "/panel/config":
            text = str(d.get("my_config") or "")
            if not text.strip():
                return self.j(400, {"error": "内容为空"})
            path = os.path.join(LOTTERY, "my_config.js")
            tmp = path + ".check.js"
            wr(tmp, text)
            chk = node_check(tmp)
            if "SyntaxError" in chk or "ERR_" in chk:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                return self.j(400, {"error": "语法检查未通过", "detail": chk[-600:]})
            try:
                shutil.copy2(path, path + ".bak." + time.strftime("%Y%m%d_%H%M%S"))
            except Exception:
                pass
            os.replace(tmp, path)
            return self.j(200, {"ok": True, "msg": "已保存（下一轮生效），旧文件已备份"})

        if p == "/panel/config/form":
            changes = d.get("changes") or {}
            if not isinstance(changes, dict) or not changes:
                return self.j(400, {"error": "没有要保存的改动"})
            ok, msg, detail = cfg_apply({k: str(v) for k, v in changes.items()})
            return self.j(200 if ok else 400, {"ok": ok, "msg": msg, "error": None if ok else msg, "detail": detail})

        if p == "/panel/schedule":
            try:
                cron_write(d.get("entries") or [])
            except Exception as e:
                return self.j(500, {"error": f"写入失败：{e}"})
            return self.j(200, {"ok": True, "entries": cron_entries(), "msg": "定时已写入 /etc/cron.d/lottery-panel"})

        if p == "/panel/backup":
            d0 = os.path.join(LOTTERY, "backups")
            os.makedirs(d0, exist_ok=True)
            name = "config-" + time.strftime("%Y%m%d_%H%M%S") + ".tar.gz"
            what = str(d.get("what") or "config")
            if what == "data":
                files = [x for x in ("data", "my_config.js", "env.js") if os.path.exists(os.path.join(LOTTERY, x))]
                name = "data-" + time.strftime("%Y%m%d_%H%M%S") + ".tar.gz"
            else:
                files = [f for f in ("my_config.js", "env.js", "account.json") if os.path.exists(os.path.join(LOTTERY, f))]
            shq(["tar", "czf", os.path.join(d0, name), "-C", LOTTERY] + files, 120)
            ok = os.path.exists(os.path.join(d0, name))
            return self.j(200 if ok else 500, {"ok": ok, "name": name, "msg": "已备份 " + name if ok else "备份失败"})
        if p == "/panel/restore":
            name = str(d.get("name") or "")
            if "/" in name or ".." in name:
                return self.j(400, {"error": "非法文件名"})
            src = os.path.join(LOTTERY, "backups", name)
            if not os.path.exists(src):
                return self.j(404, {"error": "备份不存在"})
            stamp = time.strftime("%Y%m%d_%H%M%S")
            for f in ("my_config.js", "env.js", "account.json"):
                p0 = os.path.join(LOTTERY, f)
                if os.path.exists(p0):
                    shutil.copy2(p0, p0 + f".prerestore.{stamp}")
            shq(["tar", "xzf", src, "-C", LOTTERY], 120)
            return self.j(200, {"ok": True, "msg": "已恢复（恢复前文件另存 .prerestore.*）"})
        if p == "/panel/password":
            new = str(d.get("password") or "")
            if len(new) < 4:
                return self.j(400, {"error": "密码至少 4 位"})
            set_password(new)
            return self.j(200, {"ok": True, "msg": "已修改（下次登录生效）"})

        if p == "/panel/notify-test":
            if not os.path.exists(os.path.join(LOTTERY, "push-run-report.sh")):
                return self.j(400, {"error": "缺少 push-run-report.sh"})
            sender = os.environ.get("QQ_SEND_SCRIPT", "/hscripts/qq_ws_send.py")
            if not os.path.exists(sender):
                return self.j(400, {"error": f"缺少 {sender}（需把 /root/.hermes/scripts 挂到 /hscripts）"})
            out = shq(["python3", sender, "【B站抽奖】面板推送通道测试 ✅ " + time.strftime("%m-%d %H:%M")], 60)
            return self.j(200, {"ok": True, "msg": "已发送测试消息（看 QQ）", "out": out[-300:]})
        if p == "/panel/qr/refresh":
            if not os.path.exists(os.path.join(LOTTERY, "login.sh")):
                return self.j(400, {"error": "缺少 login.sh"})
            subprocess.Popen(["/bin/bash", os.path.join(LOTTERY, "login.sh")], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, cwd=LOTTERY)
            return self.j(200, {"ok": True, "msg": "已拉起重登容器，约 15–30 秒后二维码刷新；扫完记得确认"})
        if p == "/panel/selfupdate":
            img = "lottery-panel:" + VERSION
            panel_dir = os.environ.get("PANEL_HOST_DIR", "/root/lottery-panel")
            subprocess.Popen(["docker", "run", "-d", "--rm", "--name", "panel-updater",
                              "-v", "/var/run/docker.sock:/var/run/docker.sock",
                              "-v", panel_dir + ":/src",
                              img, "bash", "/src/recreate.sh", img],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return self.j(200, {"ok": True, "msg": "已在后台重建（build → 替换容器），本页 30 秒后会短暂断开"})
        return self.j(404, {"error": "not found"})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        import sys
        if sys.exc_info()[0] in (BrokenPipeError, ConnectionResetError):
            return
        super().handle_error(request, client_address)


def sampler():
    while True:
        try:
            sys_payload()
        except Exception:
            pass
        try:
            snapshots_touch()
        except Exception:
            pass
        try:
            alert_tick()
        except Exception:
            pass
        time.sleep(60)


def _ensure_cert():
    """生成自签证书（10 年），供 HTTPS 端口用。"""
    crt = os.path.join(LOTTERY, "panel.crt")
    key = os.path.join(LOTTERY, "panel.key")
    if os.path.exists(crt) and os.path.exists(key):
        return crt, key
    try:
        shq(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
             "-subj", "/CN=lottery-panel", "-keyout", key, "-out", crt], 60)
    except Exception:
        pass
    return (crt, key) if os.path.exists(crt) else (None, None)


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    port = int(os.environ.get("PANEL_PORT", "5311"))
    srv = Server(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"LotteryAutoScript 控制台 {VERSION} → http://0.0.0.0:{port}", flush=True)

    tls_port = int(os.environ.get("PANEL_TLS_PORT", "5312"))
    crt, key = _ensure_cert()
    if crt:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        tsrv = Server(("0.0.0.0", tls_port), Handler)
        tsrv.socket = ctx.wrap_socket(tsrv.socket, server_side=True)
        print(f"HTTPS 已启用 → https://0.0.0.0:{tls_port}", flush=True)
        tsrv.serve_forever()
    else:
        srv.serve_forever()
