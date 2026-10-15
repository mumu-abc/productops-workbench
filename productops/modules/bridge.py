#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块四：Bridge —— 把「用户吐槽」变成「回归测试用例」

这是整个产品里唯一一个**新造**的东西，也是三个模块被做成一个产品的理由。

---------------------------------------------------------------------
为什么需要它（这是我认为最值钱的一段产品思考）

原来的三个工具各自都能跑，但连起来看有一个断点：

    feedback 模块告诉你：用户反馈「餐厅信息是过期的」，归因为技术问题，
                        优先级 P0，责任人算法。
    然后呢？派单 → 研发修 Prompt → 上线 → **然后没有人知道它到底修好没有。**

「修好了」这句话的可信度等于零，因为：
  1. 用户不会再回来告诉你「这次是对的」；
  2. 研发说修好了，但他测的是他自己挑的例子；
  3. 下次改 Prompt 的时候，这个 case 还会不会再坏，没人管。

所以真正的闭环必须有第五步：**把这条反馈变成一条会一直跑下去的测试用例。**
修好不算完，进了评测集、并且每次发版都跑过，才算完。

Bridge 就是把这一段接上的那个零件：
    反馈原文  ->  评测集样本（origin=feedback，可溯源）
    评测集样本 ->  每次发版都跑
    跑过了    ->  才允许把这条反馈推进到「复测通过」

于是「闭环率」不再是一句形容词，而是一个能被 SQL 算出来的数：
    SELECT COUNT(*) FROM fb_items WHERE status IN ('verified','closed');

---------------------------------------------------------------------
边界说明（诚实交代，别吹）
自动生成的样本只保证两件事：
  1. **问法来自真实用户**（不是我们自己编的）
  2. **期望行为由归因结果给出**
关键词和禁忌表述用启发式提取，**质量不如人工标注**，
所以接口保留了覆盖参数，真实生产里这一步应该有人过一遍。
"""

import re
from typing import Dict, List, Optional

from ..core import db
from . import evaluation as ev
from . import feedback as fb

# 关键词候选词表：这些是「AI 旅行规划」这个产品里，用户抱怨时真正会提到的实体。
# 用词表而不是分词器，是因为要零依赖；真实场景应该换成 jieba / 领域词典。
_TERM_VOCAB = [
    "餐厅", "酒店", "景点", "门票", "行程", "路线", "距离", "汇率", "预算", "日期",
    "格式", "乱码", "PDF", "地图", "交通", "地铁", "机场", "签证", "天气", "机票",
    "门店", "营业", "关门", "停业", "价格", "费用", "时间", "天数", "顺序", "重复",
    "搜索结果", "推荐", "名称", "地址", "电话", "链接", "加载", "生成", "导出", "分享",
    "复制", "缓存", "超时", "字数", "语言", "币种", "时区", "排队", "预约",
]

# 技术类抱怨 → 期望行为的映射。
# 这张表决定了「这条反馈变成测试用例后，到底考什么」。
_EXPECTATION_RULES = [
    (("关门", "停业", "过时", "过期", "早就", "去年"),
     "不得给出已停业 / 已过期的商户信息；无法确认时应明确说明变动可能", ["门店", "营业"]),
    (("重复", "出现三次", "出现两次", "一样"),
     "同一行程内不得重复推荐同一地点", ["去重"]),
    (("距离", "算错", "不对", "完全不一样", "搞混"),
     "地理与常识判断必须正确，不得出现明显错误的城市/距离关系", ["核对"]),
    (("日期", "星期", "有误"),
     "日期与星期必须对应正确", ["核对"]),
    (("汇率", "币种", "预算对不上"),
     "金额换算必须正确，并说明汇率会波动", ["汇率", "波动"]),
    (("太慢", "等了", "卡", "断掉", "没生成", "只给了一"),
     "应在合理时间内返回完整结果；失败时必须给出明确提示而不是静默卡住",
     ["重试", "提示"]),
    (("格式", "乱码", "符号", "打不开"),
     "输出格式必须可读、可正常打开", ["格式"]),
    (("八个", "走不完", "太多"),
     "单日景点数量要符合实际可达性，不得超出体力与时间上限", ["可达"]),
    (("名称", "搜不到"),
     "地名 / 商户名必须准确可检索", ["核对"]),
]
_DEFAULT_EXPECTATION = "该问题不应复现；若无法做到，必须明确说明不确定性与替代方案"


_STOP = {"这个", "那个", "完全", "根本", "什么", "怎么", "就是", "已经", "还是",
         "一下", "一个", "而且", "然后", "但是", "所以", "真的", "好多", "好多"}


def suggest_keywords(text: str) -> List[str]:
    """
    从反馈原文里提取可能的评测要点。

    三级降级策略，优先级从高到低：
      1. 命中领域词表（餐厅 / 门票 / 退出码……）—— 最准，直接当要点
      2. 按标点切成小句，取长度 <= 6 的整句 —— 宁可少给，也别给「生成到一」这种半截词。
         之前用固定 4 字硬切中文，切出来的碎片放进评测集里毫无意义。
      3. 退无可退时才取 3~4 字连续汉字，并去掉明显无意义的停用词
    """
    kws = [t for t in _TERM_VOCAB if t in text]
    if len(kws) >= 2:
        return kws[:6]

    # 第 2 级：整句
    for seg in re.split(r"[^\u4e00-\u9fff]+", text):
        if not (2 <= len(seg) <= 6):
            continue           # 太短没信息，太长不像「要点」
        if seg in _STOP or seg in kws or any(seg in k for k in kws):
            continue
        kws.append(seg)
        if len(kws) >= 5:
            break
    if len(kws) >= 2:
        return kws[:6]

    # 第 3 级：连续汉字片段（兜底，质量最差，故放最后）。
    # 含虚词的片段一律丢掉 —— "把东京和" 这种碎片进评测集只会让人笑。
    glue = set("的了和是在把说就也有我你它都很还要着过给对从把")
    for g in re.findall(r"[\u4e00-\u9fff]{3,4}", text):
        if g in _STOP or g in kws or any(ch in glue for ch in g):
            continue
        kws.append(g)
        if len(kws) >= 4:
            break
    return kws[:6]


def suggest_expectation(text: str) -> tuple:
    """返回 (期望行为描述, 必须出现的要点)。匹配不到就用默认。"""
    for keys, expectation, musts in _EXPECTATION_RULES:
        if any(k in text for k in keys):
            return expectation, list(musts)
    return _DEFAULT_EXPECTATION, []


def suggest_tags(text: str) -> List[str]:
    """判断这条反馈转化来的样本，是不是「边界题」（考察该不该拒绝）。"""
    tags: List[str] = []
    if any(k in text for k in ("隐私", "身份信息", "别人的卡", "骗过", "伪造", "假")):
        tags += ["违规", "隐私"]
    if any(k in text for k in ("处方药", "医疗", "用药")):
        tags.append("医疗")
    return list(dict.fromkeys(tags))


# ===========================================================================
def promotable(min_priority: float = 60.0, category: str = "technical",
               only_unpromoted: bool = True, limit: int = 50) -> List[Dict]:
    """
    列出「值得变成回归测试」的反馈。

    默认只取归因为 technical 的：因为需求 / 表达 / 分发问题的验收方式是
    「功能有没有上」「文案改了没」，不需要回归测试集；
    只有技术问题（输出错了、慢了、幻觉了）才需要一条常驻用例来防复发。
    """
    sql = ("SELECT * FROM fb_items WHERE priority >= ? AND pred_type = ?"
           if category != "any" else "SELECT * FROM fb_items WHERE priority >= ?")
    params: List = [min_priority, category] if category != "any" else [min_priority]
    if only_unpromoted:
        sql += " AND (promoted_sample IS NULL OR promoted_sample = '')"
    sql += " ORDER BY priority DESC LIMIT ?"
    params.append(limit)

    rows = db.query(sql, params)
    for r in rows:
        exp, musts = suggest_expectation(r["text"])
        r["suggested_keywords"] = suggest_keywords(r["text"])
        r["suggested_expectation"] = exp
        r["suggested_musts"] = musts
        r["suggested_tags"] = suggest_tags(r["text"])
        r["suggested_difficulty"] = "hard"   # 用户真实吐槽天然是难例
    return rows


def promote(item_id: int, difficulty: str = "hard", keywords: Optional[List[str]] = None,
            expected: Optional[str] = None, must_not: Optional[List[str]] = None,
            tags: Optional[List[str]] = None, note: str = "") -> Dict:
    """
    把一条反馈提升为评测集样本。

    - sample_id 用 `FB<item_id>`，保证可溯源、重复调用幂等
    - 写入 eval_samples（origin='feedback'，source_item=item_id）
    - 回写 fb_items.promoted_sample
    - 记一条 fb_events（审计：这条样本是怎么来的）
    """
    item = fb.get_item(item_id)
    if not item:
        raise KeyError(f"item_id={item_id} 不存在")
    if item["promoted_sample"]:
        return {"item_id": item_id, "sample_id": item["promoted_sample"],
                "already": True, "message": "该反馈已提升为评测样本，未重复创建"}

    text = item["text"]
    exp_auto, musts_auto = suggest_expectation(text)
    sample = {
        "id": f"FB{item_id:04d}",
        "category": fb.CATEGORIES.get(item["pred_type"] or "", {}).get("label", "用户反馈"),
        "difficulty": difficulty if difficulty in ("easy", "medium", "hard") else "hard",
        "question": text,
        "expected": expected or exp_auto,
        "keywords": keywords if keywords is not None else suggest_keywords(text),
        "must_not": must_not if must_not is not None else list(musts_auto),
        "tags": tags if tags is not None else suggest_tags(text),
        "origin": "feedback",
        "source_item": item_id,
    }
    ev.add_sample(sample)

    with db.tx() as conn:
        conn.execute("UPDATE fb_items SET promoted_sample=?, updated_at=? WHERE item_id=?",
                     (sample["id"], db.now(), item_id))
        conn.execute(
            """INSERT INTO fb_events (item_id, from_status, to_status, note, created_at)
               VALUES (?,?,?,?,?)""",
            (item_id, item["status"], item["status"],
             note or f"【动作】已提升为回归测试用例 {sample['id']}"
                     f"（难度 {sample['difficulty']}，评测要点 {'、'.join(sample['keywords']) or '待补'}）",
             db.now()))

    return {"item_id": item_id, "sample_id": sample["id"], "sample": sample,
            "already": False,
            "message": f"已把反馈 #{item_id} 提升为回归用例 {sample['id']}"}


def auto_promote(top_n: int = 5, min_priority: float = 60.0) -> Dict:
    """一键把 Top N 条技术类反馈全部提升（演示「闭环自动化」用）。"""
    cands = promotable(min_priority=min_priority, limit=top_n)
    done, skipped = [], 0
    for c in cands:
        r = promote(c["item_id"])
        if r["already"]:
            skipped += 1
        else:
            done.append(r["sample_id"])
    return {"promoted": done, "count": len(done), "skipped": skipped,
            "considered": len(cands)}


# ===========================================================================
def verify(item_id: int) -> Dict:
    """
    复测：查这条反馈对应的样本，在**最近一次跑到过它的评测**里表现如何。

    为什么不是「最近一次评测」：过滤跑（--limit / --difficulty）也会留下运行记录，
    但它里面没有这条用例。用最新的运行去判断，会把跑过的样本错判成「未跑过」。
    复测要回答的是「这条用例上次被检验是什么时候、结果怎样」。

    这一步是「闭环」真正闭合的地方 —— 研发说修好了不算，
    评测跑过了才算。而且因为样本常驻评测集，以后每次发版都会被再验一次。

    注意：复测通过**不会自动把状态推到 verified**，
    只会给出 `can_advance` 建议。原因和自动派单一样 ——
    自动化到「改状态」这一步就越权了，状态变更必须留给人。
    """
    item = fb.get_item(item_id)
    if not item:
        raise KeyError(f"item_id={item_id} 不存在")
    sid = item["promoted_sample"]
    if not sid:
        return {"item_id": item_id, "linked": False,
                "message": "该反馈还没有对应的回归测试用例，先用 bridge promote 提升"}

    # 注意用 latest_run_with_sample 而不是 latest_run_id：
    # 最后一次运行可能是 --limit 过滤跑，那不构成「这条用例被检验过」。
    run_id = ev.latest_run_with_sample(sid)
    if not run_id:
        return {"item_id": item_id, "sample_id": sid, "linked": True,
                "ran": False, "message": "该用例还没有被任何一次评测跑到过，先执行 eval run"}

    row = db.query_one(
        "SELECT * FROM eval_results WHERE run_id=? AND sample_id=?", (run_id, sid))
    if not row:
        return {"item_id": item_id, "sample_id": sid, "linked": True, "ran": False,
                "run_id": run_id, "message": "评测结果缺失，重跑一次即可"}

    passed = bool(row["passed"])
    cur = item["status"] or "new"
    can_advance = passed and "verified" in fb.TRANSITIONS.get(cur, [])
    return {
        "item_id": item_id, "sample_id": sid, "linked": True, "ran": True,
        "run_id": run_id, "total": row["total"], "scores": {
            "accuracy": row["accuracy"], "completeness": row["completeness"],
            "safety": row["safety"], "readability": row["readability"]},
        "passed": passed, "status": cur, "can_advance": can_advance,
        "message": ("复测通过，可以推进到「复测通过」" if can_advance else
                    "复测通过，但当前状态不允许直接推进（先走 assigned→fixed）"
                    if passed else "复测未通过，问题仍然存在"),
    }


def coverage() -> Dict:
    """
    覆盖率：技术类反馈里，有多少条真正进入了回归测试集，以及跑过之后通过多少。

    这个数字是我认为最该放到运营周报首页的一个 ——
    它直接回答「我们上次提的问题，现在还有没有再犯」。
    """
    tech = db.query("SELECT * FROM fb_items WHERE pred_type='technical'")
    if not tech:
        return {"has_data": False}

    promoted = [t for t in tech if t.get("promoted_sample")]

    verified, failed, not_run = 0, 0, 0
    details: List[Dict] = []
    for t in promoted:
        sid = t["promoted_sample"]
        # 逐条找「这条用例最近一次被跑到」的运行，避免被过滤跑的最新记录干扰
        rid = ev.latest_run_with_sample(sid)
        row = (db.query_one("SELECT * FROM eval_results WHERE run_id=? AND sample_id=?",
                            (rid, sid)) if rid else None)
        if not row:
            not_run += 1
            details.append({"item_id": t["item_id"], "sample_id": sid,
                            "text": t["text"], "state": "未跑过"})
        elif row["passed"]:
            verified += 1
            details.append({"item_id": t["item_id"], "sample_id": sid, "text": t["text"],
                            "state": "复测通过", "total": row["total"], "run_id": rid})
        else:
            failed += 1
            details.append({"item_id": t["item_id"], "sample_id": sid, "text": t["text"],
                            "state": "复测未通过", "total": row["total"], "run_id": rid})

    total_samples = db.query_one(
        "SELECT COUNT(*) AS n FROM eval_samples WHERE origin='feedback'") or {"n": 0}
    return {
        "has_data": True,
        "technical_total": len(tech),
        "promoted": len(promoted),
        "promote_rate": round(len(promoted) / len(tech), 4) if tech else 0.0,
        "verified": verified, "failed": failed, "not_run": not_run,
        "verify_pass_rate": round(verified / len(promoted), 4) if promoted else 0.0,
        "evalset_samples_from_feedback": total_samples["n"],
        "latest_run_id": ev.latest_run_id(),
        "details": details,
    }


def summary() -> Dict:
    """给总览页用的桥接摘要。"""
    cov = coverage()
    if not cov.get("has_data"):
        return {"has_data": False, "message": "暂无技术类反馈"}
    return cov


def close_if_verified(item_id: int, note: str = "") -> Dict:
    """
    复测通过后关单：fixed → verified → closed。

    这里的「自动」和「不自动」要分清楚，这是产品设计上的一个判断：

      不自动 —— `triaged → assigned`（归因置信度 90% 意味着每 10 条错 1 条，
                让 10% 的错误自动流向研发，是给研发制造噪音）
      自动   —— `fixed → verified → closed`（研发说修好了，测试跑过了；
                这一步是**机械确认**，判断权在测试，不在人）

    必须先跑到 `fixed`（研发已修复）才允许关单 ——
    不能因为一条反馈「碰巧在评测里过了」就把它关掉，
    否则会掩盖「问题只是恰好没复现」这种情况。
    """
    v = verify(item_id)
    if not v.get("linked"):
        return {**v, "closed": False}
    if not v.get("ran"):
        return {**v, "closed": False}
    if not v.get("passed"):
        return {**v, "closed": False, "reason": "复测未通过，不允许关单"}

    cur = v.get("status")
    if cur == "fixed":
        steps = [("verified", note or "复测通过，自动确认"), ("closed", "闭环完成")]
    elif cur == "verified":
        steps = [("closed", note or "闭环完成")]
    else:
        return {**v, "closed": False,
                "reason": f"当前状态是「{fb.STATE_LABELS.get(cur, cur)}」，"
                          f"必须先推进到「已修复」才能关单 —— "
                          f"复测通过不等于问题已修，也可能是恰好没复现"}

    trail = []
    for to_status, nt in steps:
        trail.append(fb.advance(item_id, to_status, nt))
    return {**v, "closed": True, "trail": trail,
            "message": f"反馈 #{item_id} 已闭环（复测 {v['total']} 分）"}


def close_loop_demo(top_n: int = 5) -> Dict:
    """
    端到端演示「一条吐槽走完整条闭环」：

        提升为用例 → 跑全量评测 → 复测 → 报告可关闭项

    这是我准备现场演示的路径，因为它一眼就能看出「闭环」到底成没成立。
    注意本函数**不改状态**（除了提升），关闭动作由 close_if_verified 显式触发。
    """
    promo = auto_promote(top_n=top_n)
    try:
        run = ev.run_evaluation(note="bridge 闭环复测")
    except RuntimeError as e:
        return {"promoted": promo, "run_id": None, "coverage": coverage(),
                "passed": [], "failed": [], "ready_to_close": [],
                "next_action": f"无法复测：{e}"}
    cov = coverage()

    passed, failed = [], []
    # coverage() 在「一条技术类反馈都没有」时返回 {"has_data": False}，没有 details。
    # 第一版直接下标 cov["details"]，于是空库上跑闭环演示会 KeyError 而不是给出解释。
    for d in cov.get("details", []):
        (passed if d.get("state") == "复测通过" else failed).append(d)

    ready = []
    for d in passed:
        item = fb.get_item(d["item_id"])
        if item and (item["status"] or "") == "fixed":
            ready.append(d["item_id"])

    return {
        "promoted": promo,
        "run_id": run["run_id"],
        "run_samples": run["samples"],
        "gate_verdict": run["metrics"]["gate"]["verdict"],
        "coverage": cov,
        "passed": [d["item_id"] for d in passed],
        "failed": [d["item_id"] for d in failed],
        "ready_to_close": ready,
        "next_action": (
            f"有 {len(ready)} 条已修复的反馈复测通过，可执行 close_if_verified 关单"
            if ready else
            "没有处于「已修复」状态且复测通过的反馈。"
            "正常流程：把通过的反馈推进到 assigned → fixed，再执行关单"),
    }
