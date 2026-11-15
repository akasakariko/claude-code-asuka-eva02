#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime

def fmt(n):
    if not isinstance(n, (int, float)):
        return "0"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(int(n))

def parse_ts(s):
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def num(v, default=0):
    # statusline 输入里 usage 字段可能是 null，直接相加会抛 TypeError
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default

# 从 transcript 尾部推算 TPS：api-request 的时间戳是请求发出时刻，
# 同一条 assistant 消息的最后一个分块（thinking / text / tool_use）是流结束时刻，
# 两者之差就是这一轮的生成耗时，除以 usage.output_tokens 得到 tokens/s。
# 先读尾部 512KB；长会话里尾部可能全是 attachment 大行（单行可达 1MB+），
# 找不到可配对的消息就逐步放大窗口，最多读到 8MB。


def _read_tps_window(path, window):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - window))
            blob = f.read()
    except Exception:
        return None

    if not blob:
        return None

    lines = blob.split(b"\n")
    if size > window:
        lines = lines[1:]  # 首行可能被截断

    pending_req = None
    req_by_id = {}  # api-request id -> 请求时间戳（2.1.301+ 的 assistant 记录带 requestRef，可精确配对）
    msgs = {}  # message id -> [末块时间戳, output_tokens, 顺序配对的请求时间戳, requestRef]

    for line in lines:
        if not line or len(line) > 512 * 1024:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "api-request":
            # 子代理等旁路请求不算主线程速度
            if d.get("querySource") not in (None, "repl_main_thread"):
                continue
            ts = parse_ts(d.get("timestamp"))
            if ts is None:
                continue
            pending_req = ts
            rid = d.get("id")
            if rid:
                req_by_id[rid] = ts
        elif d.get("type") == "assistant":
            if d.get("isSidechain"):
                continue
            m = d.get("message") or {}
            mid = m.get("id")
            ts = parse_ts(d.get("timestamp"))
            out = num((m.get("usage") or {}).get("output_tokens"))
            if not mid or ts is None or not out:
                continue
            prev = msgs.get(mid)
            if prev is None:
                msgs[mid] = [ts, out, pending_req, d.get("requestRef")]
            elif ts > prev[0]:
                # 同一条消息分多行落盘，取最后一行的时间戳作为流结束时刻
                prev[0] = ts

    # 取结束时间最晚的一条完整消息
    latest = None
    for end_ts, out, seq_req, ref in msgs.values():
        if not out:
            continue
        req_ts = req_by_id.get(ref) if ref else None
        if req_ts is None:
            req_ts = seq_req
        if req_ts is None:
            continue
        if latest is None or end_ts > latest[0]:
            latest = (end_ts, out, req_ts)

    if latest is None:
        return None

    elapsed = (latest[0] - latest[2]).total_seconds()
    if elapsed < 0.15 or elapsed > 1800:
        return None

    # 最后一条消息已经很旧（用户刚发新 prompt、还在等首个 token）时，
    # 继续显示上一轮速度会误导，按“无数据”处理
    try:
        idle = (datetime.now(latest[0].tzinfo) - latest[0]).total_seconds()
        if idle > 90:
            return None
    except Exception:
        pass

    return latest[1] / elapsed


def read_tps(path):
    for window in (512 * 1024, 2 * 1024 * 1024, 8 * 1024 * 1024):
        tps = _read_tps_window(path, window)
        if tps is not None:
            return tps
        try:
            if os.path.getsize(path) <= window:
                break  # 已经读了整个文件，再放大没有意义
        except Exception:
            break
    return None

try:
    raw = sys.stdin.read().strip()
    if not raw:
        sys.exit(0)
    data = json.loads(raw)
except Exception:
    sys.exit(0)

# 模型与思考强度
model = data.get("model") or {}
model_name = model.get("display_name") or model.get("id") or "Claude"
effort_obj = data.get("effort") or {}
effort = effort_obj.get("level")
thinking = data.get("thinking") or {}
if not effort and thinking.get("enabled"):
    effort = "on"

effort_str = f" [effort: {effort}]" if effort else ""

# 上下文窗口
cw = data.get("context_window") or {}
usage = cw.get("current_usage") or {}

pct = int(num(cw.get("used_percentage")))
max_tokens = cw.get("context_window_size") or 200000

# 输入、输出、缓存
# In 是这次请求的完整输入（含缓存读写），否则会和左侧的上下文百分比严重对不上
uncached_in = num(usage.get("input_tokens"))
cache_write = num(usage.get("cache_creation_input_tokens"))
cache_read = num(usage.get("cache_read_input_tokens"))
in_tokens = uncached_in + cache_write + cache_read
out_tokens = num(usage.get("output_tokens"))

# prompt_cache 命中率
# 官方口径 hit_ratio = cache_read / (cache_read + cache_creation + uncached_input)，
# 与这里的 in_tokens（三项之和）一致。caching_observed 为 false 表示这个提供商
# 根本没报告缓存令牌（缓存关闭或不支持），此时算出来的比例没有意义，显示 --。
prompt_cache = data.get("prompt_cache") or {}
hit_ratio = prompt_cache.get("hit_ratio")
caching_observed = prompt_cache.get("caching_observed")

if isinstance(hit_ratio, (int, float)) and not isinstance(hit_ratio, bool):
    hit_str = f"{hit_ratio * 100:.0f}%"
elif caching_observed is False or (cache_read + cache_write) == 0:
    hit_str = "--"
elif in_tokens > 0:
    hit_str = f"{cache_read / in_tokens * 100:.0f}%"
else:
    hit_str = "--"

# 生成速度
tps = None
transcript = data.get("transcript_path")
if transcript:
    try:
        tps = read_tps(transcript)
    except Exception:
        tps = None

# 进度条（EVA-02 HUD 同步率条）
bar_width = 10
filled = min(bar_width, max(0, pct * bar_width // 100))
bar = "▰" * filled + "▱" * (bar_width - filled)

# 终端色彩（EVA-02 明日香 Truecolor 调色板）
C_EVA_RED = "\033[38;2;255;30;39m"
C_EVA_BG_RED = "\033[48;2;180;12;24;38;2;255;245;235;1m"
C_ASUKA_ORANGE = "\033[38;2;255;140;0m"
C_HEADSET_GREEN = "\033[38;2;0;255;102m"
C_EYE_BLUE = "\033[38;2;56;189;248m"
C_WARN_GOLD = "\033[38;2;255;209;102m"
C_DIM = "\033[38;2;184;93;67m"
C_RESET = "\033[0m"

color = C_EVA_RED if pct >= 90 else C_WARN_GOLD if pct >= 70 else C_HEADSET_GREEN
a10_str = f" {C_EYE_BLUE}[A10:{effort}]{C_RESET}" if effort else ""

if tps is None:
    tps_str = f"{C_DIM}TPS: --{C_RESET}"
else:
    tps_color = C_EVA_RED if tps < 20 else C_WARN_GOLD if tps < 50 else C_HEADSET_GREEN
    tps_val = f"{tps:.1f}" if tps < 10 else f"{tps:.0f}"
    tps_str = f"{C_DIM}TPS:{C_RESET} {tps_color}{tps_val}{C_RESET}"

print(
    f"{C_EVA_BG_RED} ◢EVA-02 弐号機·アスカ◣ {C_RESET} "
    f"{C_ASUKA_ORANGE}[{model_name}]{C_RESET}{a10_str} "
    f"{color}SYNC {bar} {pct}%{C_RESET} {C_DIM}({fmt(max_tokens)}){C_RESET} "
    f"{C_DIM}|{C_RESET} In: {fmt(in_tokens)} "
    f"{C_DIM}|{C_RESET} Out: {fmt(out_tokens)} "
    f"{C_DIM}|{C_RESET} Cache: {hit_str} "
    f"{C_DIM}|{C_RESET} {tps_str}"
)
