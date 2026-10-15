#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ProductOps Workbench 测试套件。

设计取舍：
  * 用标准库 unittest，不引入 pytest —— 和产品本身「零依赖」的承诺保持一致。
    一个需要先 pip install 才能验证自己的项目，说服力会打折。
  * 每个用例自带独立的 PO_HOME（临时目录），互不干扰，跑完即删。
    这比共用一个 data/ 目录可靠得多：上一个用例的状态不会污染下一个。
  * 最后一组是**跨进程可复现性**测试 —— 这是整套系统的底线。
    如果换个进程数字就变，那所有结论都没法和人讨论。

跑法：
    python run.py test            （推荐，带上彩色输出与统计）
    python -m unittest discover -s tests -v
    python tests/test_productops.py
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from productops.core import console as c          # noqa: E402
from productops.core import db, stats             # noqa: E402
from productops.core.config import Config, stable_seed   # noqa: E402
from productops.modules import analytics as an    # noqa: E402
from productops.modules import bridge as br       # noqa: E402
from productops.modules import evaluation as ev   # noqa: E402
from productops.modules import feedback as fb     # noqa: E402


# ===========================================================================
class Base(unittest.TestCase):
    """
    给每个**用例**一个干净的 PO_HOME。

    为什么隔离粒度是「每个用例」而不是「每个类」：
        第一版按类隔离，同一个类里前一个用例插入的样本、关掉的单子
        会漏到后一个用例里 —— 于是「新建库应该是 100 条样本」变成了 101 条，
        「还没关过单所以闭环率是 0」变成了 1.4%。
        这类失败看起来像产品 bug，其实是测试自己没隔离干净，最耗时间。
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="po_test_"))
        os.environ["PO_HOME"] = str(self._tmp)

    def tearDown(self):
        os.environ.pop("PO_HOME", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    # 常用夹具
    @staticmethod
    def seed_evalset():
        return ev.save_evalset(ev.build_evalset())

    def seed_all(self, run=True, analytics=True, classify=True):
        n = self.seed_evalset()
        fb.seed_items()
        if classify:
            fb.run_classification()
        if analytics:
            an.generate()
        if run:
            return n, ev.run_evaluation(note="test")
        return n, None


# ===========================================================================
# 1. core
# ===========================================================================
class TestConfig(unittest.TestCase):
    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(Config.WEIGHTS.values()), 1.0, places=9)

    def test_dim_labels_cover_weights(self):
        self.assertEqual(set(Config.WEIGHTS), set(Config.DIM_LABELS))
        self.assertEqual(set(Config.WEIGHTS), {"accuracy", "completeness",
                                              "safety", "readability"})

    def test_fusion_weights_sum_to_one(self):
        self.assertAlmostEqual(Config.W_RULE + Config.W_LLM, 1.0, places=9)

    def test_priority_weights_sum_to_one(self):
        self.assertAlmostEqual(
            Config.W_SEVERITY + Config.W_REACH + Config.W_FREQ, 1.0, places=9)

    def test_safety_is_the_strictest_gate(self):
        # 安全性阈值必须比准确性阈值更严，否则「一票否决」就是空话
        self.assertGreater(Config.GATE_SAFETY, Config.GATE_ACCURACY)

    def test_paths_are_derived_from_file(self):
        self.assertTrue(str(Config.WEB_DIR).endswith("web"))
        self.assertTrue(str(Config.SQL_DIR).endswith("sql"))
        self.assertTrue((Config.WEB_DIR / "index.html").is_file())
        self.assertTrue((Config.SQL_DIR / "analytics.sql").is_file())


class TestStableSeed(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(stable_seed("abc"), stable_seed("abc"))

    def test_differs_by_content(self):
        self.assertNotEqual(stable_seed("abc"), stable_seed("abd"))

    def test_known_value_is_frozen(self):
        # 锁定一个具体数值：如果有人换了哈希算法，这个断言会立刻炸。
        # 换算法本身不是错，但必须是**有意识的**决定，不能悄悄发生。
        self.assertEqual(stable_seed("doctor-check"), 9926591)

    def test_never_uses_builtin_hash(self):
        # 内置 hash 每进程随机；stable_seed 必须与它无关
        self.assertEqual(stable_seed("x"), stable_seed("x"))

    def test_range(self):
        for s in ("", "a", "很长的中文句子用来测试编码", "🚀"):
            self.assertTrue(0 <= stable_seed(s) < (1 << 24))


class TestStats(unittest.TestCase):
    def test_avg_and_std(self):
        self.assertEqual(stats.avg([1, 2, 3]), 2)
        self.assertEqual(stats.avg([]), 0)
        self.assertAlmostEqual(stats.std([2, 2, 2]), 0.0, places=9)
        self.assertGreater(stats.std([1, 5]), 0)

    def test_safe_div(self):
        self.assertEqual(stats.safe_div(6, 3), 2)
        self.assertEqual(stats.safe_div(1, 0), 0)
        self.assertEqual(stats.safe_div(1, 0, default=None), None)

    def test_percentile(self):
        # 注意 p 是 0~1，不是 0~100
        data = [1, 2, 3, 4]
        self.assertEqual(stats.percentile(data, 0.0), 1)
        self.assertEqual(stats.percentile(data, 1.0), 4)
        self.assertAlmostEqual(stats.percentile(data, 0.5), 2.5, places=6)
        self.assertEqual(stats.percentile([], 0.5), 0.0)
        self.assertEqual(stats.percentile([7], 0.9), 7)

    def test_percentile_clamps_out_of_range(self):
        """传 95 而不是 0.95 是很容易犯的错，不能直接 IndexError。"""
        data = [1, 2, 3, 4]
        self.assertEqual(stats.percentile(data, 95), 4)
        self.assertEqual(stats.percentile(data, -3), 1)

    def test_log_norm_monotonic_and_bounded(self):
        vals = [stats.log_norm(v, 100) for v in range(0, 101, 10)]
        self.assertEqual(vals, sorted(vals))
        self.assertAlmostEqual(stats.log_norm(0, 100), 0.0, places=9)
        self.assertAlmostEqual(stats.log_norm(100, 100), 1.0, places=9)

    def test_clamp(self):
        self.assertEqual(stats.clamp(5, 0, 3), 3)
        self.assertEqual(stats.clamp(-5, 0, 3), 0)
        self.assertEqual(stats.clamp(2, 0, 3), 2)


class TestDb(Base):
    def test_schema_creates_all_tables(self):
        rows = db.query("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r["name"] for r in rows}
        for t in ("eval_samples", "eval_runs", "eval_results", "fb_items",
                  "fb_events", "users", "events", "meta"):
            self.assertIn(t, names)

    def test_tx_rolls_back_on_error(self):
        db.execute("INSERT INTO meta(key,value) VALUES('keep','1')")
        with self.assertRaises(RuntimeError):
            with db.tx() as conn:
                conn.execute("INSERT INTO meta(key,value) VALUES('gone','2')")
                raise RuntimeError("boom")
        self.assertIsNone(db.meta_get("gone"))
        self.assertEqual(db.meta_get("keep"), "1")

    def test_meta_upsert(self):
        db.meta_set("k", "v1")
        db.meta_set("k", "v2")
        self.assertEqual(db.meta_get("k"), "v2")
        self.assertEqual(db.meta_get("nope", "fallback"), "fallback")

    def test_clear_analytics_only_touches_events(self):
        db.execute("INSERT INTO users(user_id,first_day,channel,platform) "
                   "VALUES('U1','2026-01-01','x','web')")
        db.execute("INSERT INTO eval_samples(sample_id,category,difficulty,question) "
                   "VALUES('S1','c','easy','q')")
        db.clear_analytics()
        self.assertEqual(db.counts()["users"], 0)
        self.assertEqual(db.counts()["eval_samples"], 1)

    def test_counts_returns_all_tables(self):
        c = db.counts()
        self.assertEqual(set(c), {"eval_samples", "eval_runs", "eval_results",
                                  "fb_items", "fb_events", "users", "events"})

    def test_busy_timeout_is_set(self):
        conn = db.connect()
        try:
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
        finally:
            conn.close()


# ===========================================================================
# 2. evaluation
# ===========================================================================
class TestEvalset(Base):
    def test_build_shape(self):
        s = ev.build_evalset()
        self.assertEqual(len(s), 100)
        self.assertEqual(len({x["id"] for x in s}), 100)   # id 唯一
        self.assertEqual(sum(1 for x in s if x["difficulty"] == "hard"), 50)

    def test_has_boundary_scenarios(self):
        s = ev.build_evalset()
        self.assertTrue(any(ev.is_boundary(x) for x in s))
        self.assertGreaterEqual(sum(1 for x in s if ev.is_boundary(x)), 20)

    def test_rubric_covers_all_dims(self):
        r = ev.rubric()
        self.assertEqual(set(r["weights"]), set(Config.WEIGHTS))
        self.assertEqual(set(r["dimensions"]), set(Config.WEIGHTS))
        for d, spec in r["dimensions"].items():
            self.assertEqual(spec["weight"], Config.WEIGHTS[d])
            self.assertTrue(spec["anchors"])
        self.assertEqual(r["fusion"]["rule"], Config.W_RULE)
        self.assertAlmostEqual(r["fusion"]["rule"] + r["fusion"]["llm"], 1.0, places=9)

    def test_save_and_load_roundtrip(self):
        n = self.seed_evalset()
        self.assertEqual(n, 100)
        rows = ev.load_samples()
        self.assertEqual(len(rows), 100)
        self.assertTrue(all(r["origin"] == "builtin" for r in rows))
        self.assertTrue(rows[0]["keywords"] is not None)

    def test_add_sample_is_idempotent_by_id(self):
        ev.add_sample({"id": "X1", "category": "c", "difficulty": "hard",
                       "question": "q", "expected": "e", "keywords": [],
                       "must_not": [], "tags": [], "origin": "imported"})
        ev.add_sample({"id": "X1", "category": "c", "difficulty": "hard",
                       "question": "q2", "expected": "e", "keywords": [],
                       "must_not": [], "tags": [], "origin": "imported"})
        self.assertEqual(len(ev.load_samples()), 1)

    def test_import_export_jsonl(self):
        self.seed_evalset()
        out = Config.home() / "evalset.jsonl"
        self.assertEqual(ev.export_evalset(str(out)), 100)
        db.execute("DELETE FROM eval_samples")
        self.assertEqual(ev.import_evalset(str(out)), 100)
        self.assertEqual(db.counts()["eval_samples"], 100)


class TestScoring(Base):
    def test_merge_respects_fusion_weights(self):
        m = ev.merge({"accuracy": 4, "completeness": 4, "safety": 4, "readability": 4},
                     {"accuracy": 2, "completeness": 2, "safety": 2, "readability": 2})
        self.assertAlmostEqual(m["accuracy"], 4 * Config.W_RULE + 2 * Config.W_LLM, places=6)

    def test_weighted_total_uses_config_weights(self):
        dims = {"accuracy": 5, "completeness": 0, "safety": 0, "readability": 0}
        self.assertAlmostEqual(ev.weighted_total(dims), 5 * Config.WEIGHTS["accuracy"], places=6)

    def test_score_is_deterministic(self):
        s = ev.build_evalset()[0]
        a = ev.score(s, "一段回答")
        b = ev.score(s, "一段回答")
        self.assertEqual(a["total"], b["total"])

    def test_score_within_bounds(self):
        import random as _random
        s = ev.build_evalset()[0]
        for i in range(12):
            r = ev.score(s, f"回答内容 {i}", rng=_random.Random(i))
            self.assertTrue(0 <= r["total"] <= 5, r["total"])
            for d in Config.WEIGHTS:
                self.assertTrue(0 <= r["scores"][d] <= 5, (d, r["scores"][d]))
                self.assertTrue(0 <= r["rule"][d] <= 5)
                self.assertTrue(0 <= r["llm"][d] <= 5)
            self.assertIsInstance(r["passed"], bool)

    def test_merge_sits_between_the_two_tracks(self):
        import random as _random
        s = ev.build_evalset()[0]
        r = ev.score(s, "回答", rng=_random.Random(7))
        for d in Config.WEIGHTS:
            lo, hi = sorted((r["rule"][d], r["llm"][d]))
            self.assertTrue(lo - 0.01 <= r["scores"][d] <= hi + 0.01, d)

    def test_mock_judge_reproduces_leniency_bias(self):
        """
        mock 评委刻意复现真模型的「宽松偏差」：安全性给分系统性偏高。
        这条断言是在保护一个产品判断 —— 如果偏差被抹平了，
        那这套 mock 就演示不出「为什么不能只信 LLM 打分」了。
        """
        import random as _random
        s = ev.build_evalset()[0]
        rule = ev.score_rule(s, "回答")
        tot = {d: 0.0 for d in Config.WEIGHTS}
        n = 200
        for i in range(n):
            j = ev.mock_judge(s, "回答", rule, _random.Random(i))
            for d in tot:
                tot[d] += j[d]
        avg = {d: v / n for d, v in tot.items()}
        self.assertGreater(avg["safety"], avg["accuracy"])
        self.assertGreater(avg["safety"], avg["completeness"])

    def test_mock_judge_is_deterministic_given_seed(self):
        import random as _random
        s = ev.build_evalset()[0]
        rule = ev.score_rule(s, "回答")
        a = ev.mock_judge(s, "回答", rule, _random.Random(42))
        b = ev.mock_judge(s, "回答", rule, _random.Random(42))
        self.assertEqual(a, b)

    def test_refusal_detection(self):
        self.assertTrue(ev.is_refusal("抱歉，我不能帮你伪造行程单。"))
        self.assertTrue(ev.is_refusal("这个请求我无法满足"))
        self.assertFalse(ev.is_refusal("东京塔的开放时间是 9:00 到 23:00。"))


class TestGate(Base):
    @staticmethod
    def _metrics(safety, accuracy, hard_pass):
        return ({"total_avg": 4.0,
                 "dims": {"safety": {"avg": safety},
                          "accuracy": {"avg": accuracy},
                          "completeness": {"avg": 4.0},
                          "readability": {"avg": 4.0}}},
                {"hard": {"pass_rate": hard_pass, "total_avg": 4.0, "count": 50}})

    def test_safety_veto_blocks_release(self):
        g = ev.evaluate_gate(*self._metrics(3.0, 5.0, 1.0))
        self.assertTrue(g["blocked"])
        self.assertEqual(g["verdict"], "禁止上线")
        self.assertTrue(any(ch["veto"] and not ch["ok"] for ch in g["checks"]))

    def test_low_hard_pass_only_gray(self):
        g = ev.evaluate_gate(*self._metrics(5.0, 5.0, 0.10))
        self.assertFalse(g["blocked"])
        self.assertFalse(g["passed"])
        self.assertEqual(g["verdict"], "允许灰度")

    def test_low_accuracy_only_gray(self):
        g = ev.evaluate_gate(*self._metrics(5.0, 1.0, 1.0))
        self.assertFalse(g["blocked"])
        self.assertEqual(g["verdict"], "允许灰度")

    def test_all_pass(self):
        g = ev.evaluate_gate(*self._metrics(5.0, 5.0, 1.0))
        self.assertTrue(g["passed"])
        self.assertEqual(g["verdict"], "允许上线")

    def test_safety_is_the_only_veto(self):
        g = ev.evaluate_gate(*self._metrics(1.0, 1.0, 0.0))
        veto_checks = [ch for ch in g["checks"] if ch["veto"]]
        self.assertEqual([ch["key"] for ch in veto_checks], ["safety"])

    def test_every_check_declares_on_fail_action(self):
        g = ev.evaluate_gate(*self._metrics(1.0, 1.0, 0.0))
        for ch in g["checks"]:
            self.assertTrue(ch["on_fail"])
            self.assertIn("veto", ch)
            self.assertIn(ch["op"], (">=", "<="))

    def test_missing_hard_bucket_is_treated_as_fail(self):
        overall = {"total_avg": 4.0, "dims": {"safety": {"avg": 5.0},
                                             "accuracy": {"avg": 5.0}}}
        g = ev.evaluate_gate(overall, {})
        self.assertFalse(g["blocked"])
        self.assertEqual(g["verdict"], "允许灰度")


class TestRunAndCompare(Base):
    def test_run_reproducible_within_process(self):
        self.seed_evalset()
        a = ev.run_evaluation(note="a")["metrics"]["overall"]
        b = ev.run_evaluation(note="b")["metrics"]["overall"]
        self.assertEqual(a["total_avg"], b["total_avg"])
        self.assertEqual(a["pass_count"], b["pass_count"])

    def test_metrics_internal_consistency(self):
        self.seed_evalset()
        m = ev.run_evaluation(note="t")["metrics"]
        o = m["overall"]
        self.assertEqual(o["count"], sum(v["count"] for v in m["by_difficulty"].values()))
        self.assertAlmostEqual(sum(m["overall"]["dims"][d]["avg"] * Config.WEIGHTS[d]
                                   for d in Config.WEIGHTS), o["total_avg"], places=1)

    def test_safety_gap_counts_boundary_failures(self):
        self.seed_evalset()
        sg = ev.run_evaluation(note="t")["metrics"]["safety_gap"]
        self.assertGreater(sg["boundary_total"], 0)
        self.assertEqual(sg["boundary_total"], sg["boundary_failed"] + sg["boundary_passed"])

    def test_empty_evalset_raises(self):
        with self.assertRaises(RuntimeError):
            ev.run_evaluation()

    def test_compare_identical_runs_is_neutral(self):
        self.seed_evalset()
        a = ev.run_evaluation(note="a")["run_id"]
        b = ev.run_evaluation(note="b")["run_id"]
        cmp = ev.compare_runs(a, b)
        self.assertEqual(cmp["regression_count"], 0)
        self.assertEqual(cmp["improvement_count"], 0)
        self.assertEqual(cmp["avg_delta"], 0.0)
        self.assertIn("无显著变化", cmp["verdict"])
        self.assertEqual(cmp["comparable_count"], 100)

    def test_compare_detects_real_regression(self):
        """人为塞一条退化样本，对比必须抓到它。"""
        self.seed_evalset()
        a = ev.run_evaluation(note="base")["run_id"]
        b = ev.run_evaluation(note="new")["run_id"]
        sid = db.query_one("SELECT sample_id FROM eval_results WHERE run_id=?", (a,))["sample_id"]
        db.execute("UPDATE eval_results SET total=0.4 WHERE run_id=? AND sample_id=?", (b, sid))
        cmp = ev.compare_runs(a, b)
        self.assertEqual(cmp["regression_count"], 1)
        self.assertEqual(cmp["regressions"][0]["sample_id"], sid)
        self.assertIn("退化", cmp["verdict"])

    def test_compare_with_no_overlap(self):
        self.seed_evalset()
        a = ev.run_evaluation(note="a", difficulty="easy")["run_id"]
        b = ev.run_evaluation(note="b", difficulty="hard")["run_id"]
        cmp = ev.compare_runs(a, b)
        self.assertEqual(cmp["comparable_count"], 0)
        self.assertIn("没有可比样本", cmp["verdict"])

    def test_latest_run_with_sample_skips_partial_runs(self):
        """
        回归测试：复测不能被「过滤跑」干扰。
        这是一种真的踩过的坑 —— 覆盖率会因此瞬间掉到 0。
        """
        self.seed_evalset()
        full = ev.run_evaluation(note="full")["run_id"]
        sid = db.query_one("SELECT sample_id FROM eval_results WHERE run_id=?", (full,))["sample_id"]
        partial = ev.run_evaluation(note="partial", limit=5)["run_id"]
        self.assertEqual(ev.latest_run_id(), partial)
        self.assertEqual(ev.latest_run_with_sample(sid), full)

    def test_load_run_and_list(self):
        self.seed_evalset()
        rid = ev.run_evaluation(note="x")["run_id"]
        d = ev.load_run(rid)
        self.assertEqual(len(d["results"]), 100)
        self.assertTrue(ev.list_runs(limit=5))
        with self.assertRaises(KeyError):
            ev.load_run(999999)

    def test_summary_for_api(self):
        self.seed_evalset()
        ev.run_evaluation(note="t")
        s = ev.summary_for_api()
        self.assertTrue(s["has_run"])
        self.assertIn("gate_verdict", s)
        self.assertIn("total_avg", s)


# ===========================================================================
# 3. feedback
# ===========================================================================
class TestFeedbackTaxonomy(unittest.TestCase):
    def test_four_categories(self):
        self.assertEqual(set(fb.CATEGORIES), {"demand", "expression",
                                             "distribution", "technical"})
        self.assertEqual(set(fb.CAT_ORDER), set(fb.CATEGORIES))

    def test_every_category_has_owner(self):
        for k, v in fb.CATEGORIES.items():
            self.assertTrue(v.get("label"))
            self.assertTrue(v.get("owner"))

    def test_transitions_targets_are_valid_states(self):
        for src, targets in fb.TRANSITIONS.items():
            self.assertIn(src, fb.STATES)
            for t in targets:
                self.assertIn(t, fb.STATES)

    def test_closed_states_exclude_fixed(self):
        """
        这是这条产品线的核心口径：`fixed` 不算闭环。
        理由是「已修复」只是研发的自述，没人验证过 —— 算进去就是自欺欺人。
        """
        self.assertEqual(fb.CLOSED_STATES, {"verified", "closed"})
        self.assertNotIn("fixed", fb.CLOSED_STATES)

    def test_fixed_can_be_rejected_back(self):
        # 复测不通过要能退回研发，否则状态机是单向的，没法反映真实返工
        self.assertIn("assigned", fb.TRANSITIONS["fixed"])

    def test_terminal_states(self):
        self.assertEqual(fb.TRANSITIONS["closed"], [])
        self.assertEqual(fb.TRANSITIONS["wontfix"], [])

    def test_state_labels_cover_states(self):
        self.assertEqual(set(fb.STATES), set(fb.STATE_LABELS))


class TestClassifier(Base):
    def test_rule_classify_obvious_cases(self):
        cases = [
            ("生成的行程里同一个景点重复出现了三次", "technical"),
            ("按钮文案看不懂，不知道点哪里", "expression"),
            ("我是做企业差旅的，这个功能对我没用", "distribution"),
            ("希望支持多语言，出国玩的时候好用", "demand"),
        ]
        for text, expect in cases:
            got, conf = fb.rule_classify(text)
            self.assertEqual(got, expect, f"{text} -> {got}")
            self.assertTrue(0 <= conf <= 1)

    def test_category_precedence_is_by_order(self):
        """
        一句话同时命中多类时，按 CAT_ORDER 取优先级最高的那一类：
        technical > expression > distribution > demand。

        这是**有意的**：先排除硬错误（信息错了），再看用户理解，最后才是诉求。
        顺序反了会把「输出是错的」误判成「用户希望加功能」，
        那就直接决定了这条反馈该派给研发还是产品。
        """
        self.assertEqual(fb.CAT_ORDER, ["technical", "expression",
                                        "distribution", "demand"])
        # 同时命中技术（重复）与表达（不知道）—— 技术优先
        got, _ = fb.rule_classify("重复出现了三次，我也不知道为什么")
        self.assertEqual(got, "technical")
        # 同时命中表达（不知道）与分发（对我没用）—— 表达优先
        got, _ = fb.rule_classify("我不知道怎么用，这个对我没用")
        self.assertEqual(got, "expression")

    def test_rule_classify_falls_back_low_confidence(self):
        """
        没有任何关键词命中时，退回 expression 并给 0.45 的低置信度。

        这条不是「缺陷」，是规则分类器被故意设计成保守：
        低置信度会转人工，而不是硬猜一个类别丢给研发。
        副作用是「同一个景点出现了三次」这种**换了个说法的技术问题**抓不到 ——
        这正是文档里承认的「规则召回低于 LLM（80% vs 100%）」的来源，
        也是双轨制保留 LLM 这一轨的理由。
        """
        got, conf = fb.rule_classify("同一个景点出现了三次")
        self.assertEqual(got, "expression")
        self.assertLess(conf, 0.5)

    def test_rule_classify_confidence_drops_on_conflicting_signals(self):
        # 同时命中两类 -> 置信度被扣，倾向于转人工
        _, conf_single = fb.rule_classify("重复出现了三次")
        _, conf_conflict = fb.rule_classify("重复出现了三次，但是我根本不知道怎么看")
        self.assertLess(conf_conflict, conf_single)

    def test_priority_increases_with_severity(self):
        lo = fb.priority_score(1, 0, 1)
        hi = fb.priority_score(5, 0, 1)
        self.assertGreater(hi, lo)

    def test_priority_increases_with_reach(self):
        a = fb.priority_score(3, 0, 1)
        b = fb.priority_score(3, 5000, 1)
        self.assertGreater(b, a)

    def test_priority_band_boundaries(self):
        self.assertTrue(fb.priority_band(95).startswith("P0"))
        self.assertTrue(fb.priority_band(1).startswith("P"))

    def test_seed_items_idempotent(self):
        n1 = fb.seed_items()
        n2 = fb.seed_items()
        self.assertEqual(n1, 72)
        self.assertEqual(db.counts()["fb_items"], 72)
        self.assertEqual(n2, 72)

    def test_dedup_merge_groups_duplicates(self):
        """60 条独立主题 + 12 条重复投诉 -> 应该并成 60 组。"""
        fb.seed_items()
        s = fb.summary()
        self.assertEqual(s["total"], 72)
        self.assertEqual(s["group_count"], 60)
        self.assertEqual(s["dedup_saving"], 12)

    def test_duplicates_excluded_from_accuracy(self):
        # 12 条重复件 gt_type=None，不该参与分类器评估（会虚高）
        fb.seed_items()
        rows = db.query("SELECT COUNT(*) AS n FROM fb_items WHERE gt_type IS NULL")
        self.assertEqual(rows[0]["n"], 12)

    def test_rule_classifier_beats_baseline(self):
        fb.seed_items()
        r = fb.run_classification()
        self.assertGreater(r["rule_eval"]["accuracy"], 0.80)
        self.assertGreater(r["llm_eval"]["accuracy"], r["rule_eval"]["accuracy"])

    def test_llm_confusion_matrix_is_square(self):
        fb.seed_items()
        mx = fb.run_classification()["llm_eval"]["matrix"]
        for g in fb.CAT_ORDER:
            for p in fb.CAT_ORDER:
                self.assertIn(p, mx.get(g, {}))

    def test_evaluate_classifier_perfect(self):
        e = fb.evaluate_classifier([("technical", "technical"), ("demand", "demand")])
        self.assertEqual(e["accuracy"], 1.0)
        self.assertEqual(e["correct"], 2)
        self.assertEqual(e["per_category"]["technical"]["recall"], 1.0)
        self.assertEqual(e["matrix"]["technical"]["technical"], 1)

    def test_evaluate_classifier_counts_misses(self):
        e = fb.evaluate_classifier([("technical", "demand"), ("technical", "technical")])
        self.assertEqual(e["accuracy"], 0.5)
        self.assertEqual(e["per_category"]["technical"]["recall"], 0.5)
        self.assertIn(("demand", 1), e["per_category"]["technical"]["missed_as"])

    def test_evaluate_classifier_empty(self):
        e = fb.evaluate_classifier([])
        self.assertEqual(e["accuracy"], 0.0)
        self.assertEqual(e["correct"], 0)

    def test_auto_dispatch_only_high_confidence(self):
        fb.seed_items()
        r = fb.run_classification()
        self.assertTrue(0 <= r["auto_dispatch_rate"] <= 1)
        owners = db.query("SELECT owner FROM fb_items WHERE owner IS NOT NULL "
                          "AND owner <> '' AND status <> 'closed'")
        for o in owners:
            self.assertTrue(o["owner"])


class TestStateMachine(Base):
    def test_legal_path_to_closed(self):
        iid = fb.add_item("页面打不开", severity=4)
        fb.run_classification()
        cur = fb.get_item(iid)["status"]
        if cur == "new":
            fb.advance(iid, "triaged")
        for s in ("assigned", "fixed", "verified", "closed"):
            fb.advance(iid, s, "test")
        self.assertEqual(fb.get_item(iid)["status"], "closed")
        self.assertTrue(fb.summary()["closed_count"] >= 1)

    def test_illegal_transition_rejected(self):
        iid = fb.add_item("测试非法流转", severity=3)
        with self.assertRaises(ValueError):
            fb.advance(iid, "closed")

    def test_wontfix_is_terminal_and_not_closed(self):
        iid = fb.add_item("这个我们不做", severity=1)
        fb.advance(iid, "wontfix", "需求不在范围内")
        self.assertEqual(fb.get_item(iid)["status"], "wontfix")
        with self.assertRaises(ValueError):
            fb.advance(iid, "assigned")

    def test_history_recorded(self):
        iid = fb.add_item("历史记录测试", severity=3)
        fb.advance(iid, "triaged", "归为技术")
        fb.advance(iid, "assigned", "派给算法")
        h = fb.history(iid)
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0]["from_status"], "new")
        self.assertEqual(h[-1]["to_status"], "assigned")

    def test_advance_unknown_item(self):
        with self.assertRaises(KeyError):
            fb.advance(999999, "triaged")

    def test_add_item_defaults(self):
        iid = fb.add_item("默认参数测试")
        it = fb.get_item(iid)
        self.assertEqual(it["status"], "new")
        self.assertEqual(it["severity"], 3)
        self.assertIsNone(it["pred_type"])


class TestFeedbackReporting(Base):
    def test_summary_keys(self):
        fb.seed_items()
        fb.run_classification()
        s = fb.summary()
        for k in ("total", "group_count", "by_status", "by_type",
                  "closed_loop_rate", "promoted_to_evalset"):
            self.assertIn(k, s)
        self.assertEqual(s["closed_loop_rate"], 0.0)   # 还没关过单

    def test_closed_loop_rate_after_closing(self):
        fb.seed_items()
        fb.run_classification()
        s0 = fb.summary()
        self.assertGreater(s0["total"], 0)
        iid = db.query_one("SELECT MAX(item_id) AS m FROM fb_items")["m"]
        if fb.get_item(iid)["status"] == "new":
            fb.advance(iid, "triaged")
        for s in ("assigned", "fixed", "verified"):
            fb.advance(iid, s)
        s1 = fb.summary()
        self.assertGreater(s1["closed_loop_rate"], 0.0)
        self.assertEqual(s1["closed_count"], 1)

    def test_list_items_filters(self):
        fb.seed_items()
        fb.run_classification()
        all_items = fb.list_items(limit=500)
        tech = fb.list_items(cat="technical", limit=500)
        self.assertLessEqual(len(tech), len(all_items))
        self.assertTrue(all(i["pred_type"] == "technical" for i in tech))

    def test_weekly_report_text(self):
        fb.seed_items()
        fb.run_classification()
        t = fb.weekly_report_text()
        self.assertIn("反馈", t)
        self.assertGreater(len(t), 80)

    def test_summary_survives_unclassified_items(self):
        """
        回归测试：没跑归因的反馈（priority 为 NULL）不能把 summary 搞崩。

        真实路径是：`feedback add` 录一条 -> 直接打开看板。
        这一步会走 summary() -> merge_similar()，
        而 NULL 参与排序会抛 TypeError，表现为整个总览页 500。
        """
        fb.seed_items()
        fb.run_classification()
        fb.add_item("刚录进来还没归因的一条反馈，接口应该依然能打开")
        s = fb.summary()                       # 这里以前会 TypeError
        self.assertGreater(s["total"], 0)
        self.assertIsInstance(s["group_count"], int)

    def test_summary_on_completely_unclassified_db(self):
        fb.add_item("只有一条未归因反馈")
        s = fb.summary()
        self.assertEqual(s["total"], 1)
        self.assertEqual(s["closed_loop_rate"], 0.0)


# ===========================================================================
# 4. analytics
# ===========================================================================
class TestAnalytics(Base):
    def test_sql_file_parses(self):
        q = an.load_queries()
        self.assertGreaterEqual(len(q), 10)
        for name, sql in q.items():
            self.assertIn("SELECT", sql.upper(), name)

    def test_generate_shape_and_idempotency(self):
        a = an.generate(days=10)
        b = an.generate(days=10)
        self.assertEqual(a["users"], b["users"])
        self.assertEqual(a["events"], b["events"])
        self.assertEqual(db.counts()["users"], a["users"])
        self.assertEqual(db.counts()["events"], a["events"])

    def test_all_queries_execute(self):
        an.generate(days=20)
        data = an.run_all()
        self.assertEqual(set(data), set(an.load_queries()))
        for name, v in data.items():
            self.assertIsInstance(v, (list, dict), name)

    def test_funnel_steps_are_conditional_and_product_matches_overall(self):
        """
        漏斗里前三步是**逐级条件率**（本步 / 上一步），不是「从打开算起的累计率」，
        所以它们之间不要求单调递减；真正该满足的是 overall = 三者的乘积。
        第一版按累计率去断言单调，跑出来是 [0.66, 0.75, 0.72, 0.36] 就挂了 ——
        不是数据错，是我把口径理解错了。
        """
        an.generate(days=20)
        f = an.run_all()["funnel"]
        steps = [f["open_to_input"], f["input_to_generate"], f["generate_to_adopt"]]
        for r in steps + [f["overall"]]:
            self.assertTrue(0.0 <= r <= 1.0, r)
        prod = 1.0
        for r in steps:
            prod *= r
        self.assertAlmostEqual(f["overall"], prod, places=3)
        self.assertLessEqual(f["overall"], min(steps))

    def test_retention_decreasing(self):
        an.generate(days=30)
        r = an.run_all()["retention_summary"]
        self.assertGreater(r["d1_rate"], r["d3_rate"])
        self.assertGreater(r["d3_rate"], r["d7_rate"])
        self.assertTrue(0 < r["d7_rate"] < 1)

    def test_retention_denominators_are_cohort_locked(self):
        """
        口径保护：留存分母必须是「在观测窗口里已经满 N 天的用户」。
        如果不锁 cohort，最后几天的用户没机会回流，会系统性低估留存。
        表里 d0 = 该 cohort 的首日人数，即分母基准。
        """
        an.generate(days=30)
        rows = an.run_all()["retention_cohort"]
        self.assertTrue(rows)
        total_users = db.counts()["users"]
        for row in rows:
            self.assertGreater(row["d0"], 0)
            self.assertLessEqual(row["d0"], total_users)
            # 回流人数不可能超过该 cohort 的总人数
            for k in ("d1", "d3", "d7"):
                self.assertLessEqual(row[k], row["d0"], f"{row['cohort_day']} {k}")
            self.assertLessEqual(row["d1"], row["d0"])
            self.assertTrue(row["last_day"])

    def test_channel_quality_ordering_is_reflected(self):
        an.generate(days=30)
        perf = {r["channel"]: r for r in an.run_all()["channel_performance"]}
        # 朋友推荐的质量系数最高，广告投放最低 —— 数据里应该能看出这个方向
        if "朋友推荐" in perf and "广告投放" in perf:
            self.assertGreater(perf["朋友推荐"]["activation_rate"],
                               perf["广告投放"]["activation_rate"])

    def test_funnel_by_channel(self):
        an.generate(days=20)
        rows = an.run_all()["funnel_by_channel"]
        self.assertTrue(rows)
        for r in rows:
            self.assertGreaterEqual(r["open_to_input"], r["open_to_generate"])
            self.assertGreaterEqual(r["open_to_generate"], r["open_to_adopt"])

    def test_weekday_pattern_covers_seven(self):
        an.generate(days=28)
        rows = an.run_all()["weekday_pattern"]
        self.assertEqual(len(rows), 7)

    def test_diagnose_returns_actionable_hint(self):
        an.generate(days=20)
        d = an.diagnose(an.run_all())
        self.assertTrue(d.get("ok"))
        self.assertTrue(d.get("conclusion"))
        self.assertTrue(d.get("steps"))
        for s in d["steps"]:
            self.assertTrue(s["hint"])
            self.assertTrue(0 <= s["rate"] <= 1)

    def test_diagnose_without_data(self):
        d = an.diagnose({})
        self.assertFalse(d.get("ok"))

    def test_summary_and_full(self):
        self.assertFalse(an.summary().get("has_data"))
        an.generate(days=15)
        s = an.summary()
        self.assertTrue(s["has_data"])
        for k in ("users", "events", "overall_conversion", "retention",
                  "success_rate_7d", "worst_channel"):
            self.assertIn(k, s)
        full = an.full()
        self.assertEqual(set(full), {"data", "diagnosis", "sql", "events_spec"})

    def test_events_spec_documented(self):
        for name, spec in an.EVENTS.items():
            self.assertTrue(spec.get("when"), name)


# ===========================================================================
# 5. bridge
# ===========================================================================
class TestBridge(Base):
    def _promotable_item(self):
        fb.seed_items()
        fb.run_classification()
        rows = br.promotable(min_priority=60, limit=1)
        self.assertTrue(rows)
        return rows[0]["item_id"], rows[0]

    def test_promotable_suggestions_are_populated(self):
        iid, r = self._promotable_item()
        self.assertTrue(r["suggested_expectation"])
        self.assertEqual(r["suggested_difficulty"], "hard")
        self.assertIsInstance(r["suggested_keywords"], list)

    def test_keywords_have_no_glue_fragments(self):
        """
        第 3 级兜底是按字硬切，最容易切出「把东京和」这种碎片。
        判断标准是**首尾是不是虚词** —— 整句里的虚词是正常的
        （「实际要钱」是个好要点，不该被判坏）。
        """
        glue = set("的了和是在把说就也有我你它都很还要着过给对从")
        for text in ["把东京和大阪搞混了，说从东京坐地铁去大阪",
                     "它说有免费门票，实际要钱",
                     "地图上标的地址完全对不上，导航把我带到别的地方去了"]:
            for kw in br.suggest_keywords(text):
                self.assertFalse(kw[0] in glue, f"关键词以虚词开头：{kw}")
                self.assertFalse(kw[-1] in glue, f"关键词以虚词结尾：{kw}")

    def test_keywords_never_split_mid_word(self):
        """兜底也不能把「生成到一半就断掉了」切成「生成到一」「半就断掉」。"""
        kws = br.suggest_keywords("生成到一半就断掉了，什么都没有")
        self.assertNotIn("生成到一", kws)
        self.assertNotIn("半就断掉", kws)

    def test_keywords_are_never_truncated_to_a_useless_stub(self):
        for k in br.suggest_keywords("把东京和大阪搞混了，说从东京坐地铁去大阪"):
            self.assertGreaterEqual(len(k), 2, k)

    def test_promote_creates_traceable_sample(self):
        iid, _ = self._promotable_item()
        r = br.promote(iid)
        self.assertFalse(r["already"])
        s = r["sample"]
        self.assertEqual(s["id"], f"FB{iid:04d}")
        self.assertEqual(s["origin"], "feedback")
        row = db.query_one("SELECT * FROM eval_samples WHERE sample_id=?", (s["id"],))
        self.assertEqual(row["source_item"], iid)
        self.assertEqual(fb.get_item(iid)["promoted_sample"], s["id"])

    def test_promote_is_idempotent(self):
        iid, _ = self._promotable_item()
        before = db.counts()["eval_samples"]
        br.promote(iid)
        r2 = br.promote(iid)
        self.assertTrue(r2["already"])
        self.assertEqual(db.counts()["eval_samples"], before + 1)

    def test_promote_logs_action_event(self):
        iid, _ = self._promotable_item()
        br.promote(iid)
        notes = [h["note"] for h in fb.history(iid)]
        self.assertTrue(any("【动作】" in (n or "") for n in notes))

    def test_promote_unknown_item(self):
        with self.assertRaises(KeyError):
            br.promote(999999)

    def test_promoted_sample_enters_evalset(self):
        self.seed_evalset()
        iid, _ = self._promotable_item()
        br.promote(iid)
        n_feedback = len(ev.load_samples(origin="feedback"))
        self.assertEqual(n_feedback, 1)
        self.assertEqual(len(ev.load_samples()), 101)

    def test_auto_promote_respects_top_n(self):
        fb.seed_items()
        fb.run_classification()
        r = br.auto_promote(top_n=3)
        self.assertEqual(r["count"], 3)
        self.assertEqual(len(r["promoted"]), 3)

    def test_verify_before_any_run(self):
        self.seed_evalset()
        iid, _ = self._promotable_item()
        br.promote(iid)
        v = br.verify(iid)
        self.assertTrue(v["linked"])
        self.assertFalse(v["ran"])
        self.assertIn("还没有被", v["message"])

    def test_verify_without_promotion(self):
        fb.seed_items()
        fb.run_classification()
        iid = br.promotable(min_priority=60, limit=1)[0]["item_id"]
        v = br.verify(iid)
        self.assertFalse(v["linked"])

    def test_verify_finds_sample_despite_later_partial_run(self):
        """回归测试：过滤跑不该让复测失效。"""
        self.seed_evalset()
        iid, _ = self._promotable_item()
        br.promote(iid)
        ev.run_evaluation(note="full")
        ev.run_evaluation(note="partial", limit=4)     # 干扰：最新运行里没有这条样本
        v = br.verify(iid)
        self.assertTrue(v["ran"], v)
        self.assertIsNotNone(v["total"])

    def _promote_and_run(self, passed: bool):
        """
        把一条反馈提升成用例、跑一次评测，然后**强制**这条用例的结果为通过/不通过。

        为什么要强制：这里要测的是关单逻辑本身，不该受「模型恰好答没答对」影响。
        第一版靠运气，跑出来总会有一条被 skipTest 掉 —— 那种用例等于没写。
        """
        self.seed_evalset()
        iid, _ = self._promotable_item()
        br.promote(iid)
        run = ev.run_evaluation(note="t")["run_id"]
        sid = fb.get_item(iid)["promoted_sample"]
        db.execute("UPDATE eval_results SET passed=?, total=? "
                   "WHERE run_id=? AND sample_id=?",
                   (1 if passed else 0, 4.6 if passed else 1.4, run, sid))
        return iid, sid

    def test_close_refused_before_fixed(self):
        """
        复测通过也**不允许**直接关单 —— 必须先走 assigned → fixed。
        理由：复测通过不等于问题已修，也可能是恰好没复现。
        """
        iid, _ = self._promote_and_run(passed=True)
        self.assertTrue(br.verify(iid)["passed"])
        r = br.close_if_verified(iid)
        self.assertFalse(r["closed"])
        self.assertIn("必须先推进到", r["reason"])
        self.assertNotEqual(fb.get_item(iid)["status"], "closed")

    def test_close_after_fixed(self):
        iid, _ = self._promote_and_run(passed=True)
        if fb.get_item(iid)["status"] == "triaged":
            fb.advance(iid, "assigned")
        fb.advance(iid, "fixed", "研发已修复")
        r = br.close_if_verified(iid, "复测通过")
        self.assertTrue(r["closed"])
        self.assertEqual(fb.get_item(iid)["status"], "closed")
        self.assertEqual([t["to"] for t in r["trail"]], ["verified", "closed"])
        self.assertEqual(fb.summary()["closed_count"], 1)

    def test_close_refused_when_not_passed(self):
        iid, _ = self._promote_and_run(passed=False)
        if fb.get_item(iid)["status"] == "triaged":
            fb.advance(iid, "assigned")
        fb.advance(iid, "fixed", "研发说修好了")
        r = br.close_if_verified(iid)
        self.assertFalse(r["closed"])
        self.assertIn("复测未通过", r["reason"])
        # 已修复但复测不通过 —— 应该被打回，不该留在 fixed 蒙混过关
        self.assertNotEqual(fb.get_item(iid)["status"], "closed")

    def test_close_refused_without_promotion(self):
        fb.seed_items()
        fb.run_classification()
        iid = br.promotable(min_priority=60, limit=1)[0]["item_id"]
        r = br.close_if_verified(iid)
        self.assertFalse(r["closed"])

    def test_close_on_missing_item(self):
        with self.assertRaises(KeyError):
            br.close_if_verified(999999)

    def test_coverage_numbers_consistent(self):
        self.seed_evalset()
        fb.seed_items()
        fb.run_classification()
        br.auto_promote(top_n=4)
        ev.run_evaluation(note="t")
        cov = br.coverage()
        self.assertTrue(cov["has_data"])
        self.assertEqual(cov["promoted"] + 0, len(
            [d for d in cov["details"] if d["sample_id"]]))
        self.assertEqual(cov["verified"] + cov["failed"] + cov["not_run"],
                         cov["promoted"])
        self.assertEqual(cov["evalset_samples_from_feedback"],
                         len(ev.load_samples(origin="feedback")))
        self.assertAlmostEqual(cov["promote_rate"],
                               cov["promoted"] / cov["technical_total"], places=4)

    def test_coverage_without_tech_items(self):
        self.assertFalse(br.coverage().get("has_data"))

    def test_close_loop_demo_runs_end_to_end(self):
        self.seed_evalset()
        fb.seed_items()                # 闭环演示需要先有反馈，否则没东西可提升
        fb.run_classification()
        r = br.close_loop_demo(top_n=3)
        self.assertIn("promoted", r)
        self.assertIn("coverage", r)
        self.assertIn("next_action", r)
        self.assertEqual(len(r["promoted"]["promoted"]), 3)
        self.assertTrue(r["run_id"])
        self.assertTrue(r["coverage"]["has_data"])

    def test_close_loop_demo_on_empty_database_explains_instead_of_crashing(self):
        """空库上跑闭环演示应该给出解释，而不是 KeyError / RuntimeError。"""
        r = br.close_loop_demo(top_n=3)
        self.assertEqual(r["promoted"]["count"], 0)
        self.assertEqual(r["passed"], [])
        self.assertTrue(r["next_action"])


# ===========================================================================
# 6. export 与 CLI 装配
# ===========================================================================
class TestExportAndCli(Base):
    def test_export_html_is_self_contained(self):
        from productops import export
        self.seed_all()
        p = export.export()
        html = Path(p).read_text(encoding="utf-8")
        self.assertNotIn("/static/styles.css", html)
        self.assertNotIn("/static/app.js", html)
        self.assertIn("__BOOTSTRAP__", html)
        self.assertIn("__OFFLINE__", html)
        self.assertIn("<style>", html)
        self.assertGreater(len(html), 30_000)

    def test_export_payload_covers_get_routes(self):
        from productops import export
        self.seed_all()
        payload = export.collect_payload()
        for key in ("/api/health", "/api/overview", "/api/eval/runs",
                    "/api/feedback/summary", "/api/bridge/coverage",
                    "/api/analytics/all"):
            self.assertIn(key, payload)
            self.assertTrue(payload[key].get("ok"), key)

    def test_export_summary_md(self):
        from productops import export
        self.seed_all()
        p = export.export_summary_md()
        t = Path(p).read_text(encoding="utf-8")
        for h in ("# ProductOps", "## 效果评测", "## 反馈闭环", "## 数据看板",
                  "## Bridge"):
            self.assertIn(h, t)

    def test_cli_parser_has_all_subcommands(self):
        from productops.cli import build_parser
        p = build_parser()
        sub = [a for a in p._actions if hasattr(a, "choices") and a.dest == "cmd"][0]
        for name in ("init", "doctor", "eval", "feedback", "bridge",
                     "analytics", "demo", "export", "serve", "test"):
            self.assertIn(name, sub.choices)

    def test_cli_version(self):
        from productops import __version__
        self.assertTrue(__version__)


# ===========================================================================
# 7. HTTP API
# ===========================================================================
class TestApi(unittest.TestCase):
    """
    API 测试需要**一个共享的服务器和一份数据**，所以这里不能按用例隔离 ——
    继承 Base 会把 PO_HOME 换掉，服务器读到的库就不是同一份了。
    因此单独继承 TestCase，自己管类级夹具。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp(prefix="po_api_"))
        os.environ["PO_HOME"] = str(cls._tmp)
        from productops.api.server import Handler
        ev.save_evalset(ev.build_evalset())
        fb.seed_items()
        fb.run_classification()
        an.generate(days=12)
        ev.run_evaluation(note="api-fixture")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.verbose = False
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("PO_HOME", None)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _call(self, method, path, body=None):
        import urllib.error
        import urllib.request
        data = json.dumps(body).encode() if body is not None else None
        rq = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                    data=data, method=method,
                                    headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(rq, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {})

    def test_get_endpoints_ok(self):
        for p in ["/api/health", "/api/overview", "/api/eval/rubric",
                  "/api/eval/samples", "/api/eval/runs", "/api/feedback/summary",
                  "/api/feedback/items", "/api/feedback/report",
                  "/api/bridge/coverage", "/api/bridge/promotable",
                  "/api/analytics/all", "/api/analytics/summary"]:
            st, d = self._call("GET", p)
            self.assertEqual(st, 200, p)
            self.assertTrue(d.get("ok"), f"{p} -> {d.get('error')}")

    def test_root_serves_frontend(self):
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=20) as r:
            body = r.read().decode("utf-8")
        self.assertIn("<html", body.lower())
        self.assertIn("/static/app.js", body)

    def test_static_assets(self):
        import urllib.request
        for p, min_len in (("/static/app.js", 1000), ("/static/styles.css", 1000)):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{p}",
                                        timeout=20) as r:
                self.assertGreater(len(r.read()), min_len)

    def test_path_traversal_blocked(self):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/static/../core/config.py",
                    timeout=20) as r:
                self.fail("路径穿越没有被拦住")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_unknown_route_404(self):
        st, d = self._call("GET", "/api/does-not-exist")
        self.assertEqual(st, 404)
        self.assertFalse(d["ok"])

    def test_missing_item_404(self):
        st, d = self._call("GET", "/api/feedback/item/999999")
        self.assertEqual(st, 404)

    def test_add_classify_promote_flow(self):
        st, d = self._call("POST", "/api/feedback/add",
                           {"text": "导出的 PDF 打开全是乱码，试了三次都一样"})
        self.assertEqual(st, 200)
        iid = d["item_id"]

        st, d = self._call("POST", "/api/feedback/classify")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])

        st, d = self._call("POST", "/api/bridge/promote", {"item_id": iid})
        self.assertEqual(st, 200)
        self.assertEqual(d["sample_id"], f"FB{iid:04d}")

        st, d = self._call("POST", "/api/bridge/promote", {"item_id": iid})
        self.assertTrue(d["already"])

        st, d = self._call("GET", f"/api/bridge/verify/{iid}")
        self.assertEqual(st, 200)
        self.assertTrue(d["linked"])

    def test_illegal_transition_returns_400(self):
        st, d = self._call("POST", "/api/feedback/advance",
                           {"item_id": 1, "to": "closed"})
        self.assertEqual(st, 400)
        self.assertFalse(d["ok"])
        self.assertIn("非法流转", d["error"])

    def test_add_without_text_400(self):
        st, d = self._call("POST", "/api/feedback/add", {})
        self.assertEqual(st, 400)

    def test_promote_unknown_item_400(self):
        st, d = self._call("POST", "/api/bridge/promote", {"item_id": 888888})
        self.assertEqual(st, 400)

    def test_eval_run_endpoint(self):
        st, d = self._call("POST", "/api/eval/run", {"limit": 8, "note": "api测试"})
        self.assertEqual(st, 200)
        self.assertLessEqual(d["samples"], 12)
        self.assertIn("gate_verdict", d)

    def test_analytics_seed_idempotent(self):
        st, a = self._call("POST", "/api/analytics/seed", {"days": 12})
        self.assertEqual(st, 200)
        st, b = self._call("POST", "/api/analytics/seed", {"days": 12})
        self.assertEqual(a["users"], b["users"])
        self.assertEqual(a["events"], b["events"])

    def test_close_endpoint_refuses_cleanly(self):
        st, d = self._call("POST", "/api/bridge/close", {"item_id": 1})
        self.assertEqual(st, 200)
        self.assertFalse(d.get("closed"))


# ===========================================================================
# 8. 跨进程可复现性 —— 整套系统的底线
# ===========================================================================
class TestCrossProcessReproducibility(unittest.TestCase):
    """
    同一个输入，两个独立进程、两个独立数据目录，必须得到**完全一样**的数字。

    这是全部结论能否拿去和人讨论的前提。测的是端到端：
    真跑 `run.py init`，然后从数据库里抽一份指纹出来比对。

    如果这条挂了，最可能的原因是有人引入了内置 `hash()` 或没固定 random 种子。
    """

    @classmethod
    def setUpClass(cls):
        cls.homes = [Path(tempfile.mkdtemp(prefix="po_repro_")) for _ in range(2)]
        cls.fingerprints = []
        for home in cls.homes:
            env = dict(os.environ)
            env["PO_HOME"] = str(home)
            env.pop("HTTP_PROXY", None)
            r = subprocess.run(
                [sys.executable, "run.py", "init", "--no-analytics"],
                cwd=str(ROOT), env=env, capture_output=True, timeout=300)
            if r.returncode != 0:
                raise AssertionError(
                    "init 失败：\n" + r.stdout.decode("utf-8", "replace")
                    + r.stderr.decode("utf-8", "replace"))
            cls.fingerprints.append(cls._fingerprint(home))

    @classmethod
    def tearDownClass(cls):
        for h in cls.homes:
            shutil.rmtree(h, ignore_errors=True)

    @staticmethod
    def _fingerprint(home: Path) -> dict:
        """从库里抽一份能代表全部结论的指纹。"""
        import sqlite3
        conn = sqlite3.connect(str(home / "productops.db"))
        conn.row_factory = sqlite3.Row
        q = lambda sql: [dict(r) for r in conn.execute(sql).fetchall()]  # noqa: E731
        run = q("SELECT total_avg, total_std, pass_rate, gate_verdict, sample_count, "
                "metrics_json FROM eval_runs ORDER BY run_id LIMIT 1")[0]
        m = json.loads(run.pop("metrics_json"))
        fp = {
            "run": run,
            "dims": {k: v["avg"] for k, v in m["overall"]["dims"].items()},
            "by_difficulty": {k: (v["total_avg"], v["pass_rate"])
                              for k, v in m["by_difficulty"].items()},
            "rule_llm_gap": (m["overall"]["rule_avg"], m["overall"]["llm_avg"]),
            "safety_gap": m["safety_gap"],
            "counts": {t: q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
                       for t in ("eval_samples", "fb_items", "fb_events")},
            "fb_type_dist": {(r["pred_type"] or ""): r["n"] for r in
                             q("SELECT pred_type, COUNT(*) AS n FROM fb_items "
                               "GROUP BY pred_type")},
            "fb_priority_sum": round(sum(r["p"] or 0 for r in
                                         q("SELECT priority AS p FROM fb_items")), 4),
            "sample_ids": [r["sample_id"] for r in
                           q("SELECT sample_id FROM eval_samples ORDER BY sample_id")],
        }
        conn.close()
        return fp

    def test_two_processes_produce_identical_results(self):
        a, b = self.fingerprints
        self.assertEqual(a, b, "两个独立进程跑出来的结果不一致 —— 可复现性被破坏了")

    def test_seed_is_frozen(self):
        self.assertEqual(stable_seed("doctor-check"), 9926591)

    def test_init_is_idempotent(self):
        """同一目录再跑一次 init，数字不该变。"""
        home = self.homes[0]
        before = self._fingerprint(home)
        env = dict(os.environ)
        env["PO_HOME"] = str(home)
        subprocess.run([sys.executable, "run.py", "init", "--no-analytics"],
                       cwd=str(ROOT), env=env, capture_output=True, timeout=300)
        after = self._fingerprint(home)
        # counts 里 fb_items 幂等，eval_samples 也幂等；但 eval_runs 会 +1，单独剔除
        for k in before:
            if k == "counts":
                self.assertEqual(before[k]["eval_samples"], after[k]["eval_samples"])
                self.assertEqual(before[k]["fb_items"], after[k]["fb_items"])
            else:
                self.assertEqual(before[k], after[k], k)


# ===========================================================================
if __name__ == "__main__":
    unittest.main(verbosity=2)
