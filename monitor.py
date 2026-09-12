#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资产低水位监控器 asset-monitor v1
================================
数据源(公网HTTP，本机与云端均可访问):
  - 腾讯 ifzq.gtimg.cn   A股/港股/美股(带交易所后缀)/ETF 日K(前复权)
  - eastmoney push2      期货/美股指数/汇率 实时
  - gate.io              BTC/ETH 日K
分层:
  §1 配置与状态   §2 数据源适配(统一Bars)  §3 指标(纯函数)
  §4 信号+冷却    §5 通知                  §6 看板渲染
  §7 回测         §8 工具(原子写/flock/日志截断)
§3/§4 为环境无关纯逻辑，供 v2 云端版平移；Mac 特有能力只出现在 §5/§6/§8。
用法:
  monitor.py run [--dry-run]      # 常规运行(launchd 每30分钟调一次)
  monitor.py doctor               # 逐资产体检
  monitor.py notify --test [--wechat]
  monitor.py render               # 仅重新渲染看板
  monitor.py backtest [--sweep "dd=.. rsi=.."] [--report out.md]
"""
import argparse
import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import warnings

# 本机 urllib3+LibreSSL 每次请求都打警告；必须在 import requests 之前注册过滤，
# 否则 urllib3 首次导入时那一条仍会漏进 launchd 日志
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")
try:
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except Exception:
    pass

import requests
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOCK_PATH = os.path.join(BASE_DIR, "state.lock")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.html")
LOG_PATH = os.path.expanduser("~/Library/Logs/asset-monitor.log")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_6)"})
# 本机系统代理(127.0.0.1:7890)时开时关；所有数据源均已验证可直连，禁用代理感知
# 让行为确定化：Clash 不在时监控照常工作
SESSION.trust_env = False

# 本机 Clash TUN 会把系统 DNS 劫持成 fake-ip（198.18.x），sctapi 的流量进 Clash 后
# 反而出不去（直连与走代理都超时）。把通知域名钉到真实 IP：只改连接目标，
# TLS SNI/证书校验仍是原域名，安全语义不变。IP 漂移时改 config "dns_pin" 覆盖。
DEFAULT_DNS_PINS = {"sctapi.ftqq.com": "82.157.177.201"}
_DNS_PIN_ORIG = None


def install_dns_pins(extra=None):
    """把指定域名的 getaddrinfo 结果钉到真实IP（幂等，进程内只装一次）。"""
    global _DNS_PIN_ORIG
    if _DNS_PIN_ORIG is not None:
        return
    table = dict(DEFAULT_DNS_PINS)
    if extra:
        table.update(extra)
    if not table:
        return
    _DNS_PIN_ORIG = socket.getaddrinfo

    def _pinned(host, *args, **kwargs):
        try:
            if isinstance(host, str):
                host = table.get(host, host)
        except Exception:
            pass
        return _DNS_PIN_ORIG(host, *args, **kwargs)

    socket.getaddrinfo = _pinned

COND_NAMES = {"s1": "深度回撤", "s2": "RSI超卖", "s3": "跌破MA200", "s4": "3年低百分位"}
GROUP_ORDER = ["A股宽基", "行业ETF", "港股", "美股", "商品", "加密", "自选", "观察位"]


# =====================================================================
# §8 工具
# =====================================================================

def now_in(tz_name):
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now(timezone(timedelta(hours=8)))


def iso(dt):
    return dt.isoformat(timespec="seconds")


def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def tz_aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt


def fmt_price(p):
    if p is None:
        return "-"
    if p >= 1000:
        return "{:,.0f}".format(p)
    if p >= 100:
        return "{:.1f}".format(p)
    if p >= 10:
        return "{:.2f}".format(p)
    return "{:.3f}".format(p)


def fmt_pct(x, signed=False):
    if x is None:
        return "-"
    return "{:+.1f}%".format(x) if signed else "{:.1f}%".format(x)


def fmt_rsi(x):
    return "%.0f" % x if x is not None else "-"


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%m-%d %H:%M:%S"), msg), flush=True)


def save_state_atomic(state):
    tmp = STATE_PATH + ".tmp"
    bak = STATE_PATH + ".bak"
    if os.path.exists(STATE_PATH):
        try:
            os.replace(STATE_PATH, bak)
        except Exception:
            pass
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)


def truncate_log():
    """launchd 的 stdout fd 是 O_APPEND 且不会轮转：>10MB 时原地截断保末尾200KB。
    绝不能 rename 轮转（写入会继续跟到改名后的旧文件）。"""
    try:
        if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) < 10 * 1024 * 1024:
            return
        with open(LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200 * 1024))
            tail = f.read()
        with open(LOG_PATH, "r+b") as f:
            f.seek(0)
            f.write(tail)
            f.truncate()
        log("日志已原地截断")
    except Exception:
        pass


def http_get(url, timeout=15, retries=1, backoff=3):
    last = None
    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, timeout=timeout)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.json() if r.text.strip().startswith(("{", "[")) else r.text
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(backoff)
    raise RuntimeError("GET %s 失败: %s" % (url.split("?")[0], last))


# =====================================================================
# §1 配置与状态
# =====================================================================

def validate_config(cfg):
    """config 结构校验：手机端网页可写 config.json，坏数据不能弄挂监控。"""
    if not isinstance(cfg, dict):
        return "config 不是对象"
    assets = cfg.get("assets")
    if not isinstance(assets, list) or not assets:
        return "assets 缺失或为空"
    seen = set()
    for a in assets:
        if not isinstance(a, dict) or not a.get("id") or not a.get("name"):
            return "存在缺 id/name 的资产: %r" % (a,)
        if a["id"] in seen:
            return "资产 id 重复: %s" % a["id"]
        seen.add(a["id"])
    return None


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 密钥与 config 分离：仓库里的 config.json 是公开的，真实渠道密钥放
    # 本机 secrets.json（gitignore），存在则整体替换 channels
    spath = os.path.join(BASE_DIR, "secrets.json")
    if os.path.exists(spath):
        try:
            with open(spath, "r", encoding="utf-8") as f:
                sec = json.load(f)
            if sec.get("channels"):
                cfg["channels"] = sec["channels"]
        except Exception as e:
            log("secrets.json 读取失败(忽略): %s" % e)
    err = validate_config(cfg)
    if err:
        raise ValueError("config.json 结构异常: %s" % err)
    return cfg


def sync_config():
    """从 GitHub 拉最新 config（手机端网页提交的增删资产）。
    先校验远端 config 再合并；失败只记日志，沿用本地，绝不弄挂监控。"""
    if not os.path.isdir(os.path.join(BASE_DIR, ".git")):
        return
    try:
        def git(*args):
            r = subprocess.run(["git", "-C", BASE_DIR] + list(args),
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip()[:200])
            return r.stdout.strip()

        git("fetch", "-q", "origin", "main")
        head = git("rev-parse", "HEAD")
        remote = git("rev-parse", "origin/main")
        if head == remote:
            return  # 无更新
        # 只允许快进：本地对 tracked 文件无改动时才合并
        dirty = git("status", "--porcelain", "--", "config.json", "monitor.py",
                    "edit.html", "README.md")
        if dirty:
            log("config同步跳过: 本地 tracked 文件有未提交改动")
            return
        remote_cfg_raw = subprocess.run(
            ["git", "-C", BASE_DIR, "show", "origin/main:config.json"],
            capture_output=True, text=True, timeout=30).stdout
        remote_cfg = json.loads(remote_cfg_raw)
        err = validate_config(remote_cfg)
        if err:
            log("远端 config 校验失败(沿用本地): %s" % err)
            return
        git("merge", "--ff-only", "-q", "origin/main")
        log("config 已同步远端更新: %s" % remote[:7])
    except Exception as e:
        log("config 同步失败(沿用本地): %s" % e)


def default_state():
    return {
        "version": 1,
        "last_run": None,
        "digest_date": None,
        "quota": {"date": None},
        "assets": {},          # id -> 每资产运行状态
        "global_alert_log": [],
    }


def load_state():
    for p in (STATE_PATH, STATE_PATH + ".bak"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                st = json.load(f)
            if not isinstance(st, dict):
                continue
            st.setdefault("quota", {"date": None})
            st.setdefault("global_alert_log", [])
            st.setdefault("assets", {})
            return st
        except Exception:
            continue
    return default_state()


def default_asset_state():
    return {
        "last_bar_date": None, "last_price": None, "chg_pct": None,
        "closes_cache": [], "indicators": {}, "conditions": {},
        "water": 0, "signal_on": False, "off_bars": 0,
        "last_alert_ts": None, "last_near_ts": None,
        "consec_fails": 0, "last_error": None, "last_success_ts": None,
        "alert_history": [],
    }


def sync_assets(state, config):
    """state 资产键与 config 对齐：删掉已移除的，初始化新增的。"""
    valid = set(a["id"] for a in all_signal_assets(config))
    valid |= set("watch:" + w["id"] for w in config.get("watch_assets", []))
    for k in list(state["assets"].keys()):
        if k not in valid:
            del state["assets"][k]
    for k in valid:
        if k not in state["assets"]:
            state["assets"][k] = default_asset_state()


def all_signal_assets(config):
    return config.get("assets", []) + config.get("watchlist", [])


# =====================================================================
# §2 数据源适配 —— 统一 Bars: [date, open, close, high, low, vol, closed]
# =====================================================================

def fetch_tencent_kline(code, n=800, start=None, end=None):
    if start and end:
        param = "%s,day,%s,%s,%d,qfq" % (code, start, end, n)
    else:
        param = "%s,day,,,%d,qfq" % (code, n)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + param
    j = http_get(url)
    d = (j.get("data") or {}).get(code) or {}
    rows = d.get("qfqday") or d.get("day") or []
    bars = []
    for r in rows:
        # 腾讯列序: [date, open, close, high, low, volume]
        bars.append([r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), True])
    if not bars:
        raise RuntimeError("腾讯K线为空: " + code)
    return bars


def mark_tencent_today(bars, tz_name):
    """腾讯盘中最后一根是当日未完成bar(其close=实时价)；按资产时区判断。"""
    today = now_in(tz_name).strftime("%Y-%m-%d")
    if bars[-1][0] == today:
        bars[-1][6] = False
    return bars


def fetch_gateio_kline(pair, n=800, from_ts=None, to_ts=None):
    def _get(endpoint):
        url = ("https://api.gateio.ws/api/v4/spot/%s?currency_pair=%s&interval=1d&limit=%d"
               % (endpoint, pair, min(n, 1000)))
        if from_ts and to_ts:
            url += "&from=%d&to=%d" % (from_ts, to_ts)
        return http_get(url)
    try:
        rows = _get("candlesticks")
    except Exception:
        rows = _get("candles")
    bars = []
    for r in rows:
        # gate 列序: [ts秒, quote_vol, close, high, low, open, base_vol, closed_str]
        ts = int(r[0])
        d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        closed = str(r[7]).lower() == "true"
        bars.append([d, float(r[5]), float(r[2]), float(r[3]), float(r[4]), float(r[6]), closed])
    if not bars:
        raise RuntimeError("gate.io K线为空: " + pair)
    bars.sort(key=lambda b: b[0])
    return bars


EM_HOSTS = ["push2.eastmoney.com", "push2delay.eastmoney.com"]  # 主域偶发整体失效，镜像兜底


def em_api(path, retries=2):
    """东财 push2 API 通用入口：双主机×重试（主域会随机掐连接）。"""
    last = None
    for _ in range(retries):
        for host in EM_HOSTS:
            try:
                return http_get("https://%s%s" % (host, path))
            except Exception as e:
                last = e
        time.sleep(1)
    raise RuntimeError("eastmoney api 失败: %s" % last)


def fetch_eastmoney_flow(secid, days=20):
    """东财日级主力资金流。klines 列：日期,主力,小单,中单,大单,超大单(净额,元),
    主力%,小%,中%,大%,超大%,收盘,涨跌幅,…；最后一根为当日盘中实时值。
    历史在 push2his（本网络不可达，每轮仍先试，恢复即自动补全）；
    兜底走 push2 当日快照(仅1行)，历史由 state 按日积累。返回行(旧→新)。"""
    hosts = ["push2his.eastmoney.com"] + EM_HOSTS
    for host in hosts:
        try:
            d = (http_get("https://%s/api/qt/stock/fflow/daykline/get?lmt=0&klt=101"
                          "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,"
                          "f60,f61,f62,f63,f64,f65&secid=%s" % (host, secid), timeout=8)
                 .get("data") or {})
            rows = []
            for k in d.get("klines") or []:
                p = k.split(",")
                try:
                    rows.append({"date": p[0], "main": float(p[1]), "main_pct": float(p[6])})
                except (ValueError, IndexError):
                    continue
            if rows:
                return rows[-days:]
        except Exception:
            continue
    return []


def fetch_eastmoney_industries():
    """东财行业板块行情(约400+细分级)，按涨跌幅降序。字段 f3涨跌% f62主力净额
    f104/f105 涨/跌家数 f128 领涨股。返回 [{"n","c","m","u","d","l"}]。"""
    seen, out = set(), []
    for pn in (1, 2, 3, 4, 5):
        d = (em_api("/api/qt/clist/get?pn=%d&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
                    "&fs=m:90+t:2+f:!50&fields=f12,f14,f3,f62,f104,f105,f128" % pn)
             .get("data") or {})
        diff = d.get("diff") or []
        if not diff:
            break
        for x in diff:
            nm = x.get("f14")
            if not nm or nm in seen:
                continue
            seen.add(nm)
            out.append({"n": nm, "c": x.get("f3"), "m": x.get("f62"),
                        "u": x.get("f104"), "d": x.get("f105"), "l": x.get("f128")})
        time.sleep(0.15)
    out = [b for b in out if isinstance(b["c"], (int, float))]
    out.sort(key=lambda b: -b["c"])
    return out


def fetch_eastmoney_rt(secid):
    last_err = None
    for host in EM_HOSTS:
        try:
            url = ("https://%s/api/qt/stock/get?secid=%s&fields=f43,f58,f86,f170"
                   % (host, secid))
            j = http_get(url)
            d = j.get("data") or {}
            if d.get("f43") in (None, "-"):
                raise RuntimeError("无数据")
            return {"raw": d["f43"], "name": d.get("f58") or secid,
                    "ts": d.get("f86"), "chg_pct": (d.get("f170") or 0) / 100.0}
        except Exception as e:
            last_err = e
    raise RuntimeError("eastmoney %s 失败: %s" % (secid, last_err))


def fetch_asset_bars(asset, n=800):
    """返回 (bars, live_price)。live 取最后一根bar的close(未完成bar即实时价)。"""
    src = asset["source"]
    if src == "tencent":
        bars = fetch_tencent_kline(asset["id"], n=n)
        bars = mark_tencent_today(bars, asset.get("tz") or "Asia/Shanghai")
    elif src == "gateio":
        bars = fetch_gateio_kline(asset["id"], n=n)
    else:
        raise RuntimeError("未知数据源: " + src)
    return bars, bars[-1][2]


# =====================================================================
# §3 指标 —— 纯函数，输入按时间升序的已完成bar收盘序列
# =====================================================================

def sma_series(closes, n):
    out = [None] * len(closes)
    if len(closes) < n:
        return out
    s = sum(closes[:n])
    out[n - 1] = s / n
    for i in range(n, len(closes)):
        s += closes[i] - closes[i - n]
        out[i] = s / n
    return out


def rsi_series(closes, period=14):
    """Wilder RSI。out[i] = 用 closes[:i+1] 算出的 RSI。"""
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains) / period, sum(losses) / period
    for i in range(period, len(closes)):
        if i > period:
            d = closes[i] - closes[i - 1]
            ag = (ag * (period - 1) + max(d, 0.0)) / period
            al = (al * (period - 1) + max(-d, 0.0)) / period
        if al == 0:
            out[i] = 100.0 if ag > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + ag / al)
    return out


def high_series(closes, n=250, min_len=120):
    """滚动窗口最高close。不足min_len返回None(不构成年线级基准)。"""
    out = [None] * len(closes)
    for i in range(len(closes)):
        if i + 1 < min_len:
            continue
        out[i] = max(closes[max(0, i + 1 - n): i + 1])
    return out


def pct_series(closes, window=750, min_len=480):
    """现价在滚动窗口内的百分位(0-100)。不足min_len返回None。"""
    out = [None] * len(closes)
    for i in range(len(closes)):
        if i + 1 < min_len:
            continue
        w = closes[max(0, i + 1 - window): i + 1]
        cur = closes[i]
        out[i] = 100.0 * sum(1 for x in w if x < cur) / len(w)
    return out


def compute_snapshot(closes):
    """对整条已完成序列算四指标快照(取各序列最后一个值)。"""
    if not closes:
        return {"high250": None, "rsi14": None, "ma200": None, "pct3y": None}
    return {
        "high250": high_series(closes)[-1],
        "rsi14": rsi_series(closes)[-1],
        "ma200": sma_series(closes, 200)[-1],
        "pct3y": pct_series(closes)[-1],
    }


# ---------- 技术分析全家桶(同样为纯函数，只用已完成bar) ----------

def ema_series(closes, n):
    out = [None] * len(closes)
    if len(closes) < n:
        return out
    e = sum(closes[:n]) / n
    out[n - 1] = e
    k = 2.0 / (n + 1)
    for i in range(n, len(closes)):
        e = closes[i] * k + e * (1 - k)
        out[i] = e
    return out


def macd_series(closes, fast=12, slow=26, sig=9):
    """返回 (dif, dea, hist)；柱采用国内口径 hist=2*(DIF-DEA)。"""
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    ln = len(closes)
    dif = [ef[i] - es[i] if ef[i] is not None and es[i] is not None else None for i in range(ln)]
    dea = [None] * ln
    start = slow - 1
    if ln >= start + sig:
        e = sum(dif[start:start + sig]) / sig
        dea[start + sig - 1] = e
        k = 2.0 / (sig + 1)
        for i in range(start + sig, ln):
            e = dif[i] * k + e * (1 - k)
            dea[i] = e
    hist = [None if dif[i] is None or dea[i] is None else 2 * (dif[i] - dea[i])
            for i in range(ln)]
    return dif, dea, hist


def kdj_series(highs, lows, closes, n=9):
    ln = len(closes)
    K, D, J = [None] * ln, [None] * ln, [None] * ln
    k = d = 50.0
    for i in range(ln):
        if i >= n - 1:
            hh, ll = max(highs[i - n + 1:i + 1]), min(lows[i - n + 1:i + 1])
            rsv = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100.0
            k = k * 2.0 / 3.0 + rsv / 3.0
            d = d * 2.0 / 3.0 + k / 3.0
        K[i], D[i], J[i] = k, d, 3 * k - 2 * d
    return K, D, J


def boll_last(closes, n=20, k=2):
    if len(closes) < n:
        return None
    w = closes[-n:]
    m = sum(w) / n
    sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    up, lo = m + k * sd, m - k * sd
    return {"mid": m, "up": up, "lo": lo,
            "pb": None if up == lo else (closes[-1] - lo) / (up - lo),
            "bw": None if m == 0 else (up - lo) / m * 100.0}


def boll_bw_pct(closes, n=20, k=2, look=120):
    """当前带宽在近look个窗口带宽中的分位(检测挤压/变盘窗口)。"""
    if len(closes) < n + 30:
        return None
    look = min(look, len(closes) - n)
    bws = []
    for end in range(len(closes) - look, len(closes)):
        w = closes[end - n + 1:end + 1]
        m = sum(w) / n
        if m == 0:
            continue
        sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
        bws.append(2 * k * sd / m * 100.0)
    if not bws:
        return None
    return 100.0 * sum(1 for x in bws if x < bws[-1]) / len(bws)


def atr_last(highs, lows, closes, n=14):
    """Wilder ATR 占现价百分比(日波动强度)。"""
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a / closes[-1] * 100.0


def compute_tech(bars):
    """输入已完成bar列表，输出可序列化的技术面快照+结论标签。"""
    closes = [b[2] for b in bars]
    highs = [b[3] for b in bars]
    lows = [b[4] for b in bars]
    vols = [b[5] for b in bars]
    n = len(closes)
    out = {"labels": []}
    if n < 35:
        return out

    ma20 = sma_series(closes, 20)[-1]
    ma60 = sma_series(closes, 60)[-1]
    ma200 = sma_series(closes, 200)[-1]
    out["ma20"] = round(ma20, 4) if ma20 else None
    out["ma60"] = round(ma60, 4) if ma60 else None
    out["ma200"] = round(ma200, 4) if ma200 else None
    trend = None
    if ma20 and ma60 and ma200:
        if ma20 > ma60 > ma200:
            trend = "多头排列"
        elif ma20 < ma60 < ma200:
            trend = "空头排列"
        else:
            trend = "均线纠缠"
    elif ma20 and ma60:
        trend = "MA20上方" if closes[-1] > ma20 > ma60 else ("MA20下方" if closes[-1] < ma20 < ma60 else "均线纠缠")
    out["trend"] = trend

    dif, dea, hist = macd_series(closes)
    d_, de_, h_, hp_ = dif[-1], dea[-1], hist[-1], hist[-2]
    macd_label = None
    if None not in (d_, de_, h_):
        out["dif"], out["dea"], out["hist"] = round(d_, 4), round(de_, 4), round(h_, 4)
        cross = None
        for back in range(1, 6):
            i = len(hist) - back
            if i < 1 or hist[i] is None or hist[i - 1] is None:
                break
            if hist[i - 1] <= 0 < hist[i]:
                cross = "MACD金叉%d日" % back
                break
            if hist[i - 1] >= 0 > hist[i]:
                cross = "MACD死叉%d日" % back
                break
        if cross:
            macd_label = cross
        elif hp_ is not None:
            widen = abs(h_) > abs(hp_)
            macd_label = ("红柱扩大" if widen else "红柱收窄") if h_ > 0 else \
                         ("绿柱扩大" if widen else "绿柱收窄")

    K, D, J = kdj_series(highs, lows, closes)
    k_, d_, j_ = K[-1], D[-1], J[-1]
    kdj_label = None
    if k_ is not None:
        out["k"], out["d"], out["j"] = round(k_, 1), round(d_, 1), round(j_, 1)
        if k_ < 20 or j_ < 0:
            kdj_label = "KDJ超卖"
        elif k_ > 80:
            kdj_label = "KDJ超买"
        else:
            for back in range(1, 4):
                i = len(K) - back
                if i < 1:
                    break
                if K[i - 1] <= D[i - 1] and K[i] > D[i]:
                    kdj_label = "KDJ金叉"
                    break
                if K[i - 1] >= D[i - 1] and K[i] < D[i]:
                    kdj_label = "KDJ死叉"
                    break

    bl = boll_last(closes)
    boll_label = None
    if bl:
        out["boll_up"] = round(bl["up"], 4)
        out["boll_mid"] = round(bl["mid"], 4)
        out["boll_lo"] = round(bl["lo"], 4)
        if bl["pb"] is not None:
            out["pb"] = round(bl["pb"], 2)
            if bl["pb"] < 0:
                boll_label = "跌破布林下轨"
            elif bl["pb"] < 0.2:
                boll_label = "贴近布林下轨"
            elif bl["pb"] > 1:
                boll_label = "突破布林上轨"
            elif bl["pb"] > 0.8:
                boll_label = "贴近布林上轨"
        if bl["bw"] is not None:
            out["bw"] = round(bl["bw"], 2)
    bwp = boll_bw_pct(closes)
    if bwp is not None:
        out["bw_pct"] = round(bwp)
        if bwp < 20:
            out["labels"].append("带宽历史低位")

    out["ret20"] = round((closes[-1] / closes[-21] - 1) * 100, 1) if n >= 21 else None
    out["ret60"] = round((closes[-1] / closes[-61] - 1) * 100, 1) if n >= 61 else None
    if n >= 120:
        w = closes[-250:]
        hi, lo = max(w), min(w)
        out["off_high52"] = round((hi - closes[-1]) / hi * 100, 1)
        out["off_low52"] = round((closes[-1] - lo) / lo * 100, 1)
        if hi > lo:
            out["pos52w"] = round((closes[-1] - lo) / (hi - lo) * 100)

    atr = atr_last(highs, lows, closes)
    if atr:
        out["atr_pct"] = round(atr, 2)

    vol_label = None
    if n >= 25 and sum(vols[-20:]) > 0:
        out["vol_ratio"] = round((sum(vols[-5:]) / 5.0) / (sum(vols[-20:]) / 20.0), 2)
        if out["vol_ratio"] >= 1.5:
            vol_label = "放量"
        elif out["vol_ratio"] <= 0.7:
            vol_label = "缩量"

    # --- 成交量分析：量价配合 / OBV方向 / 多空量能比 / 中期量能 ---
    pq_label = obv_label = None
    if n >= 21 and vols[-1] > 0 and vols[-2] > 0:
        r1d = vols[-1] / vols[-2]
        if closes[-1] > closes[-2]:
            pq_label = "量价齐升" if r1d >= 1.1 else "缩量上涨"
        elif closes[-1] < closes[-2]:
            pq_label = "放量下跌" if r1d >= 1.1 else "缩量下跌"
        out["pq"] = pq_label
        out["pq_vol"] = round(r1d, 2)
        obv20 = sum((1 if closes[i] > closes[i - 1] else -1 if closes[i] < closes[i - 1] else 0) * vols[i]
                    for i in range(n - 20, n))
        out["obv20_up"] = obv20 > 0
        obv_label = "OBV上行" if obv20 > 0 else ("OBV下行" if obv20 < 0 else None)
        upv = [vols[i] for i in range(n - 20, n) if closes[i] > closes[i - 1]]
        dnv = [vols[i] for i in range(n - 20, n) if closes[i] < closes[i - 1]]
        if upv and dnv and sum(dnv) > 0:
            out["bull_bear_vol"] = round((sum(upv) / len(upv)) / (sum(dnv) / len(dnv)), 2)
    if n >= 60 and sum(vols[-60:]) > 0:
        out["vol_5_60"] = round((sum(vols[-5:]) / 5.0) / (sum(vols[-60:]) / 60.0), 2)

    out["labels"] = [x for x in (trend, macd_label, kdj_label, boll_label, vol_label,
                                 pq_label, obv_label) if x] + out["labels"]
    return out


# =====================================================================
# §4 信号与冷却 —— 纯逻辑(可平移云端)
# =====================================================================

def thresholds_for(config, asset):
    return config["thresholds_class"][asset.get("class") or "index"]


def evaluate(config, asset, bars, live):
    """返回报告 dict；S1/S3 用实时现价，S2/S4 仅用已完成bar。"""
    th = thresholds_for(config, asset)
    near = config.get("near", {})
    completed = [b for b in bars if b[6]]
    closes = [b[2] for b in completed]
    if len(closes) < 30:
        return {"ok": False, "err": "数据不足(%d根)" % len(closes)}
    snap = compute_snapshot(closes)

    # 涨跌幅基准：有未完成bar → 基准=最后完成bar收盘；全完成 → 基准=倒数第二根
    if not bars[-1][6]:
        prev_close = closes[-1]
    else:
        live = closes[-1]
        prev_close = closes[-2] if len(closes) >= 2 else closes[-1]
    chg = (live - prev_close) / prev_close * 100.0 if prev_close else None

    dd = dev = pct = None
    conds = {"s1": None, "s2": None, "s3": None, "s4": None}
    if snap["high250"]:
        dd = (snap["high250"] - live) / snap["high250"] * 100.0
        conds["s1"] = dd >= th["dd"]
    if snap["rsi14"] is not None:
        conds["s2"] = snap["rsi14"] < th["rsi"]
    if snap["ma200"]:
        dev = (live - snap["ma200"]) / snap["ma200"] * 100.0
        conds["s3"] = dev <= -th["ma_dev"]
    if snap["pct3y"] is not None:
        pct = snap["pct3y"]
        conds["s4"] = pct < th["pct3y"]

    computable = sum(1 for v in conds.values() if v is not None)
    met = sum(1 for v in conds.values() if v is True)
    triggered = computable >= 2 and met >= 2

    nears = []
    if not triggered:
        if dd is not None and conds["s1"] is not None and dd >= th["dd"] - near.get("dd_pp", 2):
            nears.append("回撤" + fmt_pct(dd))
        if snap["rsi14"] is not None and snap["rsi14"] < near.get("rsi", 35):
            nears.append("RSI%.0f" % snap["rsi14"])
        if dev is not None and conds["s3"] is not None and dev <= -(th["ma_dev"] - near.get("ma_pp", 2)):
            nears.append("MA偏离" + fmt_pct(dev, True))
        if pct is not None and conds["s4"] is not None and pct < near.get("pct3y", 30):
            nears.append("分位%.0f%%" % pct)

    water = met * 25 if triggered else met * 25 + len(nears) * 10
    water = min(100, water)

    rep = {
        "ok": True, "live": live, "chg_pct": chg,
        "indicators": {"dd": round(dd, 1) if dd is not None else None,
                       "rsi14": round(snap["rsi14"], 1) if snap["rsi14"] is not None else None,
                       "ma_dev": round(dev, 1) if dev is not None else None,
                       "pct3y": round(snap["pct3y"], 1) if snap["pct3y"] is not None else None,
                       "high250": snap["high250"], "ma200": snap["ma200"]},
        "conditions": conds,
        "computable": computable, "met": met, "triggered": triggered,
        "nears": nears, "water": water,
        "last_bar_date": completed[-1][0] if completed else bars[-1][0],
        "closes_tail": closes[-120:],
        "short_window": len(closes) < 250,
        "tech": compute_tech(completed),
    }
    rep["verdict"] = entry_verdict(rep)
    return rep


def decide_action(st, rep, now, new_bar, repeat_drop_pct=5):
    """冷却状态机(就地更新 st，返回动作 'low'/'near'/None)。
    new_bar: 本轮是否出现上一轮没有的新完成bar(调用方须在更新 last_bar_date 前判断)。
    低水位重复提醒：冷却7天到期 且 价格比上次提醒时又低了 repeat_drop_pct% 才重报
    （否则白酒这类常年趴地的资产会每7天轰炸一次）。首次触发不受价格门槛限制。"""
    cd = timedelta(days=7)
    off_needed = 2
    near_cd = timedelta(hours=72)

    if rep["triggered"]:
        st["off_bars"] = 0
        if not st.get("signal_on"):
            st["signal_on"] = True
            st["last_alert_ts"] = iso(now)
            st["last_alert_price"] = rep["live"]
            return "low"
        last_alert = tz_aware(parse_iso(st.get("last_alert_ts") or ""))
        cooldown_ok = last_alert is None or (tz_aware(now) - last_alert) >= cd
        prev_p = st.get("last_alert_price")
        lower_ok = prev_p is None or rep["live"] <= prev_p * (1 - repeat_drop_pct / 100.0)
        if cooldown_ok and lower_ok:
            st["last_alert_ts"] = iso(now)
            st["last_alert_price"] = rep["live"]
            return "low"
        return None

    if st.get("signal_on"):
        if new_bar:
            st["off_bars"] = st.get("off_bars", 0) + 1
        if st.get("off_bars", 0) >= off_needed:
            st["signal_on"] = False
            st["off_bars"] = 0
        return None

    st["off_bars"] = 0
    if rep["nears"]:
        last_near = tz_aware(parse_iso(st.get("last_near_ts") or ""))
        if last_near is None or (tz_aware(now) - last_near) >= near_cd:
            st["last_near_ts"] = iso(now)
            return "near"
    return None


def entry_verdict(rep):
    """一句话技术面综合结论(纯规则)：低水位状态 × 趋势/止跌信号 组合出一句状态描述。
    只陈述技术分析结果，不含任何买卖/仓位建议。"""
    tech = rep.get("tech") or {}
    labels = tech.get("labels") or []
    triggered = bool(rep.get("triggered"))
    near = bool(rep.get("nears"))
    stabilizing = any(l.startswith("MACD金叉") or l == "绿柱收窄" or l == "KDJ金叉"
                      for l in labels)
    bull = tech.get("trend") == "多头排列"
    if triggered:
        if bull:
            return {"tag": "低位·趋势向好", "level": "act",
                    "text": "技术面综合：已进入低水位区（便宜），且均线多头排列、中期趋势偏强——「便宜+走强」组合。"}
        if stabilizing:
            return {"tag": "低位·现止跌迹象", "level": "act",
                    "text": "技术面综合：已进入低水位区（便宜），且MACD绿柱收窄或KDJ金叉等止跌信号出现——「便宜+企稳迹象」组合。"}
        return {"tag": "低位·跌势未止", "level": "wait",
                "text": "技术面综合：已进入低水位区（便宜），但均线仍偏空、暂无止跌信号——「便宜但短期仍弱」。"}
    if near:
        return {"tag": "接近低位", "level": "watch",
                "text": "技术面综合：接近低水位区但信号尚未确认，处于临界状态，距触发门槛不远。"}
    if bull:
        return {"tag": "趋势强·不在低位", "level": "chase",
                "text": "技术面综合：均线多头排列、中期趋势向上，但价格不在低位区——「强势但偏贵」。"}
    return {"tag": "不在低位", "level": "chase",
            "text": "技术面综合：各项指标均未进入低水位区，暂无低吸信号；触发时会微信/Mac提醒。"}


def record_alert(state, asset, rep, kind, now):
    name = asset.get("name") or asset["id"]
    detail = "、".join(rep.get("nears") or []) or "、".join(
        COND_NAMES[k] for k, v in (rep.get("conditions") or {}).items() if v)
    state["global_alert_log"].append({
        "ts": iso(now), "asset": name, "kind": kind,
        "price": round(rep["live"], 4) if rep.get("live") else None,
        "detail": detail})
    del state["global_alert_log"][:-100]
    st = state["assets"].setdefault(asset["id"], default_asset_state())
    st["alert_history"] = (st.get("alert_history") or [])[-19:] + [{
        "ts": iso(now), "kind": kind, "price": rep.get("live")}]


# =====================================================================
# §5 通知
# =====================================================================

def mac_notify(title, body):
    if not sys.platform.startswith("darwin"):
        return
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')[:200]
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification "%s" with title "%s"' % (esc(body), esc(title))],
            timeout=10, capture_output=True)
    except Exception:
        pass


class Notifier(object):
    def __init__(self, ch, state):
        self.ch = ch
        self.state = state

    def _quota_ok(self):
        q = self.state["quota"]
        today = now_in("Asia/Shanghai").strftime("%Y-%m-%d")
        if q.get("date") != today:
            self.state["quota"] = {"date": today}   # 跨天清零全部渠道计数
            q = self.state["quota"]
        return q.get("quota:" + self.ch["type"], 0) < self.ch.get("daily_limit", 5)

    def _quota_inc(self):
        key = "quota:" + self.ch["type"]
        self.state["quota"][key] = self.state["quota"].get(key, 0) + 1


class ServerChanNotifier(Notifier):
    def send(self, title, md):
        key = self.ch.get("key") or ""
        if not key or "填" in key:
            log("serverchan 未配置 SendKey，跳过")
            return False
        if not self._quota_ok():
            log("serverchan 当日额度已满，跳过（Mac兜底）")
            return False
        try:
            r = SESSION.post("https://sctapi.ftqq.com/%s.send" % key,
                             data={"title": title[:32], "desp": md}, timeout=15)
            ok = (r.json().get("code") == 0)
            if ok:
                self._quota_inc()
            else:
                log("serverchan 返回异常: %s" % r.text[:120])
            return ok
        except Exception as e:
            log("serverchan 发送失败: %s" % e)
            return False


class PushPlusNotifier(Notifier):
    def send(self, title, md):
        if not self.ch.get("token"):
            return False
        if not self._quota_ok():
            return False
        try:
            r = SESSION.post("http://www.pushplus.plus/send",
                             json={"token": self.ch["token"],
                                   "title": title[:32], "content": md,
                                   "template": "markdown"}, timeout=15)
            ok = r.json().get("code") == 200
            if ok:
                self._quota_inc()
            return ok
        except Exception as e:
            log("pushplus 发送失败: %s" % e)
            return False


class WecomBotNotifier(Notifier):
    def send(self, title, md):
        if not self.ch.get("webhook"):
            return False
        if not self._quota_ok():
            return False
        try:
            r = SESSION.post(self.ch["webhook"],
                             json={"msgtype": "markdown",
                                   "markdown": {"content": ("**%s**\n%s" % (title, md))[:4000]}},
                             timeout=15)
            ok = r.json().get("errcode") == 0
            if ok:
                self._quota_inc()
            return ok
        except Exception as e:
            log("wecombot 发送失败: %s" % e)
            return False


def make_notifiers(config, state):
    install_dns_pins(config.get("dns_pin"))
    out = []
    for ch in config.get("channels", []):
        if not ch.get("enabled"):
            continue
        cls = {"serverchan": ServerChanNotifier, "pushplus": PushPlusNotifier,
               "wecombot": WecomBotNotifier}.get(ch["type"])
        if cls:
            out.append(cls(ch, state))
    return out


def cond_marks(rep):
    return " ".join("%s%s" % (COND_NAMES[k], "✓" if v else ("✗" if v is False else "–"))
                    for k, v in rep["conditions"].items() if k in COND_NAMES)


def asset_line(asset, rep):
    """单资产低水位块：首行加粗标题，其余用列表项逐行排（Server酱单\n会折叠）。"""
    i = rep["indicators"]
    labels = " · ".join((rep.get("tech") or {}).get("labels") or [])
    v = rep.get("verdict") or {}
    parts = ["**🔵 %s** 现价%s %s" % (
        asset.get("name"), fmt_price(rep["live"]), fmt_pct(rep["chg_pct"], True)),
        "- 回撤%s · RSI%s · MA偏离%s · 3年分位%s" % (
            fmt_pct(i["dd"]), fmt_rsi(i["rsi14"]),
            fmt_pct(i["ma_dev"], True), fmt_pct(i["pct3y"])),
        "- 满足: %s" % cond_marks(rep)]
    if labels:
        parts.append("- 技术: %s" % labels)
    if v.get("tag"):
        parts.append("- **技术面结论【%s】**: %s" % (v["tag"], v.get("text", "")))
    return "\n".join(parts)


def compose_low_message(alerts, total, stale_infos, now):
    names = "、".join(a["asset"]["name"] for a in alerts[:3])
    if len(alerts) > 3:
        names += "等%d项" % len(alerts)
    title = "低水位: %s（%d/%d）" % (names, len(alerts), total)
    blocks = []
    for a in alerts:
        blocks.append(asset_line(a["asset"], a["rep"])
                      + "\n\n⏳ 冷却至%s（其间再跌≥5%%才重报）"
                      % (now + timedelta(days=7)).strftime("%m-%d"))
    body = "\n\n".join(blocks)
    if stale_infos:
        body += "\n\n---\n⚠️ 数据异常: " + " | ".join(stale_infos)
    return title[:32], body


def compose_digest(reports, now, link=None, market=None):
    """每日水位日报。Server酱(微信)会把单个\n折叠成空格，段落间必须用\n\n，
    行级内容用 markdown 列表项才能逐行显示。"""
    groups = {"low": [], "near": [], "other": []}
    for r in reports:
        rep = r["rep"]
        groups["low" if rep["triggered"] else ("near" if rep["nears"] else "other")].append(r)
    skey = lambda r: (-(r["rep"].get("water") or 0), r["asset"]["name"])
    for g in groups.values():
        g.sort(key=skey)

    def line(r):
        rep, i = r["rep"], r["rep"]["indicators"]
        v = rep.get("verdict") or {}
        vtag = "「%s」" % v["tag"] if v.get("tag") else ""
        return "- **%s** %s %s · 回撤%s · 分位%s %s" % (
            r["asset"]["name"], fmt_price(rep["live"]),
            fmt_pct(rep["chg_pct"], True),
            fmt_pct(i["dd"]), fmt_pct(i["pct3y"]), vtag)

    parts = ["## 📊 资产水位日报 %s" % now.strftime("%m-%d")]
    if market:
        ups = [b for b in market if b["c"] > 0][:3]
        dns = [b for b in market if b["c"] < 0][-3:][::-1]
        if ups or dns:
            parts.append("\n**🏭 行业板块**\n\n- 领涨: %s\n- 领跌: %s" % (
                "、".join("%s%+.1f%%" % (b["n"], b["c"]) for b in ups) or "-",
                "、".join("%s%+.1f%%" % (b["n"], b["c"]) for b in dns) or "-"))
    for key, head in (("low", "🔵 低水位"), ("near", "🟡 接近低位"), ("other", "⚪ 其余")):
        if not groups[key]:
            continue
        parts.append("\n**%s（%d）**\n\n" % (head, len(groups[key]))
                     + "\n".join(line(r) for r in groups[key]))
    parts.append("\n---\n📊 完整看板: %s" % (link or DASHBOARD_PATH))
    title = "水位日报 %s（%d资产）" % (now.strftime("%m-%d"), len(reports))
    return title[:32], "\n".join(parts)


# =====================================================================
# §6 看板渲染 —— 纯静态HTML/内联SVG，无外部依赖
# =====================================================================

def htmlesc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def sparkline_svg(closes, w=240, h=48, pad=4):
    n = len(closes)
    if n < 2:
        return ""
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or 1.0
    pts = []
    for i, c in enumerate(closes):
        x = pad + i * (w - 2 * pad) / (n - 1)
        y = h - pad - (c - lo) / span * (h - 2 * pad)
        pts.append((x, y))
    d = "M" + " L".join("%.1f %.1f" % p for p in pts)
    dot = '<circle cx="%.1f" cy="%.1f" r="2.6" fill="var(--line)"/>' % pts[-1]
    hi_y = h - pad - (hi - lo) / span * (h - 2 * pad)
    ref = ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" class="ref"/>'
           % (pad, hi_y, w - pad, hi_y)) if hi_y > pad + 1 else ""
    return ('<svg class="spark" viewBox="0 0 %d %d" preserveAspectRatio="none">'
            '<path d="%s"/>%s%s</svg>') % (w, h, d, ref, dot)


def cond_grid(ind, conds):
    """四条件 2×2 小格：圆点(满足=实心)+指标名+当前值，替代旧的单行圆点(曾溢出重叠)。"""
    items = [("回撤", fmt_pct(ind.get("dd")), conds.get("s1")),
             ("RSI14", fmt_rsi(ind.get("rsi14")), conds.get("s2")),
             ("MA200", fmt_pct(ind.get("ma_dev"), True), conds.get("s3")),
             ("3年分位", fmt_pct(ind.get("pct3y")), conds.get("s4"))]
    cells = []
    for lab, val, on in items:
        cls = "cond on" if on else ("cond na" if on is None else "cond")
        cells.append('<span class="%s"><i class="cdot"></i>%s %s</span>'
                     % (cls, lab, val if val != "-" else "-"))
    return '<div class="conds">%s</div>' % "".join(cells)


def fmt_yi(v):
    """主力净额(元)格式化：亿/万。"""
    if v is None:
        return "-"
    s = v / 1e8
    if abs(s) >= 1:
        return "%+.1f亿" % s
    return "%+d万" % round(v / 1e4)


def flow_line(st):
    """卡片资金流行：主力 今日/5日 净额（红流入绿流出，A股惯例）。"""
    f = st.get("flow")
    if not f:
        return ""
    d5 = f.get("d5")
    d5n = f.get("d5_days")
    d5_lab = "5日%s" % fmt_yi(d5) if d5n is None or d5n >= 5 else "5日累计中%s(%d/5)" % (fmt_yi(d5), d5n)
    cls = "up" if (d5 or 0) >= 0 else "dn"
    return ('<div class="flow %s">主力 今日%s · %s<span class="fl-date">%s</span></div>'
            % (cls, fmt_yi(f.get("today")), d5_lab, htmlesc(f.get("date") or "")))


def heat_tile(b, big=False):
    c = b["c"] or 0
    a = min(abs(c) / 4.0, 1.0) * 0.55 + 0.12
    tip = "%s · 涨%s/跌%s · 领涨%s · 主力%s" % (
        b["n"], b.get("u"), b.get("d"), b.get("l") or "-", fmt_yi(b.get("m")))
    inner = "<b>%s</b><i>%+.2f%%</i>" % (htmlesc(b["n"]), c)
    if big:
        inner += "<em>主力%s</em>" % fmt_yi(b.get("m"))
    return ('<div class="htile %s%s" style="--a:%.2f" title="%s">%s</div>'
            % ("h-up" if c >= 0 else "h-dn", " big" if big else "", a, tip, inner))


def market_section(state):
    """行业板块热力图：露头的是领涨/领跌TOP15大砖块，全量板块折叠。"""
    mk = state.get("market") or {}
    inds = mk.get("industries") or []
    if not inds:
        return ""
    ups = [b for b in inds if b["c"] > 0][:15]
    dns = [b for b in inds if b["c"] < 0][-15:][::-1]
    when = (mk.get("ts") or "")[5:16].replace("T", " ")
    html = ['<section class="heatbox"><h2>行业板块热力图'
            '<span class="hsub">%s · %d个板块 · 红涨绿跌 · 深浅=涨跌幅度</span></h2>' % (
                htmlesc(when), len(inds))]
    if ups:
        html.append('<div class="hgrid big">%s</div>' % "".join(heat_tile(b, True) for b in ups))
    if dns:
        html.append('<div class="hgrid big dn-row">%s</div>' % "".join(heat_tile(b, True) for b in dns))
    html.append('<details class="tech"><summary>全部 %d 个板块热力图</summary>'
                '<div class="hgrid">%s</div></details>'
                % (len(inds), "".join(heat_tile(b) for b in inds)))
    html.append("</section>")
    return "".join(html)


def tech_details(st):
    """卡片内可展开的技术分析区(<details>，默认收起保持网格整齐)。"""
    t = st.get("tech") or {}
    if not t:
        return ""
    chips = "".join('<span class="chip">%s</span>' % htmlesc(c) for c in t.get("labels", []))

    def pb_txt():
        if t.get("pb") is None:
            return "-"
        return "%.2f（%.0f%%位）" % (t["pb"], t["pb"] * 100)

    def bw_txt():
        if t.get("bw") is None:
            return "-"
        if t.get("bw_pct") is not None:
            return "%s%%（近半年%s%%位）" % (t["bw"], t["bw_pct"])
        return "%s%%" % t["bw"]

    def atr_txt():
        if t.get("atr_pct") is None:
            return "-"
        return "%s%%/日（年化约%.0f%%）" % (t["atr_pct"], t["atr_pct"] * (252 ** 0.5))

    def vol_txt():
        parts = ["×%s" % t["vol_ratio"]] if t.get("vol_ratio") is not None else []
        if t.get("pq"):
            v = "（昨量%.1f×）" % t["pq_vol"] if t.get("pq_vol") is not None else ""
            parts.append("%s%s" % (t["pq"], v))
        if t.get("obv20_up") is not None:
            parts.append("OBV%s" % ("上行" if t["obv20_up"] else "下行"))
        if t.get("bull_bear_vol") is not None:
            parts.append("多空量比%s（>1.2多方/<0.8空方）" % t["bull_bear_vol"])
        if t.get("vol_5_60") is not None:
            parts.append("5/60日量×%s" % t["vol_5_60"])
        return " · ".join(parts) or "-"

    def flow_txt():
        f = st.get("flow")
        if not f:
            return "无数据（非A股或接口失败）"
        d5n = f.get("d5_days")
        d5_lab = fmt_yi(f.get("d5")) if d5n is None or d5n >= 5 else \
            "%s（累计中 %d/5 日，自动补全）" % (fmt_yi(f.get("d5")), d5n)
        return "今日主力净流入 %s（占比%s%%）· 5日 %s" % (
            fmt_yi(f.get("today")), f.get("today_pct"), d5_lab)

    def mom_txt():
        return "%s / %s" % (fmt_pct(t.get("ret20"), True), fmt_pct(t.get("ret60"), True))

    def pos_txt():
        if t.get("pos52w") is None:
            return "-"
        return "区间%.0f%%位 · 距高点%s · 高于低点%s" % (
            t["pos52w"], fmt_pct(t.get("off_high52")), fmt_pct(t.get("off_low52")))

    rows = [
        ("均线", "%s（MA20 %s / MA60 %s / MA200 %s）" % (
            t.get("trend") or "-", fmt_price(t.get("ma20")),
            fmt_price(t.get("ma60")), fmt_price(t.get("ma200")))),
        ("MACD", "DIF %s · DEA %s · 柱 %s" % (
            fmt_price(t.get("dif")), fmt_price(t.get("dea")), fmt_price(t.get("hist")))),
        ("KDJ", "K %s · D %s · J %s" % (
            fmt_rsi(t.get("k")), fmt_rsi(t.get("d")), fmt_rsi(t.get("j")))),
        ("BOLL", "上 %s / 中 %s / 下 %s · %%B %s · 带宽 %s" % (
            fmt_price(t.get("boll_up")), fmt_price(t.get("boll_mid")),
            fmt_price(t.get("boll_lo")), pb_txt(), bw_txt())),
        ("动量", "20日 / 60日 %s" % mom_txt()),
        ("52周", pos_txt()),
        ("波动", "ATR14 %s" % atr_txt()),
        ("量能", "5日/20日 %s" % vol_txt()),
        ("主力资金", flow_txt()),
    ]
    trs = "".join("<tr><td>%s</td><td>%s</td></tr>" % (k, htmlesc(v)) for k, v in rows)
    return ('<details class="tech"><summary>技术分析 %s</summary>'
            '<table class="tech">%s</table></details>' % (chips, trs))


def badge(kind):
    m = {"low": ("低水位", "b-low"), "near": ("接近", "b-near"),
         "stale": ("数据异常", "b-stale"), "rt": ("仅实时", "b-rt"),
         "short": ("短窗", "b-rt")}
    t, c = m.get(kind, ("", ""))
    return '<span class="badge %s">%s</span>' % (c, t) if t else ""


def chg_html(chg):
    if chg is None:
        return ""
    cls = "up" if chg >= 0 else "dn"
    arrow = "▲" if chg >= 0 else "▼"
    return '<span class="%s">%s%s</span>' % (cls, arrow, fmt_pct(abs(chg)))


def water_meter(st):
    """价格水位条：显示现价在近3年(或52周)的分位——条越短越便宜。
    25%处画低水位线刻度；与"信号徽章"解耦，消除"条满=低水位"的歧义。"""
    ind = st.get("indicators") or {}
    tech = st.get("tech") or {}
    val, src = ind.get("pct3y"), "近3年分位"
    if val is None:
        val, src = tech.get("pos52w"), "52周位置"
    if val is None:
        return ""
    v = int(round(val))
    return ('<div class="meter" title="%s：条越短价格越便宜；刻度线=25%%低水位区门槛">'
            '<span class="tick"></span><i style="width:%d%%"></i></div>'
            '<div class="meter-lab">水位 %d%% · %s（越短越便宜）</div>' % (src, v, v, src))


def verdict_html(st):
    v = st.get("verdict") or {}
    if not v:
        return ""
    return '<div class="verdict v-%s"><b>▸ %s：</b>%s</div>' % (
        v.get("level", "chase"), v.get("tag", ""), v.get("text", ""))


def asset_card(asset, st, is_watch=False):
    name = htmlesc(asset.get("name") or asset["id"])
    price = st.get("last_price")
    chg = st.get("chg_pct")
    body = []
    if is_watch:
        unit = htmlesc(asset.get("unit") or "")
        body.append('<div class="price">%s<small>%s</small> %s %s</div>'
                    % (fmt_price(price), unit, badge("rt"), chg_html(chg)))
    else:
        ind = st.get("indicators") or {}
        bd = badge("low") if st.get("signal_on") else (badge("near") if st.get("has_near") else "")
        extra = badge("short") if st.get("short_window") else ""
        body.append('<div class="price">%s %s %s%s</div>'
                    % (fmt_price(price), chg_html(chg), bd, extra))
        body.append(water_meter(st))
        body.append(cond_grid(ind, st.get("conditions")))
        body.append(verdict_html(st))
        body.append(flow_line(st))
        cache = st.get("closes_cache") or []
        if len(cache) >= 2:
            body.append(sparkline_svg(cache))
        body.append(tech_details(st))
    stale = ""
    last_ok = tz_aware(parse_iso(st.get("last_success_ts") or ""))
    if st.get("consec_fails", 0) >= 10 or (
            last_ok and (now_in("Asia/Shanghai") - last_ok).total_seconds() > 24 * 3600):
        stale = badge("stale")
    body.append('<div class="foot">数据截至 %s · 上次提醒 %s %s</div>' % (
        htmlesc(st.get("last_bar_date") or "-"),
        htmlesc((st.get("last_alert_ts") or "-")[:16].replace("T", " ")), stale))
    return '<div class="card"><div class="name">%s</div>%s</div>' % (name, "".join(body))


def publish_dashboard(config):
    """把看板复制到 GitHub Pages 仓库并 push（config "publish"）。
    仅推 index.html，config/state（含密钥）永远不出本机；失败只记日志。"""
    pub = config.get("publish") or {}
    if not pub.get("enabled"):
        return
    repo_dir = os.path.expanduser(pub.get("repo_dir") or "")
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        log("看板发布跳过: repo_dir 非 git 仓库 %s" % repo_dir)
        return
    try:
        def git(*args):
            r = subprocess.run(["git", "-C", repo_dir] + list(args),
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip()[:200])
            return r.stdout.strip()

        shutil.copyfile(DASHBOARD_PATH, os.path.join(repo_dir, "index.html"))
        edit_src = os.path.join(BASE_DIR, "edit.html")
        if os.path.exists(edit_src):
            shutil.copyfile(edit_src, os.path.join(repo_dir, "edit.html"))
        git("add", "index.html", "edit.html")
        if not git("status", "--porcelain", "--", "index.html", "edit.html"):
            return  # 无变化不提交，避免空commit刷屏
        git("commit", "-q", "-m",
            "dashboard %s" % now_in("Asia/Shanghai").strftime("%m-%d %H:%M"))
        # Pages 由 gh-pages 分支服务（公开仓库首次推该分支时自动激活）
        git("push", "-q", "origin", "HEAD:gh-pages")
        log("看板已发布: %s" % (pub.get("url") or ""))
    except Exception as e:
        log("看板发布失败(不影响本地监控): %s" % e)


def render_dashboard(config, state):
    groups = {}
    for a in all_signal_assets(config):
        st = state["assets"].get(a["id"]) or default_asset_state()
        groups.setdefault(a.get("group") or "其他", []).append(asset_card(a, st))
    for w in config.get("watch_assets", []):
        st = state["assets"].get("watch:" + w["id"]) or default_asset_state()
        groups.setdefault("观察位", []).append(asset_card(w, st, is_watch=True))
    low = sum(1 for k, st in state["assets"].items()
              if not k.startswith("watch:") and st.get("signal_on"))
    near = sum(1 for st in state["assets"].values() if st.get("has_near"))
    stale_n = sum(1 for st in state["assets"].values() if st.get("consec_fails", 0) >= 10)
    secs = []
    for g in GROUP_ORDER + [g for g in groups if g not in GROUP_ORDER]:
        if g in groups:
            secs.append('<section><h2>%s</h2><div class="grid">%s</div></section>'
                        % (htmlesc(g), "".join(groups[g])))
    rows = []
    for e in reversed(state.get("global_alert_log", [])[-20:]):
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            htmlesc((e.get("ts") or "")[:16].replace("T", " ")), htmlesc(e.get("asset")),
            htmlesc("低水位" if e.get("kind") == "low" else "接近"),
            htmlesc(fmt_price(e.get("price"))), htmlesc(e.get("detail") or "")))
    html = HTML_TMPL
    for token, val in (
            ("@@GEN@@", htmlesc(state.get("last_run") or iso(now_in("Asia/Shanghai")))),
            ("@@BANNER@@", '<div class="banner">部分资产数据源连续失败，相关卡片可能过期</div>'
             if stale_n else ""),
            ("@@LOW@@", str(low)), ("@@NEAR@@", str(near)), ("@@STALE@@", str(stale_n)),
            ("@@LASTRUN@@", htmlesc((state.get("last_run") or "-")[:16].replace("T", " "))),
            ("@@SECTIONS@@", "".join(secs)),
            ("@@MARKET@@", market_section(state)),
            ("@@TIMELINE@@", "".join(rows) or '<tr><td colspan="5">暂无提醒记录</td></tr>')):
        html = html.replace(token, val)
    tmp = DASHBOARD_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, DASHBOARD_PATH)


HTML_TMPL = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="600">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>资产低水位看板</title>
<style>
:root{--surface:#fcfcfb;--card:#ffffff;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--line:#2a78d6;--grid:#e1e0d9;--bord:#e7e6e0;--up:#d03b3b;--dn:#0ca30c;
--crit:#d03b3b;--seri:#ec835a}
@media(prefers-color-scheme:dark){:root{--surface:#1a1a19;--card:#222221;--ink:#ffffff;
--ink2:#c3c2b7;--muted:#898781;--line:#3987e5;--grid:#2c2c2a;--bord:#333332;
--up:#e05252;--dn:#2fb32f;--crit:#e05252;--seri:#f0946f}}
*{box-sizing:border-box}
body{margin:0;background:var(--surface);color:var(--ink);
font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:20px}
header{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;
border-bottom:1px solid var(--bord);padding-bottom:10px;margin-bottom:14px}
h1{font-size:19px;margin:0}
.sub{color:var(--muted);font-size:12px}
.sub a{color:var(--line);text-decoration:none;font-weight:600}
.flow{font-size:12px;margin-top:6px;font-weight:600}
.flow .fl-date{float:right;color:var(--muted);font-weight:400;font-size:10.5px}
.heatbox{background:var(--card);border:1px solid var(--bord);border-radius:12px;
padding:14px;margin:12px 0}
.heatbox h2{font-size:14.5px;margin-bottom:10px}
.hsub{color:var(--muted);font-size:11px;font-weight:400;margin-left:8px}
.hgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:4px;margin-bottom:6px}
.hgrid.big{grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:6px}
.hgrid.dn-row{margin-bottom:2px}
.htile{border-radius:7px;padding:7px 4px 6px;text-align:center;color:#fff;
overflow:hidden;line-height:1.3}
.htile b{display:block;font-size:11px;font-weight:600;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.htile i{display:block;font-style:normal;font-size:13px;font-weight:700}
.htile em{display:block;font-style:normal;font-size:10px;opacity:.85}
.htile.big b{font-size:12px}.htile.big i{font-size:15px}
.h-up{background:color-mix(in srgb,var(--up) calc(var(--a)*100%),var(--card))}
.h-dn{background:color-mix(in srgb,var(--dn) calc(var(--a)*100%),var(--card))}
.hgrid:not(.big) .htile{padding:5px 2px 4px}
.hgrid:not(.big) .htile i{font-size:10.5px}
.banner{background:var(--seri);color:#fff;padding:8px 12px;border-radius:8px;
margin-bottom:12px;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;
margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--bord);border-radius:10px;
padding:10px 14px;display:flex;align-items:baseline;gap:8px}
.kpi b{font-size:22px}.kpi span{color:var(--ink2);font-size:12px}
section{margin-bottom:18px}
h2{font-size:13px;color:var(--ink2);margin:0 0 8px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--bord);border-radius:10px;padding:12px}
.name{font-weight:600;margin-bottom:2px}
.price{font-size:20px;font-variant-numeric:tabular-nums}
.price small{font-size:12px;color:var(--muted)}
.up{color:var(--up);font-size:13px}.dn{color:var(--dn);font-size:13px}
.badge{font-size:11px;padding:1px 7px;border-radius:99px;margin-left:6px;vertical-align:2px}
.b-low{background:var(--crit);color:#fff}
.b-near{background:var(--seri);color:#fff}
.b-stale{background:var(--muted);color:#fff}
.b-rt{border:1px solid var(--muted);color:var(--ink2)}
.meter{height:6px;background:var(--grid);border-radius:4px;margin:8px 0 2px;overflow:visible;
position:relative}
.meter i{display:block;height:100%;border-radius:4px 0 0 4px;
background:linear-gradient(90deg,#104281,#86b6ef)}
.meter .tick{position:absolute;left:25%;top:-3px;height:12px;width:1.5px;background:var(--muted)}
.meter-lab{font-size:11px;color:var(--muted)}
.verdict{font-size:12.5px;margin-top:7px;line-height:1.45;color:var(--ink2)}
.verdict b{font-weight:600}
.v-act{color:var(--line)}
.v-act b{color:var(--line)}
.conds{display:grid;grid-template-columns:1fr 1fr;gap:3px 6px;margin-top:7px}
.cond{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--ink2);
font-variant-numeric:tabular-nums;white-space:nowrap}
.cond.on{color:var(--ink);font-weight:600}
.cond.na{opacity:.5}
.cdot{flex:0 0 8px;width:8px;height:8px;border-radius:50%;border:1.5px solid var(--muted)}
.cond.on .cdot{border-color:var(--line);background:var(--line)}
details.tech{margin-top:8px;font-size:12px}
details.tech summary{cursor:pointer;color:var(--ink2);user-select:none;list-style:none;
display:flex;flex-wrap:wrap;gap:4px;align-items:center}
details.tech summary::-webkit-details-marker{display:none}
details.tech summary::before{content:"技术分析 ▸";font-weight:600;margin-right:2px}
details.tech[open] summary::before{content:"技术分析 ▾"}
.chip{border:1px solid var(--bord);border-radius:99px;padding:0 7px;font-size:11px;
color:var(--ink2);background:var(--surface)}
table.tech{width:100%;border-collapse:collapse;margin-top:6px}
table.tech td{padding:3px 0;border-bottom:1px dashed var(--bord);color:var(--ink2);
font-variant-numeric:tabular-nums}
table.tech td:first-child{width:46px;color:var(--muted)}
table.tech td:last-child{text-align:right;color:var(--ink)}
.foot{font-size:11px;color:var(--muted);margin-top:6px;border-top:1px dashed var(--bord);
padding-top:6px}
.spark{width:100%;height:48px;margin-top:8px;display:block}
.spark path{fill:none;stroke:var(--line);stroke-width:1.5}
.spark .ref{stroke:var(--muted);stroke-width:.7;stroke-dasharray:3 3;opacity:.6}
table{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--bord)}
th{color:var(--ink2);font-weight:600}
</style></head><body>
<header><h1>资产低水位看板</h1>
<div class="sub">生成于 @@GEN@@ · 每30分钟自动更新 · <a href="edit.html">⚙️ 管理资产</a></div></header>
@@BANNER@@
<div class="kpis">
<div class="kpi"><b>@@LOW@@</b><span>低水位资产</span></div>
<div class="kpi"><b>@@NEAR@@</b><span>接近提醒</span></div>
<div class="kpi"><b>@@STALE@@</b><span>数据异常</span></div>
<div class="kpi"><b>@@LASTRUN@@</b><span>上次运行</span></div>
</div>
@@MARKET@@
@@SECTIONS@@
<section><h2>提醒时间线（最近20条）</h2>
<table><tr><th>时间</th><th>资产</th><th>类型</th><th>现价</th><th>详情</th></tr>
@@TIMELINE@@</table></section>
</body></html>"""


def scaled_price(w, raw):
    """eastmoney f43 的缩放随行情主机不同会漂移(实测 push2=×100, push2delay 对
    GC00Y=×10)。expected_range 就是真值量级的最权威线索：先按 config 的 scale 试，
    落界内即用；否则在候选档里找唯一落界内的。"""
    hint = w.get("scale", 100)
    lo, hi = w.get("expected_range") or (None, None)
    p = raw / hint
    if lo is None or lo <= p <= hi:
        return p
    for div in (1, 10, 100, 1000, 10000):
        q = raw / div
        if lo <= q <= hi:
            log("%s f43缩放漂移: scale %d → %d" % (w.get("name"), hint, div))
            return q
    return p   # 都不在界内，交给 sanity_check 报错


# =====================================================================
# 主流程 run
# =====================================================================

def sanity_check(asset, price):
    lo, hi = asset.get("expected_range") or (None, None)
    if lo is not None and not (lo <= price <= hi):
        raise RuntimeError("价格%s越界[%s,%s]，疑似接口倍率/代码变更" %
                           (fmt_price(price), lo, hi))


def do_run(config, dry_run=False):
    state = load_state()
    sync_assets(state, config)
    now = now_in(config.get("tz") or "Asia/Shanghai")

    prev = tz_aware(parse_iso(state.get("last_run") or ""))
    if prev and (tz_aware(now) - prev).total_seconds() < config.get("min_run_interval_sec", 600):
        log("距上次运行过近，跳过")
        return

    alerts, nears, reports, stale_infos = [], [], [], []
    for asset in all_signal_assets(config):
        st = state["assets"].setdefault(asset["id"], default_asset_state())
        try:
            bars, live = fetch_asset_bars(asset)
            sanity_check(asset, live)
            rep = evaluate(config, asset, bars, live)
            if not rep.get("ok"):
                raise RuntimeError(rep.get("err"))
            new_bar = rep["last_bar_date"] != st.get("last_bar_date")
            st.update({
                "last_bar_date": rep["last_bar_date"], "last_price": round(live, 6),
                "chg_pct": round(rep["chg_pct"], 2) if rep["chg_pct"] is not None else None,
                "closes_cache": [round(c, 6) for c in rep["closes_tail"]],
                "indicators": rep["indicators"], "conditions": rep["conditions"],
                "water": rep["water"], "has_near": bool(rep["nears"]),
                "short_window": rep["short_window"], "tech": rep["tech"],
                "verdict": rep["verdict"],
                "consec_fails": 0, "last_error": None, "last_success_ts": iso(now)})
            # 注意：qfq 历史在除权日会整体改写 → closes_cache 每轮整体替换(如上)，不能增量追加
            action = decide_action(st, rep, now, new_bar,
                                   repeat_drop_pct=config.get("repeat_drop_pct", 5))
            if action == "low":
                alerts.append({"asset": asset, "rep": rep, "st": st})
                record_alert(state, asset, rep, "low", now)
            elif action == "near":
                nears.append({"asset": asset, "rep": rep})
                record_alert(state, asset, rep, "near", now)
            reports.append({"asset": asset, "rep": rep})
            # 主力资金流(仅 A股上市: 指数/ETF/LOF；港美股与加密无此口径)
            if asset["id"][:2] in ("sh", "sz"):
                try:
                    secid = ("1." if asset["id"].startswith("sh") else "0.") + asset["id"][2:]
                    fl = fetch_eastmoney_flow(secid)
                    if fl:
                        # 按日积累(push2his 不通时快照只有当日1行)，多行则一次补全
                        hist = st.setdefault("flow_hist", {})
                        for r in fl:
                            hist[r["date"]] = r["main"]
                        if len(hist) > 20:
                            for k in sorted(hist)[:-20]:
                                del hist[k]
                        last5 = sorted(hist)[-5:]
                        st["flow"] = {"date": fl[-1]["date"],
                                      "today": fl[-1]["main"], "today_pct": fl[-1]["main_pct"],
                                      "d5": sum(hist[d] for d in last5),
                                      "d5_days": len(last5)}
                    time.sleep(0.15)
                except Exception as e:
                    log("%-14s 资金流获取失败(跳过): %s" % (asset["name"], str(e)[:80]))
            log("%-14s %10s %s 回撤%-6s RSI%-4s MA%-7s 分位%-5s%s" % (
                asset["name"], fmt_price(live), fmt_pct(rep["chg_pct"], True),
                fmt_pct(rep["indicators"]["dd"]), fmt_rsi(rep["indicators"]["rsi14"]),
                fmt_pct(rep["indicators"]["ma_dev"], True),
                fmt_pct(rep["indicators"]["pct3y"]),
                " ←低水位" if rep["triggered"] else (" ←接近" if rep["nears"] else "")))
        except Exception as e:
            st["consec_fails"] = st.get("consec_fails", 0) + 1
            st["last_error"] = str(e)[:200]
            log("%-14s 失败(%d): %s" % (asset.get("name"), st["consec_fails"], e))
            if st["consec_fails"] == 10:
                stale_infos.append(asset.get("name"))

    # 市场层：行业板块热力图（失败不影响主链路，state 保留上一版）
    try:
        inds = fetch_eastmoney_industries()
        if inds:
            state["market"] = {"industries": inds, "ts": iso(now)}
    except Exception as e:
        log("板块数据获取失败(沿用上一版): %s" % e)

    # 观察位(仅实时展示，无信号)
    for w in config.get("watch_assets", []):
        key = "watch:" + w["id"]
        st = state["assets"].setdefault(key, default_asset_state())
        try:
            rt = fetch_eastmoney_rt(w["id"])
            price = scaled_price(w, rt["raw"])
            sanity_check(w, price)
            st.update({
                "last_price": round(price, 6),
                "chg_pct": round(rt["chg_pct"], 2) if rt.get("chg_pct") is not None else None,
                "consec_fails": 0, "last_error": None, "last_success_ts": iso(now),
                "last_bar_date": datetime.fromtimestamp(
                    rt["ts"], tz=timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
                if rt.get("ts") else None})
            log("%-14s %10s %s (观察)" % (w["name"], fmt_price(price),
                                          fmt_pct(st["chg_pct"], True)))
        except Exception as e:
            st["consec_fails"] = st.get("consec_fails", 0) + 1
            st["last_error"] = str(e)[:200]
            log("%-14s 失败: %s" % (w["name"], e))

    total_signals = len(reports)
    mac_budget = config.get("mac_notify_cap_per_run", 5)

    if dry_run:
        if alerts:
            t, b = compose_low_message(alerts, total_signals, stale_infos, now)
            log("[dry-run 微信将发] %s\n%s" % (t, b))
        if nears:
            log("[dry-run Mac将发] 接近低水位: %s" % "、".join(
                "%s(%s)" % (a["asset"]["name"], "/".join(a["rep"]["nears"])) for a in nears))
        log("[dry-run] 完成：低水位%d 接近%d 信号资产%d" % (len(alerts), len(nears), total_signals))
    else:
        state["last_run"] = iso(now)
        notifiers = make_notifiers(config, state)
        if alerts:
            t, b = compose_low_message(alerts, total_signals, stale_infos, now)
            sent = False
            for n in notifiers:
                if n.send(t, b):
                    sent = True
                    break
            for a in alerts[:3]:
                if mac_budget > 0:
                    mac_notify("低水位 " + a["asset"]["name"],
                               "、".join(COND_NAMES[k]
                                         for k, v in a["rep"]["conditions"].items() if v)
                               + " 现价" + fmt_price(a["rep"]["live"]))
                    mac_budget -= 1
            if len(alerts) > 3 and mac_budget > 0:
                mac_notify("低水位", "另有%d项资产触发" % (len(alerts) - 3))
                mac_budget -= 1
        if nears and mac_budget > 0:
            mac_notify("接近低水位", "、".join(
                "%s(%s)" % (a["asset"]["name"], "/".join(a["rep"]["nears"]))
                for a in nears[:4])[:200])
            mac_budget -= 1
        # 每日水位日报(20点后首轮)
        if (config.get("daily_digest") and reports
                and now.hour >= config.get("digest_after_hour", 20)
                and state.get("digest_date") != now.strftime("%Y-%m-%d")):
            t, b = compose_digest(reports, now,
                                  link=(config.get("publish") or {}).get("url"),
                                  market=(state.get("market") or {}).get("industries"))
            for n in notifiers:
                if n.send(t, b):
                    state["digest_date"] = now.strftime("%Y-%m-%d")
                    break
            else:
                log("日报未能经任何渠道发送（检查 config.json 渠道配置）")
        save_state_atomic(state)   # 通知尝试完成后才落盘
    try:
        render_dashboard(config, state)
        if not dry_run:
            publish_dashboard(config)
    except Exception as e:
        log("看板渲染失败(保留上一版): %s" % e)


# =====================================================================
# doctor / notify / render / backtest
# =====================================================================

def do_doctor(config):
    ok = True
    print("== 信号资产体检 ==")
    for asset in all_signal_assets(config):
        try:
            bars, live = fetch_asset_bars(asset, n=30)
            sanity_check(asset, live)
            last = bars[-1]
            print(" OK  %-14s %-12s 现价%-12s 最后一根%s%s" % (
                asset["name"], asset["id"], fmt_price(live), last[0],
                "" if last[6] else "(盘中)"))
        except Exception as e:
            ok = False
            print("FAIL %-14s %-12s %s" % (asset.get("name"), asset["id"], e))
    print("== 观察位体检 ==")
    for w in config.get("watch_assets", []):
        try:
            rt = fetch_eastmoney_rt(w["id"])
            price = scaled_price(w, rt["raw"])
            sanity_check(w, price)
            print(" OK  %-14s %-14s %s" % (w["name"], w["id"], fmt_price(price)))
        except Exception as e:
            ok = False
            print("FAIL %-14s %-14s %s" % (w["name"], w["id"], e))
    print("== 通知渠道 ==")
    any_on = False
    for ch in config.get("channels", []):
        if ch.get("enabled"):
            any_on = True
            print(" ON   %s" % ch["type"])
        else:
            print(" off  %s" % ch["type"])
    if not any_on:
        print("提示：微信渠道未启用。到 sct.ftqq.com 微信扫码拿 SendKey，"
              "填入 config.json 并把 enabled 改为 true")
    print("体检结果: %s" % ("全部通过" if ok else "存在失败项，见上"))
    return ok


def do_notify_test(config, wechat=False):
    mac_notify("asset-monitor", "这是一条测试通知 ✓ 渠道正常")
    print("已发送 Mac 测试通知")
    if wechat:
        state = load_state()
        for n in make_notifiers(config, state):
            if n.send("asset-monitor 测试", "监控已就绪。收到本条说明微信通道正常。"):
                print("已通过 %s 发送测试消息" % n.ch["type"])
                save_state_atomic(state)
                return
        print("没有可用且配置完整的微信渠道（检查 config.json 的 enabled 与 key）")


def do_render(config):
    state = load_state()
    render_dashboard(config, state)
    print("已生成 %s" % DASHBOARD_PATH)


# ---------- backtest ----------

def fetch_history(asset, years=5):
    src = asset["source"]
    if src == "gateio":
        now_ts = int(time.time())
        start_ts = now_ts - years * 365 * 86400
        out, cur = [], start_ts
        while cur < now_ts:
            to = min(cur + 999 * 86400, now_ts)
            out.extend(fetch_gateio_kline(asset["id"], n=1000, from_ts=cur, to_ts=to))
            cur = to
            time.sleep(0.5)
        seen = {}
        for b in out:
            seen[b[0]] = b
        return [seen[k] for k in sorted(seen)]
    start = (datetime.now() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
    mid = (datetime.now() - timedelta(days=years * 365 // 2)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    segs = []
    for a, b in ((start, mid), (mid, today)):
        try:
            segs.append(fetch_tencent_kline(asset["id"], n=800, start=a, end=b))
        except Exception:
            segs.append([])   # 晚上市的品种前半段会为空，只要合计够长即可
        time.sleep(0.3)
    seen = {}
    for bar in segs[0] + segs[1]:
        seen[bar[0]] = bar
    bars = [seen[k] for k in sorted(seen)]
    if len(bars) < 300:
        raise RuntimeError("历史拼接仅%d根" % len(bars))
    return bars


def replay(asset, bars, config, th_overrides=None):
    """逐日重放。铁律：第i日只用 closes[:i+1] 与当bar价格(无未来函数)。"""
    th = dict(thresholds_for(config, asset))
    if th_overrides:
        th.update(th_overrides)
    closes_all = [b[2] for b in bars]
    n = len(bars)
    rsi = rsi_series(closes_all)
    ma = sma_series(closes_all, 200)
    hi = high_series(closes_all)
    pct = pct_series(closes_all)
    episodes, per_year = [], {}
    sig_on, off_bars, last_alert_i, last_alert_price = False, 0, -10 ** 9, None
    for i in range(251, n):
        price = bars[i][2]
        c1 = c2 = c3 = c4 = None
        if hi[i]:
            c1 = (hi[i] - price) / hi[i] * 100 >= th["dd"]
        if rsi[i] is not None:
            c2 = rsi[i] < th["rsi"]
        if ma[i]:
            c3 = (price - ma[i]) / ma[i] * 100 <= -th["ma_dev"]
        if pct[i] is not None:
            c4 = pct[i] < th["pct3y"]
        conds = [c1, c2, c3, c4]
        comp = sum(1 for v in conds if v is not None)
        met = sum(1 for v in conds if v is True)
        trig = comp >= 2 and met >= 2
        if trig:
            off_bars = 0
            lower_ok = last_alert_price is None or price <= last_alert_price * 0.95
            if not sig_on or (i - last_alert_i >= 7 and lower_ok):   # 与线上同规则：重报需再跌5%
                sig_on = True
                last_alert_i = i
                last_alert_price = price
                date = bars[i][0]
                episodes.append((date, price, "".join(
                    k for k, v in zip(["1", "2", "3", "4"], conds) if v)))
                per_year[date[:4]] = per_year.get(date[:4], 0) + 1
        elif sig_on:
            off_bars += 1
            if off_bars >= 2:
                sig_on, off_bars = False, 0
    return episodes, per_year


def fwd_return(bars, i, days):
    j = min(i + days, len(bars) - 1)
    if j <= i:
        return None
    return (bars[j][2] - bars[i][2]) / bars[i][2] * 100.0


def do_backtest(config, years=5, sweep=None, report_path=None):
    lines = []

    def out(s=""):
        print(s)
        lines.append(s)

    out("# 低水位信号回测 (%d年)" % years)
    assets = [a for a in all_signal_assets(config) if a.get("source") in ("tencent", "gateio")]
    scenarios = [
        ("sh518880", "2026-02-15", "2026-04-15", "2026-03黄金暴跌周"),
        ("sh000300", "2024-01-01", "2024-03-01", "2024年初A股低位"),
        ("BTC_USDT", "2025-11-01", "2026-09-11", "BTC自12万回落"),
    ]
    all_eps = {}
    for asset in assets:
        try:
            bars = fetch_history(asset, years=years)
        except Exception as e:
            out("## %s 历史获取失败: %s" % (asset["name"], e))
            continue
        eps, per_year = replay(asset, bars, config)
        all_eps[asset["id"]] = (asset, bars, eps)
        out("")
        out("## %s（%d根，%s~%s）触发%d次" % (
            asset["name"], len(bars), bars[0][0], bars[-1][0], len(eps)))
        out("  按年: %s" % (" ".join("%s:%d" % (y, c) for y, c in sorted(per_year.items())) or "无"))
        date_idx = {b[0]: i for i, b in enumerate(bars)}
        for date, price, cond in eps:
            i = date_idx[date]
            future = [b[2] for b in bars[i:i + 61]]
            low_d = min(range(len(future)), key=lambda k: future[k]) if future else 0
            out("  %s %s 条件[%s] 距后续低点%dd +5日%s +20日%s +60日%s" % (
                date, fmt_price(price), cond, low_d,
                fmt_pct(fwd_return(bars, i, 5), True),
                fmt_pct(fwd_return(bars, i, 20), True),
                fmt_pct(fwd_return(bars, i, 60), True)))
    out("")
    out("== 场景验收 ==")
    for aid, d1, d2, label in scenarios:
        if aid not in all_eps:
            out("  ✘ %s: 无数据(%s)" % (label, aid))
            continue
        asset, bars, eps = all_eps[aid]
        hit = [e for e in eps if d1 <= e[0] <= d2]
        if hit:
            out("  ✔ %s: 触发%d次 %s" % (label, len(hit),
                                         "; ".join("%s@%s" % (e[0], fmt_price(e[1])) for e in hit)))
        else:
            window = [(b[0], b[2]) for b in bars if d1 <= b[0] <= d2]
            low = min(window, key=lambda x: x[1]) if window else ("-", 0)
            out("  ✘ %s: 窗口内未触发(窗口最低 %s @%s) → 可考虑放宽该档阈值" %
                (label, fmt_price(low[1]), low[0]))
    out("")
    out("== 防过拟合提醒 ==")
    out("以上为样本内校准；若调整阈值，每次幅度≤5个百分点，且需全资产全期重跑确认触发次数未放大多倍。")
    if sweep:
        out("")
        out("== 阈值网格 %s ==" % sweep)
        grid = {}
        for kv in sweep.split():
            k, v = kv.split("=")
            grid[k] = [float(x) for x in v.split(",")]
        keys = list(grid)
        import itertools
        out("组合 | 触发总数(全部资产)")
        for combo in itertools.product(*[grid[k] for k in keys]):
            ov = {k: v for k, v in zip(keys, combo) if k in ("dd", "rsi", "ma_dev", "pct3y")}
            total = 0
            for asset in assets:
                if asset["id"] not in all_eps:
                    continue
                _, bars, _ = all_eps[asset["id"]]
                eps, _ = replay(asset, bars, config, th_overrides=ov)
                total += len(eps)
            out("%s | %d" % (",".join(str(v) for v in combo), total))
    if report_path:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print("报告已写入 %s" % report_path)


# =====================================================================
# §0 CLI 入口
# =====================================================================

def main():
    ap = argparse.ArgumentParser(description="资产低水位监控器")
    sub = ap.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="常规运行")
    p_run.add_argument("--dry-run", action="store_true")
    sub.add_parser("doctor", help="逐资产体检")
    p_nt = sub.add_parser("notify", help="通知测试")
    p_nt.add_argument("--test", action="store_true")
    p_nt.add_argument("--wechat", action="store_true")
    sub.add_parser("render", help="仅渲染看板")
    p_bt = sub.add_parser("backtest", help="历史回测")
    p_bt.add_argument("--years", type=int, default=5)
    p_bt.add_argument("--sweep", default=None, help='如 "dd=15,20,25 rsi=25,30"')
    p_bt.add_argument("--report", default=None)
    args = ap.parse_args()

    if args.cmd in (None, "run"):
        lock_f = open(LOCK_PATH, "w")   # flock 防并发：拿不到锁说明上一轮还在跑
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("另一实例运行中，退出")
            return
        truncate_log()
        sync_config()   # 手机端网页提交的资产增删 → git pull（仅 run 拉取）
        config = load_config()
        do_run(config, dry_run=bool(getattr(args, "dry_run", False)))
        return

    config = load_config()
    if args.cmd == "doctor":
        sys.exit(0 if do_doctor(config) else 1)
    elif args.cmd == "notify":
        do_notify_test(config, wechat=args.wechat)
    elif args.cmd == "render":
        do_render(config)
    elif args.cmd == "backtest":
        do_backtest(config, years=args.years, sweep=args.sweep, report_path=args.report)


if __name__ == "__main__":
    main()
