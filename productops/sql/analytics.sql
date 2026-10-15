-- ===========================================================================
-- 运营分析 SQL 模板集（10 条）
--
-- **这个文件是看板所有数字的唯一来源。**
-- 指标必须在 SQL 里算出来，不能在 Python 里拼 —— 原因很实在：
-- 把 SQL 单独放一个文件，
-- 数据团队可以直接复用、可以直接 review，不用从 Python 字符串里往外抠。
--
-- 命名规则：`-- === 名称 ===` 之后的内容归属该名称，直到下一个标记。
--
-- ---------------------------------------------------------------------------
-- 【口径备忘一】为什么漏斗必须限定「首日」
--
-- 第一版写的是「全周期去重漏斗」：统计整个观测窗口内出现过 input / generate
-- 的独立用户数。跑出来 open→input 是 91%，明显不合理 ——
-- 真实产品的新客转化不可能这么高。
--
-- 原因是：老用户回访时会重新走一遍流程，等于变相把分母补满了。
-- 用户基数越大、回访越多，这个漏斗就越「好看」，
-- 但它衡量的是「有多少人这辈子用过这个功能」，不是「新用户转化得好不好」。
--
-- 所以漏斗必须锁定 cohort：只统计用户首次使用当天的事件。
-- 这是运营分析里最经典的口径坑之一。
--
-- ---------------------------------------------------------------------------
-- 【口径备忘二】为什么留存每个指标要用自己的分母
--
-- 算 D7 留存时，分母里如果包含「刚注册 3 天」的用户，他们根本没有机会
-- 在第 7 天出现 —— 分母被撑大，留存被系统性低估。
-- 所以每个指标各自筛 `cohort_day <= date(last_day, '-N day')`。
-- ===========================================================================


-- === funnel ===
-- 新用户首日转化漏斗：打开 → 输入需求 → 生成成功 → 采纳
-- 口径：只统计用户首次使用当日的事件（见备忘一）
-- 用途：定位新用户在哪一步流失最多，这是所有运营分析的起点。
WITH first_seen AS (
    SELECT user_id, MIN(day) AS cohort_day
    FROM events WHERE event = 'open' GROUP BY user_id
)
SELECT
    COUNT(DISTINCT CASE WHEN e.event = 'open'     THEN e.user_id END) AS open_users,
    COUNT(DISTINCT CASE WHEN e.event = 'input'    THEN e.user_id END) AS input_users,
    COUNT(DISTINCT CASE WHEN e.event = 'generate' THEN e.user_id END) AS generate_users,
    COUNT(DISTINCT CASE WHEN e.event = 'adopt'    THEN e.user_id END) AS adopt_users,
    ROUND(COUNT(DISTINCT CASE WHEN e.event = 'input'    THEN e.user_id END) * 1.0
          / NULLIF(COUNT(DISTINCT CASE WHEN e.event = 'open'     THEN e.user_id END), 0), 4) AS open_to_input,
    ROUND(COUNT(DISTINCT CASE WHEN e.event = 'generate' THEN e.user_id END) * 1.0
          / NULLIF(COUNT(DISTINCT CASE WHEN e.event = 'input'    THEN e.user_id END), 0), 4) AS input_to_generate,
    ROUND(COUNT(DISTINCT CASE WHEN e.event = 'adopt'    THEN e.user_id END) * 1.0
          / NULLIF(COUNT(DISTINCT CASE WHEN e.event = 'generate' THEN e.user_id END), 0), 4) AS generate_to_adopt,
    ROUND(COUNT(DISTINCT CASE WHEN e.event = 'adopt'    THEN e.user_id END) * 1.0
          / NULLIF(COUNT(DISTINCT CASE WHEN e.event = 'open'     THEN e.user_id END), 0), 4) AS overall
FROM events e
JOIN first_seen f ON e.user_id = f.user_id AND e.day = f.cohort_day;


-- === funnel_by_channel ===
-- 分渠道首日漏斗。
-- 用途：整体漏斗只能告诉你「哪里漏了」，分渠道才能告诉你「是谁在漏」——
--      这是判断「该不该继续投这个渠道」的直接依据。
SELECT
    u.channel,
    COUNT(DISTINCT u.user_id) AS new_users,
    ROUND(COUNT(DISTINCT CASE WHEN e.event = 'input' THEN e.user_id END) * 1.0
          / NULLIF(COUNT(DISTINCT u.user_id), 0), 4) AS open_to_input,
    ROUND(COUNT(DISTINCT CASE WHEN e.event = 'generate' THEN e.user_id END) * 1.0
          / NULLIF(COUNT(DISTINCT u.user_id), 0), 4) AS open_to_generate,
    ROUND(COUNT(DISTINCT CASE WHEN e.event = 'adopt' THEN e.user_id END) * 1.0
          / NULLIF(COUNT(DISTINCT u.user_id), 0), 4) AS open_to_adopt
FROM users u
LEFT JOIN events e
       ON u.user_id = e.user_id
      AND e.day = u.first_day
      AND e.event IN ('input', 'generate', 'adopt')
GROUP BY u.channel
ORDER BY open_to_adopt DESC;


-- === dau ===
-- 日活趋势（按 open 事件去重）
-- 用途：看大盘走势，判断是自然波动还是真的在掉。
SELECT day, COUNT(DISTINCT user_id) AS dau
FROM events WHERE event = 'open'
GROUP BY day ORDER BY day;


-- === retention_cohort ===
-- 分日留存明细：按首次使用日期分组，看同一批用户在第 N 天回来的比例
-- 用途：判断「产品有没有真实价值」最硬的指标。总量涨但留存掉，
--      说明只是在买量，不是产品变好了。
-- 注意：靠后的 cohort 观测窗口不完整，下钻时要剔除，否则会把整体留存拉低。
WITH first_seen AS (
    SELECT user_id, MIN(day) AS cohort_day FROM events WHERE event = 'open' GROUP BY user_id
),
activity AS (
    SELECT f.cohort_day, e.user_id,
           CAST(julianday(e.day) - julianday(f.cohort_day) AS INTEGER) AS day_diff
    FROM events e JOIN first_seen f ON e.user_id = f.user_id
    WHERE e.event = 'open'
)
SELECT
    cohort_day,
    COUNT(DISTINCT CASE WHEN day_diff = 0  THEN user_id END) AS d0,
    COUNT(DISTINCT CASE WHEN day_diff = 1  THEN user_id END) AS d1,
    COUNT(DISTINCT CASE WHEN day_diff = 3  THEN user_id END) AS d3,
    COUNT(DISTINCT CASE WHEN day_diff = 7  THEN user_id END) AS d7,
    (SELECT MAX(day) FROM events) AS last_day
FROM activity GROUP BY cohort_day ORDER BY cohort_day;


-- === retention_summary ===
-- 整体留存率（所有 cohort 汇总），用于看板顶部 KPI
-- 关键：每个指标用**自己的**分母（见备忘二）
WITH first_seen AS (
    SELECT user_id, MIN(day) AS cohort_day FROM events WHERE event = 'open' GROUP BY user_id
),
bounds AS (SELECT MAX(day) AS last_day FROM events),
activity AS (
    SELECT e.user_id, f.cohort_day, b.last_day,
           CAST(julianday(e.day) - julianday(f.cohort_day) AS INTEGER) AS day_diff
    FROM events e
    JOIN first_seen f ON e.user_id = f.user_id
    CROSS JOIN bounds b
    WHERE e.event = 'open'
)
SELECT
    COUNT(DISTINCT CASE WHEN day_diff = 0 THEN user_id END) AS base_users,
    ROUND(COUNT(DISTINCT CASE WHEN day_diff = 1 THEN user_id END) * 1.0
        / NULLIF(COUNT(DISTINCT CASE WHEN cohort_day <= date(last_day, '-1 day') THEN user_id END), 0), 4) AS d1_rate,
    ROUND(COUNT(DISTINCT CASE WHEN day_diff = 3 THEN user_id END) * 1.0
        / NULLIF(COUNT(DISTINCT CASE WHEN cohort_day <= date(last_day, '-3 day') THEN user_id END), 0), 4) AS d3_rate,
    ROUND(COUNT(DISTINCT CASE WHEN day_diff = 7 THEN user_id END) * 1.0
        / NULLIF(COUNT(DISTINCT CASE WHEN cohort_day <= date(last_day, '-7 day') THEN user_id END), 0), 4) AS d7_rate
FROM activity;


-- === channel_performance ===
-- 分渠道表现：拉新量、首日激活率、人均生成次数
-- 口径：激活率限定首日（同 funnel 的理由）；人均生成次数看全周期（衡量活跃度）
SELECT
    u.channel,
    COUNT(DISTINCT u.user_id) AS new_users,
    COUNT(DISTINCT CASE WHEN e.event = 'generate' AND e.day = u.first_day
                        THEN e.user_id END) AS activated_users,
    ROUND(COUNT(DISTINCT CASE WHEN e.event = 'generate' AND e.day = u.first_day
                              THEN e.user_id END) * 1.0
          / NULLIF(COUNT(DISTINCT u.user_id), 0), 4) AS activation_rate,
    ROUND(COUNT(CASE WHEN e.event = 'generate' THEN 1 END) * 1.0
          / NULLIF(COUNT(DISTINCT u.user_id), 0), 2) AS generate_per_user
FROM users u
LEFT JOIN events e ON u.user_id = e.user_id
GROUP BY u.channel ORDER BY new_users DESC;


-- === stability ===
-- 生成成功率趋势
-- 用途：AI 产品的稳定性直接决定体验，失败率是必须天天盯的指标。
SELECT
    day,
    SUM(CASE WHEN event = 'generate'      THEN 1 ELSE 0 END) AS success,
    SUM(CASE WHEN event = 'generate_fail' THEN 1 ELSE 0 END) AS failed,
    ROUND(SUM(CASE WHEN event = 'generate' THEN 1 ELSE 0 END) * 1.0
          / NULLIF(SUM(CASE WHEN event IN ('generate','generate_fail') THEN 1 ELSE 0 END), 0), 4)
          AS success_rate
FROM events WHERE event IN ('generate','generate_fail')
GROUP BY day ORDER BY day;


-- === cost_daily ===
-- 每日 token 消耗与单次生成成本
-- 用途：AI 产品特有指标。体验和成本天然冲突，必须量化着看。
SELECT
    day,
    SUM(tokens) AS total_tokens,
    COUNT(CASE WHEN event = 'generate' THEN 1 END) AS generate_calls,
    ROUND(AVG(CASE WHEN event = 'generate' THEN tokens END), 0) AS avg_tokens_per_call
FROM events WHERE event = 'generate'
GROUP BY day ORDER BY day;


-- === weekday_pattern ===
-- 按星期几看拉新与激活
-- 用途：投放在哪天加码、运维在什么时候值班，靠这个排期。
--       （这个查询是原来的 demo 没有的）
SELECT
    CASE CAST(strftime('%w', u.first_day) AS INTEGER)
        WHEN 0 THEN '周日' WHEN 1 THEN '周一' WHEN 2 THEN '周二' WHEN 3 THEN '周三'
        WHEN 4 THEN '周四' WHEN 5 THEN '周五' ELSE '周六' END AS weekday,
    COUNT(DISTINCT u.user_id) AS new_users,
    COUNT(DISTINCT CASE WHEN e.event = 'generate' AND e.day = u.first_day
                        THEN e.user_id END) AS activated,
    ROUND(COUNT(DISTINCT CASE WHEN e.event = 'generate' AND e.day = u.first_day
                              THEN e.user_id END) * 1.0
          / NULLIF(COUNT(DISTINCT u.user_id), 0), 4) AS activation_rate
FROM users u
LEFT JOIN events e ON u.user_id = e.user_id
GROUP BY strftime('%w', u.first_day)
ORDER BY CAST(strftime('%w', u.first_day) AS INTEGER);


-- === platform_split ===
-- 分平台表现
-- 用途：判断要不要在某个平台上单独优化（性能、交互差异）。
SELECT
    platform,
    COUNT(DISTINCT user_id) AS users,
    COUNT(CASE WHEN event = 'generate' THEN 1 END) AS generates,
    ROUND(AVG(CASE WHEN event = 'generate' THEN duration_ms END), 0) AS avg_generate_ms
FROM events GROUP BY platform ORDER BY users DESC;
