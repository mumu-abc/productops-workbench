#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出静态 HTML 快照。

为什么需要它：
    服务能跑，不代表你能把结果发给人看。多数人不会 clone 代码再起服务，
    他们只会双击一个文件。所以需要一个**自包含、可单独发送**的产物。

实现取巧但很实用：
    把 API 的响应嵌进 HTML，并劫持 window.fetch 从这个 payload 里取数。
    于是**同一个前端既能接服务、也能离线打开**，不需要维护两套视图代码。
    这也是前面「不用 CDN 图表库」这个决定的第二次回报 ——
    换行本钱已经付过一次了。
"""

import json
import re
from pathlib import Path
from typing import Dict, Optional

from .api.server import ROUTES, Request
from .core import db
from .core.config import Config
from .modules import bridge as br
from .modules import evaluation as ev


def _call(path: str, method: str = "GET", body: Optional[Dict] = None) -> Dict:
    """直接调用路由处理函数（不起 HTTP），拿到和 API 完全一致的 JSON。"""
    body = body or {}
    req = Request(method, path, {}, json.dumps(body).encode("utf-8") if body else b"")
    for m_method, pattern, fn in ROUTES:
        if m_method != method:
            continue
        m = re.match(pattern, path)
        if m:
            out = fn(req, m)
            return out[0] if isinstance(out, tuple) else out
    return {"ok": False, "error": f"no route {method} {path}"}


def collect_payload() -> Dict[str, Dict]:
    """把所有 GET 接口的结果抓成一张表，供前端离线读取。"""
    paths = [
        "/api/health", "/api/overview", "/api/eval/rubric", "/api/eval/runs",
        "/api/feedback/summary", "/api/feedback/items",
        "/api/bridge/coverage", "/api/bridge/promotable", "/api/analytics/all",
    ]
    payload = {p: _call(p) for p in paths}

    # 与具体 id 绑定的接口，逐个补上
    for r in ev.list_runs(limit=10):
        rid = r["run_id"]
        payload[f"/api/eval/run/{rid}"] = _call(f"/api/eval/run/{rid}")
        if ev.previous_run_id(rid):
            payload[f"/api/eval/compare?new={rid}"] = _call(f"/api/eval/compare?new={rid}")

    # 反馈筛选：把常见筛选组合也预生成，离线时筛选也能用
    from .modules import feedback as fb
    for t in list(fb.CATEGORIES.keys()) + [""]:
        for s in list(fb.STATE_LABELS.keys()) + [""]:
            p = f"/api/feedback/items?type={t}&status={s}&q="
            payload[p] = _call(p)
    return payload


def export(out_path: Optional[str] = None) -> str:
    """生成自包含 HTML。"""
    web = Config.WEB_DIR
    html = (web / "index.html").read_text(encoding="utf-8")
    css = (web / "styles.css").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")

    payload = collect_payload()
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    # 防止 payload 里出现 </script> 提前闭合脚本标签
    payload_json = payload_json.replace("</", "<\\/")

    shim = f"""
<script>
/* ---- 离线数据桥：把 fetch 指向内嵌 payload ---- */
window.__OFFLINE__ = true;
window.__BOOTSTRAP__ = {payload_json};
(function(){{
  var P = window.__BOOTSTRAP__;
  function find(url){{
    if (P[url]) return P[url];
    var path = url.split('?')[0];
    if (P[path]) return P[path];
    for (var k in P) {{ if (k.split('?')[0] === path) return P[k]; }}
    return null;
  }}
  window.fetch = function(url, opts){{
    var data = find(String(url));
    if (!data) data = {{ok:false, error:'离线快照中没有这个接口：' + url}};
    return Promise.resolve({{
      ok: true, status: 200,
      json: function(){{ return Promise.resolve(data); }}
    }});
  }};
}})();
</script>
"""

    # 内联资源 + 去掉外链
    html = html.replace('<link rel="stylesheet" href="/static/styles.css">',
                        f"<style>\n{css}\n</style>")
    html = html.replace('<script src="/static/app.js"></script>',
                        shim + f"<script>\n{js}\n</script>")
    html = html.replace("</head>", """<style>
      .officon{position:fixed;right:16px;bottom:16px;z-index:60;background:#1b2130;color:#fff;
        padding:7px 13px;border-radius:8px;font-size:11.5px;opacity:.82;}
    </style></head>""")
    html = html.replace("</body>", """<div class="officon">离线快照 · 导出后数据已冻结</div>
<script>
/* 离线快照里所有写操作都没有意义，点下去只会让人困惑。
 *
 * 第一版是 setTimeout(600ms) 之后把按钮 disabled 掉 —— 有竞态：
 * render() 是异步的，它晚于 600ms 完成时按钮就还是活的。
 * 改成捕获阶段拦点击：跟渲染时机无关，永远不会漏。
 */
(function(){
  var BLOCK = '离线快照不支持写入操作，请在有服务的情况下执行';
  function guard(e){
    var b = e.target && e.target.closest && e.target.closest('#topActions button');
    if (!b) return;
    e.preventDefault(); e.stopPropagation();
    if (window.__PO_TOAST__) window.__PO_TOAST__(BLOCK, 'warn');
    else alert(BLOCK);
  }
  document.addEventListener('click', guard, true);
  // 顺手把按钮变灰，让「不可点」在视觉上也能看出来
  function grey(){
    document.querySelectorAll('#topActions button').forEach(function(b){
      b.disabled = true; b.title = BLOCK;
    });
  }
  document.addEventListener('DOMContentLoaded', function(){
    grey();
    // render() 完成后按钮才存在，用 MutationObserver 兜住异步渲染
    var host = document.getElementById('topActions');
    if (host && window.MutationObserver) {
      new MutationObserver(grey).observe(host, {childList:true, subtree:true});
    }
  });
})();
</script></body>""")

    out = Path(out_path) if out_path else (Config.export_dir() / "dashboard.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return str(out)


def export_summary_md(out_path: Optional[str] = None) -> str:
    """导出一份纯文本摘要，方便直接粘进周报。"""
    ov = _call("/api/overview")
    ev_s = ov.get("evaluation", {})
    fb_s = ov.get("feedback", {})
    an_s = ov.get("analytics", {})
    br_s = ov.get("bridge", {})
    lines = [
        "# ProductOps Workbench · 运行摘要",
        "",
        f"- 数据库：`{Config.db_path()}`（{db.db_size_kb()} KB）",
        f"- 运行模式：{Config.MODE}",
        "",
        "## 效果评测",
        f"- 门禁结论：**{ev_s.get('gate_verdict', '未跑')}**"
        f"（总分 {ev_s.get('total_avg')} ± {ev_s.get('total_std')}，"
        f"通过率 {ev_s.get('pass_rate')}）" if ev_s.get("has_run") else "- 尚未运行",
        f"- 合规缺口：{json.dumps(ev_s.get('safety_gap', {}), ensure_ascii=False)}"
        if ev_s.get("has_run") else "",
        "",
        "## 反馈闭环",
        f"- 反馈总数：{fb_s.get('total')}，去重后 {fb_s.get('group_count')} 个问题",
        f"- 闭环率：{fb_s.get('closed_loop_rate')}",
        f"- 归因分布：{json.dumps(fb_s.get('by_type', {}), ensure_ascii=False)}",
        "",
        "## 数据看板",
        f"- 首日整体转化：{an_s.get('overall_conversion')}",
        f"- 留存：{json.dumps(an_s.get('retention', {}), ensure_ascii=False)}",
        f"- 结论：{(an_s.get('diagnosis') or {}).get('conclusion', '—')}",
        "",
        "## Bridge（吐槽 -> 回归用例）",
        f"- 技术类反馈 {br_s.get('technical_total')} 条，已提升 {br_s.get('promoted')} 条"
        f"（覆盖率 {br_s.get('promote_rate')}）",
        f"- 复测通过 {br_s.get('verified')} / 未通过 {br_s.get('failed')}",
        "",
    ]
    text = "\n".join([x for x in lines if x is not None])
    out = Path(out_path) if out_path else (Config.export_dir() / "summary.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return str(out)
