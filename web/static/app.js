'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  view: 'library',
  docs: [],
  doc: null,
  chart: [],
  activeStmt: null,
  selectedCell: null,
  pdfPage: 1,
  pdfDoc: null,
  mappingQuery: '',
  manualFilter: 'all',
  evalData: null,
};

// ------------------------------------------------------------------ utils
const fmt = (v, digits = 2) => {
  if (v === null || v === undefined || v === '') return '';
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  const neg = n < 0;
  const s = Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
  return neg ? `(${s})` : s;
};

const fmtShort = (v) => {
  if (v === null || v === undefined) return '';
  const n = Number(v);
  if (!Number.isFinite(n)) return '';
  const a = Math.abs(n);
  if (a >= 1e8) return (n / 1e8).toFixed(2) + '亿';
  if (a >= 1e4) return (n / 1e4).toFixed(1) + '万';
  return fmt(n, 0);
};

const pct = (v) => `${(Number(v) * 100).toFixed(1)}%`;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show' + (isError ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 2800);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

const postJson = (path, body) => api(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

const STMT_NAME = { BS: '资产负债表', IS: '利润表', CF: '现金流量表' };
const STMT_CODE = { BS: 'BS', IS: 'IS', CF: 'CF' };
const SCOPE_NAME = { consolidated: '合并', parent: '母公司' };

// ------------------------------------------------------------------ tabs
function showView(name) {
  state.view = name;
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  if (name === 'library') loadLibrary();
  if (name === 'accuracy') loadAccuracy();
  if (name === 'mapping') renderMapping();
  if (['review', 'checks', 'manual'].includes(name) && !state.doc) {
    if (state.docs.length) openDoc(state.docs[0].doc_id);
    else toast('请先导入文档');
  }
  if (name === 'review' && state.doc) renderReview();
  if (name === 'checks' && state.doc) renderChecks();
  if (name === 'manual' && state.doc) renderManual();
}

// ------------------------------------------------------------------ library
async function loadLibrary() {
  const grid = $('#doc-grid');
  try {
    const data = await api('/api/documents');
    state.docs = data.documents;
    renderLibrary();
  } catch (e) {
    grid.innerHTML = `<p class="muted">加载失败：${esc(e.message)}</p>`;
  }
}

function docStatus(doc) {
  return doc.status || 'unreviewed';
}

function renderLibrary() {
  const q = $('#search').value.trim().toLowerCase();
  const kind = $('#filter-kind').value;
  const fstat = $('#filter-status').value;
  const list = state.docs.filter((d) => {
    if (q && !(`${d.company || ''}${d.file_name || ''}${d.doc_id}`.toLowerCase().includes(q))) return false;
    if (kind === 'scanned' && !d.scanned) return false;
    if (kind === 'digital' && d.scanned) return false;
    if (fstat === 'error' && !d.findings.error) return false;
    if (fstat === 'approved' && d.status !== 'approved') return false;
    return true;
  });
  $('#library-count').textContent = `${list.length} / ${state.docs.length} 份文档`;
  const grid = $('#doc-grid');
  $('#library-empty').classList.toggle('hidden', state.docs.length > 0);
  grid.innerHTML = list.map((d) => {
    const stmtChips = (d.statements || []).map((s) =>
      `<span class="chip">${SCOPE_NAME[s.scope] || s.scope}${STMT_NAME[s.code] || s.code} · ${s.rows}</span>`
    ).join('');
    const errs = d.findings.error;
    const warns = d.findings.warning;
    const statusChip = docStatus(d) === 'approved'
      ? '<span class="chip ok">已复核</span>'
      : docStatus(d) === 'in_progress' ? '<span class="chip warn">复核中</span>' : '';
    return `
      <article class="doc-card" data-id="${esc(d.doc_id)}">
        <div class="row1">
          <span class="name">${esc(d.company || d.file_name)}</span>
          <span class="code">${esc(d.doc_id)}</span>
          ${d.scanned ? '<span class="chip scanned">扫描件</span>' : ''}
          ${statusChip}
        </div>
        <div class="meta">${d.pages} 页 · ${(d.statements || []).length} 张报表 · ${esc(d.source === 'corpus' ? '语料库' : '上传')}</div>
        <div class="stmts">${stmtChips || '<span class="chip">未识别到报表</span>'}</div>
        <div class="foot">
          ${errs ? `<span style="color:var(--err)">● ${errs} 项勾稽错误</span>` : '<span style="color:var(--ok)">● 勾稽通过</span>'}
          ${warns ? `<span style="color:var(--warn)">● ${warns} 项警告</span>` : ''}
          <span>${d.edits ? `✎ ${d.edits} 处已修改` : ''}</span>
        </div>
      </article>`;
  }).join('');
  $$('.doc-card').forEach((el) => el.addEventListener('click', () => openDoc(el.dataset.id)));
}

async function openDoc(id) {
  try {
    const doc = await api(`/api/documents/${id}`);
    state.doc = doc;
    state.activeStmt = doc.statements[0] ? doc.statements[0].code + '.' + doc.statements[0].scope : null;
    state.selectedCell = null;
    state.pdfPage = doc.statements[0]?.pages[0] || 1;
    state.pdfDoc = null;
    if (state.view === 'library') showView('review');
    if (state.view === 'review') renderReview();
    if (state.view === 'checks') renderChecks();
    if (state.view === 'manual') renderManual();
    if (state.view === 'mapping') renderMapping();
  } catch (e) {
    toast(`打开失败：${e.message}`, true);
  }
}

// ------------------------------------------------------------------ doc header
function renderDocHeader(el) {
  const d = state.doc;
  if (!d) { el.innerHTML = ''; return; }
  const errs = d.findings.filter((f) => f.status === 'error').length;
  const warns = d.findings.filter((f) => f.status === 'warning').length;
  el.innerHTML = `
    <div>
      <div class="title">${esc(companyName(d))}</div>
      <div class="meta">${esc(d.file_name)} · ${d.pages} 页 · ${d.scanned ? '扫描件（OCR）' : '电子版'} · 用时 ${d.elapsed_ms} ms</div>
    </div>
    <span class="chip ${errs ? 'err' : 'ok'}">${errs ? errs + ' 项错误' : '勾稽通过'}</span>
    ${warns ? `<span class="chip warn">${warns} 项警告</span>` : ''}
    <span class="spacer"></span>
    <button class="ghost" id="btn-recheck">重新校验</button>
    <a class="ghost" href="/api/documents/${d.doc_id}/pdf" target="_blank">打开原 PDF</a>
  `;
  const btn = $('#btn-recheck', el);
  if (btn) btn.addEventListener('click', async () => {
    await openDoc(state.doc.doc_id);
    toast('已重新校验');
  });
}

function companyName(d) {
  for (const s of d.statements || []) {
    const m = (s.header_text || '').split(/\s{2,}/).find((x) => x.includes('公司'));
    if (m) return m.trim();
  }
  return d.file_name;
}

// ------------------------------------------------------------------ review
function renderReview() {
  const d = state.doc;
  if (!d) return;
  renderDocHeader($('#review-doc-header'));
  renderStmtNav();
  renderStmtTable();
  renderPdfPane();
}

function stmtId(s) { return `${s.code}.${s.scope}`; }

function renderStmtNav() {
  const d = state.doc;
  const nav = $('#stmt-nav');
  const order = ['BS', 'IS', 'CF'];
  const sorted = [...d.statements].sort((a, b) => {
    const oa = order.indexOf(a.code), ob = order.indexOf(b.code);
    if (oa !== ob) return oa - ob;
    return a.scope === 'consolidated' ? -1 : 1;
  });
  nav.innerHTML = sorted.map((s) => {
    const id = stmtId(s);
    const errs = rowsWithErrors(s).size;
    return `<button data-id="${id}" class="${id === state.activeStmt ? 'active' : ''}">
      <span class="nm">${SCOPE_NAME[s.scope]}${STMT_NAME[s.code]}</span>
      <span class="sm">${s.rows.length} 行 · ${s.unit} · p${s.pages[0]}${errs ? ` · ${errs} 处异常` : ''}</span>
    </button>`;
  }).join('');
  $$('#stmt-nav button').forEach((b) => b.addEventListener('click', () => {
    state.activeStmt = b.dataset.id;
    const s = activeStmt();
    state.pdfPage = s.pages[0];
    state.selectedCell = null;
    renderStmtNav();
    renderStmtTable();
    renderPdfPane();
  }));
}

function activeStmt() {
  if (!state.doc) return null;
  return state.doc.statements.find((s) => stmtId(s) === state.activeStmt)
    || state.doc.statements[0];
}

function rowsWithErrors(stmt) {
  const bad = new Set();
  for (const f of state.doc.findings) {
    if (f.statement !== stmt.code) continue;
    if (f.status === 'ok') continue;
    for (const p of f.participants || []) if (p.key) bad.add(p.key);
    if (stmt.code === 'BS') bad.add('total_assets');
  }
  return bad;
}

function renderStmtTable() {
  const s = activeStmt();
  const wrap = $('#stmt-table');
  const toolbar = $('#stmt-toolbar');
  if (!s) {
    wrap.innerHTML = '<p class="muted" style="padding:20px">该文档未识别出报表。</p>';
    toolbar.innerHTML = '';
    return;
  }
  const errs = rowsWithErrors(s);
  const overlays = cellOverrides(s);
  toolbar.innerHTML = `
    <strong>${s.title || (SCOPE_NAME[s.scope] + STMT_NAME[s.code])}</strong>
    <span class="chip">${s.pages.map((p) => 'p' + p).join(', ')}</span>
    <span class="unit">单位：${s.unit}</span>
    ${s.scanned ? '<span class="chip scanned">OCR</span>' : ''}
    <span class="muted small">${s.rows.length} 个科目 · 金额已换算为元</span>
    <span class="spacer" style="margin-left:auto"></span>
    ${s.warnings.length ? `<span class="chip warn">${esc(s.warnings.join('；'))}</span>` : ''}
  `;
  const cols = [...s.columns].sort((a, b) => a.index - b.index);
  const head = cols.map((c) => {
    const label = c.label || (c.role === 'current' ? '本期' : '上期');
    return `<th title="${esc(label)}">${esc(label)}<br><span style="font-weight:400">${c.year || ''}</span></th>`;
  }).join('');
  let rows = '';
  let lastSection = null;
  for (const row of s.rows) {
    const section = sectionName(row.section);
    if (section && section !== lastSection) {
      rows += `<tr class="section"><td colspan="${cols.length + 1}">${esc(section)}</td></tr>`;
      lastSection = section;
    }
    const isTotal = /total$|^total_/.test(row.key);
    const cells = cols.map((c) => {
      const v = row.values[c.index];
      return renderCell(s, row, c, v, overlays);
    }).join('');
    rows += `<tr class="${isTotal ? 'total' : ''}">
      <td class="lbl">${esc(row.label)}
        ${row.match_method !== 'exact' ? `<span class="badge ${row.match_method === 'fuzzy' ? 'fuzzy' : ''}">${matchWord(row.match_method)}</span>` : ''}
        <span class="sub">${esc(row.canonical)}${errs.has(row.key) ? ' · <span style="color:var(--err)">勾稽异常</span>' : ''}</span>
      </td>${cells}</tr>`;
  }
  wrap.innerHTML = `<table class="stmt">
    <thead><tr><th class="lbl">项目</th>${head}</tr></thead>
    <tbody>${rows}</tbody></table>`;
  bindCells();
}

function matchWord(m) {
  return { exact: '精确', alias: '别名', fuzzy: '模糊', user: '自定义' }[m] || m;
}

function sectionName(sec) {
  return {
    current_assets: '流动资产', noncurrent_assets: '非流动资产',
    current_assets_total: '', noncurrent_assets_total: '', assets_total: '',
    current_liabilities: '流动负债', noncurrent_liabilities: '非流动负债',
    current_liabilities_total: '', noncurrent_liabilities_total: '',
    liabilities_total: '', equity_parent: '所有者权益', equity_minority: '',
    equity_parent_total: '', equity_total: '', liabilities_equity_total: '',
    revenue: '营业收入与成本', cost: '', expense: '期间费用',
    finance_detail: '', other_income: '其他损益', operating_profit: '利润',
    nonoperating: '营业外收支', total_profit: '', tax: '',
    net_profit: '', net_profit_detail: '', net_profit_parent: '',
    minority_profit: '', eps: '每股收益',
    operating_in: '经营活动', operating_out: '', operating_in_total: '',
    operating_out_total: '', operating_net: '',
    investing_in: '投资活动', investing_out: '', investing_in_total: '',
    investing_out_total: '', investing_net: '',
    financing_in: '筹资活动', financing_out: '', financing_in_total: '',
    financing_out_total: '', financing_net: '',
    fx: '汇率与现金', cash_change: '', cash_balance: '',
  }[sec] ?? '';
}

function cellOverrides(s) {
  const out = {};
  const ov = (state.doc.review && state.doc.review.overrides) || {};
  for (const [k, v] of Object.entries(ov)) {
    const [code, scope, key, col] = k.split('.');
    if (code === s.code && scope === s.scope) out[`${key}.${col}`] = v;
  }
  return out;
}

function renderCell(s, row, col, val, overlays) {
  const key = `${row.key}.${col.index}`;
  const ov = overlays[key];
  const value = ov ? ov.value : (val ? val.value : null);
  const conf = val ? (val.confidence ?? 1) : 1;
  const blank = !val || val.blank;
  const edited = !!ov;
  const low = conf < 0.85;
  const cls = ['num'];
  if (blank && !edited) cls.push('empty');
  if (edited) cls.push('edited');
  else if (low) cls.push('lowconf');
  if (state.selectedCell === key) cls.push('sel');
  const disp = edited && value !== null && value !== ''
    ? fmt(value, 2) : (val ? fmt(val.value, 2) : '');
  const title = [
    val ? `原文：${val.printed}` : '',
    val && val.confidence < 1 ? `置信度 ${(val.confidence * 100).toFixed(0)}%` : '',
    edited ? `已人工修改${ov.note ? '：' + ov.note : ''}` : '',
  ].filter(Boolean).join('\n');
  const badge = (!edited && val && val.confidence < 0.85 && val.raw_confidence < 100)
    ? `<span class="conf">${Math.round(val.raw_confidence)}%</span>` : '';
  return `<td class="${cls.join(' ')}" data-key="${esc(row.key)}" data-col="${col.index}"
    data-page="${val ? val.page || '' : ''}"
    data-box="${val && val.box ? esc(val.box.join(',')) : ''}" title="${esc(title)}">${disp}${badge}</td>`;
}

function bindCells() {
  $$('#stmt-table td.num').forEach((td) => {
    td.addEventListener('click', (ev) => {
      const s = activeStmt();
      const key = td.dataset.key;
      const col = Number(td.dataset.col);
      state.selectedCell = `${key}.${col}`;
      $$('#stmt-table td.num.sel').forEach((x) => x.classList.remove('sel'));
      td.classList.add('sel');
      const page = Number(td.dataset.page);
      const box = td.dataset.box ? td.dataset.box.split(',').map(Number) : null;
      if (page) gotoPdf(page, box);
      if (ev.altKey) {
        editCell(s, key, col, td);
      }
    });
    td.addEventListener('dblclick', () => {
      const s = activeStmt();
      editCell(s, td.dataset.key, Number(td.dataset.col), td);
    });
  });
}

function editCell(s, key, colIndex, td) {
  const row = s.rows.find((r) => r.key === key);
  if (!row) return;
  const cur = row.values[colIndex];
  const existing = cur ? fmt(cur.value, 2) : '';
  const input = window.prompt(
    `${row.canonical}（${row.label}）\n输入换算为「元」的金额，留空表示清除：`,
    existing.replace(/,/g, ''));
  if (input === null) return;
  const raw = input.trim();
  let value = null;
  if (raw !== '') {
    const cleaned = raw.replace(/,/g, '');
    if (!/^-?\d+(\.\d+)?$/.test(cleaned)) {
      toast('请输入数字', true);
      return;
    }
    value = String(Number(cleaned));
  }
  postJson(`/api/documents/${state.doc.doc_id}/override`, {
    code: s.code, scope: s.scope, key, col: String(colIndex),
    value, note: '人工复核',
  }).then((doc) => {
    state.doc = doc;
    renderStmtTable();
    toast('已保存，勾稽关系已重新计算');
  }).catch((e) => toast(e.message, true));
}

// ------------------------------------------------------------------ pdf
async function renderPdfPane() {
  const s = activeStmt();
  if (!s) return;
  const page = state.pdfPage;
  $('#pdf-page-label').textContent = `第 ${page} 页 / ${state.doc.pages}`;
  const dpi = 150;
  const url = `/api/documents/${state.doc.doc_id}/page/${page}.png?dpi=${dpi}`;
  const img = new Image();
  img.onload = () => {
    const host = $('#pdf-page');
    host.innerHTML = `<img src="${url}" alt="第 ${page} 页"><div class="text-layer"></div>`;
    // the image fills the pane width, so highlight boxes are placed as
    // percentages of the page's own point dimensions
    host.dataset.pageW = String((img.naturalWidth / dpi) * 72);
    host.dataset.pageH = String((img.naturalHeight / dpi) * 72);
    drawHighlight();
  };
  img.onerror = () => {
    $('#pdf-page').innerHTML = '<p class="muted" style="padding:20px">页面渲染失败</p>';
  };
  img.src = url;
}

function placeBox(host, box, low) {
  if (!host || !box) return null;
  const pw = Number(host.dataset.pageW) || 595;
  const ph = Number(host.dataset.pageH) || 842;
  const [x0, y0, x1, y1] = box;
  const div = document.createElement('div');
  div.className = 'hl' + (low ? ' low' : '');
  div.style.left = `${(x0 / pw) * 100}%`;
  div.style.top = `${(y0 / ph) * 100}%`;
  div.style.width = `${Math.max(0.7, ((x1 - x0) / pw) * 100)}%`;
  div.style.height = `${Math.max(0.45, ((y1 - y0) / ph) * 100)}%`;
  host.appendChild(div);
  div.scrollIntoView({ block: 'center', behavior: 'smooth' });
  return div;
}

function drawHighlight() {
  const host = $('#pdf-page');
  if (!host) return;
  $$('.hl', host).forEach((x) => x.remove());
  const cell = state.selectedCell;
  if (!cell) return;
  const td = $('#stmt-table td.num.sel');
  if (!td || !td.dataset.box) return;
  const pageNo = Number(td.dataset.page);
  if (pageNo !== state.pdfPage) return;
  placeBox(host, td.dataset.box.split(',').map(Number),
    td.classList.contains('lowconf'));
}

function gotoPdf(page, box) {
  state.pdfPage = page || 1;
  renderPdfPane();
  setTimeout(drawHighlight, 80);
}

// ------------------------------------------------------------------ checks
function renderChecks() {
  const d = state.doc;
  if (!d) return;
  renderDocHeader($('#checks-doc-header'));
  const fs = d.findings;
  const nErr = fs.filter((f) => f.status === 'error').length;
  const nWarn = fs.filter((f) => f.status === 'warning').length;
  const nOk = fs.filter((f) => f.status === 'ok').length;
  const body = fs.filter((f) => f.status !== 'ok');
  $('#checks-summary').innerHTML = `
    <div class="sum-card err"><div class="n">${nErr}</div><div class="k">勾稽错误</div></div>
    <div class="sum-card warn"><div class="n">${nWarn}</div><div class="k">超出容差警告</div></div>
    <div class="sum-card ok"><div class="n">${nOk}</div><div class="k">通过</div></div>
    <div class="sum-card"><div class="n">${fs.length}</div><div class="k">校验项合计</div></div>
  `;
  const list = $('#checks-list');
  if (!body.length) {
    list.innerHTML = `<div class="check-item"><div class="head">
      <span class="status-pill">通过</span><span class="label">全部勾稽关系成立</span></div>
      <div class="msg">资产 = 负债 + 所有者权益、各小计与明细之和、净利润链条、现金流量表内部勾稽均在容差内。</div></div>`;
    return;
  }
  list.innerHTML = body.map((f) => {
    const parts = (f.participants || []).filter((p) => p.key).map((p) =>
      `<span class="part" data-page="${p.page || ''}" data-box="${(p.box || []).join(',')}"
        title="定位">${esc(accountZh(p.key))} ${fmt(p.value, 2)}</span>`).join('');
    return `<article class="check-item ${f.status}">
      <div class="head">
        <span class="status-pill ${f.status}">${f.status === 'error' ? '不平' : '警告'}</span>
        <span class="label">${esc(f.label)}</span>
        <span class="rule">${esc(f.rule)} · ${SCOPE_NAME[f.role] || f.role}${f.year ? ' ' + f.year : ''}</span>
      </div>
      <div class="nums">
        <div><span>应有</span>${fmt(f.expected, 2)}</div>
        <div><span>实际</span>${fmt(f.actual, 2)}</div>
        <div><span>差异</span><b class="diff">${fmt(f.diff, 2)}</b></div>
        <div><span>容差</span>${fmt(f.tolerance, 2)}</div>
      </div>
      ${f.message ? `<div class="msg">${esc(f.message)}</div>` : ''}
      ${parts ? `<div class="parts">${parts}</div>` : ''}
    </article>`;
  }).join('');
  $$('#checks-list .part').forEach((p) => p.addEventListener('click', () => {
    const page = Number(p.dataset.page);
    const box = p.dataset.box ? p.dataset.box.split(',').map(Number) : null;
    if (!page) return;
    state.pdfPage = page;
    if (state.view !== 'review') {
      state.activeStmt = guessStmt(p.textContent);
      showView('review');
    }
    state.selectedCell = null;
    renderPdfPane();
    setTimeout(() => {
      $$('#pdf-page .hl').forEach((x) => x.remove());
      placeBox($('#pdf-page'), box, false);
    }, 180);
  }));
}

function accountZh(key) {
  const a = state.chart.find((x) => x.key === key);
  return a ? a.name : key;
}

function guessStmt(text) {
  if (!state.doc) return null;
  const st = state.doc.statements.find((s) => s.scope === 'consolidated') || state.doc.statements[0];
  return st ? stmtId(st) : null;
}

// ------------------------------------------------------------------ manual
function renderManual() {
  const d = state.doc;
  if (!d) return;
  renderDocHeader($('#manual-doc-header'));
  const s = activeStmt();
  const tasks = buildTasks();
  const filter = state.manualFilter;
  const shown = tasks.filter((t) => filter === 'all' || t.kind === filter);
  $('#manual-list').innerHTML = shown.length ? shown.map(taskHtml).join('')
    : `<p class="muted">当前筛选下没有待处理事项。</p>`;
  bindTasks();
  const reviewed = tasks.filter((t) => t.done).length;
  const p = tasks.length ? reviewed / tasks.length : 1;
  $('#review-progress-bar').style.width = `${Math.round(p * 100)}%`;
  $('#review-progress-text').textContent =
    `${reviewed} / ${tasks.length} 项已处理 · 状态：${statusZh(docStatus(d))}`;
  $$('.status-buttons button').forEach((b) => {
    b.onclick = () => postJson(`/api/documents/${d.doc_id}/status`, { state: b.dataset.status })
      .then((r) => { d.review = r; d.status = r.status; renderManual(); toast('状态已更新'); })
      .catch((e) => toast(e.message, true));
  });
}

function statusZh(s) {
  return { approved: '已复核', in_progress: '复核中', unreviewed: '未复核' }[s] || s;
}

function buildTasks() {
  const d = state.doc;
  const tasks = [];
  for (const f of d.findings) {
    if (f.status === 'ok') continue;
    tasks.push({
      kind: 'error', title: f.label, desc: `应有 ${fmt(f.expected, 2)}，实际 ${fmt(f.actual, 2)}，差异 ${fmt(f.diff, 2)}（${SCOPE_NAME[f.role] || ''}${f.year || ''}）`,
      stmt: f.statement, page: f.pages[0], key: (f.participants[0] || {}).key,
      done: false,
    });
  }
  for (const st of d.statements) {
    for (const row of st.rows) {
      for (const [col, v] of Object.entries(row.values)) {
        if (v.confidence < 0.85) {
          tasks.push({
            kind: 'lowconf', title: `${st.title} · ${row.canonical}`,
            desc: `原文「${v.printed}」置信度 ${(v.confidence * 100).toFixed(0)}%`,
            stmt: st.code + '.' + st.scope, page: v.page || st.pages[0],
            key: row.key, col: Number(col), done: false,
          });
        }
      }
    }
  }
  const overlays = (d.review && d.review.overrides) || {};
  for (const st of d.statements) {
    for (const row of st.rows) {
      if (row.match_method === 'fuzzy' || row.match_method === 'none') {
        tasks.push({
          kind: 'unmatched', title: `科目匹配待确认：${row.label}`,
          desc: `自动匹配到「${row.canonical}」（${matchWord(row.match_method)}，${(row.match_score * 100).toFixed(0)}%）`,
          stmt: st.code + '.' + st.scope, page: row.page, key: row.key, done: false,
        });
      }
    }
  }
  for (const [k, v] of Object.entries(overlays)) {
    const [code, scope, key] = k.split('.');
    tasks.push({
      kind: 'edits', title: `已手工修改：${accountZh(key)}`,
      desc: `改为 ${fmt(v.value, 2)}${v.note ? ' · ' + v.note : ''}`,
      stmt: code + '.' + scope, page: null, key, done: true,
    });
  }
  return tasks;
}

function taskHtml(t) {
  return `<div class="task" data-stmt="${esc(t.stmt || '')}" data-page="${t.page || ''}"
      data-key="${esc(t.key || '')}" data-col="${t.col ?? ''}">
    <span class="dot ${t.kind === 'error' ? 'error' : t.kind === 'warn' ? 'warn' : 'info'}"></span>
    <div class="body"><div class="t">${esc(t.title)}</div><div class="d">${esc(t.desc)}</div></div>
    <button>定位</button>
  </div>`;
}

function bindTasks() {
  $$('.task').forEach((el) => {
    $('button', el).addEventListener('click', (ev) => {
      ev.stopPropagation();
      const stmt = el.dataset.stmt;
      if (stmt) state.activeStmt = stmt;
      const page = Number(el.dataset.page);
      if (page) state.pdfPage = page;
      const key = el.dataset.key;
      const col = el.dataset.col;
      state.selectedCell = col !== '' && key ? `${key}.${col}` : null;
      showView('review');
      renderReview();
      if (page) setTimeout(() => gotoPdf(page, null), 40);
    });
  });
}

// ------------------------------------------------------------------ mapping
async function loadChart() {
  if (state.chart.length) return state.chart;
  const data = await api('/api/chart');
  state.chart = data.accounts;
  return state.chart;
}

async function renderMapping() {
  await loadChart();
  const q = state.mappingQuery.toLowerCase();
  const byStmt = { BS: [], IS: [], CF: [] };
  for (const a of state.chart) {
    if (q && !(`${a.name}${a.key}${(a.aliases || []).join('')}`.toLowerCase().includes(q))) continue;
    byStmt[a.statement].push(a);
  }
  $('#mapping-count').textContent = `${state.chart.length} 个标准科目`;
  $('#chart-list').innerHTML = Object.entries(byStmt).map(([code, items]) => `
    <div class="chart-group">
      <h3>${STMT_NAME[code]} · ${items.length}</h3>
      ${items.map((a) => `<div class="chart-item">
        <div>${esc(a.name)}</div>
        <div class="al">${esc((a.aliases || []).join(' / ') || '—')}</div>
        <div class="k">${esc(a.key)}</div>
      </div>`).join('')}
    </div>`).join('');

  const detail = $('#doc-mapping');
  if (!state.doc) {
    detail.innerHTML = '<p class="muted">先在文档库中选择一份文档。</p>';
    return;
  }
  const rows = [];
  for (const st of state.doc.statements) {
    for (const row of st.rows) {
      rows.push({ st, row });
    }
  }
  const seen = new Set();
  const uniq = rows.filter(({ st, row }) => {
    const k = `${st.code}|${row.label}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  detail.innerHTML = uniq.map(({ st, row }) => {
    const opts = state.chart.filter((a) => a.statement === st.code)
      .map((a) => `<option value="${a.key}" ${a.key === row.key ? 'selected' : ''}>${esc(a.name)}</option>`).join('');
    return `<div class="map-row" data-code="${st.code}" data-label="${esc(row.label)}">
      <div class="from" title="${esc(row.label)}">${esc(row.label)}</div>
      <select>${opts}</select>
      <div class="method">${matchWord(row.match_method)} ${(row.match_score * 100).toFixed(0)}%</div>
    </div>`;
  }).join('') || '<p class="muted">—</p>';
  $$('#doc-mapping select').forEach((sel) => {
    sel.addEventListener('change', () => {
      const rowEl = sel.closest('.map-row');
      postJson(`/api/documents/${state.doc.doc_id}/alias`, {
        code: rowEl.dataset.code, label: rowEl.dataset.label, key: sel.value,
      }).then(() => {
        toast('映射已保存；重新抽取同一版式时会沿用');
      }).catch((e) => toast(e.message, true));
    });
  });
}

// ------------------------------------------------------------------ accuracy
async function loadAccuracy() {
  try {
    const data = await api('/api/eval');
    state.evalData = data;
    renderAccuracy();
  } catch (e) {
    $('#accuracy-summary').innerHTML = `<p class="muted">评测报告不可用：${esc(e.message)}</p>`;
  }
}

function renderAccuracy() {
  const data = state.evalData;
  if (!data || !data.available) {
    $('#accuracy-summary').innerHTML = `<div class="card"><p class="muted">
      尚未生成评测报告。在命令行运行：<code class="mono">python3 -m finreport evaluate --corpus data/corpus</code></p></div>`;
    return;
  }
  const ov = data.summary._overall;
  $('#accuracy-meta').textContent =
    `${ov.documents} 份文档 · ${ov.statements} 张报表 · ${ov.expected_values} 个数值`;
  $('#accuracy-summary').innerHTML = `
    <div class="sum-card ok"><div class="n">${pct(ov.value_accuracy)}</div><div class="k">科目数值准确率</div></div>
    <div class="sum-card"><div class="n">${pct(ov.unit_accuracy)}</div><div class="k">单位识别正确率</div></div>
    <div class="sum-card"><div class="n">${pct(ov.page_accuracy)}</div><div class="k">表格页定位正确率</div></div>
    <div class="sum-card"><div class="n">${ov.matched_values}<span class="muted" style="font-size:13px">/${ov.expected_values}</span></div><div class="k">完全一致的数值</div></div>
  `;
  const byStmt = ['BS', 'IS', 'CF'].map((code) => {
    const s = data.summary[code];
    if (!s) return '';
    return `<div class="bar-row">
      <div>${STMT_NAME[code]}</div>
      <div class="bar ${s.value_accuracy >= 0.95 ? 'good' : s.value_accuracy < 0.8 ? 'bad' : ''}">
        <div style="width:${s.value_accuracy * 100}%"></div></div>
      <div class="v">${pct(s.value_accuracy)}</div>
    </div>
    <div class="bar-row"><div class="muted small">　单位 ${pct(s.unit_accuracy)} · 页定位 ${pct(s.page_accuracy)}</div>
      <div class="muted small">漏识别 ${s.missing_keys} · 多识别 ${s.extra_keys}</div><div></div></div>`;
  }).join('');
  $('#accuracy-by-statement').innerHTML = byStmt;

  const docs = data.per_document || [];
  $('#accuracy-by-doc').innerHTML = docs.map((d) => {
    const vals = d.statements.flatMap((s) => s.wrong_values);
    const exp = d.statements.reduce((a, s) => a + s.expected_values, 0);
    const mat = d.statements.reduce((a, s) => a + s.matched_values, 0);
    const acc = exp ? mat / exp : 0;
    return `<div class="bar-row" title="${esc(d.doc_id)}">
      <div>${esc(d.doc_id)} <span class="muted small">${d.truth_unit}${d.scanned ? ' · 扫描' : ''}</span></div>
      <div class="bar ${acc >= 0.95 ? 'good' : acc < 0.8 ? 'bad' : ''}"><div style="width:${acc * 100}%"></div></div>
      <div class="v">${pct(acc)}</div>
    </div>`;
  }).join('');

  const accounts = data.per_account || [];
  $('#accuracy-by-account').innerHTML = `<table class="report">
    <thead><tr><th>科目</th><th>报表</th><th>应识别</th><th>一致</th><th>准确率</th><th>错误样例</th></tr></thead>
    <tbody>${accounts.map((a) => {
      const ex = (a.examples || [])[0];
      return `<tr>
        <td>${esc(a.key)}</td><td>${a.code}</td>
        <td>${a.expected}</td><td>${a.matched}</td>
        <td class="acc-cell ${a.accuracy < 0.9 ? 'bad' : a.accuracy < 1 ? 'warn' : ''}">${pct(a.accuracy)}</td>
        <td class="muted small">${ex ? `${esc(ex.doc_id)} ${ex.year}：抽到 ${ex.actual ?? '—'} / 应为 ${ex.expected}` : ''}</td>
      </tr>`;
    }).join('')}</tbody></table>`;
}

// ------------------------------------------------------------------ wiring
function bindTop() {
  $$('.tab').forEach((t) => t.addEventListener('click', () => showView(t.dataset.view)));
  $('#search').addEventListener('input', renderLibrary);
  $('#filter-kind').addEventListener('change', renderLibrary);
  $('#filter-status').addEventListener('change', renderLibrary);
  $('#mapping-search').addEventListener('input', (e) => {
    state.mappingQuery = e.target.value.trim();
    renderMapping();
  });
  $('#manual-filter').addEventListener('change', (e) => {
    state.manualFilter = e.target.value;
    renderManual();
  });
  $('#file-input').addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append('file', f);
    toast('正在解析 PDF…');
    try {
      const doc = await api('/api/documents/upload', { method: 'POST', body: fd });
      toast('解析完成');
      state.doc = doc;
      state.activeStmt = doc.statements[0] ? stmtId(doc.statements[0]) : null;
      state.pdfPage = doc.statements[0]?.pages[0] || 1;
      showView('review');
      renderReview();
      loadLibrary();
    } catch (err) {
      toast(`解析失败：${err.message}`, true);
    }
    e.target.value = '';
  });
  $('#import-corpus').addEventListener('click', async () => {
    toast('正在导入样例（首次需要抽取，约需数十秒）…');
    try {
      const r = await api('/api/documents/import-corpus?limit=8', { method: 'POST' });
      toast(`已导入 ${r.imported.length} 份文档`);
      await loadLibrary();
      if (r.imported.length) openDoc(r.imported[0]);
    } catch (e) {
      toast(`导入失败：${e.message}`, true);
    }
  });
  $('#pdf-prev').addEventListener('click', () => {
    if (state.pdfPage > 1) { state.pdfPage--; renderPdfPane(); }
  });
  $('#pdf-next').addEventListener('click', () => {
    if (state.pdfPage < state.doc.pages) { state.pdfPage++; renderPdfPane(); }
  });
  $('#export-csv').addEventListener('click', () => {
    if (!state.doc) return;
    window.location = `/api/documents/${state.doc.doc_id}/export.csv`;
  });
  $('#download-json').addEventListener('click', () => {
    if (!state.doc) return;
    const blob = new Blob([JSON.stringify(state.doc, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${state.doc.doc_id}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  });
  $('#rerun-eval').addEventListener('click', () => {
    toast('评测需在命令行运行：python3 -m finreport evaluate');
  });
  document.addEventListener('keydown', (e) => {
    if (state.view !== 'review') return;
    if (e.key === 'ArrowLeft') { $('#pdf-prev').click(); }
    if (e.key === 'ArrowRight') { $('#pdf-next').click(); }
  });
}

async function boot() {
  bindTop();
  await loadChart();
  await loadLibrary();
  if (state.docs.length) openDoc(state.docs[0].doc_id);
}

document.addEventListener('DOMContentLoaded', boot);
