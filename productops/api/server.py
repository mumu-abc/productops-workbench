#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REST API + 静态资源服务（纯标准库）。

设计取舍：
- **不用 Flask/FastAPI。** 这个产品的主张是「clone 下来就能跑」，
  而 http.server + ThreadingHTTPServer 足够撑住一个单人演示的看板。
  真要上生产，换 FastAPI 只需要把下面每个 handler 搬过去，业务层一行不用改。
- **默认只绑 127.0.0.1。** 不绑 0.0.0.0 —— 一个没人管的本机演示服务
  如果暴露在局域网里，就是给别人开了一扇门。要对外暴露必须显式传 --host。
- **用 ThreadingHTTPServer。** 跑一次全量评测要几秒到几十秒，
  单线程会把整个 UI 卡死；多线程下前端还能正常轮询。
"""

import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..core import db
from ..core.config import Config
from ..modules import analytics as an
from ..modules import bridge as br
from ..modules import evaluation as ev
from ..modules import feedback as fb

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".woff2": "font/woff2",
}

ROUTES: List[Tuple[str, str, Callable]] = []


def route(method: str, pattern: str):
    """注册路由。pattern 是正则，捕获组按顺序传给 handler。"""
    def deco(fn):
        ROUTES.append((method, pattern, fn))
        return fn
    return deco


# ===========================================================================
# 总览
# ===========================================================================
@route("GET", r"^/api/health$")
def h_health(req, m):
    c = db.counts()
    return {
        "ok": True, "version": __version__,
        "mode": Config.MODE,
        "db": str(Config.db_path()), "db_size_kb": db.db_size_kb(),
        "counts": c,
        "initialized": c["eval_samples"] > 0 or c["fb_items"] > 0 or c["events"] > 0,
    }


@route("GET", r"^/api/overview$")
def h_overview(req, m):
    counts = db.counts()
    ev_s = ev.summary_for_api()
    fb_s = fb.summary()
    an_s = an.summary()
    cov = br.coverage()

    highlights: List[Dict] = []
    if ev_s.get("has_run"):
        highlights.append({
            "module": "评测", "tone": "warn" if ev_s["gate_verdict"] == "禁止上线" else "ok",
            "headline": f"门禁：{ev_s['gate_verdict']}",
            "detail": f"加权总分 {ev_s['total_avg']} ± {ev_s['total_std']}，"
                      f"通过率 {ev_s['pass_rate'] * 100:.1f}%",
        })
    if fb_s.get("has_data"):
        highlights.append({
            "module": "反馈", "tone": "warn" if fb_s["closed_loop_rate"] < 0.5 else "ok",
            "headline": f"闭环率 {fb_s['closed_loop_rate'] * 100:.1f}%",
            "detail": f"{fb_s['total']} 条反馈归为 {fb_s['group_count']} 个问题，"
                      f"去重省掉 {fb_s['dedup_saving']} 条重复",
        })
    if an_s.get("has_data"):
        highlights.append({
            "module": "数据", "tone": "info",
            "headline": f"首日整体转化 {an_s['overall_conversion'] * 100:.1f}%"
                        if an_s.get("overall_conversion") is not None else "数据已就绪",
            "detail": an_s.get("diagnosis", {}).get("conclusion", ""),
        })
    if cov.get("has_data"):
        highlights.append({
            "module": "bridge", "tone": "ok" if cov["promoted"] else "info",
            "headline": f"{cov['promoted']} 条吐槽已变成回归用例",
            "detail": f"技术类反馈覆盖率 {cov['promote_rate'] * 100:.1f}%，"
                      f"其中复测通过 {cov['verified']} 条 / 未通过 {cov['failed']} 条",
        })

    # 总览页只需要 Top5，完整清单在反馈页 —— 避免首页 payload 过大
    if fb_s.get("has_data"):
        fb_brief = dict(fb_s)
        fb_brief["top_groups"] = fb_s.get("top_groups", [])[:5]
    else:
        fb_brief = fb_s

    return {"ok": True, "counts": counts, "evaluation": ev_s,
            "feedback": fb_brief, "analytics": an_s, "bridge": cov,
            "highlights": highlights}


# ===========================================================================
# 评测
# ===========================================================================
@route("GET", r"^/api/eval/rubric$")
def h_rubric(req, m):
    return {"ok": True, "rubric": ev.rubric()}


@route("GET", r"^/api/eval/samples$")
def h_samples(req, m):
    q = req.query
    rows = ev.load_samples(difficulty=q.get("difficulty") or None,
                           origin=q.get("origin") or None)
    return {"ok": True, "count": len(rows), "samples": rows}


@route("GET", r"^/api/eval/runs$")
def h_runs(req, m):
    return {"ok": True, "runs": ev.list_runs(limit=int(req.query.get("limit", 30)))}


@route("GET", r"^/api/eval/run/(\d+)$")
def h_run_detail(req, m):
    try:
        return {"ok": True, **ev.load_run(int(m.group(1)))}
    except KeyError as e:
        return {"ok": False, "error": str(e)}, 404


@route("GET", r"^/api/eval/compare$")
def h_compare(req, m):
    base = req.query.get("base")
    new = req.query.get("new") or ev.latest_run_id()
    base = base or ev.previous_run_id(int(new)) if new else None
    if not base or not new:
        return {"ok": False, "error": "需要至少两次评测才能对比"}, 400
    return {"ok": True, **ev.compare_runs(int(base), int(new))}


@route("POST", r"^/api/eval/run$")
def h_run_eval(req, m):
    body = req.json_body
    if not ev.load_samples():
        ev.save_evalset(ev.build_evalset())
    res = ev.run_evaluation(limit=int(body.get("limit") or 0),
                            note=body.get("note") or "（看板发起）")
    return {"ok": True, "run_id": res["run_id"], "model": res["model"],
            "mode": res["mode"], "fell_back": res["fell_back"],
            "samples": res["samples"], "gate_verdict": res["metrics"]["gate"]["verdict"],
            "metrics": res["metrics"]}


# ===========================================================================
# 反馈
# ===========================================================================
@route("GET", r"^/api/feedback/summary$")
def h_fb_summary(req, m):
    return {"ok": True, **fb.summary()}


@route("GET", r"^/api/feedback/items$")
def h_fb_items(req, m):
    q = req.query
    rows = fb.list_items(cat=q.get("type") or None, status=q.get("status") or None,
                         min_priority=float(q.get("min_priority") or 0),
                         keyword=q.get("q") or "", limit=int(q.get("limit", 200)))
    return {"ok": True, "count": len(rows), "items": rows}


@route("GET", r"^/api/feedback/item/(\d+)$")
def h_fb_item(req, m):
    r = fb.get_item(int(m.group(1)))
    return ({"ok": True, "item": r} if r else ({"ok": False, "error": "不存在"}, 404))


@route("GET", r"^/api/feedback/report$")
def h_fb_report(req, m):
    return {"ok": True, "text": fb.weekly_report_text()}


@route("POST", r"^/api/feedback/seed$")
def h_fb_seed(req, m):
    n = fb.seed_items()
    return {"ok": True, "seeded": n}


@route("POST", r"^/api/feedback/classify$")
def h_fb_classify(req, m):
    return {"ok": True, **fb.run_classification()}


@route("POST", r"^/api/feedback/advance$")
def h_fb_advance(req, m):
    b = req.json_body
    try:
        return {"ok": True, **fb.advance(int(b["item_id"]), b["to"], b.get("note", ""))}
    except (KeyError, ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}, 400


@route("POST", r"^/api/feedback/add$")
def h_fb_add(req, m):
    b = req.json_body
    if not b.get("text"):
        return {"ok": False, "error": "text 不能为空"}, 400
    iid = fb.add_item(b["text"], b.get("channel", "手动录入"),
                      int(b.get("severity", 3)), int(b.get("users_affected", 0)),
                      int(b.get("freq", 1)))
    return {"ok": True, "item_id": iid}


# ===========================================================================
# Bridge
# ===========================================================================
@route("GET", r"^/api/bridge/promotable$")
def h_br_promotable(req, m):
    return {"ok": True, "items": br.promotable(
        min_priority=float(req.query.get("min_priority") or 60),
        category=req.query.get("type") or "technical",
        only_unpromoted=(req.query.get("all") != "1"),
        limit=int(req.query.get("limit", 30)))}


@route("GET", r"^/api/bridge/coverage$")
def h_br_coverage(req, m):
    return {"ok": True, **br.coverage()}


@route("GET", r"^/api/bridge/verify/(\d+)$")
def h_br_verify(req, m):
    try:
        return {"ok": True, **br.verify(int(m.group(1)))}
    except KeyError as e:
        return {"ok": False, "error": str(e)}, 404


@route("POST", r"^/api/bridge/promote$")
def h_br_promote(req, m):
    b = req.json_body
    try:
        return {"ok": True, **br.promote(int(b["item_id"]),
                                        difficulty=b.get("difficulty", "hard"),
                                        note=b.get("note", ""))}
    except (KeyError, ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}, 400


@route("POST", r"^/api/bridge/auto$")
def h_br_auto(req, m):
    b = req.json_body
    return {"ok": True, **br.auto_promote(top_n=int(b.get("top_n", 5)),
                                          min_priority=float(b.get("min_priority", 60)))}


@route("POST", r"^/api/bridge/close$")
def h_br_close(req, m):
    b = req.json_body
    try:
        return {"ok": True, **br.close_if_verified(int(b["item_id"]),
                                                   b.get("note", ""))}
    except (KeyError, ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}, 400


# ===========================================================================
# 数据看板
# ===========================================================================
@route("GET", r"^/api/analytics/all$")
def h_an_all(req, m):
    f = an.full()
    return {"ok": True, "data": f["data"], "diagnosis": f["diagnosis"],
            "events_spec": f["events_spec"], "sql": f["sql"]}


@route("GET", r"^/api/analytics/summary$")
def h_an_summary(req, m):
    return {"ok": True, **an.summary()}


@route("POST", r"^/api/analytics/seed$")
def h_an_seed(req, m):
    days = int(req.json_body.get("days") or Config.DAYS)
    return {"ok": True, **an.generate(days=days)}


# ===========================================================================
# HTTP 处理
# ===========================================================================
class Request:
    def __init__(self, method: str, path: str, query: Dict[str, str], body: bytes):
        self.method = method
        self.path = path
        self.query = query
        self.body = body

    @property
    def json_body(self) -> Dict:
        if not self.body:
            return {}
        try:
            obj = json.loads(self.body.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}


class Handler(BaseHTTPRequestHandler):
    server_version = f"ProductOps/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):     # 默认日志太吵，压成一行简短输出
        if self.server.verbose:            # type: ignore[attr-defined]
            print(f"  {self.address_string()} {fmt % args}")

    # -- 工具 --
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   MIME[".json"])

    def _static(self, rel: str):
        """从 web/ 目录读静态文件。做了路径穿越防护。"""
        base = Config.WEB_DIR.resolve()
        target = (base / rel.lstrip("/")).resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            self._send(404, b"404 not found", "text/plain; charset=utf-8")
            return
        self._send(200, target.read_bytes(),
                   MIME.get(target.suffix.lower(), "application/octet-stream"))

    # -- 分发 --
    def _handle(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        req = Request(self.command, path, query, body)

        for method, pattern, fn in ROUTES:
            m = re.match(pattern, path)
            if not m:
                continue
            if method != self.command:
                continue
            try:
                result = fn(req, m)
            except Exception as e:          # 兜底：API 不该让整个服务挂掉
                tb = traceback.format_exc(limit=3)
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}",
                                   "traceback": tb}, 500)
            if isinstance(result, tuple):
                return self._json(result[0], result[1])
            return self._json(result)

        self._json({"ok": False, "error": f"未知接口 {self.command} {path}"}, 404)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_HEAD(self):
        self._handle()


def serve(host: str = "127.0.0.1", port: int = 8770, verbose: bool = False) -> None:
    # 允许在空库上启动：前端会检测 /api/health 里的 initialized 并引导用户先 init
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.verbose = verbose        # type: ignore[attr-defined]
    shown = host if host != "0.0.0.0" else "127.0.0.1"
    print()
    print(f"  ProductOps Workbench v{__version__}")
    print(f"  看板地址：http://{shown}:{port}")
    print(f"  数据库：  {Config.db_path()}")
    print(f"  绑定地址：{host}" + ("  ← 已暴露到局域网" if host == "0.0.0.0" else "  （仅本机可访问）"))
    print("  Ctrl+C 停止")
    print()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        httpd.server_close()
