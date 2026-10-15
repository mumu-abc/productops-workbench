#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ProductOps Workbench —— AI 产品运营工作台

三个模块共用一个数据库、一套配置、一个服务：

    evaluation  上线前：这个版本能不能上？       -> 评测集 / 双轨打分 / 上线门禁
    feedback    上线后：用户到底在骂什么？       -> 四类归因 / 优先级 / 闭环状态机
    analytics   日常：  数据到底好不好？         -> 埋点 / 8 条运营 SQL / 看板
    bridge      ★ 联动：把「用户吐槽」变成「回归测试用例」

bridge 是这个产品区别于「三个独立脚本」的地方 —— 也是闭环真正闭合的地方。
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
