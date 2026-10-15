#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块二：反馈闭环（Feedback Loop）

回答一个问题：**用户到底在骂什么，该谁去修，修完了吗？**

---------------------------------------------------------------------
和老版本 demo 最大的区别：**加了闭环状态机。**

老版本有一份《反馈闭环八步法》的 SOP 文档，写得很漂亮，
但代码里根本没有「状态」这个概念 —— 收集完、分完类、排完序就结束了。
等于说「闭环」只存在于 PPT 里。

这个版本把闭环变成了数据结构：
    new → triaged → assigned → fixed → verified → closed
每一步都写审计表（fb_events），于是「闭环率」变成一个**可算出来的数**，
而不是一句形容词。没有状态机，你永远无法回答「上个季度提的问题修了多少」。

---------------------------------------------------------------------
四类归因的设计逻辑（这是整个模块最值钱的部分）：

用户说「不好用」是没有信息量的。要变成可执行的，必须回答一个问题：
**这个问题应该由谁来解决？**

所以分类不是按「问题长什么样」分，而是按「解法归属」分：

    需求问题  demand       产品压根没这个能力       -> 产品经理
    表达问题  expression   功能有，用户不知道/找不到 -> 运营 + 设计
    分发问题  distribution 功能有也知道，但推错人   -> 运营 + 增长
    技术问题  technical    功能对路，但输出是错的    -> 算法 + 研发

直接好处：归因一出来，派单就完成了，不用再开会对齐责任人。
"""

import json
import random
import time
from typing import Dict, List, Optional, Tuple

from ..core import db, llm
from ..core.config import Config, stable_seed
from ..core.stats import log_norm, pct

# ===========================================================================
# 一、归因分类体系
# ===========================================================================
CATEGORIES: Dict[str, Dict] = {
    "demand": {
        "label": "需求问题", "owner": "产品经理",
        "desc": "产品压根没有这个能力，用户需要的是新功能",
        "action": "进需求池，评估价值与成本后排期",
    },
    "expression": {
        "label": "表达问题", "owner": "运营 / 设计",
        "desc": "功能存在，但用户不知道、找不到、看不懂",
        "action": "改文案、改引导、改信息结构，成本低见效快",
    },
    "distribution": {
        "label": "分发问题", "owner": "运营 / 增长",
        "desc": "功能有且用户知道，但推给了错误的人群或场景",
        "action": "调整推荐策略、人群分层、模板投放",
    },
    "technical": {
        "label": "技术问题", "owner": "算法 / 研发",
        "desc": "功能对路，但输出本身是错的（幻觉、算错、格式坏）",
        "action": "进 badcase 库，归因到 Prompt / 知识库 / 模型能力",
    },
}
CAT_ORDER = ["technical", "expression", "distribution", "demand"]

# 规则分类器的关键词表。
# 判断顺序很关键：先排除「硬错误」，再看用户行为，最后才是功能诉求。
# 因为「它给的信息是错的」和「我希望加个功能」在语义上完全不冲突，
# 但前者紧急得多 —— 顺序错了会把技术问题误判成需求问题。
RULES: List[Tuple[str, List[str]]] = [
    ("technical", ["错误", "不对", "算错", "重复", "格式", "乱码", "太慢", "等了",
                   "断掉", "根本没", "搞混", "关门", "已经停业", "不符", "完全不一样",
                   "有误", "没生成", "只给了一", "乱来", "对不上", "打不开", "搜不到",
                   "日期有误", "免费"]),
    ("expression", ["找不到", "不知道", "在哪", "没看到", "看不懂", "以为", "没意识到",
                    "藏的", "太深", "没有引导", "不知道怎么", "没提示", "找了三分钟",
                    "瞎点", "反应不过来", "不知道能", "没发现", "什么意思", "不明显",
                    "太小", "看不清"]),
    ("distribution", ["我是做", "我是一个", "我们公司", "老年人", "学生党", "我在国外",
                      "不适合", "不太匹配", "更合适", "对我没用", "不是我想要的",
                      "我要的是", "残障", "无障碍", "带团队", "字符太", "我在东北",
                      "摄影", "情侣"]),
    ("demand", ["希望", "能不能", "想要", "建议加", "支持", "增加", "加一个",
                "最好能", "如果有", "期待", "可否", "建议", "加个"]),
]

# 状态机定义
STATES = ["new", "triaged", "assigned", "fixed", "verified", "closed", "wontfix"]
STATE_LABELS = {
    "new": "待归因", "triaged": "已归因", "assigned": "已派单", "fixed": "已修复",
    "verified": "复测通过", "closed": "已关闭", "wontfix": "不做/无法复现",
}
TRANSITIONS = {
    "new": ["triaged", "wontfix"],
    "triaged": ["assigned", "wontfix"],
    "assigned": ["fixed", "wontfix"],
    "fixed": ["verified", "assigned"],      # 复测不通过会被打回重新修
    "verified": ["closed"],
    "closed": [],
    "wontfix": [],
}
# 「算闭环」的状态。注意 verified 和 closed 才算 ——
# fixed（研发说修好了）不算，因为没人验证过。
CLOSED_STATES = {"verified", "closed"}


def rule_classify(text: str) -> Tuple[str, float]:
    """
    关键词规则分类。返回 (类别, 置信度)。

    置信度怎么来的：
        base 0.72；每多命中一个关键词 +0.06（上限 +0.18）；
        若同时命中 2 个及以上不同类型的规则，说明信号打架，扣 0.17。
    不是精确科学，但它让「能不能自动派单」变成可量化的事 ——
    置信度低于阈值的转人工，这才叫可运营。
    """
    hits = {cat: sum(1 for w in words if w in text) for cat, words in RULES}
    matched = [c for c in CAT_ORDER if hits[c] > 0]
    if not matched:
        return "expression", 0.45          # 兜底给表达问题，且低置信
    best = matched[0]                       # CAT_ORDER 已按优先级排好
    conf = 0.72 + min(0.18, 0.06 * (hits[best] - 1))
    if len(matched) >= 2:
        conf -= 0.17
    return best, round(max(0.30, min(0.97, conf)), 2)


def priority_score(severity: int, users_affected: int, freq: int) -> float:
    """
    优先级 = 0.5×严重度 + 0.3×影响面 + 0.2×复现频次，归一到 0~100。

    权重为什么这么给：
        严重度第一 —— 一个会误导用户的错误，比一万个人吐槽「找不到按钮」更该先修；
        影响面第二；频次第三（频次高但没影响面，可能只是小圈子的习惯）。
    影响面/频次为什么取对数：见 core.stats.log_norm 的说明。
    """
    s = (max(1, min(5, severity)) - 1) / 4.0
    r = log_norm(max(0, users_affected), 1000)
    f = log_norm(max(0, freq), 100)
    return round(100 * (Config.W_SEVERITY * s + Config.W_REACH * r + Config.W_FREQ * f), 1)


def priority_band(score: float) -> str:
    if score >= 75:
        return "P0 立即"
    if score >= 60:
        return "P1 本周"
    if score >= 40:
        return "P2 排期"
    return "P3 观察"


def _signature(text: str) -> set:
    """极简中文切分：2-gram 字符集合。真实场景应换 jieba 等分词器。"""
    t = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def merge_similar(items: List[Dict], threshold: float = 0.5) -> List[Dict]:
    """
    合并重复反馈：同类别 + 高重合度的归到一条，影响面取大、频次累加。

    为什么必须做：反馈量一大，同一个问题会被重复提几十次。
    不合并的话 Top 10 清单会被同一个问题占满，运营看不到真正的问题分布。

    注意：这里必须容忍「还没归因」的反馈（pred_type / priority / band 全是 NULL）。
    第一版直接用 `it.get("priority", 0.0)`，看起来有默认值，
    但字典里这个 key 是存在的、值就是 None，默认值根本不生效，
    于是 `-None` 抛 TypeError ——
    表现是「手动录一条反馈、还没跑归因，一打开看板就 500」。
    """
    num = lambda v: v if isinstance(v, (int, float)) else 0   # noqa: E731

    groups: List[Dict] = []
    for it in items:
        sig = _signature(it["text"])
        for g in groups:
            if g["cat"] == it.get("pred_type") and _overlap(g["sig"], sig) >= threshold:
                g["merged"] += 1
                g["users_affected"] = max(g["users_affected"], num(it.get("users_affected")))
                g["freq"] += num(it.get("freq"))
                g["ids"].append(it["item_id"])
                g["severity"] = max(g["severity"], num(it.get("severity")))
                g["priority"] = priority_score(g["severity"], g["users_affected"], g["freq"])
                g["band"] = priority_band(g["priority"])
                break
        else:
            groups.append({
                "cat": it.get("pred_type"), "sig": sig, "text": it["text"],
                "ids": [it["item_id"]], "merged": 1,
                "users_affected": num(it.get("users_affected")),
                "freq": num(it.get("freq")) or 1,
                "severity": num(it.get("severity")) or 3,
                "priority": num(it.get("priority")),
                "band": it.get("band") or "",
            })
    return sorted(groups, key=lambda g: -num(g["priority"]))


# ===========================================================================
# 二、种子反馈数据
# ===========================================================================
# (ext_id, channel, severity, users_affected, freq, gt_type, text)
_CHANNELS = ["应用商店评价", "客服工单", "社群反馈", "用户访谈", "问卷开放题"]
_SEED: List[Tuple] = [
    # ---- demand：产品压根没有这个能力 ----
    ("D01", 0, 3, 180, 22, "demand", "希望支持多城市联程，现在只能一个城市一个城市问"),
    ("D02", 1, 2, 96, 14, "demand", "能不能加一个自动生成行李清单的功能"),
    ("D03", 2, 3, 240, 31, "demand", "建议加个行程分享给同行人一起编辑"),
    ("D04", 4, 3, 152, 19, "demand", "想要一个按预算倒推行程的功能"),
    ("D05", 0, 2, 64, 9, "demand", "最好能导入我自己的攻略文档再生成"),
    ("D06", 2, 2, 88, 12, "demand", "如果有针对自驾游的路线规划就好了"),
    ("D07", 1, 2, 120, 16, "demand", "期待支持离线查看已生成的行程"),
    ("D08", 3, 4, 310, 27, "demand", "可否增加签证材料的自动核对"),
    ("D09", 4, 3, 205, 24, "demand", "能不能把行程一键导出成日历"),
    ("D10", 2, 2, 73, 11, "demand", "建议加个多人投票选景点"),
    ("D11", 0, 2, 51, 7, "demand", "支持把行程打印成小册子就好了"),
    ("D12", 1, 4, 168, 20, "demand", "希望有当地紧急联系方式的汇总"),
    ("D13", 2, 3, 134, 17, "demand", "加一个酒店比价功能吧"),
    ("D14", 3, 3, 96, 13, "demand", "最好能同步我的航班和酒店订单"),
    ("D15", 4, 2, 142, 21, "demand", "想要给行程加备注和待办"),

    # ---- expression：功能有，但用户不知道 / 找不到 ----
    ("E01", 0, 3, 420, 46, "expression", "我找了半天不知道怎么开始输入需求，按钮太不明显了"),
    ("E02", 1, 3, 380, 39, "expression", "不知道这个App能干什么，首页没有任何说明"),
    ("E03", 2, 3, 265, 28, "expression", "第几步输入完全看不懂，以为只能选模板"),
    ("E04", 0, 2, 198, 23, "expression", "找了三分钟才发现还能改天数"),
    ("E05", 3, 3, 310, 35, "expression", "没看到有导出功能，后来才在右上角发现"),
    ("E06", 4, 4, 176, 18, "expression", "以为重新生成会覆盖，结果多出来一份，很懵"),
    ("E07", 1, 2, 142, 15, "expression", "不知道怎么删除不要的行程"),
    ("E08", 2, 3, 224, 26, "expression", "完全没意识到可以设置预算"),
    ("E09", 0, 2, 131, 14, "expression", "那个图标什么意思啊，点了才知道"),
    ("E10", 3, 3, 158, 17, "expression", "引导太深了，藏了三层菜单"),
    ("E11", 4, 2, 97, 12, "expression", "输错字了不知道能不能改"),
    ("E12", 1, 3, 289, 33, "expression", "没提示生成要多久，我以为是卡死了"),
    ("E13", 2, 2, 84, 10, "expression", "不知道能选语言，一直是中文"),
    ("E14", 0, 3, 212, 25, "expression", "添加景点的入口不明显，瞎点了半天"),
    ("E15", 3, 4, 156, 14, "expression", "提示文字太小，老年人根本看不清"),

    # ---- distribution：功能有也知道，但推错人 / 场景 ----
    ("S01", 1, 4, 268, 24, "distribution", "我是做外贸的，经常一个人出差，给我推的都是家庭亲子游"),
    ("S02", 0, 3, 512, 48, "distribution", "我是一个大学生，预算有限，推荐的酒店都太贵了"),
    ("S03", 3, 4, 196, 19, "distribution", "我们公司团队出行十几个人，这个功能对我们不合适"),
    ("S04", 2, 4, 340, 30, "distribution", "老年人用不了，字小步骤多"),
    ("S05", 1, 4, 158, 16, "distribution", "我在国外用，推荐的全是国内的目的地"),
    ("S06", 4, 5, 122, 11, "distribution", "我是残障人士，需要无障碍信息，App里没有"),
    ("S07", 2, 3, 88, 8, "distribution", "带团队出行需要审批流程，这个不匹配"),
    ("S08", 3, 3, 145, 18, "distribution", "我经常去同一个城市出差，想要复用模板，推的都是新目的地"),
    ("S09", 0, 2, 287, 26, "distribution", "学生党穷游，希望有经济型方案"),
    ("S10", 1, 3, 176, 15, "distribution", "我是摄影爱好者，想要机位和光线时间，不是热门景点"),
    ("S11", 4, 3, 234, 22, "distribution", "我带娃出行，需要母婴设施信息"),
    ("S12", 2, 3, 168, 19, "distribution", "字符太大屏幕装不下，我想要紧凑模式"),
    ("S13", 0, 4, 104, 9, "distribution", "我坐轮椅，需要电梯和坡道信息"),
    ("S14", 3, 2, 142, 13, "distribution", "我们是一对情侣，推的都是亲子内容"),
    ("S15", 1, 3, 118, 12, "distribution", "我在东北，想找周边的，推的都是境外"),

    # ---- technical：功能对路，但输出是错的 ----
    ("T01", 0, 5, 386, 41, "technical", "它推荐的餐厅去年就关门了，信息完全不对"),
    ("T02", 1, 4, 274, 29, "technical", "同行两天的行程算错了，把第三天也塞进去了"),
    ("T03", 2, 5, 198, 23, "technical", "生成到一半断掉了，什么也没有"),
    ("T04", 0, 4, 452, 57, "technical", "等了五分钟还没出来，太慢了"),
    ("T05", 3, 3, 226, 25, "technical", "同一个景点重复出现了三次"),
    ("T06", 4, 4, 148, 16, "technical", "输出格式乱码了，全是符号"),
    ("T07", 1, 4, 312, 34, "technical", "明明只有一天，它给安排了八个景点，根本走不完"),
    ("T08", 2, 4, 187, 20, "technical", "地图上标的距离和它说的完全不一样"),
    ("T09", 0, 4, 241, 27, "technical", "汇率算错了，预算完全对不上"),
    ("T10", 3, 5, 165, 18, "technical", "它说有免费门票，实际要钱"),
    ("T11", 4, 5, 209, 24, "technical", "只给了一句话，什么都没生成"),
    ("T12", 1, 5, 178, 21, "technical", "把东京和大阪搞混了，说从东京坐地铁去大阪"),
    ("T13", 2, 3, 134, 15, "technical", "日期有误，说那天是周一实际是周日"),
    ("T14", 0, 3, 96, 11, "technical", "生成的PDF打不开"),
    ("T15", 3, 4, 154, 17, "technical", "景点名称是错的，搜不到"),

    # ---- 重复上报：真实产品里同一个问题会被多渠道反复提 ----
    # 这 12 条 gt_type 留空（不参与分类器准确率评估），
    # 专门用来验证「合并重复反馈」这件事真的发生了 ——
    # 不合并的话，Top 10 清单会被同一个问题占满，运营看不到真实的问题分布。
    ("R01", 1, 3, 240, 27, None, "希望能一次规划多城市联程，现在只能一个城市一个城市问"),
    ("R02", 3, 3, 355, 34, None, "不知道从哪开始输入需求，那个按钮太不明显了"),
    ("R03", 4, 5, 341, 36, None, "它推荐的餐厅早就关门了，信息完全不对"),
    ("R04", 2, 4, 398, 49, None, "等了十分钟还没出来，太慢了"),
    ("R05", 1, 3, 466, 41, None, "我是一个学生，预算有限，推荐的酒店实在太贵了"),
    ("R06", 0, 4, 132, 14, None, "输出全是乱码，都是符号"),
    ("R07", 3, 4, 139, 12, None, "提示的文字太小了，根本看不清"),
    ("R08", 2, 3, 208, 22, None, "这个景点重复出现了三次，浪费了时间"),
    ("R09", 0, 2, 176, 20, None, "找了三分钟才发现天数还能改"),
    ("R10", 4, 4, 302, 26, None, "老年人根本用不了，字小步骤又多"),
    ("R11", 1, 3, 188, 22, None, "能不能把行程导出到日历里"),
    ("R12", 2, 5, 181, 19, None, "生成到一半就断掉了，什么都没有"),
]


def seed_items() -> int:
    """写入种子反馈。ext_id 唯一，重复调用幂等。"""
    created = db.now()
    rows = []
    for ext_id, ch_i, sev, users, freq, gt, text in _SEED:
        rows.append((ext_id, text, _CHANNELS[ch_i], sev, users, freq, gt, "new",
                     created, created))
    with db.tx() as conn:
        conn.executemany(
            """INSERT INTO fb_items
               (ext_id, text, channel, severity, users_affected, freq, gt_type,
                status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ext_id) DO UPDATE SET
                 text=excluded.text, channel=excluded.channel,
                 severity=excluded.severity, users_affected=excluded.users_affected,
                 freq=excluded.freq, gt_type=excluded.gt_type""",
            rows,
        )
        return len(rows)


def add_item(text: str, channel: str = "手动录入", severity: int = 3,
             users_affected: int = 0, freq: int = 1,
             gt_type: Optional[str] = None) -> int:
    """单条录入（API / CLI 用）。ext_id 用时间戳保证唯一。"""
    ext_id = f"MAN{int(time.time() * 1000)}"
    return db.execute(
        """INSERT INTO fb_items (ext_id, text, channel, severity, users_affected,
                                 freq, gt_type, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (ext_id, text, channel, severity, users_affected, freq, gt_type, "new",
         db.now(), db.now()))


# ===========================================================================
# 三、分类器
# ===========================================================================
# 模拟 LLM 分类器的整体准确率。**真实场景必须用真实标注集测出来，别照抄。**
MOCK_LLM_ACCURACY = 0.90

# 典型的语义混淆对：这两组在真实数据里最容易混
CONFUSION_MAP = {"technical": "demand", "demand": "technical",
                 "expression": "distribution", "distribution": "expression"}

CLASSIFY_PROMPT = """你是 AI 产品运营，负责给用户反馈做归因分类。

【反馈原文】
{text}

【分类标准（必须严格按"该谁来解决"来分，不要按"问题长什么样"分）】
- demand（需求问题）：产品压根没有这个能力，用户要的是新功能。解法归属：产品经理
- expression（表达问题）：功能存在，但用户不知道 / 找不到 / 看不懂。解法归属：运营+设计
- distribution（分发问题）：功能有且用户知道，但推给了错误的人群或场景。解法归属：运营+增长
- technical（技术问题）：功能对路，但输出本身是错的（幻觉、算错、格式坏、太慢）。解法归属：算法+研发

【判断要点】
- "某某功能能不能加" -> demand
- "我找不到 / 我不知道有" -> expression
- "这个不适合我这类人" -> distribution
- "它给的内容是错的 / 太慢 / 断了" -> technical

只输出一行 JSON：
{{"type":"technical","confidence":0.9,"reason":"一句话理由"}}
"""


class RuleClassifier:
    name = "rule-based"

    def classify(self, item: Dict) -> Tuple[str, float, str]:
        cat, conf = rule_classify(item["text"])
        return cat, conf, "关键词规则命中"


class MockLLMClassifier:
    """模拟 LLM：整体更准，但会在语义相近的类别上翻车（这就是要对比的原因）。"""

    name = "mock-llm-classifier"

    def classify(self, item: Dict) -> Tuple[str, float, str]:
        rng = random.Random(stable_seed(str(item.get("ext_id") or item.get("item_id"))))
        gt = item.get("gt_type")
        if not gt:
            return rule_classify(item["text"])[0], 0.6, "无标注，退化为规则判断"
        if rng.random() < MOCK_LLM_ACCURACY:
            return gt, round(rng.uniform(0.80, 0.97), 2), "语义判断命中"
        return (CONFUSION_MAP.get(gt, gt), round(rng.uniform(0.55, 0.71), 2),
                "语义相近类别混淆")


class RealLLMClassifier:
    def __init__(self) -> None:
        if not llm.is_real_mode():
            raise RuntimeError("real 模式需要在 .env 配置 PO_API_KEY / PO_API_BASE")
        self.name = Config.MODEL

    def classify(self, item: Dict) -> Tuple[str, float, str]:
        obj = llm.chat_json([{"role": "user",
                              "content": CLASSIFY_PROMPT.format(text=item["text"])}])
        if not obj.get("_ok"):
            return "expression", 0.3, f"调用失败：{obj.get('_error', '')[:60]}"
        cat = obj.get("type", "expression")
        if cat not in CATEGORIES:
            cat = "expression"
        try:
            conf = float(obj.get("confidence", 0.6))
        except (TypeError, ValueError):
            conf = 0.6
        return cat, round(conf, 2), str(obj.get("reason", ""))[:80]


def build_classifier(mode: Optional[str] = None):
    mode = mode or Config.MODE
    if mode == "real":
        try:
            return RealLLMClassifier(), False
        except RuntimeError:
            return MockLLMClassifier(), True
    return MockLLMClassifier(), False


def evaluate_classifier(preds: List[Tuple[str, str]]) -> Dict:
    """preds = [(gt, pred)] -> 准确率 + 混淆矩阵 + 每类召回。"""
    cats = CAT_ORDER
    total = len(preds)
    correct = sum(1 for gt, p in preds if gt == p)
    matrix = {gt: {p: 0 for p in cats} for gt in cats}
    for gt, p in preds:
        matrix.setdefault(gt, {c: 0 for c in cats})
        matrix[gt][p] = matrix[gt].get(p, 0) + 1

    per_cat = {}
    for c in cats:
        row = matrix.get(c, {})
        n = sum(row.values())
        per_cat[c] = {
            "label": CATEGORIES[c]["label"], "total": n,
            "recall": pct(row.get(c, 0), n),
            "missed_as": sorted([(k, v) for k, v in row.items() if k != c and v > 0],
                                key=lambda x: -x[1])[:2],
        }
    return {"total": total, "correct": correct,
            "accuracy": pct(correct, total), "matrix": matrix, "per_category": per_cat}


# ===========================================================================
# 四、跑一轮完整归因
# ===========================================================================
def run_classification(mode: Optional[str] = None, classifier=None,
                       progress=None) -> Dict:
    """
    对全部待归因反馈跑分类器，写入 pred_type / priority / band / owner，
    并把状态从 new 推到 triaged。

    **注意这里不做「自动派单」的越权动作**：只把状态推到「已归因」，
    派单仍需人工或明确的规则。原因：分类器准确率 90% 意味着每 10 条错 1 条，
    让 10% 的错误自动流向研发，是给研发制造噪音。
    """
    items = db.query("SELECT * FROM fb_items ORDER BY item_id")
    if not items:
        raise RuntimeError("没有反馈数据，请先执行 `productops feedback seed`")

    clf = classifier or (build_classifier(mode)[0])
    rule_clf = RuleClassifier()

    rule_preds, llm_preds, auto_dispatched = [], [], 0
    for i, it in enumerate(items, 1):
        llm_cat, llm_conf, llm_reason = clf.classify(it)
        rule_cat, rule_conf, _ = rule_clf.classify(it)

        if it.get("gt_type"):
            rule_preds.append((it["gt_type"], rule_cat))
            llm_preds.append((it["gt_type"], llm_cat))

        # 置信度够高才自动派责任人；不够高的留给人看
        auto = llm_conf >= 0.75
        auto_dispatched += 1 if auto else 0
        pr = priority_score(it["severity"], it["users_affected"], it["freq"])

        with db.tx() as conn:
            conn.execute(
                """UPDATE fb_items SET pred_type=?, pred_conf=?, pred_reason=?,
                       classifier=?, priority=?, band=?, owner=?,
                       status=CASE WHEN status='new' THEN 'triaged' ELSE status END,
                       updated_at=? WHERE item_id=?""",
                (llm_cat, llm_conf, llm_reason, clf.name, pr, priority_band(pr),
                 CATEGORIES[llm_cat]["owner"] if auto else None, db.now(), it["item_id"]))
            if it["status"] == "new":
                conn.execute(
                    """INSERT INTO fb_events (item_id, from_status, to_status, note, created_at)
                       VALUES (?,?,?,?,?)""",
                    (it["item_id"], "new", "triaged",
                     f"自动归因为{CATEGORIES[llm_cat]['label']}（置信度 {llm_conf}）", db.now()))
        if progress:
            progress(i, len(items), it["ext_id"])

    return {
        "items": len(items),
        "rule_eval": evaluate_classifier(rule_preds),
        "llm_eval": evaluate_classifier(llm_preds),
        "auto_dispatch_rate": pct(auto_dispatched, len(items)),
        "classifier": clf.name,
    }


# ===========================================================================
# 五、闭环状态机
# ===========================================================================
def advance(item_id: int, to_status: str, note: str = "") -> Dict:
    """
    推进一条反馈的状态。非法流转直接拒绝，并把允许的下一步告诉他。

    为什么要有状态机而不是一个文本字段：
        一个自由文本的 status 字段，三个月后必然会出现
        「已修复」「修好了」「fix」「done」四种写法同时存在，
        然后没有任何统计能算得出来。
    """
    row = db.query_one("SELECT * FROM fb_items WHERE item_id=?", (item_id,))
    if not row:
        raise KeyError(f"item_id={item_id} 不存在")
    cur = row["status"] or "new"
    if to_status not in STATES:
        raise ValueError(f"未知状态 {to_status}，可选：{STATES}")
    allowed = TRANSITIONS.get(cur, [])
    if to_status not in allowed:
        raise ValueError(
            f"非法流转 {cur} → {to_status}；当前允许：{allowed or '（终态，不可再流转）'}")

    with db.tx() as conn:
        conn.execute("UPDATE fb_items SET status=?, updated_at=? WHERE item_id=?",
                     (to_status, db.now(), item_id))
        conn.execute("""INSERT INTO fb_events (item_id, from_status, to_status, note, created_at)
                        VALUES (?,?,?,?,?)""", (item_id, cur, to_status, note, db.now()))
    return {"item_id": item_id, "from": cur, "to": to_status,
            "label": STATE_LABELS[to_status],
            "is_closed": to_status in CLOSED_STATES}


def history(item_id: int) -> List[Dict]:
    return db.query(
        """SELECT from_status, to_status, note, created_at FROM fb_events
           WHERE item_id=? ORDER BY event_id""", (item_id,))


def summary() -> Dict:
    """反馈模块的 KPI。闭环率是这里最该看的数字。"""
    items = db.query("SELECT * FROM fb_items")
    if not items:
        return {"has_data": False}

    total = len(items)
    by_status = {}
    for it in items:
        s = it["status"] or "new"
        by_status[s] = by_status.get(s, 0) + 1

    by_type = {}
    for it in items:
        t = it.get("pred_type") or "unclassified"
        by_type[t] = by_type.get(t, 0) + 1

    closed = sum(1 for it in items if (it["status"] or "") in CLOSED_STATES)
    promoted = sum(1 for it in items if it.get("promoted_sample"))

    merged = merge_similar(items)
    return {
        "has_data": True,
        "total": total,
        "by_status": by_status,
        "by_type": by_type,
        "closed_loop_rate": pct(closed, total),
        "closed_count": closed,
        "open_count": total - closed,
        "promoted_to_evalset": promoted,
        "top_groups": merged[:10],
        "group_count": len(merged),
        "dedup_saving": total - len(merged),
        "categories": {k: {"label": v["label"], "owner": v["owner"]} for k, v in CATEGORIES.items()},
        "states": STATES,
        "state_labels": STATE_LABELS,
        "transitions": TRANSITIONS,
    }


def list_items(cat: Optional[str] = None, status: Optional[str] = None,
               min_priority: float = 0.0, keyword: str = "",
               limit: int = 200) -> List[Dict]:
    sql = "SELECT * FROM fb_items WHERE 1=1"
    params: List = []
    if cat:
        sql += " AND pred_type=?"
        params.append(cat)
    if status:
        sql += " AND status=?"
        params.append(status)
    if min_priority:
        sql += " AND priority >= ?"
        params.append(min_priority)
    if keyword:
        sql += " AND text LIKE ?"
        params.append(f"%{keyword}%")
    sql += " ORDER BY priority DESC, item_id LIMIT ?"
    params.append(limit)
    rows = db.query(sql, params)
    for r in rows:
        r["label"] = CATEGORIES.get(r.get("pred_type") or "", {}).get("label", "未分类")
        r["status_label"] = STATE_LABELS.get(r.get("status") or "new", r.get("status"))
        r["allowed_next"] = TRANSITIONS.get(r.get("status") or "new", [])
    return rows


def get_item(item_id: int) -> Optional[Dict]:
    r = db.query_one("SELECT * FROM fb_items WHERE item_id=?", (item_id,))
    if not r:
        return None
    r["label"] = CATEGORIES.get(r.get("pred_type") or "", {}).get("label", "未分类")
    r["status_label"] = STATE_LABELS.get(r.get("status") or "new", r.get("status"))
    r["allowed_next"] = TRANSITIONS.get(r.get("status") or "new", [])
    r["history"] = history(item_id)
    return r


def weekly_report_text() -> str:
    """把 module 结论写成可粘贴进周报的纯文本。运营的产出最终要落到周报里。"""
    s = summary()
    if not s.get("has_data"):
        return "（暂无反馈数据）"
    ci = CATEGORIES
    lines = [
        f"本周共收到反馈 {s['total']} 条，去重后归为 {s['group_count']} 个问题。",
        f"闭环率 {s['closed_loop_rate'] * 100:.1f}%（{s['closed_count']}/{s['total']}），"
        f"其中 {s['promoted_to_evalset']} 条已提升为回归测试用例。",
        "",
        "归因分布：",
    ]
    for t, n in sorted(s["by_type"].items(), key=lambda kv: -kv[1]):
        lb = ci.get(t, {}).get("label", t)
        owner = ci.get(t, {}).get("owner", "-")
        lines.append(f"  {lb:<8} {n:>3} 条   -> 责任人：{owner}")
    lines.append("")
    lines.append("Top 5 待处理问题：")
    for i, g in enumerate(s["top_groups"][:5], 1):
        lb = ci.get(g["cat"], {}).get("label", g["cat"])
        lines.append(f"  {i}. [{g['band']} {g['priority']}] {g['text']}"
                     f"  （{lb}，影响 {g['users_affected']} 人，重复 {g['merged']} 次）")
    return "\n".join(lines)
