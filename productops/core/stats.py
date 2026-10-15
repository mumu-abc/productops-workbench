#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计工具。集中放一处，避免每个模块各写一遍 round() 然后四舍五入口径不一致。"""

import math
import statistics
from typing import List, Optional

__all__ = ["avg", "std", "pct", "safe_div", "percentile", "log_norm", "clamp"]


def avg(values: List[float], digits: int = 2) -> float:
    return round(statistics.fmean(values), digits) if values else 0.0


def std(values: List[float], digits: int = 2) -> float:
    """总体标准差。样本数 <2 时返回 0（而不是抛异常）。"""
    return round(statistics.pstdev(values), digits) if len(values) > 1 else 0.0


def pct(numerator: float, denominator: float, digits: int = 4) -> float:
    """比率，分母为 0 时返回 0.0 而不是崩掉。"""
    if not denominator:
        return 0.0
    return round(numerator / denominator, digits)


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def percentile(values: List[float], p: float) -> float:
    """
    线性插值分位数。**p 取 0~1**（不是 0~100）。算 P95 延迟这种指标用。

    p 会被夹到 [0,1]：调用方很容易顺手传 95 而不是 0.95，
    第一版没夹取，于是 `xs[int(95 * (n-1))]` 直接 IndexError，
    报错信息还停在 IndexError 上，看不出是「百分位写成了百分数」。
    """
    if not values:
        return 0.0
    p = max(0.0, min(1.0, p))
    xs = sorted(values)
    if len(xs) == 1:
        return round(xs[0], 2)
    pos = p * (len(xs) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return round(xs[min(lo, len(xs) - 1)], 2)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo), 2)


def log_norm(value: float, ceiling: float) -> float:
    """
    对数归一化到 0~1。

    为什么影响面 / 频次要用对数而不是线性：
        「影响 1000 人」和「影响 100 人」的差别，远大于「100 和 10」的差别，
        但绝不应该是 10 倍。线性缩放会让少数极端值直接绑架整个排序，
        对数让「量级」说话。
    """
    if value <= 0:
        return 0.0
    return min(1.0, math.log10(value + 1) / math.log10(ceiling + 1))


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
