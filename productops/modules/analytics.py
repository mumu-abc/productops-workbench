#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块三：数据看板（Analytics）

回答一个问题：**数据到底好不好，该往哪使劲？**

---------------------------------------------------------------------
埋点事件表是整个数据看板的地基。**事件设计错了，后面所有指标都算不出来。**
所以这个文件先定义「什么算一个事件」，再谈生成数据。

事件设计的三个原则：

1. **每个事件都要能回答一个业务问题。** 回答不了的问题不要埋 ——
   埋点是有维护成本的，无用的埋点会污染整个数据表。
2. **事件必须有明确的触发时刻定义。** 「激活」到底是注册成功，
   还是完成第一次有价值的动作？定义不同，结论可以完全相反。
3. **事件里要带够维度**（渠道、平台、来源），否则只能看总量，做不了下钻。

本模块的指标**全部由 sql/analytics.sql 里的 SQL 算出**，Python 只负责
执行与呈现 —— 这样 SQL 可以单独 review、单独替换、直接丢给数据团队复用。
"""

import json
import random
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from ..core import db
from ..core.config import Config
from ..core.stats import pct

# ===========================================================================
# 事件定义
# ===========================================================================
EVENTS: Dict[str, Dict[str, str]] = {
    "open": {
        "when": "用户打开应用",
        "question": "有多少人来了？（DAU / 留存的分母）",
    },
    "input": {
        "when": "用户输入需求并提交",
        "question": "多少人真的开始用了？",
    },
    "generate": {
        "when": "模型成功产出可用结果",
        "question": "核心价值有没有交付出去？（北极星的分子）",
    },
    "generate_fail": {
        "when": "生成失败（超时 / 报错 / 输出为空）",
        "question": "技术稳定性怎么样？失败率是多少？",
    },
    "adopt": {
        "when": "用户复制 / 导出 / 分享结果",
        "question": "生成的结果他真的接受了吗？",
    },
}

CHANNELS: List[Tuple[str, float]] = [
    ("应用商店", 0.32), ("朋友推荐", 0.24), ("社交媒体", 0.22),
    ("搜索引擎", 0.14), ("广告投放", 0.08),
]
PLATFORMS: List[Tuple[str, float]] = [("iOS", 0.46), ("Android", 0.54)]

FUNNEL = {
    "open_to_input": 0.66,
    "input_to_generate": 0.79,
    "generate_success": 0.93,
    "generate_to_adopt": 0.71,
}

# 渠道质量系数：不同来源的用户意图强度不一样。
# 朋友推荐来的用户目标更明确，广告投放来的用户更容易来了就走。
# **这个差异是数据能「讲故事」的前提** —— 否则各渠道数字一模一样，
# 渠道分析这个模块就没有存在的意义。
CHANNEL_QUALITY = {
    "朋友推荐": 1.14, "搜索引擎": 1.04, "应用商店": 1.00,
    "社交媒体": 0.92, "广告投放": 0.76,
}

# 留存曲线：第 N 天回访概率
RETENTION = {1: 0.42, 2: 0.34, 3: 0.28, 5: 0.22, 7: 0.18, 14: 0.11, 30: 0.06}


def _weighted(rng: random.Random, pairs) -> str:
    total = sum(w for _, w in pairs)
    r = rng.random() * total
    acc = 0.0
    for name, w in pairs:
        acc += w
        if r <= acc:
            return name
    return pairs[-1][0]


def _retention_prob(day_n: int) -> float:
    """在给定观测点上取留存概率，中间日期线性插值。"""
    keys = sorted(RETENTION)
    if day_n in RETENTION:
        return RETENTION[day_n]
    if day_n < keys[0]:
        return RETENTION[keys[0]] * (day_n / keys[0])
    if day_n > keys[-1]:
        return max(0.02, RETENTION[keys[-1]] * (keys[-1] / day_n))
    lo = max(k for k in keys if k < day_n)
    hi = min(k for k in keys if k > day_n)
    ratio = (day_n - lo) / (hi - lo)
    return RETENTION[lo] + (RETENTION[hi] - RETENTION[lo]) * ratio


def generate(days: Optional[int] = None, progress=None) -> Dict[str, int]:
    """
    生成观测窗口内的用户与埋点事件，写入共用的 users / events 表。

    规模设定：每天新增 190~400（周末更高，符合旅行类产品的规律），
    最终约 9000 用户、6~8 万条事件 —— 小到能秒级跑完，大到能算出有意义的留存曲线。

    实现上注意：用 cohort_index 按天缓存新增用户，避免在嵌套循环里全表扫 user_rows
    （O(n²) 在 9000 用户 × 30 天时会明显卡）。
    """
    days = days or Config.DAYS
    rng = random.Random(Config.SEED)
    start = datetime(2026, 9, 15)

    user_rows: List[tuple] = []
    event_rows: List[tuple] = []
    cohort_index: Dict[str, List[tuple]] = {}
    uid_seq = 0

    def add(uid, event, day_str, base_ts, minutes, channel, platform,
            tokens=0, dur=0):
        t = base_ts + timedelta(minutes=minutes)
        event_rows.append((uid, event, day_str, t.strftime("%Y-%m-%d %H:%M:%S"),
                           channel, platform, tokens, dur))

    for d in range(days):
        date = start + timedelta(days=d)
        day_str = date.strftime("%Y-%m-%d")
        is_weekend = date.weekday() >= 5
        n_new = rng.randint(280, 400) if is_weekend else rng.randint(190, 300)

        for _ in range(n_new):
            uid_seq += 1
            uid = f"U{uid_seq:06d}"
            channel = _weighted(rng, CHANNELS)
            platform = _weighted(rng, PLATFORMS)
            user_rows.append((uid, day_str, channel, platform))

            base_ts = date.replace(hour=rng.randint(7, 23), minute=rng.randint(0, 59),
                                   second=rng.randint(0, 59))
            add(uid, "open", day_str, base_ts, 0, channel, platform,
                dur=rng.randint(800, 4000))

            quality = CHANNEL_QUALITY.get(channel, 1.0)

            if rng.random() > min(0.96, FUNNEL["open_to_input"] * quality):
                continue
            add(uid, "input", day_str, base_ts, rng.randint(1, 6), channel, platform,
                dur=rng.randint(3000, 20000))

            if rng.random() > min(0.96, FUNNEL["input_to_generate"] * quality):
                continue

            tokens = rng.randint(1200, 2800)
            dur = rng.randint(4000, 26000)
            if rng.random() > FUNNEL["generate_success"]:
                add(uid, "generate_fail", day_str, base_ts, rng.randint(3, 30),
                    channel, platform, tokens // 3, dur)
                continue
            add(uid, "generate", day_str, base_ts, rng.randint(3, 25),
                channel, platform, tokens, dur)
            if rng.random() <= FUNNEL["generate_to_adopt"]:
                add(uid, "adopt", day_str, base_ts, rng.randint(1, 8), channel, platform)

        cohort_index[day_str] = user_rows[-n_new:]

        # ---- 回访事件（留存的分母/分子都来自这里）----
        for past in range(d):
            past_day = (start + timedelta(days=past)).strftime("%Y-%m-%d")
            prob = _retention_prob(d - past)
            if prob <= 0:
                continue
            for uid, _, channel, platform in cohort_index.get(past_day, ()):
                if rng.random() > prob:
                    continue
                t = date.replace(hour=rng.randint(8, 23), minute=rng.randint(0, 59),
                                 second=rng.randint(0, 59))
                event_rows.append((uid, "open", day_str,
                                   t.strftime("%Y-%m-%d %H:%M:%S"), channel, platform,
                                   rng.randint(600, 3000), rng.randint(800, 5000)))
                if rng.random() < 0.55:
                    event_rows.append((uid, "input", day_str,
                                       (t + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
                                       channel, platform, 0, rng.randint(2000, 15000)))
                    if rng.random() < 0.82:
                        event_rows.append((uid, "generate", day_str,
                                           (t + timedelta(minutes=6)).strftime("%Y-%m-%d %H:%M:%S"),
                                           channel, platform, rng.randint(1200, 2600),
                                           rng.randint(4000, 24000)))
        if progress:
            progress(d + 1, days, day_str)

    db.clear_analytics()
    with db.tx() as conn:
        conn.executemany("INSERT INTO users (user_id, first_day, channel, platform) "
                         "VALUES (?,?,?,?)", user_rows)
        conn.executemany(
            "INSERT INTO events (user_id, event, day, ts, channel, platform, tokens, duration_ms) "
            "VALUES (?,?,?,?,?,?,?,?)", event_rows)
    return {"users": len(user_rows), "events": len(event_rows), "days": days}


# ===========================================================================
# SQL 加载与执行
# ===========================================================================
import re  # noqa: E402  （放在这里是因为上面是数据定义，逻辑从这行开始）

_TAG = re.compile(r"^--\s*===\s*([a-zA-Z_0-9]+)\s*===\s*$")
_SINGLE_ROW = {"funnel", "retention_summary"}


def load_queries(path: Optional[str] = None) -> Dict[str, str]:
    """把 analytics.sql 解析成 {名称: SQL}。SQL 与代码分离的好处见文件头。"""
    p = path or str(Config.SQL_DIR / "analytics.sql")
    queries: Dict[str, str] = {}
    current: Optional[str] = None
    buf: List[str] = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            m = _TAG.match(line.strip())
            if m:
                if current:
                    queries[current] = "\n".join(buf).strip().rstrip(";")
                current, buf = m.group(1), []
                continue
            if current is not None:
                buf.append(line.rstrip("\n"))
    if current:
        queries[current] = "\n".join(buf).strip().rstrip(";")
    return queries


def run_all(conn: Optional[sqlite3.Connection] = None) -> Dict[str, object]:
    """执行全部查询，返回 {名称: 结果}。单行查询返回 dict，其余返回 list。"""
    own = conn is None
    conn = conn or db.connect()
    try:
        conn.row_factory = sqlite3.Row
        out: Dict[str, object] = {}
        for name, sql in load_queries().items():
            rows = [dict(r) for r in conn.execute(sql).fetchall()]
            out[name] = (rows[0] if rows else {}) if name in _SINGLE_ROW else rows
        return out
    finally:
        if own:
            conn.close()


def sql_source(name: Optional[str] = None):
    """返回 SQL 原文。API 里「左 SQL、右结果」的展示靠它。"""
    q = load_queries()
    return q.get(name, "") if name else q


# ===========================================================================
# 结论层：把数字翻译成「该做什么」
# ===========================================================================
_STEP_LABELS = [
    ("open_to_input", "打开→输入需求", "表达问题：用户不知道要输入什么、入口不明显"),
    ("input_to_generate", "输入→生成成功", "技术问题：生成失败或太慢"),
    ("generate_to_adopt", "生成成功→采纳", "价值问题：结果质量不够，用户不认"),
]


def diagnose(data: Dict) -> Dict:
    """
    定位最大流失步骤，并把「数字」翻译成「归因方向」。

    这一步是运营和数据的分界线：
    数据同学交出来的是一张表，运营交出来的是「所以我们要去改什么」。
    而且这里给出的归因方向，正好能和 feedback 模块的四类归因对上 ——
    两个模块的结论互相印证，这是把三个模块做成一个产品的意义之一。
    """
    funnel = data.get("funnel") or {}
    if not funnel:
        return {"ok": False, "reason": "无漏斗数据"}

    drops = []
    for key, label, hint in _STEP_LABELS:
        rate = funnel.get(key)
        if rate is None:
            continue
        drops.append({"step": label, "rate": rate, "loss": round(1 - rate, 4),
                      "hint": hint})
    worst = min(drops, key=lambda x: x["rate"]) if drops else None

    # 最差渠道（按激活率）
    chans = data.get("channel_performance") or []
    worst_channel = None
    if chans:
        valid = [c for c in chans if c.get("activation_rate") is not None]
        if valid:
            worst_channel = min(valid, key=lambda c: c["activation_rate"])

    return {
        "ok": True,
        "steps": drops,
        "worst_step": worst,
        "worst_channel": worst_channel,
        "conclusion": (
            f"最大流失在「{worst['step']}」，该步通过率仅 {worst['rate'] * 100:.1f}%"
            f"（流失 {worst['loss'] * 100:.1f}%）。归因方向：{worst['hint']}。"
            if worst else "漏斗数据不足"
        ),
    }


def summary() -> Dict:
    """给总览页 / API 用的紧凑摘要。"""
    count = db.query_one("SELECT COUNT(*) AS n FROM events") or {"n": 0}
    if not count["n"]:
        return {"has_data": False}
    data = run_all()
    ret = data.get("retention_summary") or {}
    funnel = data.get("funnel") or {}
    stab = data.get("stability") or []
    cost = data.get("cost_daily") or []
    users = db.query_one("SELECT COUNT(*) AS n FROM users") or {"n": 0}

    recent_stab = stab[-7:] if stab else []
    avg_success = (sum(s["success_rate"] for s in recent_stab) / len(recent_stab)
                   if recent_stab else 0.0)
    total_tokens = sum(c["total_tokens"] or 0 for c in cost)
    total_calls = sum(c["generate_calls"] or 0 for c in cost)
    return {
        "has_data": True,
        "users": users["n"],
        "events": count["n"],
        "days": len(data.get("dau") or []),
        "dau_latest": (data.get("dau") or [{}])[-1].get("dau"),
        "overall_conversion": funnel.get("overall"),
        "retention": {"d1": ret.get("d1_rate"), "d3": ret.get("d3_rate"),
                      "d7": ret.get("d7_rate")},
        "success_rate_7d": round(avg_success, 4),
        "total_tokens": total_tokens,
        "avg_tokens": round(total_tokens / total_calls, 0) if total_calls else 0,
        "worst_channel": (min(data.get("channel_performance") or [],
                              key=lambda c: c.get("activation_rate") or 1) or {}).get("channel")
        if data.get("channel_performance") else None,
        "diagnosis": diagnose(data),
        "events_spec": EVENTS,
    }


def full() -> Dict:
    """完整数据 + SQL 原文，供看板页使用。"""
    data = run_all()
    return {
        "data": data,
        "diagnosis": diagnose(data),
        "sql": sql_source(),
        "events_spec": EVENTS,
    }
