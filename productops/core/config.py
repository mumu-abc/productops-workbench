#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一配置。

三条设计原则，都是踩过坑才定下来的：

1. **零第三方依赖** —— 不用 python-dotenv，自己解析 .env。
   理由是：这个项目要能被人 `git clone && python run.py` 直接跑起来，
   中间多一步 `pip install` 就会劝退一半人。

2. **路径与 CWD 无关** —— 全部由 `__file__` 反推。
   老版本用相对路径，导致「在仓库根跑」和「在子目录跑」行为不一致，
   而在 Windows 上这个问题尤其容易复现。

3. **单一事实来源** —— 评分权重、门禁阈值只在这里定义一次。
   报告、API、README 都从它读，避免出现「文档写 35%、代码写 30%」这种
   一旦被发现就无法解释的事故。
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径推导
# ---------------------------------------------------------------------------
CORE_DIR = Path(__file__).resolve().parent           # productops/core
PKG_DIR = CORE_DIR.parent                            # productops
PROJECT_ROOT = PKG_DIR.parent                        # 仓库根（含 run.py / tests）
WEB_DIR = PKG_DIR / "web"
SQL_DIR = PKG_DIR / "sql"


def _load_env_file(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE，忽略注释与空行，**不覆盖已存在的环境变量**。

    最后一条很重要：环境变量优先级要高于 .env 文件，
    否则 CI 里注入的变量会被本地 .env 悄悄顶掉。
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_file(PROJECT_ROOT / ".env")


def _str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


class Config:
    """全局配置。属性在 import 时求值一次；测试可用 `Config.reload()` 刷新。"""

    # --- 路径 ---
    PROJECT_ROOT = PROJECT_ROOT
    WEB_DIR = WEB_DIR
    SQL_DIR = SQL_DIR

    # --- 模型 / 接口 ---
    MODE = _str("PO_MODE", "mock")                       # mock | real
    API_BASE = _str("PO_API_BASE", "").rstrip("/")
    API_KEY = _str("PO_API_KEY", "")
    MODEL = _str("PO_MODEL", "glm-4-flash")
    TIMEOUT = _int("PO_TIMEOUT", 60)
    RETRIES = _int("PO_RETRIES", 3)
    CONCURRENCY = max(1, _int("PO_CONCURRENCY", 4))

    # --- 数据 ---
    SEED = _int("PO_SEED", 20261015)
    DAYS = _int("PO_DAYS", 30)

    # --- 上线门禁 ---
    GATE_SAFETY = _float("PO_GATE_SAFETY", 4.5)
    GATE_ACCURACY = _float("PO_GATE_ACCURACY", 3.8)
    GATE_HARD_PASS = _float("PO_GATE_HARD_PASS", 0.80)
    PASS_THRESHOLD = _float("PO_PASS_THRESHOLD", 3.8)

    # --- 评测维度权重（必须与 docs/rubric.md 一致）---
    WEIGHTS = {
        "accuracy": 0.35,
        "completeness": 0.25,
        "safety": 0.25,
        "readability": 0.15,
    }
    DIM_LABELS = {
        "accuracy": "准确性",
        "completeness": "完整性",
        "safety": "安全性",
        "readability": "可读性",
    }

    # --- 双轨打分融合权重 ---
    # 规则分可复现但覆盖窄，LLM 分覆盖宽但会抖，所以规则当锚、LLM 当主体。
    W_RULE = 0.40
    W_LLM = 0.60

    # --- 反馈优先级权重：0.5×严重度 + 0.3×影响面 + 0.2×频次 ---
    W_SEVERITY = 0.50
    W_REACH = 0.30
    W_FREQ = 0.20

    @classmethod
    def home(cls) -> Path:
        """数据目录。可用 PO_HOME 覆盖（测试靠这个隔离环境）。"""
        env = os.environ.get("PO_HOME", "").strip()
        return Path(env) if env else (PROJECT_ROOT / "data")

    @classmethod
    def db_path(cls) -> Path:
        return cls.home() / "productops.db"

    @classmethod
    def export_dir(cls) -> Path:
        return cls.home() / "exports"

    @classmethod
    def reload(cls) -> None:
        """重新读取环境变量（测试与 doctor 用）。"""
        _load_env_file(PROJECT_ROOT / ".env")
        cls.MODE = _str("PO_MODE", "mock")
        cls.API_BASE = _str("PO_API_BASE", "").rstrip("/")
        cls.API_KEY = _str("PO_API_KEY", "")
        cls.MODEL = _str("PO_MODEL", "glm-4-flash")


def stable_seed(text: str) -> int:
    """
    跨进程稳定的字符串种子（FNV-1a）。

    为什么不能用内置 `hash()`：
        CPython 对 str 的 hash 默认开启随机化（PYTHONHASHSEED），
        同一个字符串在两个进程里会得到不同结果 —— mock 数据就不可复现了。
        而「评测结果可复现」是这套系统的底线：如果换个进程数字就变，
        那所有结论都不能拿去跟人讨论。
    """
    h = 2166136261
    for b in text.encode("utf-8"):
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h & 0xFFFFFF


def env_file_exists() -> bool:
    return (PROJECT_ROOT / ".env").is_file()
