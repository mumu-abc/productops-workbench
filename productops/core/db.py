#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库层：一个产品，一个库。

为什么要把三个模块的数据放在**同一个** sqlite 文件里（而不是各管各的）：
    因为 bridge 需要跨模块查询 —— 「这条被归因为技术问题的反馈，
    后来是不是变成评测样本了？复测通过了吗？」这个问题横跨反馈表和评测表。
    分成三个库就只能靠导出文件对账，那是三个脚本而不是一个产品。

迁移策略：
    建表全部用 `CREATE TABLE IF NOT EXISTS`，每次连接时执行一遍 ——
    幂等、无版本号、不需要迁移框架。对这个规模的项目，这是最优解；
    等到需要改列类型时再引入真正的迁移，不提前复杂化。
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import Config

SCHEMA = """
-- ===========================================================================
-- 模块一：效果评测
-- ===========================================================================

-- 评测集。入库而不是只放 jsonl 的原因：bridge 需要往里面「追加」样本，
-- 而追加来的样本必须能溯源到具体哪条用户反馈。
CREATE TABLE IF NOT EXISTS eval_samples (
    sample_id    TEXT PRIMARY KEY,
    category     TEXT NOT NULL,
    difficulty   TEXT NOT NULL,
    question     TEXT NOT NULL,
    expected     TEXT,
    keywords     TEXT,                    -- JSON 数组
    must_not     TEXT,                    -- JSON 数组
    tags         TEXT,                    -- JSON 数组
    origin       TEXT DEFAULT 'builtin',  -- builtin | imported | feedback
    source_item  INTEGER,                 -- 若由反馈提升而来，记录 fb_items.item_id
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_origin ON eval_samples(origin);
CREATE INDEX IF NOT EXISTS idx_samples_diff ON eval_samples(difficulty);

-- 每次评测 = 一次 run。保留历史是为了做版本对比：
-- 「改了 Prompt 之后，哪些样本退化了」必须能查到上一次。
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    model        TEXT NOT NULL,
    mode         TEXT NOT NULL,
    note         TEXT,
    sample_count INTEGER,
    total_avg    REAL,
    total_std    REAL,
    pass_rate    REAL,
    gate_verdict TEXT,
    metrics_json TEXT
);

CREATE TABLE IF NOT EXISTS eval_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL,
    sample_id    TEXT NOT NULL,
    category     TEXT,
    difficulty   TEXT,
    question     TEXT,
    answer       TEXT,
    total        REAL,
    accuracy     REAL,
    completeness REAL,
    safety       REAL,
    readability  REAL,
    rule_total   REAL,
    llm_total    REAL,
    passed       INTEGER,
    latency      REAL,
    tokens       INTEGER,
    reason       TEXT,
    FOREIGN KEY (run_id) REFERENCES eval_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_results_run ON eval_results(run_id);
CREATE INDEX IF NOT EXISTS idx_results_sample ON eval_results(sample_id);

-- ===========================================================================
-- 模块二：反馈闭环
-- ===========================================================================
CREATE TABLE IF NOT EXISTS fb_items (
    item_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ext_id         TEXT UNIQUE,          -- 外部渠道原始 id，保证重复导入幂等
    text           TEXT NOT NULL,
    channel        TEXT,
    severity       INTEGER DEFAULT 3,    -- 1~5
    users_affected INTEGER DEFAULT 0,
    freq           INTEGER DEFAULT 1,
    gt_type        TEXT,                 -- 人工标注（有则用于评估分类器）
    pred_type      TEXT,                 -- 分类器预测
    pred_conf      REAL,
    pred_reason    TEXT,
    classifier     TEXT,
    priority       REAL,
    band           TEXT,
    owner          TEXT,
    status         TEXT DEFAULT 'new',
    promoted_sample TEXT,                -- 被 bridge 提升为评测样本后的 sample_id
    created_at     TEXT,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_fb_status ON fb_items(status);
CREATE INDEX IF NOT EXISTS idx_fb_type ON fb_items(pred_type);

-- 状态流转审计表。闭环系统的可信度全靠它：
-- 没有审计表，「这条反馈修了吗」就只能靠人嘴说。
CREATE TABLE IF NOT EXISTS fb_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT,
    FOREIGN KEY (item_id) REFERENCES fb_items(item_id)
);
CREATE INDEX IF NOT EXISTS idx_fbe_item ON fb_events(item_id);

-- ===========================================================================
-- 模块三：数据看板（埋点）
-- ===========================================================================
CREATE TABLE IF NOT EXISTS users (
    user_id   TEXT PRIMARY KEY,
    first_day TEXT NOT NULL,
    channel   TEXT,
    platform  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    event       TEXT NOT NULL,
    day         TEXT NOT NULL,
    ts          TEXT NOT NULL,
    channel     TEXT,
    platform    TEXT,
    tokens      INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
CREATE INDEX IF NOT EXISTS idx_events_channel ON events(channel);

-- ===========================================================================
-- 运行时元信息（上次初始化时间、数据来源等）
-- ===========================================================================
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """打开连接并保证表结构存在（幂等）。"""
    p = Path(path) if path else Config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10)
    conn.row_factory = sqlite3.Row
    # 看板跑在 ThreadingHTTPServer 上，同一时刻可能有「写评测」和「读看板」并发。
    # sqlite 默认 5s 锁等待，高并发下会直接抛 database is locked ——
    # 这里放宽到 10s，让写操作排队而不是失败。
    # 不用 WAL：WAL 会额外产生 -wal/-shm 两个旁挂文件，
    # 而这个产品的一个卖点就是「数据只有 productops.db 一个文件，拷走就能跑」。
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(path: Optional[Path] = None):
    """事务上下文。异常时回滚 —— 避免「跑一半失败留下半套数据」这种最难查的状态。"""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: Iterable = (), path: Optional[Path] = None) -> List[Dict[str, Any]]:
    conn = connect(path)
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def query_one(sql: str, params: Iterable = (), path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    rows = query(sql, params, path)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable = (), path: Optional[Path] = None) -> int:
    with tx(path) as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.lastrowid or cur.rowcount


def executescript(script: str, path: Optional[Path] = None) -> None:
    with tx(path) as conn:
        conn.executescript(script)


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------
def meta_set(key: str, value: str, path: Optional[Path] = None) -> None:
    execute("INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value), path)


def meta_get(key: str, default: Optional[str] = None, path: Optional[Path] = None) -> Optional[str]:
    row = query_one("SELECT value FROM meta WHERE key=?", (key,), path)
    return row["value"] if row else default


# ---------------------------------------------------------------------------
# 维护
# ---------------------------------------------------------------------------
def db_exists(path: Optional[Path] = None) -> bool:
    p = Path(path) if path else Config.db_path()
    return p.is_file()


def db_size_kb(path: Optional[Path] = None) -> float:
    p = Path(path) if path else Config.db_path()
    return round(p.stat().st_size / 1024, 1) if p.is_file() else 0.0


def counts(path: Optional[Path] = None) -> Dict[str, int]:
    """各表行数，doctor 和总览页都用它。"""
    out: Dict[str, int] = {}
    for t in ("eval_samples", "eval_runs", "eval_results", "fb_items",
              "fb_events", "users", "events"):
        try:
            row = query_one(f"SELECT COUNT(*) AS n FROM {t}", (), path)
            out[t] = int(row["n"]) if row else 0
        except sqlite3.Error:
            out[t] = 0
    return out


def clear_analytics(path: Optional[Path] = None) -> None:
    """只清埋点数据，不动评测与反馈。"""
    with tx(path) as conn:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM users")


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
