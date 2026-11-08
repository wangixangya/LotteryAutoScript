#!/usr/bin/env python3
"""LotteryAutoScript 控制台 v2.1 —— 单文件、零依赖（只用标准库）。

设计要点
- LotteryAutoScript 是一次性 Node 脚本（跑一轮就退出），没有 API；所以全部通过
  docker CLI + 文件读写 + 日志抓取来做，不假设它常驻。
- 面板自己常驻（Docker 容器），定时任务写 /etc/cron.d/lottery-panel（挂载宿主 /etc/cron.d），
  由宿主 cron 执行，面板重启不影响调度。
- 可视化数据来源：日志解析（每轮/每中奖）+ 每日快照（panel-stats.json）+ 内存采样（CPU/内存曲线）。
"""
import glob, http.server, ipaddress, json, os, re, shutil, socketserver, subprocess
import threading, time, secrets

BASE = os.path.dirname(os.path.abspath(__file__))
LOTTERY = os.environ.get("LOTTERY_DIR", "/app/lottery")                # 容器内路径
LOTTERY_HOST = os.environ.get("LOTTERY_HOST", "/root/lottery-docker")  # docker -v 必须用宿主路径
CROND = os.environ.get("CROND_DIR", "/etc/cron.d")
CRON_FILE = os.path.join(CROND, "lottery-panel")
CONF = os.path.join(BASE, "panel.conf")
STATS = os.path.join(LOTTERY, "panel-stats.json")
HOST_ROOT = os.environ.get("HOST_ROOT", "/host")   # 宿主根目录只读挂载，用于读主机名/发行版/磁盘
CONTAINER = "lottery-auto-script"
NODE_IMAGE = "node:lts-alpine"
VERSION = "2.1.0"

SESSIONS = {}
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
    m = re.search(r"^PANEL_PASSWORD=(.*)$", rd(CONF), re.M)
    return (m.group(1).strip() if m else "admin") or "admin"


def set_password(new):
    txt = rd(CONF, "PANEL_PASSWORD=admin\n")
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


def winners():
    txt = rd(os.path.join(LOTTERY, "logs/winner-check.log"))
    out, seen = [], set()
    for m in re.finditer(r"\[(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)[^\]]*\]\s*\[(Notice|Info)\]\s*\[([^\]]*)\]\s*\[([^\]]{0,120})", txt):
        date, tm, lvl, tag, msg = m.groups()
        key = (date, tm, msg)
        if key in seen:
            continue
        # 嵌套数组/对象（如 at_users: [[名字, uid], ...]）表单改不了，交给源码模式
        if _kind(raw) == "list" and ("[" in raw[1:] or "{" in raw):
            continue
        if ("可能中奖" in tag) or ("可能中奖" in msg) or ("中奖了" in msg) or ("恭喜" in msg):
            seen.add(key)
            out.append({"time": f"{date} {tm}", "level": lvl, "tag": tag, "msg": msg[:120]})
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
        fields.append({"key": key, "raw": raw, "kind": kind, "line": i})
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

# ---------- 账号 / 二维码 / 审计 / 上游 ----------
AUDIT = os.path.join(LOTTERY, "logs/panel-audit.log")
QR = os.path.join(LOTTERY, "bilibili-login-qr.png")


def audit(ip, action, extra=""):
    try:
        os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
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
        out.append({"number": num, "note": note, "uid": uid, "clear": clear,
                    "cookieLen": len(cookie), "hasBiliJct": "bili_jct=" in cookie})
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
            return self.j(200, {"container": docker_state(), "dyids": len(dyid_list()),
                                "info": lottery_info_counts(), "schedule": cron_entries(),
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
        time.sleep(60)


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    port = int(os.environ.get("PANEL_PORT", "5311"))
    srv = Server(("0.0.0.0", port), Handler)
    print(f"LotteryAutoScript 控制台 {VERSION} → http://0.0.0.0:{port}", flush=True)
    srv.serve_forever()
