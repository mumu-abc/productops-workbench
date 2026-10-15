/* ===========================================================================
   ProductOps Workbench · 前端应用
   零依赖：不引 Vue / React / ECharts，图表全部手写 SVG + CSS。
   理由见 README —— 演示环境可能没有网络，CDN 一挂整个看板就是白屏。
   =========================================================================== */
'use strict';

/* ------------------------------------------------------------------ 工具 */
const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

const pct = (v, d = 1) => (v == null || isNaN(v)) ? '—' : (v * 100).toFixed(d) + '%';
const num = (v, d = 2) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(d);
const int = (v) => (v == null || isNaN(v)) ? '—' : Number(v).toLocaleString('zh-CN');
const cut = (s, n = 90) => { s = String(s || ''); return s.length > n ? s.slice(0, n) + '…' : s; };

function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show ' + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = 'toast ' + kind; }, 3200);
}
/* 离线快照注入的脚本要复用同一套提示样式，所以把 toast 暴露出去 */
window.__PO_TOAST__ = toast;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data;
  try { data = await res.json(); }
  catch (e) { throw new Error(`接口返回非 JSON（HTTP ${res.status}）`); }
  if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* ------------------------------------------------------------- 图表（SVG） */

/** 折线 / 面积图。用于 DAU、成功率这类时间序列。 */
function lineChart(rows, xKey, yKey, opt = {}) {
  const W = opt.w || 760, H = opt.h || 190, P = { t: 16, r: 16, b: 26, l: 46 };
  const data = (rows || []).filter(r => r[yKey] != null);
  if (!data.length) return '<div class="empty">暂无数据</div>';

  const vals = data.map(r => Number(r[yKey]));
  const max = Math.max(...vals), min = Math.min(...vals);
  const pad = (max - min) * 0.15 || 1;
  const hi = max + pad, lo = Math.max(0, min - pad);
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  const x = i => P.l + (data.length === 1 ? iw / 2 : (i / (data.length - 1)) * iw);
  const y = v => P.t + ih - ((v - lo) / (hi - lo || 1)) * ih;

  const pts = data.map((r, i) => [x(i), y(Number(r[yKey]))]);
  const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const area = line + ` L ${pts[pts.length - 1][0].toFixed(1)} ${P.t + ih} L ${pts[0][0].toFixed(1)} ${P.t + ih} Z`;

  // 4 条水平网格线
  let grid = '';
  for (let g = 0; g <= 4; g++) {
    const gy = P.t + (ih / 4) * g, gv = hi - ((hi - lo) / 4) * g;
    grid += `<line x1="${P.l}" y1="${gy.toFixed(1)}" x2="${W - P.r}" y2="${gy.toFixed(1)}"
             stroke="#eef1f6" stroke-width="1"/>
             <text x="${P.l - 7}" y="${(gy + 4).toFixed(1)}" text-anchor="end"
             font-size="10" fill="#8792a2">${opt.fmtY ? opt.fmtY(gv) : Math.round(gv)}</text>`;
  }
  // x 轴标签抽稀，避免拥挤
  const step = Math.max(1, Math.ceil(data.length / 8));
  let xlab = '';
  data.forEach((r, i) => {
    if (i % step && i !== data.length - 1) return;
    xlab += `<text x="${x(i).toFixed(1)}" y="${H - 7}" text-anchor="middle"
             font-size="10" fill="#8792a2">${esc(String(r[xKey]).slice(5))}</text>`;
  });

  const color = opt.color || '#4f46e5';
  const gid = 'g' + Math.random().toString(36).slice(2, 8);
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block" preserveAspectRatio="none">
    <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity=".26"/>
      <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs>
    ${grid}
    <path d="${area}" fill="url(#${gid})"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>
    ${xlab}
  </svg>`;
}

/** 通用条形组（HTML，不是 SVG —— 中文标签用 HTML 更好排版）。 */
function barRows(items, opt = {}) {
  if (!items || !items.length) return '<div class="empty">暂无数据</div>';
  const maxV = Math.max(...items.map(i => Number(i.value) || 0)) || 1;
  return items.map(i => {
    const w = Math.max(1, (Number(i.value) || 0) / maxV * 100);
    return `<div class="bar-row">
      <div class="name" title="${esc(i.name)}">${esc(i.name)}</div>
      <div class="bar-track"><div class="bar-fill ${i.tone || ''}" style="width:${w}%"></div></div>
      <div class="num">${esc(i.label != null ? i.label : i.value)}</div>
    </div>`;
  }).join('');
}

function kpi(label, value, unit, foot, tone) {
  return `<div class="card kpi ${tone || ''}">
    <div class="label">${esc(label)}</div>
    <div class="val">${value}${unit ? `<span class="unit">${esc(unit)}</span>` : ''}</div>
    <div class="foot">${foot || ''}</div>
  </div>`;
}

function table(headers, rows, opt = {}) {
  if (!rows || !rows.length) return '<div class="empty">暂无数据</div>';
  const th = headers.map(h => `<th${h.num ? ' class="num"' : ''}>${esc(h.t)}</th>`).join('');
  const tb = rows.map(r => '<tr>' + r.map((c, i) =>
    `<td${headers[i] && headers[i].num ? ' class="num"' : ''}>${c}</td>`).join('') + '</tr>').join('');
  return `<table><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table>`;
}

const toneOf = (v) => v === 'ok' ? 'ok' : v === 'warn' ? 'warn' : v === 'bad' ? 'bad' : 'plain';
const verdictTone = (v) => v === '允许上线' ? 'ok' : v === '允许灰度' ? 'warn' : 'bad';

/* ------------------------------------------------------------------ 路由 */
const VIEWS = {
  overview:   { title: '总览',      sub: '三个模块 + 一条桥：评测 / 反馈 / 数据' },
  evaluation: { title: '效果评测',  sub: '这个版本能不能上 —— 评测集、双轨打分、上线门禁' },
  feedback:   { title: '反馈闭环',  sub: '用户到底在骂什么、该谁修、修完了吗' },
  analytics:  { title: '数据看板',  sub: '指标全部由 SQL 算出，SQL 与代码分离' },
  bridge:     { title: 'Bridge 联动', sub: '把用户吐槽变成会一直跑下去的回归测试用例' },
};

function currentView() {
  const h = (location.hash || '#/overview').replace('#/', '');
  return VIEWS[h] ? h : 'overview';
}

async function render() {
  const v = currentView();
  $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.view === v));
  $('#pageTitle').textContent = VIEWS[v].title;
  $('#pageSub').textContent = VIEWS[v].sub;
  $('#view').innerHTML = '<div class="loading"><span class="spin"></span>加载中…</div>';
  $('#topActions').innerHTML = '';
  try {
    await ({ overview: viewOverview, evaluation: viewEvaluation, feedback: viewFeedback,
             analytics: viewAnalytics, bridge: viewBridge })[v]();
  } catch (e) {
    $('#view').innerHTML = `<div class="note bad"><b>加载失败：</b>${esc(e.message)}
      <div class="mt8 muted">如果是空库，先在命令行跑：<code>python run.py init</code></div></div>`;
  }
}

window.addEventListener('hashchange', render);

/* ================================================================== 总览 */
async function viewOverview() {
  const d = await api('/api/overview');
  const ev = d.evaluation || {}, fb = d.feedback || {}, an = d.analytics || {}, br = d.bridge || {};
  const c = d.counts || {};

  $('#topActions').innerHTML = `
    <button class="btn" onclick="actClassify()">重跑归因</button>
    <button class="btn pri" onclick="actRunEval(0)">跑一次全量评测</button>`;

  const hl = (d.highlights || []).map(h => `
    <div class="note ${toneOf(h.tone) === 'plain' ? '' : toneOf(h.tone)}">
      <div class="between"><b>${esc(h.module)}</b>
        <span class="badge ${toneOf(h.tone)}">${esc(h.headline)}</span></div>
      <div class="muted mt8">${esc(h.detail)}</div>
    </div>`).join('');

  const statusFlow = fb.state_labels ? Object.keys(fb.state_labels)
    .filter(k => (fb.by_status || {})[k])
    .map(k => `<span class="badge ${k === 'closed' || k === 'verified' ? 'ok' : 'plain'}">
        ${esc(fb.state_labels[k])} ${fb.by_status[k]}</span>`).join('') : '';

  $('#view').innerHTML = `
    <div class="grid g4">
      ${kpi('评测样本', int(c.eval_samples), '条',
            `历史运行 ${int(c.eval_runs)} 次 · 明细 ${int(c.eval_results)} 行`)}
      ${kpi('门禁结论', ev.has_run ? esc(ev.gate_verdict) : '未跑',
            '', ev.has_run ? `总分 ${num(ev.total_avg)} ± ${num(ev.total_std)} · 通过率 ${pct(ev.pass_rate)}` : '还没有评测记录',
            ev.has_run ? verdictTone(ev.gate_verdict) : '')}
      ${kpi('反馈闭环率', pct(fb.closed_loop_rate), '',
            fb.has_data ? `${fb.closed_count}/${fb.total} 条闭环 · 归为 ${fb.group_count} 个问题` : '没有反馈数据',
            fb.has_data ? (fb.closed_loop_rate >= 0.5 ? 'ok' : 'warn') : '')}
      ${kpi('吐槽→回归用例', int(br.promoted), '条',
            br.has_data ? `覆盖率 ${pct(br.promote_rate)} · 复测通过 ${br.passed || br.verified || 0} 条` : '暂无技术类反馈',
            'accent')}
    </div>

    <div class="sec-title">一句话现状</div>
    <div class="grid g2">${hl || '<div class="empty">库里还没有数据，先执行 <code>python run.py init</code></div>'}</div>

    <div class="grid g2 mt16">
      <div class="card">
        <div class="card-head"><h2>评测：分层通过率</h2>
          <span class="hint">均分最容易骗人，所以要分层看</span></div>
        <div class="card-body">
          ${ev.has_run && ev.by_difficulty ? barRows(
            Object.entries(ev.by_difficulty).map(([k, v]) => ({
              name: { easy: '简单 easy', medium: '中等 medium', hard: '困难 hard' }[k] || k,
              value: v.pass_rate, label: pct(v.pass_rate),
              tone: v.pass_rate >= 0.7 ? 'ok' : v.pass_rate >= 0.4 ? 'warn' : 'bad',
            }))) : '<div class="empty">还没有评测记录</div>'}
          ${ev.has_run && ev.safety_gap && ev.safety_gap.boundary_total ? `
            <div class="note bad mt12">
              <b>合规缺口：</b>${ev.safety_gap.boundary_failed}/${ev.safety_gap.boundary_total}
              条边界样本没有正确拒绝（缺口率 ${pct(ev.safety_gap.gap_rate)}）。
              这是安全性一票否决制的直接输入。
            </div>` : ''}
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h2>反馈：归因分布</h2>
          <span class="hint">按「该谁解决」分，不是按「问题长什么样」分</span></div>
        <div class="card-body">
          ${fb.has_data ? barRows(Object.entries(fb.by_type || {}).map(([k, n]) => {
            const cat = (fb.categories || {})[k] || {};
            return { name: cat.label || k, value: n, label: n + ' 条',
                     tone: k === 'technical' ? 'bad' : k === 'expression' ? 'warn' : '' };
          })) : '<div class="empty">没有反馈数据</div>'}
          ${statusFlow ? `<div class="hr"></div><div class="row">${statusFlow}</div>` : ''}
        </div>
      </div>
    </div>

    ${an.has_data ? `
    <div class="card mt16">
      <div class="card-head"><h2>数据：结论层</h2>
        <span class="hint">数字 -> 归因方向，这一步是运营和数据的分界线</span></div>
      <div class="card-body">
        <div class="note">${esc(an.diagnosis && an.diagnosis.conclusion || '—')}</div>
        <div class="grid g4 mt16">
          ${kpi('首日整体转化', pct(an.overall_conversion), '',
                `最新 DAU ${int(an.dau_latest)}`)}
          ${kpi('D1 / D3 / D7', `${pct(an.retention && an.retention.d1)} <span class="unit">/</span>
                ${pct(an.retention && an.retention.d3)} <span class="unit">/</span>
                ${pct(an.retention && an.retention.d7)}`, '', '分母已按成熟度校正')}
          ${kpi('近 7 日生成成功率', pct(an.success_rate_7d), '', 'AI 产品的稳定性')}
          ${kpi('最差渠道', esc(an.worst_channel || '—'), '',
                '激活率最低，是最该停投或改造的')}
        </div>
      </div>
    </div>` : ''}

    ${br.has_data ? `
    <div class="card mt16">
      <div class="card-head"><h2>Bridge：闭环进度</h2>
        <span class="hint">这是三个模块被做成一个产品的理由</span></div>
      <div class="card-body">
        <div class="grid g4">
          ${kpi('技术类反馈', int(br.technical_total), '条', '这些才需要常驻回归用例')}
          ${kpi('已提升为用例', int(br.promoted), '条', `覆盖率 ${pct(br.promote_rate)}`, 'accent')}
          ${kpi('复测通过', int(br.verified), '条', '研发说修好了不算，跑过了才算', 'ok')}
          ${kpi('复测未通过', int(br.failed), '条', '问题仍然存在，不能关单', br.failed ? 'bad' : '')}
        </div>
        <div class="note mt16">
          原来的三个工具走到「派单」就断了 —— <b>没人知道到底修好没有</b>。
          Bridge 把反馈变成评测集里的常驻用例，于是每次发版都会替你验一遍。
        </div>
      </div>
    </div>` : ''}
  `;
}

/* ============================================================== 效果评测 */
async function viewEvaluation() {
  const [runs, rub] = await Promise.all([
    api('/api/eval/runs'), api('/api/eval/rubric')]);

  $('#topActions').innerHTML = `
    <select id="evLimit" class="btn" style="padding:6px 8px">
      <option value="0">全量 100 条</option>
      <option value="30">抽样 30 条（快速）</option>
      <option value="60">抽样 60 条</option>
    </select>
    <button class="btn pri" id="btnRun">跑评测</button>`;

  let detail = null, compare = null;
  if (runs.runs.length) {
    detail = await api(`/api/eval/run/${runs.runs[0].run_id}`);
    if (runs.runs.length > 1) {
      try { compare = await api(`/api/eval/compare?new=${runs.runs[0].run_id}`); }
      catch (e) { compare = null; }
    }
  }

  const W = rub.rubric;
  const dims = Object.entries(W.dimensions).map(([k, d]) => ({
    key: k, label: d.label, weight: d.weight, anchors: d.anchors,
  }));

  const rubricHtml = `
    <div class="card">
      <div class="card-head"><h2>评分 Rubric</h2>
        <span class="hint">${esc(W.scale)} · ${esc(W.weights_note)}</span></div>
      <div class="card-body">
        ${barRows(dims.map(d => ({ name: d.label, value: d.weight,
          label: pct(d.weight, 0), tone: 'accent' })))}
        <div class="hr"></div>
        <div class="note">融合权重：规则 <b>${pct(W.fusion.rule, 0)}</b> +
          LLM <b>${pct(W.fusion.llm, 0)}</b>。${esc(W.fusion.note)}</div>
        <div class="mrow mt12" style="font-size:11.5px;color:var(--ink-3)">
          ${dims.map(d => `<div class="mt8"><b style="color:var(--ink-2)">${esc(d.label)}</b>
            <span class="badge ${d.key === 'safety' ? 'bad' : 'plain'}">
              ${d.key === 'safety' ? '一票否决' : '影响灰度'}</span>
            <div class="mt8">${Object.entries(d.anchors).map(([s, t]) =>
              `<div><span class="badge plain">${s} 分</span> ${esc(t)}</div>`).join('')}</div>
          </div>`).join('')}
        </div>
      </div>
    </div>`;

  if (!detail) {
    $('#view').innerHTML = `<div class="grid g2">${rubricHtml}
      <div class="card"><div class="empty"><div class="big">✓</div>
      还没有评测记录。点右上角「跑评测」跑第一次 ——
      默认 mock 模式，不联网、不需要 Key，结果可复现。</div></div></div>`;
    bindEval();
    return;
  }

  const m = detail.metrics, o = m.overall, g = m.gate || {};
  const dimRows = dims.map(d => {
    const s = o.dims[d.key];
    return { name: d.label, value: s.avg, label: `${num(s.avg)} ± ${num(s.std)}`,
             tone: s.avg >= 4 ? 'ok' : s.avg >= 3.4 ? 'warn' : 'bad' };
  });

  $('#view').innerHTML = `
    <div class="grid g4">
      ${kpi('加权总分', num(o.total_avg), '/ 5',
            `标准差 ${num(o.total_std)} · 最低 ${num(o.total_min)} · 最高 ${num(o.total_max)}`,
            o.total_avg >= 4 ? 'ok' : o.total_avg >= 3.4 ? 'warn' : 'bad')}
      ${kpi('通过率', pct(o.pass_rate), '',
            `${o.pass_count} / ${o.count} 条达到 ${W.dimensions ? '' : ''}${detail.run.sample_count || o.count} 分阈值`)}
      ${kpi('上线门禁', esc(detail.run.gate_verdict), '',
            g.checks ? g.checks.filter(c => !c.ok).map(c => c.name).join('、') || '全部达标' : '',
            verdictTone(detail.run.gate_verdict))}
      ${kpi('规则 vs LLM', `${num(o.rule_avg)} <span class="unit">/</span> ${num(o.llm_avg)}`,
            '', '差距大说明规则覆盖窄或裁判有偏见')}
    </div>

    <div class="grid g2 mt16">
      <div class="card">
        <div class="card-head"><h2>上线门禁</h2>
          <span class="hint">安全性不达标一票否决，不给灰度机会</span></div>
        <div class="card-body">
          ${(g.checks || []).map(c => `
            <div class="between" style="padding:8px 0;border-bottom:1px solid var(--line-2)">
              <div>
                <div><b>${esc(c.name)}</b>
                  <span class="badge ${c.ok ? 'ok' : 'bad'}">${c.ok ? '达标' : '未达标'}</span>
                  ${c.veto ? '<span class="badge bad">一票否决</span>' : ''}</div>
                <div class="muted" style="font-size:11.5px">未达标动作：${esc(c.on_fail)}</div>
              </div>
              <div class="num" style="font-weight:700">
                ${c.name.includes('通过率') ? pct(c.value) : num(c.value)}
                <span class="muted" style="font-weight:400">${esc(c.op)} ${c.name.includes('通过率') ? pct(c.threshold) : num(c.threshold)}</span>
              </div>
            </div>`).join('')}
          <div class="note ${g.blocked ? 'bad' : g.passed ? 'ok' : 'warn'} mt12">
            判定：<b>${esc(detail.run.gate_verdict)}</b>。
            ${g.blocked ? '安全性未达标，按规则不允许上线，也不允许灰度。'
              : g.passed ? '全部指标达标，可以上。' : '存在未达标项，只能灰度 5% 流量并持续观察。'}
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h2>四维得分</h2>
          <span class="hint">均值 ± 标准差</span></div>
        <div class="card-body">
          ${barRows(dimRows)}
          ${m.safety_gap && m.safety_gap.boundary_total ? `
            <div class="note bad mt12">
              <b>合规缺口：</b>${m.safety_gap.boundary_failed}/${m.safety_gap.boundary_total}
              条边界样本未做拒绝声明（${pct(m.safety_gap.gap_rate)}）。
              这类问题是「越界请求被配合回答」，比答得不准严重得多。
            </div>` : ''}
          <div class="hr"></div>
          <div class="row">
            <span class="badge plain">平均延迟 ${num(o.avg_latency)}s</span>
            <span class="badge plain">P95 ${num(o.p95_latency)}s</span>
            <span class="badge plain">总 tokens ${int(o.total_tokens)}</span>
            <span class="badge plain">${esc(detail.run.mode)} 模式</span>
          </div>
        </div>
      </div>
    </div>

    <div class="grid g2 mt16">
      <div class="card">
        <div class="card-head"><h2>分层通过率</h2><span class="hint">整体会掩盖难例</span></div>
        <div class="card-body">
          ${barRows(Object.entries(m.by_difficulty || {}).map(([k, v]) => ({
            name: { easy: '简单', medium: '中等', hard: '困难' }[k] || k,
            value: v.pass_rate, label: `${pct(v.pass_rate)} (${v.count}条)`,
            tone: v.pass_rate >= 0.7 ? 'ok' : v.pass_rate >= 0.4 ? 'warn' : 'bad',
          })))}
          <div class="hr"></div>
          <div class="note">简单与困难层的通过率差距，是「这个模型到底能不能用」最直接的证据。
            只看整体均分，会被简单题堆出来的好看数字骗过去。</div>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h2>分类别表现</h2><span class="hint">从低到高，找最弱的场景</span></div>
        <div class="card-body">
          ${barRows(Object.entries(m.by_category || {}).map(([k, v]) => ({
            name: k, value: v.total_avg, label: `${num(v.total_avg)} (${pct(v.pass_rate)})`,
            tone: v.total_avg >= 4 ? 'ok' : v.total_avg >= 3.4 ? 'warn' : 'bad',
          })), {})}
        </div>
      </div>
    </div>

    ${compare ? `
    <div class="card mt16">
      <div class="card-head"><h2>版本对比 #${compare.base_run} → #${compare.new_run}</h2>
        <span class="hint">评测体系真正的价值：找退化样本，不是看总分涨没涨</span></div>
      <div class="card-body">
        <div class="row">
          <span class="badge ${compare.avg_delta >= 0 ? 'ok' : 'bad'}">
            均分 ${num(compare.base_avg)} → ${num(compare.new_avg)}（${compare.avg_delta >= 0 ? '+' : ''}${num(compare.avg_delta)}）</span>
          <span class="badge bad">退化 ${compare.regression_count} 条</span>
          <span class="badge ok">改善 ${compare.improvement_count} 条</span>
        </div>
        <div class="note warn mt12">${esc(compare.verdict)}</div>
        ${compare.regressions.length ? `
          <div class="sec-title">退化样本</div>
          ${table([{ t: '样本' }, { t: '问题' }, { t: '旧' , num: true}, { t: '新', num: true}, { t: 'Δ', num: true}],
            compare.regressions.map(r => [
              `<code>${esc(r.sample_id)}</code>`, esc(cut(r.question, 46)),
              num(r.old), num(r.new),
              `<span style="color:var(--bad);font-weight:700">${num(r.delta)}</span>`]))}
        ` : '<div class="note ok mt12">没有退化样本。</div>'}
      </div>
    </div>` : ''}

    <div class="card mt16">
      <div class="card-head"><h2>Badcase 清单</h2>
        <span class="hint">按分数从低到高，最多 20 条</span></div>
      <div class="card-body tight scroll">
        ${table([{ t: '样本' }, { t: '难度' }, { t: '问题' }, { t: '回答（截断）' },
                 { t: '总分', num: true }, { t: '准确', num: true }, { t: '完整', num: true },
                 { t: '安全', num: true }],
          (m.failures || []).map(r => [
            `<code>${esc(r.id)}</code>`,
            `<span class="badge ${r.difficulty === 'hard' ? 'bad' : r.difficulty === 'medium' ? 'warn' : 'plain'}">${esc(r.difficulty)}</span>`,
            esc(cut(r.question, 40)),
            `<span class="muted">${esc(cut(r.answer, 52))}</span>`,
            `<b>${num(r.total)}</b>`, num(r.scores.accuracy),
            num(r.scores.completeness), num(r.scores.safety)]))}
      </div>
    </div>

    <div class="grid g2 mt16">
      ${rubricHtml}
      <div class="card">
        <div class="card-head"><h2>历史运行</h2>
          <span class="hint">留存历史是为了能做版本对比</span></div>
        <div class="card-body tight scroll">
          ${table([{ t: '#' }, { t: '时间' }, { t: '模型' }, { t: '样本', num: true },
                   { t: '总分', num: true }, { t: 'σ', num: true },
                   { t: '通过率', num: true }, { t: '门禁' }],
            runs.runs.map(r => [
              `<code>${r.run_id}</code>`, esc(r.created_at), esc(r.model),
              int(r.sample_count), num(r.total_avg), num(r.total_std), pct(r.pass_rate),
              `<span class="badge ${verdictTone(r.gate_verdict)}">${esc(r.gate_verdict || '—')}</span>`]))}
        </div>
      </div>
    </div>

    ${Object.keys(m.by_category || {}).length ? `
    <div class="card mt16">
      <div class="card-head"><h2>样本明细（Top 40）</h2>
        <span class="hint">分数从低到高</span></div>
      <div class="card-body tight scroll">
        ${table([{ t: '样本' }, { t: '类别' }, { t: '难度' }, { t: '总分', num: true },
                 { t: '通过' }],
          detail.results.slice(0, 40).map(r => [
            `<code>${esc(r.sample_id)}</code>`, esc(r.category), esc(r.difficulty),
            num(r.total),
            r.passed ? '<span class="badge ok">通过</span>' : '<span class="badge bad">未过</span>']),
          {})}
      </div>
    </div>` : ''}
  `;
  bindEval();
}

function bindEval() {
  const b = $('#btnRun');
  if (!b) return;
  b.onclick = async () => {
    const limit = Number($('#evLimit').value) || 0;
    b.disabled = true;
    b.innerHTML = '<span class="spin"></span>跑评测中…';
    try {
      const r = await api('/api/eval/run', { method: 'POST', body: { limit, note: '（看板发起）' } });
      toast(`评测完成：#${r.run_id}，门禁「${r.gate_verdict}」`, 'ok');
      render();
    } catch (e) {
      toast('评测失败：' + e.message, 'err');
      b.disabled = false; b.textContent = '跑评测';
    }
  };
}

async function actRunEval(limit) {
  toast('开始跑评测…');
  try {
    const r = await api('/api/eval/run', { method: 'POST', body: { limit, note: '（总览发起）' } });
    toast(`评测完成：#${r.run_id}，门禁「${r.gate_verdict}」`, 'ok');
    render();
  } catch (e) { toast('失败：' + e.message, 'err'); }
}

async function actClassify() {
  toast('重新归因中…');
  try {
    const r = await api('/api/feedback/classify', { method: 'POST' });
    toast(`归因完成：规则 ${pct(r.rule_eval.accuracy)} vs LLM ${pct(r.llm_eval.accuracy)}`, 'ok');
    render();
  } catch (e) { toast('失败：' + e.message, 'err'); }
}

/* ============================================================== 反馈闭环 */
let fbFilter = { type: '', status: '', q: '' };

async function viewFeedback() {
  const [sum, items] = await Promise.all([
    api('/api/feedback/summary'),
    api(`/api/feedback/items?type=${encodeURIComponent(fbFilter.type)}` +
        `&status=${encodeURIComponent(fbFilter.status)}` +
        `&q=${encodeURIComponent(fbFilter.q)}`)]);

  $('#topActions').innerHTML = `
    <button class="btn" onclick="actClassify()">重跑归因</button>
    <button class="btn pri" onclick="actAutoPromote()">一键提升 P0 为回归用例</button>`;

  if (!sum.has_data) {
    $('#view').innerHTML = `<div class="card"><div class="empty"><div class="big">⟳</div>
      还没有反馈数据。命令行执行 <code>python run.py feedback seed</code> 灌入示例反馈。</div></div>`;
    return;
  }

  const cats = Object.keys(sum.categories || {});
  const T = sum.state_labels || {};

  const flow = (sum.states || []).filter(s => s !== 'wontfix').map((s, i, arr) => {
    const n = (sum.by_status || {})[s] || 0;
    const cls = s === 'closed' ? 'done' : n ? 'on' : '';
    return `<span class="st ${cls}">${esc(T[s] || s)}${n ? ' ' + n : ''}</span>` +
           (i < arr.length - 1 ? '<span class="arw">→</span>' : '');
  }).join('');
  const wontfix = (sum.by_status || {}).wontfix || 0;

  const typeRows = Object.entries(sum.by_type || {}).map(([k, n]) => {
    const c = (sum.categories || {})[k] || {};
    return { name: `${c.label || k} · ${c.owner || ''}`, value: n, label: n + ' 条',
             tone: k === 'technical' ? 'bad' : k === 'expression' ? 'warn' : '' };
  });

  const statusRows = Object.entries(sum.by_status || {}).map(([k, n]) => ({
    name: T[k] || k, value: n, label: n + ' 条',
    tone: (k === 'verified' || k === 'closed') ? 'ok' : k === 'wontfix' ? '' : 'warn',
  }));

  const topHtml = (sum.top_groups || []).map(g => {
    const c = (sum.categories || {})[g.cat] || {};
    return `<div class="item">
      <div class="meta">
        <span class="badge ${g.priority >= 75 ? 'bad' : 'warn'}">${esc(g.band)} ${num(g.priority, 1)}</span>
        <span class="badge plain">${esc(c.label || '')}</span>
        <span class="badge plain">→ ${esc(c.owner || '')}</span>
        <span class="badge plain">影响 ${int(g.users_affected)} 人</span>
        <span class="badge plain">重复上报 ${g.merged} 次</span>
      </div>
      <div class="txt">${esc(g.text)}</div>
    </div>`;
  }).join('');

  const itemHtml = (items.items || []).map(it => {
    const next = (it.allowed_next || []).map(s =>
      `<button class="btn sm" onclick="actAdvance(${it.item_id},'${s}')">→ ${esc(T[s] || s)}</button>`
    ).join('');
    const canPromote = it.pred_type === 'technical' && !it.promoted_sample;
    return `<div class="item">
      <div class="meta">
        <span class="badge ${it.priority >= 75 ? 'bad' : it.priority >= 60 ? 'warn' : 'plain'}">
          ${esc(it.band || '—')} ${num(it.priority, 1)}</span>
        <span class="badge accent">${esc(it.label || '未分类')}</span>
        <span class="badge plain">${esc(it.status_label || it.status)}</span>
        <span class="badge plain">${esc(it.channel || '')}</span>
        <span class="muted" style="font-size:11px">#${it.item_id} · 置信 ${num(it.pred_conf, 2)}</span>
        ${it.promoted_sample ? `<span class="badge ok">已是用例 ${esc(it.promoted_sample)}</span>` : ''}
      </div>
      <div class="txt">${esc(it.text)}</div>
      <div class="acts">${next}
        ${canPromote ? `<button class="btn sm pri" onclick="actPromote(${it.item_id})">提升为回归用例</button>` : ''}
        ${it.pred_type === 'technical' && it.promoted_sample ?
          `<button class="btn sm" onclick="actVerify(${it.item_id})">查复测结果</button>` : ''}
      </div>
    </div>`;
  }).join('') || '<div class="empty">没有符合条件的反馈</div>';

  $('#view').innerHTML = `
    <div class="grid g4">
      ${kpi('反馈总数', int(sum.total), '条',
            `去重后归为 <b>${sum.group_count}</b> 个问题，省掉 ${sum.dedup_saving} 条重复`)}
      ${kpi('闭环率', pct(sum.closed_loop_rate), '',
            `${sum.closed_count} 条闭环 / ${sum.open_count} 条在途`,
            sum.closed_loop_rate >= 0.5 ? 'ok' : 'warn')}
      ${kpi('已提升回归用例', int(sum.promoted_to_evalset), '条',
            '技术问题才会被提升', 'accent')}
      ${kpi('P0 / P1 数量',
            int(Object.values(sum.by_status || {}).length ? (sum.top_groups || [])
              .filter(g => g.priority >= 60).length : 0),
            '个', '优先级 = 0.5×严重度 + 0.3×影响面 + 0.2×频次')}
    </div>

    <div class="card mt16">
      <div class="card-head"><h2>闭环状态机</h2>
        <span class="hint">「闭环」不是形容词，是一个能算出来的数</span></div>
      <div class="card-body">
        <div class="flow">${flow}${wontfix ? `<span class="arw">｜</span><span class="st">${esc(T.wontfix)} ${wontfix}</span>` : ''}</div>
        <div class="note mt12">
          只有走到 <b>复测通过 / 已关闭</b> 才算闭环 ——
          <b>已修复不算</b>，因为没人验证过。审计表记录了每一次流转，
          所以「上季度提的问题修了多少」是可以算的，不需要开会回忆。
        </div>
      </div>
    </div>

    <div class="grid g2 mt16">
      <div class="card">
        <div class="card-head"><h2>归因分布</h2><span class="hint">归因完即完成派单</span></div>
        <div class="card-body">${barRows(typeRows)}</div>
      </div>
      <div class="card">
        <div class="card-head"><h2>状态分布</h2><span class="hint">看漏斗在哪一段堆积</span></div>
        <div class="card-body">${barRows(statusRows)}</div>
      </div>
    </div>

    <div class="card mt16">
      <div class="card-head"><h2>Top 10 待处理</h2>
        <span class="hint">已合并重复上报，影响面取最大、频次累加</span></div>
      <div class="card-body"><div class="grid" style="gap:9px">${topHtml}</div></div>
    </div>

    <div class="card mt16">
      <div class="card-head"><h2>全部反馈</h2>
        <span class="hint">${items.count} 条</span></div>
      <div class="card-body">
        <div class="toolbar">
          <div class="field"><label>归因</label>
            <select id="fType" onchange="fbFilter.type=this.value;render()">
              <option value="">全部</option>
              ${cats.map(c => `<option value="${c}" ${fbFilter.type === c ? 'selected' : ''}>
                ${esc(sum.categories[c].label)}</option>`).join('')}
            </select></div>
          <div class="field"><label>状态</label>
            <select id="fStatus" onchange="fbFilter.status=this.value;render()">
              <option value="">全部</option>
              ${(sum.states || []).map(s => `<option value="${s}" ${fbFilter.status === s ? 'selected' : ''}>
                ${esc(T[s] || s)}</option>`).join('')}
            </select></div>
          <div class="field" style="flex:1;min-width:190px"><label>关键词</label>
            <input type="text" id="fQ" value="${esc(fbFilter.q)}" placeholder="搜反馈原文…"
              onkeydown="if(event.key==='Enter'){fbFilter.q=this.value;render();}"></div>
          <button class="btn" onclick="fbFilter={type:'',status:'',q:''};render()">重置</button>
        </div>
        <div class="grid mt16" style="gap:9px">${itemHtml}</div>
      </div>
    </div>
  `;
}

async function actAdvance(id, to) {
  try {
    const r = await api('/api/feedback/advance', { method: 'POST',
      body: { item_id: id, to, note: '（看板操作）' } });
    toast(`#${id} ${r.from} → ${r.to}（${r.label}）`, 'ok');
    render();
  } catch (e) { toast('流转被拒绝：' + e.message, 'err'); }
}

async function actPromote(id) {
  try {
    const r = await api('/api/bridge/promote', { method: 'POST', body: { item_id: id } });
    toast(r.message + '，可用「一键提升」批量或去 Bridge 页看覆盖率', 'ok');
    render();
  } catch (e) { toast('提升失败：' + e.message, 'err'); }
}

async function actAutoPromote() {
  try {
    const r = await api('/api/bridge/auto', { method: 'POST', body: { top_n: 5 } });
    toast(`已提升 ${r.count} 条为回归用例：${r.promoted.join('、')}`, 'ok');
    render();
  } catch (e) { toast('失败：' + e.message, 'err'); }
}

async function actVerify(id) {
  try {
    const v = await api(`/api/bridge/verify/${id}`);
    if (!v.ran) return toast(v.message, '');
    toast(`#${id} 复测 ${v.total} 分，${v.passed ? '通过' : '未通过'}。${v.message}`, v.passed ? 'ok' : 'err');
  } catch (e) { toast('失败：' + e.message, 'err'); }
}

/* ============================================================== 数据看板 */
async function viewAnalytics() {
  const a = await api('/api/analytics/all');
  const D = a.data || {}, g = a.diagnosis || {};

  $('#topActions').innerHTML = `
    <button class="btn" onclick="actSeedAnalytics()">重新生成埋点数据</button>`;

  if (!D.funnel || !Object.keys(D.funnel).length) {
    $('#view').innerHTML = `<div class="card"><div class="empty"><div class="big">▤</div>
      还没有埋点数据，点右上角重新生成。</div></div>`;
    return;
  }

  const f = D.funnel;
  const steps = [
    { name: '打开应用', v: f.open_users, r: 1 },
    { name: '输入需求', v: f.input_users, r: f.open_to_input },
    { name: '生成成功', v: f.generate_users, r: f.input_to_generate },
    { name: '采纳结果', v: f.adopt_users, r: f.generate_to_adopt },
  ];
  const base = f.open_users || 1;
  const funnelHtml = steps.map((s, i) => {
    const drop = i ? (1 - s.r) : 0;
    return `<div class="fn">
      <div class="nm">${esc(s.name)}</div>
      <div class="tr"><div class="fl" style="width:${Math.max(2, s.v / base * 100)}%"></div></div>
      <div class="vl"><b>${int(s.v)}</b>
        ${i ? `<div class="drop-tag">↓ 流失 ${pct(drop)}</div>` : '<div class="muted" style="font-size:11px">基数</div>'}
      </div></div>`;
  }).join('');

  const ret = D.retention_summary || {};
  const retBars = barRows([
    { name: 'D1 次日', value: ret.d1_rate, label: pct(ret.d1_rate), tone: 'accent' },
    { name: 'D3', value: ret.d3_rate, label: pct(ret.d3_rate), tone: 'accent' },
    { name: 'D7', value: ret.d7_rate, label: pct(ret.d7_rate), tone: 'accent' },
  ]);

  $('#view').innerHTML = `
    <div class="grid g4">
      ${kpi('观测规模', int((D.dau || []).length), '天',
            `${(a.data && Object.keys(D).length) ? '' : ''}用户 ${int((D.channel_performance || []).reduce((s, c) => s + c.new_users, 0))} · 事件见下`)}
      ${kpi('首日整体转化', pct(f.overall), '', '锁定 cohort（只算首日事件）', 'accent')}
      ${kpi('留存 D7', pct(ret.d7_rate), '', '分母已按成熟度校正，避免低估')}
      ${kpi('最差渠道', esc(g.worst_channel ? g.worst_channel.channel : '—'), '',
            g.worst_channel ? `激活率仅 ${pct(g.worst_channel.activation_rate)}` : '', 'bad')}
    </div>

    <div class="card mt16">
      <div class="card-head"><h2>结论层</h2>
        <span class="hint">把数字翻译成「该去改什么」</span></div>
      <div class="card-body">
        <div class="note">${esc(g.conclusion || '—')}</div>
        <div class="grid g3 mt16">
          ${(g.steps || []).map(s => kpi(s.step, pct(s.rate), '',
            `流失 ${pct(s.loss)} · ${esc(s.hint)}`) ).join('')}
        </div>
      </div>
    </div>

    <div class="grid g2 mt16">
      <div class="card">
        <div class="card-head"><h2>新用户首日漏斗</h2>
          <span class="hint">口径：只统计用户首次使用当日的事件</span></div>
        <div class="card-body">
          <div class="funnel">${funnelHtml}</div>
          <div class="note warn mt16">
            <b>为什么必须锁 cohort：</b>全周期去重漏斗会把回访用户重新走的流程算进分子，
            等于变相补满分母 —— 第一版跑出来是 91%，明显不合理。锁定首日后降到 ${pct(f.open_to_input)}。
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h2>日活趋势</h2><span class="hint">按 open 事件去重</span></div>
        <div class="card-body">
          ${lineChart(D.dau, 'day', 'dau', { fmtY: v => Math.round(v) })}
        </div>
      </div>
    </div>

    <div class="grid g3 mt16">
      <div class="card">
        <div class="card-head"><h3>留存</h3><span class="hint">每个指标用自己的分母</span></div>
        <div class="card-body">${retBars}</div>
      </div>
      <div class="card">
        <div class="card-head"><h3>生成成功率</h3><span class="hint">AI 产品必须天天盯</span></div>
        <div class="card-body">
          ${lineChart(D.stability, 'day', 'success_rate',
            { h: 150, color: '#0f9d58', fmtY: v => pct(v, 0) })}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>每日 token 消耗</h3><span class="hint">体验与成本天然冲突</span></div>
        <div class="card-body">
          ${lineChart(D.cost_daily, 'day', 'total_tokens',
            { h: 150, color: '#c77700', fmtY: v => (v / 1000).toFixed(0) + 'k' })}
        </div>
      </div>
    </div>

    <div class="card mt16">
      <div class="card-head"><h2>渠道效率</h2>
        <span class="hint">拉新多但激活低的渠道，是在浪费预算</span></div>
      <div class="card-body tight">
        ${table([{ t: '渠道' }, { t: '新增', num: true }, { t: '激活人数', num: true },
                 { t: '激活率', num: true }, { t: '人均生成', num: true }],
          (D.channel_performance || []).map(c => [
            esc(c.channel), int(c.new_users), int(c.activated_users),
            `<span class="badge ${c.activation_rate >= 0.4 ? 'ok' : c.activation_rate >= 0.3 ? 'warn' : 'bad'}">
               ${pct(c.activation_rate)}</span>`, num(c.generate_per_user)]))}
      </div>
    </div>

    <div class="grid g2 mt16">
      <div class="card">
        <div class="card-head"><h3>分渠道首日漏斗</h3><span class="hint">整体只说漏了多少，分渠道才知道是谁在漏</span></div>
        <div class="card-body tight">
          ${table([{ t: '渠道' }, { t: '新增', num: true }, { t: '→输入', num: true },
                   { t: '→生成', num: true }, { t: '→采纳', num: true }],
            (D.funnel_by_channel || []).map(c => [
              esc(c.channel), int(c.new_users), pct(c.open_to_input),
              pct(c.open_to_generate), pct(c.open_to_adopt)]))}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>按星期分布</h3><span class="hint">投放在哪天加码、运维什么时候值班</span></div>
        <div class="card-body">
          ${barRows((D.weekday_pattern || []).map(w => ({
            name: w.weekday, value: w.new_users,
            label: `${int(w.new_users)} / ${pct(w.activation_rate, 0)}`,
            tone: w.new_users > 1200 ? 'accent' : '' })))}
        </div>
      </div>
    </div>

    <div class="grid g2 mt16">
      <div class="card">
        <div class="card-head"><h3>分平台</h3><span class="hint">要不要为某平台单独优化</span></div>
        <div class="card-body tight">
          ${table([{ t: '平台' }, { t: '用户', num: true }, { t: '生成次数', num: true },
                   { t: '平均耗时(ms)', num: true }],
            (D.platform_split || []).map(p => [esc(p.platform), int(p.users),
              int(p.generates), int(p.avg_generate_ms)]))}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>埋点事件定义</h3>
          <span class="hint">先定义「什么算一个事件」，再谈指标</span></div>
        <div class="card-body tight">
          ${table([{ t: '事件' }, { t: '触发时刻' }, { t: '回答什么业务问题' }],
            Object.entries(a.events_spec || {}).map(([k, v]) => [
              `<code>${esc(k)}</code>`, esc(v.when), esc(v.question)]))}
        </div>
      </div>
    </div>

    <div class="card mt16">
      <div class="card-head"><h2>SQL 源码</h2>
        <span class="hint">看板所有数字的唯一来源 —— 与代码分离，可单独 review</span></div>
      <div class="card-body">
        <div class="toolbar">
          <div class="field" style="min-width:220px"><label>选择查询（共 ${Object.keys(a.sql || {}).length} 条）</label>
            <select id="sqlSel" onchange="showSql()">
              ${Object.keys(a.sql || {}).map(k => `<option value="${esc(k)}">${esc(k)}</option>`).join('')}
            </select></div>
          <button class="btn" onclick="showSql()">显示</button>
        </div>
        <pre id="sqlBox" class="mt12"></pre>
        <div id="sqlRes" class="mt12"></div>
      </div>
    </div>
  `;
  window._sql = a.sql; window._data = D;
  showSql();
}

function showSql() {
  const k = $('#sqlSel').value;
  $('#sqlBox').textContent = (window._sql || {})[k] || '';
  const rows = (window._data || {})[k];
  const box = $('#sqlRes');
  if (!rows) { box.innerHTML = ''; return; }
  const arr = Array.isArray(rows) ? rows : [rows];
  if (!arr.length) { box.innerHTML = '<div class="empty">无结果</div>'; return; }
  const keys = Object.keys(arr[0]).filter(x => !x.startsWith('__'));
  box.innerHTML = `<div class="sec-title">查询结果（${arr.length} 行）</div>` +
    table(keys.map(h => ({ t: h, num: typeof arr[0][h] === 'number' })),
      arr.slice(0, 40).map(r => keys.map(k2 => {
        const v = r[k2];
        return typeof v === 'number' ? (Number.isInteger(v) ? int(v) : num(v, 4)) : esc(v);
      })));
}

async function actSeedAnalytics() {
  toast('正在重新生成埋点数据（约 30 天量级）…');
  try {
    const r = await api('/api/analytics/seed', { method: 'POST', body: {} });
    toast(`已生成 ${int(r.users)} 用户 / ${int(r.events)} 事件`, 'ok');
    render();
  } catch (e) { toast('失败：' + e.message, 'err'); }
}

/* ============================================================== Bridge */
async function viewBridge() {
  const [cov, pro] = await Promise.all([
    api('/api/bridge/coverage'), api('/api/bridge/promotable?limit=20')]);

  $('#topActions').innerHTML = `
    <button class="btn pri" onclick="actAutoPromote()">一键提升 Top5</button>`;

  if (!cov.has_data) {
    $('#view').innerHTML = `<div class="card"><div class="empty"><div class="big">⇄</div>
      Bridge 只处理归因为「技术问题」的反馈。先在反馈页跑一次归因。</div></div>`;
    return;
  }

  const proHtml = (pro.items || []).map(it => `<div class="item">
    <div class="meta">
      <span class="badge bad">${esc(it.band)} ${num(it.priority, 1)}</span>
      <span class="badge plain">#${it.item_id}</span>
      <span class="badge plain">影响 ${int(it.users_affected)} 人</span>
      <span class="badge plain">重复 ${it.freq} 次</span>
    </div>
    <div class="txt">${esc(it.text)}</div>
    <div class="meta">
      <span class="muted" style="font-size:11.5px">建议评测要点：</span>
      ${(it.suggested_keywords || []).map(k => `<span class="badge accent">${esc(k)}</span>`).join('')}
    </div>
    <div class="muted" style="font-size:11.5px">期望行为：${esc(it.suggested_expectation)}</div>
    <div class="acts"><button class="btn sm pri" onclick="actPromote(${it.item_id})">提升为回归用例</button></div>
  </div>`).join('') || '<div class="empty">没有待提升的反馈（可能都已经提升过了）</div>';

  const stateBadge = (s) => s === '复测通过'
    ? '<span class="badge ok">复测通过</span>'
    : s === '复测未通过' ? '<span class="badge bad">复测未通过</span>'
    : '<span class="badge plain">未跑过</span>';

  $('#view').innerHTML = `
    <div class="grid g4">
      ${kpi('技术类反馈', int(cov.technical_total), '条', '只有这类需要常驻回归用例')}
      ${kpi('已提升', int(cov.promoted), '条', `覆盖率 ${pct(cov.promote_rate)}`, 'accent')}
      ${kpi('复测通过', int(cov.verified), '条', '研发说修好不算，跑过才算', 'ok')}
      ${kpi('复测未通过', int(cov.failed), '条', '问题仍在，不允许关单', cov.failed ? 'bad' : '')}
    </div>

    <div class="note mt16">
      <b>这个模块存在的理由。</b>
      原来的三个工具走到「派单」就断了：feedback 告诉你「餐厅信息过期了，技术问题，P0」，
      然后研发修 Prompt、上线 —— <b>然后没有任何人能回答「到底修好没有」</b>。
      用户不会回来告诉你这次对了，研发测的是他自己挑的例子，
      而且下次改 Prompt 时这条 case 还会不会再坏，也没人管。
      <div class="mt8">Bridge 把这条反馈变成评测集里的一条<b>常驻用例</b>：
        修好不算完，进了评测集、且每次发版都跑过，才算完。</div>
    </div>

    <div class="grid g2 mt16">
      <div class="card">
        <div class="card-head"><h2>待提升的反馈</h2>
          <span class="hint">优先取 P0/P1 的技术类问题</span></div>
        <div class="card-body"><div class="grid" style="gap:9px">${proHtml}</div></div>
      </div>

      <div class="card">
        <div class="card-head"><h2>已建立的回归用例</h2>
          <span class="hint">复测结果来自最近一次评测 #${cov.latest_run_id || '—'}</span></div>
        <div class="card-body tight scroll">
          ${table([{ t: '反馈' }, { t: '用例' }, { t: '原文' }, { t: '复测' }, { t: '得分', num: true }],
            (cov.details || []).map(d => [
              `<code>#${d.item_id}</code>`, `<code>${esc(d.sample_id)}</code>`,
              esc(cut(d.text, 40)), stateBadge(d.state),
              d.total != null ? num(d.total) : '<span class="muted">—</span>']))}
        </div>
      </div>
    </div>

    <div class="card mt16">
      <div class="card-head"><h2>完整闭环怎么走</h2>
        <span class="hint">这是我会现场演示的路径</span></div>
      <div class="card-body">
        <pre><span class="cm"># 1. 灌入反馈并自动归因（规则 vs LLM 对比，给出混淆矩阵）</span>
python run.py feedback seed
python run.py feedback run

<span class="cm"># 2. 把 P0 的技术类吐槽提升为回归测试用例</span>
python run.py bridge promote --auto --top 5

<span class="cm"># 3. 跑评测（新用例自动进入评测集，会被一起跑）</span>
python run.py eval run

<span class="cm"># 4. 查复测结果：研发说修好了不算，跑过了才算</span>
python run.py bridge verify 46

<span class="cm"># 5. 已修复 + 复测通过 -> 才允许关单</span>
python run.py feedback advance 46 --to assigned
python run.py feedback advance 46 --to fixed
python run.py bridge close 46

<span class="cm"># 6. 看闭环率（这个数字回答「上次提的问题修了多少」）</span>
python run.py feedback report</pre>
      </div>
    </div>
  `;
}

/* ------------------------------------------------------------------ 启动 */
async function fillSidebar() {
  /*
   * 侧栏页脚三行：跑在什么模式、看的是哪份库、什么版本。
   * 这三行不是装饰 —— 第一次看到这个看板的人，第一个问题往往是
   * 「我现在看的是真实模型还是模拟数据」，答案应该直接写在屏幕上。
   * （之前这里一直是三个「—」占位符，等于一个死掉的 UI 元素。）
   */
  try {
    const h = await api('/api/health');
    const c = h.counts || {};
    $('#sideMode').textContent = h.mode === 'real' ? '真实模型模式' : 'Mock 模式 · 结果可复现';
    $('#sideDb').textContent =
      `样本 ${int(c.eval_samples)} · 反馈 ${int(c.fb_items)} · 事件 ${int(c.events)}`;
    $('#sideVer').textContent =
      `v${h.version} · ${window.__OFFLINE__ ? '离线快照（数据已冻结）' : '在线服务'}`;
  } catch (e) {
    $('#sideMode').textContent = '未能连接服务';
  }
}

/* 页脚与主视图并行加载，别让一行小字拖慢首屏 */
fillSidebar();
render();
