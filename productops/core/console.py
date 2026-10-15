#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
终端输出。

一个「实打实的项目」和「一段脚本」的区别之一：脚本用 print 糊一行行字，
项目会告诉你「现在在跑什么、跑到第几、结论是什么」。

颜色在非 TTY（重定向到文件、CI）时自动关闭 —— 否则日志里全是转义码。
"""

import os
import sys
import time
from typing import Iterable, List, Sequence

__all__ = ["C", "rule", "title", "kv", "table", "ok", "warn", "err", "info",
           "step", "Progress"]


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    # Windows 10+ 的 conhost / Windows Terminal 支持 ANSI，但需要开启 VT
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            return False
    return True


class C:
    if _supports_color():
        OK, WARN, ERR = "\033[92m", "\033[93m", "\033[91m"
        INFO, DIM, B, END = "\033[96m", "\033[90m", "\033[1m", "\033[0m"
        MAGENTA = "\033[95m"
    else:
        OK = WARN = ERR = INFO = DIM = B = END = MAGENTA = ""


def rule(width: int = 64) -> None:
    print(C.DIM + "─" * width + C.END)


def title(text: str) -> None:
    print()
    print(f"{C.B}{C.INFO}▌{text}{C.END}")


def kv(key: str, value, unit: str = "") -> None:
    print(f"  {key:<22}{C.B}{value}{C.END}{unit}")


def ok(text: str) -> None:
    print(f"  {C.OK}✓{C.END} {text}")


def warn(text: str) -> None:
    print(f"  {C.WARN}!{C.END} {text}")


def err(text: str) -> None:
    print(f"  {C.ERR}✗{C.END} {text}", file=sys.stderr)


def info(text: str) -> None:
    print(f"  {C.DIM}{text}{C.END}")


def step(n: int, total: int, text: str) -> None:
    print(f"  {C.MAGENTA}[{n}/{total}]{C.END} {text}")


def _visible_len(s: str) -> int:
    """按东亚字符宽度算长度，否则中文表格会歪。"""
    n = 0
    for ch in s:
        n += 2 if ("\u1100" <= ch <= "\u115f" or "\u2e80" <= ch <= "\ua4cf"
                   or "\uac00" <= ch <= "\ud7a3" or "\uf900" <= ch <= "\ufaff"
                   or "\ufe30" <= ch <= "\ufe6f" or "\uff00" <= ch <= "\uff60"
                   or "\uffe0" <= ch <= "\uffe6") else 1
    return n


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _visible_len(s))


def table(headers: Sequence[str], rows: Iterable[Sequence], max_width: int = 34) -> None:
    rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    for r in rows:
        for i, v in enumerate(r):
            if _visible_len(v) > max_width:
                cut = 0
                total = 0
                for ch in v:
                    total += 2 if ord(ch) > 0x2E7F else 1
                    if total > max_width - 1:
                        break
                    cut += 1
                r[i] = v[:cut] + "…"

    widths = [max(_visible_len(h), *(  [_visible_len(r[i]) for r in rows] or [0]))
              for i, h in enumerate(headers)]
    print("  " + "  ".join(C.B + _pad(h, w) + C.END for h, w in zip(headers, widths)))
    print("  " + "  ".join(C.DIM + "─" * w + C.END for w in widths))
    for r in rows:
        print("  " + "  ".join(_pad(v, w) for v, w in zip(r, widths)))


class Progress:
    """极简进度条。避免为了一行进度条引入 tqdm。"""

    def __init__(self, total: int, label: str = "", width: int = 28):
        self.total = max(1, total)
        self.label = label
        self.width = width
        self.n = 0
        self.t0 = time.time()
        self._enabled = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    def tick(self, n: int = 1) -> None:
        self.n += n
        if not self._enabled:
            return
        ratio = min(1.0, self.n / self.total)
        filled = int(self.width * ratio)
        bar = "█" * filled + "·" * (self.width - filled)
        elapsed = time.time() - self.t0
        sys.stdout.write(f"\r  {self.label} [{bar}] {self.n}/{self.total}  {elapsed:5.1f}s")
        sys.stdout.flush()

    def done(self) -> None:
        if self._enabled:
            sys.stdout.write("\r" + " " * 78 + "\r")
            sys.stdout.flush()


def fmt_pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%"
