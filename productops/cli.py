#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一命令行。

一个产品只需要记住一条命令：`python run.py <子命令>`。
三个模块各有自己的子命令组，另外有三个跨模块的：
    init    一次性把数据准备好
    doctor  自检（环境、配置、数据完整性）
    demo    把「吐槽 -> 回归用例 -> 复测 -> 关单」整条链路演一遍
    serve   起看板
    export  导出自包含 HTML 快照
"""

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .core import console as c
from .core import db, llm
from .core.config import Config, stable_seed
from .export import export as export_html, export_summary_md
from .modules import analytics as an
from .modules import bridge as br
from .modules import evaluation as ev
from .modules import feedback as fb


# ===========================================================================
def cmd_init(args) -> int:
    c.title("初始化数据")
    if args.reset and Config.db_path().is_file():
        Config.db_path().unlink()
        c.warn(f"已删除旧数据库 {Config.db_path()}")
    Config.home().mkdir(parents=True, exist_ok=True)

    total = 4
    c.step(1, total, "生成评测集（25 场景 × 4 难度变体）")
    n = ev.save_evalset(ev.build_evalset())
    c.ok(f"{n} 条样本入库（easy 25 / medium 25 / hard 50）")

    c.step(2, total, "灌入示例反馈并自动归因")
    c.ok(f"{fb.seed_items()} 条反馈入库")
    r = fb.run_classification()
    c.ok(f"规则分类器 {r['rule_eval']['accuracy'] * 100:.1f}% "
         f"vs LLM {r['llm_eval']['accuracy'] * 100:.1f}%")

    if args.no_analytics:
        c.warn("跳过埋点数据生成（--no-analytics）")
    else:
        c.step(3, total, f"生成 {Config.DAYS} 天埋点数据")
        a = an.generate()
        c.ok(f"{a['users']} 用户 / {a['events']} 事件")

    c.step(4, total, "跑首次评测（建立版本基线）")
    res = ev.run_evaluation(note="init 基线")
    c.ok(f"评测 #{res['run_id']}，门禁结论「{res['metrics']['gate']['verdict']}」")

    db.meta_set("initialized_at", db.now())
    db.meta_set("initialized_mode", Config.MODE)
    c.title("完成")
    c.kv("数据库", Config.db_path())
    c.kv("大小", db.db_size_kb(), " KB")
    print()
    c.info("下一步：python run.py serve   然后浏览器打开提示的地址")
    return 0


# ===========================================================================
def cmd_doctor(args) -> int:
    c.title("环境自检")
    problems = 0

    def check(name, ok, detail="", fatal=False):
        nonlocal problems
        if ok:
            c.ok(f"{name}{'：' + detail if detail else ''}")
        else:
            (c.err if fatal else c.warn)(f"{name}{'：' + detail if detail else ''}")
            problems += 1

    check("Python", sys.version_info >= (3, 9),
          f"{sys.version.split()[0]}（需要 >= 3.9）", fatal=True)
    check("运行模式", True,
          f"{Config.MODE}" + ("（不联网、可复现）" if Config.MODE == "mock" else ""))
    if Config.MODE == "real":
        check("real 模式配置", llm.is_real_mode(),
              "PO_API_KEY / PO_API_BASE 已配置" if llm.is_real_mode()
              else "缺少 PO_API_KEY 或 PO_API_BASE，会自动回退 mock")
    check("数据库", db.db_exists(), str(Config.db_path()) + 
          (f"（{db.db_size_kb()} KB）" if db.db_exists() else "（不存在，请先 init）"))
    check("Web 资源", (Config.WEB_DIR / "index.html").is_file(), str(Config.WEB_DIR))
    check("SQL 模板", (Config.SQL_DIR / "analytics.sql").is_file(), str(Config.SQL_DIR))

    # 稳定性自检：这是整个项目可复现的前提
    s1, s2 = stable_seed("doctor-check"), stable_seed("doctor-check")
    check("随机种子可复现", s1 == s2, f"stable_seed('doctor-check') = {s1}")

    counts = db.counts()
    print()
    c.title("数据完整性")
    for k, v in counts.items():
        (c.ok if v else c.warn)(f"{k:<16} {v:>8}")

    if counts["eval_samples"] == 0:
        c.warn("评测集为空 —— 执行 python run.py init")
    if counts["eval_runs"] == 0:
        c.warn("还没有评测记录 —— 执行 python run.py eval run")
    if counts["fb_items"] and counts["fb_items"] > 0:
        s = fb.summary()
        if any(it.get("pred_type") in (None, "") for it in db.query(
                "SELECT pred_type FROM fb_items LIMIT 5")):
            c.warn("有反馈尚未归因 —— 执行 python run.py feedback run")
        else:
            c.ok(f"反馈闭环率 {s['closed_loop_rate'] * 100:.1f}%"
                 f"（{s['closed_count']}/{s['total']}）")

    print()
    if problems:
        c.warn(f"自检完成，发现 {problems} 处提醒（多数不影响 mock 模式运行）")
    else:
        c.ok("自检通过，一切正常")
    return 0 if problems == 0 else 0


# ===========================================================================
def cmd_eval(args) -> int:
    if args.action == "build":
        n = ev.save_evalset(ev.build_evalset())
        c.ok(f"{n} 条样本已生成并入库")
        return 0

    if args.action in ("run", "runs", "show", "compare", "import", "export", "samples"):
        pass
    else:
        c.err(f"未知动作 {args.action}")
        return 2

    if args.action == "run":
        if not ev.load_samples():
            c.warn("评测集为空，先自动生成")
            ev.save_evalset(ev.build_evalset())
        print()
        c.title("开始评测")
        bar = c.Progress(args.limit or len(ev.load_samples()), "评测进度")
        res = ev.run_evaluation(limit=args.limit, note=args.note,
                                difficulty=args.difficulty,
                                progress=lambda i, n, sid: bar.tick())
        bar.done()
        m = res["metrics"]
        o = m["overall"]
        if res["fell_back"]:
            c.warn("real 模式配置不全，已回退 mock")
        print()
        c.kv("运行编号", res["run_id"])
        c.kv("被测模型", res["model"], f"  （{res['mode']} 模式）")
        c.kv("样本数", res["samples"])
        c.kv("加权总分", f"{o['total_avg']} ± {o['total_std']}")
        c.kv("通过率", f"{o['pass_rate'] * 100:.1f}%  ({o['pass_count']}/{o['count']})")
        c.kv("规则/LLM 均分", f"{o['rule_avg']} / {o['llm_avg']}")
        print()
        c.title("四维得分")
        c.table(["维度", "均分", "标准差", "最低", "最高"],
                [[Config.DIM_LABELS[d], v["avg"], v["std"], v["min"], v["max"]]
                 for d, v in o["dims"].items()])
        print()
        c.title("分层通过率")
        c.table(["难度", "样本", "均分", "通过率"],
                [[k, v["count"], v["total_avg"], f"{v['pass_rate'] * 100:.1f}%"]
                 for k, v in m["by_difficulty"].items()])
        print()
        c.title("上线门禁")
        c.table(["检查项", "实测", "阈值", "结果", "未达标动作"],
                [[ch["name"], ch["value"], f"{ch['op']} {ch['threshold']}",
                  "达标" if ch["ok"] else "未达标", ch["on_fail"]]
                 for ch in m["gate"]["checks"]])
        verdict = m["gate"]["verdict"]
        print()
        (c.ok if verdict == "允许上线" else c.warn if verdict == "允许灰度" else c.err)(
            f"判定：{verdict}")
        sg = m["safety_gap"]
        if sg.get("boundary_total"):
            c.info(f"合规缺口：{sg['boundary_failed']}/{sg['boundary_total']} "
                   f"条边界样本未正确拒绝（{sg['gap_rate'] * 100:.1f}%）")
        return 0

    if args.action == "runs":
        runs = ev.list_runs(limit=args.limit)
        if not runs:
            c.warn("还没有评测记录")
            return 0
        c.table(["#", "时间", "模型", "样本", "总分", "σ", "通过率", "门禁"],
                [[r["run_id"], r["created_at"], r["model"], r["sample_count"],
                  r["total_avg"], r["total_std"], f"{(r['pass_rate'] or 0) * 100:.1f}%",
                  r["gate_verdict"]] for r in runs])
        return 0

    if args.action == "show":
        rid = args.run_id or ev.latest_run_id()
        if not rid:
            c.warn("还没有评测记录")
            return 0
        d = ev.load_run(rid)
        c.title(f"评测 #{rid}  {d['run']['created_at']}")
        c.kv("总分", d["run"]["total_avg"])
        c.kv("门禁", d["run"]["gate_verdict"])
        fails = [r for r in d["results"] if not r["passed"]][:args.top]
        print()
        c.table(["样本", "难度", "总分", "问题"],
                [[r["sample_id"], r["difficulty"], r["total"],
                  (r["question"] or "")[:30]] for r in fails])
        return 0

    if args.action == "compare":
        new_id = args.new or ev.latest_run_id()
        base_id = args.base or (ev.previous_run_id(new_id) if new_id else None)
        if not base_id or not new_id:
            c.warn("需要至少两次评测才能对比")
            return 0
        cmpr = ev.compare_runs(base_id, new_id)
        c.title(f"版本对比 #{base_id} → #{new_id}")
        c.kv("均分", f"{cmpr['base_avg']} → {cmpr['new_avg']} "
                     f"({'+' if cmpr['avg_delta'] >= 0 else ''}{cmpr['avg_delta']})")
        c.kv("可比样本", cmpr["comparable_count"])
        c.kv("退化样本", cmpr["regression_count"])
        c.kv("改善样本", cmpr["improvement_count"])
        print()
        c.warn(cmpr["verdict"])
        if cmpr["regressions"]:
            print()
            c.table(["样本", "旧", "新", "Δ", "问题"],
                    [[r["sample_id"], r["old"], r["new"], r["delta"],
                      (r["question"] or "")[:28]] for r in cmpr["regressions"][:15]])
        return 0

    if args.action == "import":
        n = ev.import_evalset(args.path)
        c.ok(f"已导入 {n} 条样本（origin=imported）")
        return 0

    if args.action == "export":
        n = ev.export_evalset(args.path)
        c.ok(f"已导出 {n} 条样本到 {args.path}")
        return 0

    if args.action == "samples":
        rows = ev.load_samples(difficulty=args.difficulty, origin=args.origin)
        c.table(["样本", "类别", "难度", "来源", "问题"],
                [[r["id"], r["category"], r["difficulty"], r.get("origin", ""),
                  r["question"][:30]] for r in rows[:args.top]])
        c.info(f"共 {len(rows)} 条")
        return 0
    return 0


# ===========================================================================
def cmd_feedback(args) -> int:
    if args.action == "seed":
        c.ok(f"{fb.seed_items()} 条示例反馈已入库（重复执行幂等）")
        return 0

    if args.action == "run":
        print()
        c.title("反馈归因")
        bar = c.Progress(len(db.query("SELECT item_id FROM fb_items")), "归因进度")
        r = fb.run_classification(progress=lambda i, n, _: bar.tick())
        bar.done()
        print()
        c.kv("反馈总数", r["items"])
        c.kv("规则分类器", f"{r['rule_eval']['accuracy'] * 100:.1f}%")
        c.kv("LLM 分类器", f"{r['llm_eval']['accuracy'] * 100:.1f}%")
        c.kv("自动派单率", f"{r['auto_dispatch_rate'] * 100:.1f}%"
                          "  （置信度 >= 0.75 才自动派）")
        print()
        c.title("LLM 混淆矩阵（行=人工标注，列=预测）")
        cats = fb.CAT_ORDER
        mx = r["llm_eval"]["matrix"]
        c.table(["真实 \\ 预测"] + [fb.CATEGORIES[x]["label"] for x in cats],
                [[fb.CATEGORIES[g]["label"]] + [mx.get(g, {}).get(p, 0) for p in cats]
                 for g in cats])
        print()
        c.title("每类召回")
        c.table(["类别", "样本", "召回率", "最常被错判为"],
                [[fb.CATEGORIES[k]["label"], v["total"], f"{v['recall'] * 100:.1f}%",
                  "、".join(f"{fb.CATEGORIES.get(a, {}).get('label', a)}×{b}"
                            for a, b in v["missed_as"]) or "—"]
                 for k, v in r["llm_eval"]["per_category"].items()])
        return 0

    if args.action == "list":
        rows = fb.list_items(cat=args.type, status=args.status, limit=args.top)
        if not rows:
            c.warn("没有符合条件的反馈")
            return 0
        c.table(["#", "优先级", "档位", "归因", "状态", "责任人", "原文"],
                [[r["item_id"], r["priority"], r["band"], r["label"],
                  r["status_label"], r["owner"] or "-", r["text"][:30]] for r in rows])
        return 0

    if args.action == "report":
        print()
        print(fb.weekly_report_text())
        return 0

    if args.action == "advance":
        try:
            r = fb.advance(args.item_id, args.to, args.note or "")
        except (KeyError, ValueError) as e:
            c.err(str(e))
            return 1
        c.ok(f"#{r['item_id']} {r['from']} → {r['to']}（{r['label']}）"
             + ("  ✓ 已闭环" if r["is_closed"] else ""))
        return 0

    if args.action == "add":
        iid = fb.add_item(args.text, severity=args.severity,
                          users_affected=args.users, freq=args.freq)
        c.ok(f"已录入反馈 #{iid}（状态：待归因）")
        return 0

    if args.action == "show":
        it = fb.get_item(args.item_id)
        if not it:
            c.err("不存在")
            return 1
        c.title(f"反馈 #{it['item_id']}")
        c.kv("原文", it["text"])
        c.kv("渠道", it["channel"])
        c.kv("归因", f"{it['label']}（置信 {it['pred_conf']}）→ {it['owner'] or '未派单'}")
        c.kv("优先级", f"{it['priority']}  {it['band']}")
        c.kv("状态", it["status_label"])
        c.kv("回归用例", it["promoted_sample"] or "（未提升）")
        if it["history"]:
            print()
            c.title("流转历史")
            c.table(["从", "到", "备注", "时间"],
                    [[h["from_status"], h["to_status"], (h["note"] or "")[:40],
                      h["created_at"]] for h in it["history"]])
        return 0
    return 0


# ===========================================================================
def cmd_bridge(args) -> int:
    if args.action == "promotable":
        rows = br.promotable(min_priority=args.min_priority, category=args.type,
                             only_unpromoted=not args.all, limit=args.top)
        if not rows:
            c.warn("没有待提升的反馈")
            return 0
        for r in rows:
            print(f"  #{r['item_id']}  [{r['band']} {r['priority']}]  {r['text']}")
            print(f"        建议评测要点：{'、'.join(r['suggested_keywords']) or '（需人工补）'}")
            print(f"        期望行为：{r['suggested_expectation']}")
        c.info(f"共 {len(rows)} 条可提升")
        return 0

    if args.action == "promote":
        if args.auto:
            r = br.auto_promote(top_n=args.top, min_priority=args.min_priority)
            c.ok(f"已提升 {r['count']} 条 -> {r['promoted']}")
            return 0
        if not args.item_id:
            c.err("需要 --id 或 --auto")
            return 2
        try:
            r = br.promote(args.item_id, note=args.note or "")
        except KeyError as e:
            c.err(str(e))
            return 1
        c.ok(r["message"])
        if not r["already"]:
            s = r["sample"]
            c.kv("问法", s["question"])
            c.kv("期望行为", s["expected"])
            c.kv("评测要点", "、".join(s["keywords"]) or "（空，建议人工补）")
        return 0

    if args.action == "verify":
        try:
            v = br.verify(args.item_id)
        except KeyError as e:
            c.err(str(e))
            return 1
        c.kv("反馈", f"#{v['item_id']}")
        c.kv("用例", v.get("sample_id") or "（未提升）")
        if not v.get("ran"):
            c.warn(v["message"])
            return 0
        c.kv("评测运行", f"#{v['run_id']}")
        c.kv("得分", v["total"])
        c.kv("四维", json.dumps(v["scores"], ensure_ascii=False))
        (c.ok if v["passed"] else c.err)(v["message"])
        return 0

    if args.action == "coverage":
        cov = br.coverage()
        if not cov.get("has_data"):
            c.warn("暂无技术类反馈")
            return 0
        c.kv("技术类反馈", cov["technical_total"])
        c.kv("已提升为用例", f"{cov['promoted']}（覆盖率 {cov['promote_rate'] * 100:.1f}%）")
        c.kv("复测通过", cov["verified"])
        c.kv("复测未通过", cov["failed"])
        c.kv("已跑但缺样本", cov["not_run"])
        print()
        c.table(["反馈", "用例", "复测", "得分", "原文"],
                [[d["item_id"], d["sample_id"], d["state"],
                  d.get("total", "-"), d["text"][:30]] for d in cov["details"][:20]])
        return 0

    if args.action == "close":
        try:
            r = br.close_if_verified(args.item_id, args.note or "")
        except (KeyError, ValueError) as e:
            c.err(str(e))
            return 1
        if r.get("closed"):
            c.ok(r["message"])
            for t in r.get("trail", []):
                c.info(f"  {t['from']} → {t['to']}（{t['label']}）")
        else:
            c.warn(r.get("reason") or r.get("message") or "无法关单")
        return 0
    return 0


# ===========================================================================
def cmd_analytics(args) -> int:
    if args.action == "seed":
        print()
        c.title(f"生成 {args.days} 天埋点数据")
        bar = c.Progress(args.days, "生成进度")
        r = an.generate(days=args.days, progress=lambda i, n, d: bar.tick())
        bar.done()
        print()
        c.ok(f"{r['users']} 用户 / {r['events']} 事件 / {r['days']} 天")
        return 0

    if args.action in ("report", "summary"):
        s = an.summary()
        if not s.get("has_data"):
            c.warn("还没有埋点数据，先跑 analytics seed")
            return 0
        c.title("核心指标")
        c.kv("用户 / 事件", f"{s['users']} / {s['events']}")
        c.kv("观测天数", s["days"])
        c.kv("最新 DAU", s["dau_latest"])
        c.kv("首日整体转化", f"{(s['overall_conversion'] or 0) * 100:.1f}%")
        c.kv("留存 D1/D3/D7",
             " / ".join(f"{(s['retention'][k] or 0) * 100:.1f}%" for k in ("d1", "d3", "d7")))
        c.kv("近 7 日生成成功率", f"{s['success_rate_7d'] * 100:.1f}%")
        c.kv("总 tokens", f"{s['total_tokens']:,}")
        c.kv("最差渠道", s["worst_channel"])
        print()
        c.title("结论层")
        print(f"  {(s.get('diagnosis') or {}).get('conclusion', '—')}")
        print()
        for st in (s.get("diagnosis") or {}).get("steps", []):
            print(f"  {st['step']:<16} 通过率 {st['rate'] * 100:5.1f}%   "
                  f"流失 {st['loss'] * 100:5.1f}%   {st['hint']}")
        return 0

    if args.action == "sql":
        q = an.load_queries()
        if args.name:
            if args.name not in q:
                c.err(f"没有这条查询。可选：{'、'.join(q)}")
                return 1
            print(q[args.name])
        else:
            for name in q:
                print(f"  {name}")
            c.info(f"共 {len(q)} 条查询，用 --name 查看单条")
        return 0

    if args.action == "full":
        data = an.run_all()
        c.title("分渠道表现")
        c.table(["渠道", "新增", "激活", "激活率", "人均生成"],
                [[r["channel"], r["new_users"], r["activated_users"],
                  f"{r['activation_rate'] * 100:.1f}%", r["generate_per_user"]]
                 for r in data["channel_performance"]])
        print()
        c.title("分渠道首日漏斗")
        c.table(["渠道", "→输入", "→生成", "→采纳"],
                [[r["channel"], f"{r['open_to_input'] * 100:.1f}%",
                  f"{r['open_to_generate'] * 100:.1f}%",
                  f"{r['open_to_adopt'] * 100:.1f}%"]
                 for r in data["funnel_by_channel"]])
        print()
        c.title("按星期")
        c.table(["星期", "新增", "激活率"],
                [[r["weekday"], r["new_users"], f"{r['activation_rate'] * 100:.1f}%"]
                 for r in data["weekday_pattern"]])
        return 0
    return 0


# ===========================================================================
def cmd_demo(args) -> int:
    """一条命令把整条闭环演一遍。这是我准备现场演示的路径。"""
    c.title("ProductOps 端到端演示")
    t0 = time.time()

    c.step(1, 6, "准备数据（评测集 / 反馈 / 埋点）")
    if not ev.load_samples():
        ev.save_evalset(ev.build_evalset())
    c.ok(f"评测集 {len(ev.load_samples())} 条")
    fb.seed_items()
    fb.run_classification()
    c.ok(f"反馈 {len(db.query('SELECT item_id FROM fb_items'))} 条，已完成归因")
    if not an.summary().get("has_data"):
        an.generate()
    c.ok("埋点数据就绪")

    c.step(2, 6, "跑一次基线评测")
    base = ev.run_evaluation(limit=0, note="demo 基线")
    c.ok(f"评测 #{base['run_id']} 门禁「{base['metrics']['gate']['verdict']}」"
         f"总分 {base['metrics']['overall']['total_avg']}")

    c.step(3, 6, "把 P0 技术类吐槽提升为回归测试用例")
    cov0 = br.coverage()
    promo = br.auto_promote(top_n=args.top)
    c.ok(f"提升 {promo['count']} 条 -> {promo['promoted']}")
    c.info(f"（此前技术类反馈覆盖率 {cov0['promote_rate'] * 100:.1f}%）")

    c.step(4, 6, "重跑评测（新用例已自动进入评测集）")
    run = ev.run_evaluation(limit=0, note="demo 复测")
    cov = br.coverage()
    c.ok(f"评测 #{run['run_id']}，共 {run['samples']} 条样本"
         f"（其中来自反馈 {cov['evalset_samples_from_feedback']} 条）")

    c.step(5, 6, "查复测结果")
    c.kv("技术类反馈", cov["technical_total"])
    c.kv("已提升为用例", f"{cov['promoted']}（覆盖率 {cov['promote_rate'] * 100:.1f}%）")
    c.kv("复测通过", cov["verified"])
    c.kv("复测未通过", cov["failed"])
    print()
    for d in cov["details"][:args.top]:
        mark = "✓" if d["state"] == "复测通过" else "✗"
        print(f"    {mark} #{d['item_id']} {d['sample_id']}  "
              f"{d['state']}  得分 {d.get('total', '-')}   {d['text'][:26]}")

    c.step(6, 6, "走完一条完整闭环（提升 → 修复 → 复测 → 关单）")
    passed = [d["item_id"] for d in cov["details"] if d["state"] == "复测通过"]
    if not passed:
        c.warn("本次没有复测通过的用例，跳过关单演示")
    else:
        iid = passed[0]
        for st in ("assigned", "fixed"):
            try:
                fb.advance(iid, st, "demo：模拟研发已修复")
            except ValueError as e:
                c.warn(f"流转 {st} 失败：{e}")
        r = br.close_if_verified(iid, "demo：复测通过，自动关单")
        if r.get("closed"):
            c.ok(f"反馈 #{iid} 已闭环："
                 + " → ".join(t["to"] for t in r.get("trail", [])))
        else:
            c.warn(r.get("reason") or r.get("message", ""))

    s = fb.summary()
    print()
    c.title("最终状态")
    c.kv("反馈闭环率", f"{s['closed_loop_rate'] * 100:.1f}%"
                       f"（{s['closed_count']}/{s['total']}）")
    c.kv("吐槽→回归用例", f"{s['promoted_to_evalset']} 条")
    c.kv("耗时", f"{time.time() - t0:.1f} s")
    print()
    c.ok("演示完成。想看界面：python run.py serve")
    return 0


# ===========================================================================
def cmd_export(args) -> int:
    if args.md:
        p = export_summary_md(args.out)
        c.ok(f"摘要已导出：{p}")
        return 0
    p = export_html(args.out)
    size = Path(p).stat().st_size
    c.ok(f"离线看板已导出：{p}（{size / 1024:.0f} KB，双击即可打开，不需要起服务）")
    return 0


def cmd_serve(args) -> int:
    from .api.server import serve
    serve(host=args.host, port=args.port, verbose=args.verbose)
    return 0


def cmd_test(args) -> int:
    """跑测试套件。不改动当前 PO_HOME —— 测试各自用临时目录。"""
    import unittest
    tests_dir = Config.PROJECT_ROOT / "tests"
    if not tests_dir.is_dir():
        c.err(f"找不到测试目录 {tests_dir}")
        return 1
    c.title("运行测试套件")
    loader = unittest.TestLoader()
    suite = loader.discover(str(tests_dir), pattern="test_*.py",
                            top_level_dir=str(Config.PROJECT_ROOT))
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1, buffer=False)
    result = runner.run(suite)
    print()
    c.kv("用例总数", result.testsRun)
    c.kv("失败", len(result.failures))
    c.kv("错误", len(result.errors))
    c.kv("跳过", len(result.skipped))
    if result.wasSuccessful():
        c.ok("全部通过")
        return 0
    c.err("存在失败用例")
    return 1


# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="productops",
        description="ProductOps Workbench —— AI 产品运营工作台"
                    "（评测 / 反馈闭环 / 数据看板，把用户吐槽变成回归测试用例）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用流程：
  python run.py init                 准备数据（评测集 + 反馈 + 埋点 + 基线评测）
  python run.py doctor               环境自检
  python run.py demo                 端到端演示整条闭环
  python run.py serve               起看板（默认 http://127.0.0.1:8770）
  python run.py export              导出可单独发送的离线 HTML 快照
""")
    p.add_argument("-V", "--version", action="version",
                   version=f"ProductOps Workbench {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="一次性准备好全部数据")
    s.add_argument("--reset", action="store_true", help="先删除旧数据库")
    s.add_argument("--no-analytics", action="store_true", help="跳过埋点数据（省时间）")
    s.set_defaults(fn=cmd_init)

    sub.add_parser("doctor", help="环境与数据自检").set_defaults(fn=cmd_doctor)

    # --- eval ---
    e = sub.add_parser("eval", help="效果评测").add_subparsers(dest="action", required=True)
    e.add_parser("build", help="生成内置评测集").set_defaults(fn=cmd_eval)
    er = e.add_parser("run", help="跑一次评测")
    er.add_argument("--limit", type=int, default=0, help="只跑 N 条（0=全量）")
    er.add_argument("--difficulty", choices=["easy", "medium", "hard"], default=None)
    er.add_argument("--note", default="", help="备注，写进历史记录")
    er.set_defaults(fn=cmd_eval)
    ers = e.add_parser("runs", help="历史运行列表")
    ers.add_argument("--limit", type=int, default=20)
    ers.set_defaults(fn=cmd_eval)
    es = e.add_parser("show", help="查看某次评测的 badcase")
    es.add_argument("run_id", nargs="?", type=int)
    es.add_argument("--top", type=int, default=20)
    es.set_defaults(fn=cmd_eval)
    ec = e.add_parser("compare", help="版本对比（找退化样本）")
    ec.add_argument("--base", type=int)
    ec.add_argument("--new", type=int)
    ec.set_defaults(fn=cmd_eval)
    ei = e.add_parser("import", help="导入外部评测集（jsonl）")
    ei.add_argument("path")
    ei.set_defaults(fn=cmd_eval)
    ee = e.add_parser("export", help="导出评测集为 jsonl")
    ee.add_argument("path")
    ee.set_defaults(fn=cmd_eval)
    ep = e.add_parser("samples", help="查看评测集")
    ep.add_argument("--difficulty", choices=["easy", "medium", "hard"])
    ep.add_argument("--origin", choices=["builtin", "imported", "feedback"])
    ep.add_argument("--top", type=int, default=40)
    ep.set_defaults(fn=cmd_eval)

    # --- feedback ---
    f = sub.add_parser("feedback", help="反馈闭环").add_subparsers(dest="action", required=True)
    f.add_parser("seed", help="灌入示例反馈").set_defaults(fn=cmd_feedback)
    f.add_parser("run", help="跑归因（规则 vs LLM）").set_defaults(fn=cmd_feedback)
    f.add_parser("report", help="周报文本").set_defaults(fn=cmd_feedback)
    fl = f.add_parser("list", help="反馈列表")
    fl.add_argument("--type", choices=list(fb.CATEGORIES.keys()))
    fl.add_argument("--status", choices=fb.STATES)
    fl.add_argument("--top", type=int, default=30)
    fl.set_defaults(fn=cmd_feedback)
    fs = f.add_parser("show", help="单条详情 + 流转历史")
    fs.add_argument("item_id", type=int)
    fs.set_defaults(fn=cmd_feedback)
    fa = f.add_parser("advance", help="推进状态")
    fa.add_argument("item_id", type=int)
    fa.add_argument("--to", required=True, choices=fb.STATES)
    fa.add_argument("--note", default="")
    fa.set_defaults(fn=cmd_feedback)
    fadd = f.add_parser("add", help="手动录入一条反馈")
    fadd.add_argument("text")
    fadd.add_argument("--severity", type=int, default=3)
    fadd.add_argument("--users", type=int, default=0)
    fadd.add_argument("--freq", type=int, default=1)
    fadd.set_defaults(fn=cmd_feedback)

    # --- bridge ---
    b = sub.add_parser("bridge", help="把反馈变成回归测试用例")
    bs = b.add_subparsers(dest="action", required=True)
    bp = bs.add_parser("promotable", help="列出可提升的反馈")
    bp.add_argument("--min-priority", type=float, default=60.0)
    bp.add_argument("--type", default="technical")
    bp.add_argument("--all", action="store_true", help="包含已提升过的")
    bp.add_argument("--top", type=int, default=20)
    bp.set_defaults(fn=cmd_bridge)
    bpr = bs.add_parser("promote", help="提升为回归用例")
    bpr.add_argument("--id", dest="item_id", type=int)
    bpr.add_argument("--auto", action="store_true", help="批量提升 Top N")
    bpr.add_argument("--top", type=int, default=5)
    bpr.add_argument("--min-priority", type=float, default=60.0)
    bpr.add_argument("--note", default="")
    bpr.set_defaults(fn=cmd_bridge)
    bv = bs.add_parser("verify", help="查复测结果")
    bv.add_argument("item_id", type=int)
    bv.set_defaults(fn=cmd_bridge)
    bs.add_parser("coverage", help="覆盖率与复测情况").set_defaults(fn=cmd_bridge)
    bc = bs.add_parser("close", help="复测通过后关单")
    bc.add_argument("item_id", type=int)
    bc.add_argument("--note", default="")
    bc.set_defaults(fn=cmd_bridge)

    # --- analytics ---
    a = sub.add_parser("analytics", help="数据看板").add_subparsers(dest="action", required=True)
    asd = a.add_parser("seed", help="生成埋点数据")
    asd.add_argument("--days", type=int, default=Config.DAYS)
    asd.set_defaults(fn=cmd_analytics)
    a.add_parser("report", help="核心指标 + 结论层").set_defaults(fn=cmd_analytics)
    a.add_parser("full", help="分渠道 / 分星期明细").set_defaults(fn=cmd_analytics)
    asl = a.add_parser("sql", help="查看 SQL 模板")
    asl.add_argument("--name")
    asl.set_defaults(fn=cmd_analytics)

    # --- 跨模块 ---
    d = sub.add_parser("demo", help="端到端演示整条闭环")
    d.add_argument("--top", type=int, default=5)
    d.set_defaults(fn=cmd_demo)

    ex = sub.add_parser("export", help="导出离线快照")
    ex.add_argument("--out", default=None)
    ex.add_argument("--md", action="store_true", help="导出 Markdown 摘要而不是 HTML")
    ex.set_defaults(fn=cmd_export)

    sv = sub.add_parser("serve", help="启动看板服务")
    sv.add_argument("--host", default="127.0.0.1",
                    help="默认只绑本机；要用 0.0.0.0 需显式指定")
    sv.add_argument("--port", type=int, default=8770)
    sv.add_argument("--verbose", action="store_true", help="打印每个请求")
    sv.set_defaults(fn=cmd_serve)

    ts = sub.add_parser("test", help="跑测试套件")
    ts.add_argument("-v", "--verbose", action="store_true", help="逐条打印用例名")
    ts.set_defaults(fn=cmd_test)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\n  用户中断。")
        return 130
    except Exception as e:
        c.err(f"{type(e).__name__}: {e}")
        if getattr(args, "verbose", False):
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
