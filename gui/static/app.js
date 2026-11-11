'use strict';
const $ = s => document.querySelector(s);
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; };
const svgEl = (tag, attrs) => { const e = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
const esc = s => String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);
const AMBER = '#ff9e2c', TEAL = '#3fb8af', SAND = '#ffd9a8';
const PI_COLORS = [AMBER, TEAL, SAND, '#e36a6a', '#9ad17b', '#b48cff', '#7fe0d8'];

const STEPS = [
  { key: 'request', title: 'Request', backend: 'request', ready: d => d.request, render: renderRequest },
  { key: 'truth', title: 'Truth table', backend: 'request', ready: d => d.request, render: renderTruth },
  { key: 'minimize', title: 'Minimization', backend: 'network', ready: d => d.network, render: renderMinimize },
  { key: 'tree', title: 'Stage tree', backend: 'network', ready: d => d.network, render: renderTree },
  { key: 'transistors', title: 'Transistors', backend: 'network', ready: d => d.network, render: renderTransistors },
  { key: 'euler', title: 'Euler paths', backend: 'euler', ready: d => d.euler, render: renderEuler },
  { key: 'layout', title: 'Layout', backend: 'layout', ready: d => d.layout, render: renderLayout },
  { key: 'checks', title: 'Checks', backend: ['gds', 'drc', 'lvs', 'sim'], ready: d => d.layout && d.switch, render: renderChecks },
  { key: 'summary', title: 'Summary', backend: 'layout', ready: d => d.layout, render: renderSummary },
];
const EXAMPLES = ['!(A & B)', '!((A & B) | C)', '(A & B) | C', 'A ^ B', '(A & B) | (B & C) | (A & C)', '(A & !S) | (B & S)', '!(A | B | C | D)'];

let job = null, data = {}, step = 0, gen = 0, poller = null, autoTimer = null, rendered = -1;

/* ---------------- shell ---------------- */
function init() {
  const ex = $('#examples');
  EXAMPLES.forEach(x => { const b = el('button', null, esc(x)); b.type = 'button'; b.onclick = () => { $('#expr').value = x; $('#expr').focus(); }; ex.appendChild(b); });
  $('#form').onsubmit = e => { e.preventDefault(); start(); };
  $('#expr').oninput = () => { $('#error').textContent = ''; };
  $('#prev').onclick = () => go(step - 1);
  $('#next').onclick = () => go(step + 1);
  $('#replay').onclick = () => show(step, true);
  $('#auto').onchange = () => { if ($('#auto').checked) scheduleAuto(); else clearTimeout(autoTimer); };
  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT') return;
    if (e.key === 'ArrowRight') go(step + 1);
    if (e.key === 'ArrowLeft') go(step - 1);
  });
}

async function start() {
  const expression = $('#expr').value.trim();
  if (!expression) { $('#error').textContent = 'Enter a Boolean function first.'; return; }
  const sizing = document.querySelector('input[name=sizing]:checked').value;
  $('#go').disabled = true;
  try {
    const r = await fetch('/api/design', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expression, sizing }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'The server refused the request.');
    job = { id: j.id }; data = {}; step = 0; rendered = -1;
    $('#intro').style.display = 'none';
    $('#main').classList.remove('idle');
    clearInterval(poller);
    poller = setInterval(poll, 600);
    poll();
  } catch (e) {
    $('#error').textContent = e.message;
  } finally {
    $('#go').disabled = false;
  }
}

async function poll() {
  if (!job) return;
  const r = await fetch('/api/job/' + job.id);
  const j = await r.json();
  Object.assign(job, j);
  data = j.data || {};
  $('#timer').textContent = j.stored ? `loaded ${j.stored.cell} from the make run (${j.stored.built})`
    : j.state === 'running' ? `flow running ${Math.round(j.elapsed)} s` : j.state === 'failed' ? 'flow stopped' : `flow done in ${Math.round(j.elapsed)} s`;
  if (j.state !== 'running') clearInterval(poller);
  if (j.state === 'failed') $('#error').textContent = j.error;
  drawRail();
  if (rendered !== step) show(step);
  else if (STEPS[step].key === 'summary' && j.state !== 'running') show(step);
  else updateChecks();
}

function drawRail() {
  const rail = $('#rail');
  rail.innerHTML = '';
  STEPS.forEach((s, i) => {
    const ready = !!s.ready(data);
    const row = el('div', 'rstep ' + (ready ? 'ready' : 'locked') + (i === step ? ' active' : ''));
    let st = '';
    if (ready && s.key === 'checks' && job && job.state === 'running') st = '<span class="spin"></span>';
    else if (ready) st = '<span style="color:var(--teal)">&#10003;</span>';
    else if (job && job.state === 'running' && [].concat(s.backend).includes(job.current)) st = '<span class="spin"></span>';
    else if (job && job.state === 'failed') st = '<span style="color:var(--bad)">&times;</span>';
    row.innerHTML = `<span class="n">${String(i + 1).padStart(2, '0')}</span><span>${s.title}</span><span class="st">${st}</span>`;
    row.onclick = () => go(i);
    rail.appendChild(row);
  });
  $('#prev').disabled = step === 0;
  $('#next').disabled = step === STEPS.length - 1;
}

function go(i) {
  if (i < 0 || i >= STEPS.length) return;
  step = i;
  drawRail();
  show(i);
}

function show(i, force) {
  const s = STEPS[i];
  gen++;
  clearTimeout(autoTimer);
  $('#num').textContent = String(i + 1).padStart(2, '0');
  $('#title').textContent = s.title;
  const vis = $('#vis'), notes = $('#notes');
  vis.innerHTML = ''; notes.innerHTML = '';
  if (!s.ready(data)) {
    rendered = -1;
    const why = job && job.state === 'failed' ? `The flow stopped before this step: ${esc(job.error || '')}` : `<span class="spin"></span> Waiting for ${s.backend === 'layout' ? 'the layout planner (placement, routing; can take minutes)' : s.backend}…`;
    vis.appendChild(el('div', 'waiting', why));
    return;
  }
  rendered = i;
  const myGen = gen;
  const done = () => { if (myGen === gen) scheduleAuto(); };
  s.render(vis, notes, myGen, done);
}

function scheduleAuto() {
  clearTimeout(autoTimer);
  if (!$('#auto').checked) return;
  autoTimer = setTimeout(() => { if (step < STEPS.length - 1 && STEPS[step + 1].ready(data)) go(step + 1); }, 2400);
}

const later = (g, ms, fn) => setTimeout(() => { if (g === gen) fn(); }, ms);
function kv(pairs) { return '<div class="kv">' + pairs.map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join('') + '</div>'; }
function req() { return data.request.request; }
function bit(tt, r) { return (tt >> r) & 1; }

/* ---------------- 01 request ---------------- */
function renderRequest(vis, notes, g, done) {
  const q = req();
  const lines = [
    ['name', q.name], ['inputs', q.inputs.join(', ')], ['output', q.output], ['expression', q.expression],
    ['sizing', q.sizing], ['truth table', q.truth_table], ['reader', data.request.status],
  ];
  const box = el('div', 'typed caret');
  vis.appendChild(box);
  const text = lines.map(([k, v]) => [k.padEnd(12, ' '), String(v)]);
  let li = 0, ci = 0, html = '';
  const tick = () => {
    if (li >= text.length) { box.classList.remove('caret'); done(); return; }
    const [k, v] = text[li];
    const full = k + v;
    ci++;
    const shown = full.slice(0, ci);
    box.innerHTML = html + `<span class="k">${esc(shown.slice(0, k.length))}</span><span class="${li === 3 ? 'h' : 'v'}">${esc(shown.slice(k.length))}</span>`;
    if (ci >= full.length) { html += `<span class="k">${esc(k)}</span><span class="${li === 3 ? 'h' : 'v'}">${esc(v)}</span>\n`; li++; ci = 0; }
    later(g, 14, tick);
  };
  tick();
  const st = job.stored;
  notes.innerHTML = (st ? `<h3>Stored cell</h3><div class="choice">make already built and checked this function as <span class="mono">${esc(st.cell)}</span> (${esc(st.built)}): every view and report below comes from cells/ and results/, nothing is recomputed.${st.typed_inputs.join() !== st.cell_inputs.join() ? ` Inputs matched by position: ${st.typed_inputs.map((x, i) => esc(x) + ' = ' + esc(st.cell_inputs[i])).join(', ')}.` : ''}</div>` : '') +
    '<h3>What the reader did</h3>' +
    `<p>request_reader.py parsed the expression, found ${q.inputs.length} input${q.inputs.length > 1 ? 's' : ''}, and turned the function into a truth table.</p>` +
    `<div class="choice">Input 0 (${esc(q.inputs[0])}) is the least significant bit of the row index. Every later check compares against this table, never against a cell name.</div>` +
    `<div class="choice">Sizing family <b>${esc(q.sizing)}</b>: ${q.sizing === 'hd' ? 'every finger x1 (NMOS 0.65 um, PMOS 1.0 um), as HD.' : q.sizing === 'stack' ? 'output-stage widths multiplied by series depth, then folded.' : 'x1 output stage, internal stages sized for the fastest worst path (Elmore).'}</div>`;
}

/* ---------------- 02 truth table ---------------- */
function renderTruth(vis, notes, g, done) {
  const q = req(), tt = q.truth_table_value, n = q.inputs.length, rows = 1 << n;
  const t = el('table', 'tt');
  t.innerHTML = '<tr class="in"><th>row</th>' + q.inputs.slice().reverse().map(x => `<th>${esc(x)}</th>`).join('') + `<th>${esc(q.output)}</th></tr>`;
  for (let r = 0; r < rows; r++) {
    const tr = el('tr');
    tr.innerHTML = `<td>${r}</td>` + q.inputs.map((_, i) => `<td>${(r >> i) & 1}</td>`).reverse().join('') + `<td class="o${bit(tt, r)}">${bit(tt, r)}</td>`;
    t.appendChild(tr);
  }
  vis.appendChild(t);
  const trs = t.querySelectorAll('tr:not(.in)');
  trs.forEach((tr, i) => later(g, 80 + i * 70, () => tr.classList.add('in')));
  later(g, 200 + rows * 70, done);
  const ones = [...Array(rows).keys()].filter(r => bit(tt, r));
  const un = data.network ? data.network.unateness : null;
  notes.innerHTML = '<h3>The function</h3>' + kv([['rows', rows], ['output 1 on', ones.length], ['hex', q.truth_table]]) +
    `<p>Minterms: <span class="mono">${ones.join(', ') || 'none'}</span></p>` +
    (un ? '<h3>Unateness</h3>' + kv(Object.entries(un)) + '<p>A positive input needs no inverter; a binate one appears both ways.</p>' : '');
}

/* ---------------- 03 minimization ---------------- */
function kmapLayout(n) {
  const gray = k => k === 0 ? [0] : k === 1 ? [0, 1] : [0, 1, 3, 2];
  const colBits = n >= 2 ? 2 : 1, rowBits = n - colBits;
  return { rows: gray(rowBits), cols: gray(colBits), colBits, rowBits };
}
function piMinterms(pi, inputs) {
  const lits = pi.trim() === '1' ? [] : pi.split('&').map(s => s.trim().replace(/[()]/g, ''));
  const out = [];
  for (let m = 0; m < (1 << inputs.length); m++) {
    if (lits.every(l => { const neg = l.startsWith('!'); const name = neg ? l.slice(1) : l; const i = inputs.indexOf(name); return i >= 0 && ((m >> i) & 1) === (neg ? 0 : 1); })) out.push(m);
  }
  return out;
}
function renderMinimize(vis, notes, g, done) {
  const q = req(), net = data.network, min = net.minimization;
  const chosenSide = /!f|not f|POS/i.test(net.formula_source) ? 'not_f' : 'f';
  let side = chosenSide;
  const tog = el('div', 'toggle');
  const wrap = el('div');
  ['f', 'not_f'].forEach(k => {
    const b = el('button', k === side ? 'on' : '', k === 'f' ? 'f (ones)' : '!f (zeros)');
    b.onclick = () => { side = k; tog.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); paint(); };
    tog.appendChild(b);
  });
  vis.appendChild(tog); vis.appendChild(wrap);
  function paint() {
    const myG = ++gen; rendered = step;
    wrap.innerHTML = '';
    const m = min[side], n = q.inputs.length, L = kmapLayout(n);
    const split = el('div', 'split');
    const km = el('div', 'kmap');
    km.style.gridTemplateColumns = `56px repeat(${L.cols.length}, 54px)`;
    const name = bits => bits.map(i => esc(q.inputs[i])).join('');
    const colVars = [...Array(L.colBits).keys()].reverse(), rowVars = [...Array(L.rowBits).keys()].map(i => i + L.colBits).reverse();
    km.appendChild(el('div', 'hdr', `${name(rowVars)}\\${name(colVars)}`));
    L.cols.forEach(c => km.appendChild(el('div', 'hdr', c.toString(2).padStart(L.colBits, '0'))));
    const cells = {};
    L.rows.forEach(rw => {
      km.appendChild(el('div', 'hdr', L.rowBits ? rw.toString(2).padStart(L.rowBits, '0') : ''));
      L.cols.forEach(c => {
        const mt = (rw << L.colBits) | c;
        const val = side === 'f' ? bit(q.truth_table_value, mt) : 1 - bit(q.truth_table_value, mt);
        const cell = el('div', 'cell', `${val}<div class="dots"></div>`);
        cell.title = 'minterm ' + mt;
        cells[mt] = { cell, val };
        km.appendChild(cell);
      });
    });
    split.appendChild(km);
    const list = el('div', 'pis');
    list.innerHTML = `<h3 style="font-family:var(--head);font-size:11px;letter-spacing:3px;color:var(--teal);font-weight:400;margin:0 0 6px">Prime implicants of ${side === 'f' ? 'f' : '!f'}</h3>`;
    split.appendChild(list);
    wrap.appendChild(split);
    const cover = el('div', 'formula fade');
    let t = 0;
    Object.values(cells).forEach(({ cell, val }, i) => { if (val) setTimeout(() => { if (myG === gen) cell.classList.add('one'); }, (t = 60 * i)); });
    m.prime_implicants.forEach((pi, k) => {
      const color = PI_COLORS[k % PI_COLORS.length];
      const ess = m.essential_prime_implicants.includes(pi);
      const row = el('div', 'pi', `<span class="sw" style="background:${color}"></span>${esc(pi)}${ess ? '<span class="ess">essential</span>' : ''}`);
      list.appendChild(row);
      setTimeout(() => {
        if (myG !== gen) return;
        row.classList.add('in');
        piMinterms(pi, q.inputs).forEach(mt => {
          if (!cells[mt]) return;
          const dot = el('i'); dot.style.background = color;
          cells[mt].cell.querySelector('.dots').appendChild(dot);
          cells[mt].cell.style.borderColor = color;
        });
      }, t + 400 + k * 700);
    });
    setTimeout(() => {
      if (myG !== gen) return;
      cover.innerHTML = `<span style="color:var(--teal);font-size:13px">minimum cover of ${side === 'f' ? 'f' : '!f'}</span><br>${esc(m.minimum_covers.join('  or  '))}`;
      wrap.appendChild(cover);
      const f = el('div', 'formula fade');
      f.style.borderColor = AMBER;
      f.innerHTML = `<span style="color:var(--teal);font-size:13px">chosen formula (${esc(net.formula_source)})</span><br>${esc(net.formula)}`;
      setTimeout(() => { if (myG === gen) { wrap.appendChild(f); done(); } }, 700);
    }, t + 600 + m.prime_implicants.length * 700);
  }
  paint();
  notes.innerHTML = '<h3>Quine-McCluskey</h3>' +
    kv([['f terms / literals', `${min.f.terms} / ${min.f.literals}`], ['!f terms / literals', `${min.not_f.terms} / ${min.not_f.literals}`], ['formulas tried', net.formulas_considered]]) +
    '<p>Both f and its complement are minimized: an SOP of f maps to one CMOS stage structure, an SOP of !f to its dual. Prime implicants are grouped on the map; the essential ones cover a minterm nobody else does.</p>' +
    `<div class="choice">Chosen: ${esc(net.formula_source)}. Among every factoring of the minimum covers, the one with the fewest transistors wins, then fewest inverters, then fewest stages.</div>`;
}

/* ---------------- 04 stage tree ---------------- */
function renderTree(vis, notes, g, done) {
  const net = data.network, root = net.decomposition_tree;
  const stageNames = [...new Set(net.stages.map(s => s.output))];
  const stageColor = s => [AMBER, TEAL, SAND, '#e36a6a', '#9ad17b', '#b48cff'][Math.max(0, stageNames.indexOf(s)) % 6];
  let leaves = 0, depthMax = 0;
  const nodes = [];
  (function lay(nd, d, parent) {
    const kids = nd.children || [];
    const me = { nd, d, parent, x: 0 };
    nodes.push(me);
    depthMax = Math.max(depthMax, d);
    if (!kids.length) { me.x = leaves++; return me; }
    const xs = kids.map(k => lay(k, d + 1, me).x);
    me.x = (Math.min(...xs) + Math.max(...xs)) / 2;
    return me;
  })(root, 0, null);
  const W = Math.max(520, leaves * 110), H = (depthMax + 1) * 100 + 30, dx = W / Math.max(1, leaves), px = n => dx * n.x + dx / 2, py = n => 40 + n.d * 100;
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', style: `max-height:${Math.max(380, H)}px` });
  vis.appendChild(svg);
  const order = [...nodes].sort((a, b) => a.d - b.d || a.x - b.x);
  order.forEach((n, i) => {
    const delay = i * 260;
    if (n.parent) {
      const x1 = px(n.parent), y1 = py(n.parent) + 20, x2 = px(n), y2 = py(n) - 20;
      const len = Math.hypot(x2 - x1, y2 - y1);
      const line = svgEl('path', { d: `M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}`, stroke: '#3fb8af88', fill: 'none', 'stroke-width': 1.5, class: 'draw', style: `--len:${len * 1.3};animation-delay:${delay}ms` });
      svg.appendChild(line);
    }
    const c = stageColor(n.nd.stage), x = px(n), y = py(n);
    const grp = svgEl('g', { class: 'pop', style: `animation-delay:${delay + 150}ms` });
    let label, sub = '';
    if (n.nd.kind === 'lit') { label = n.nd.text || n.nd.gate; }
    else if (n.nd.kind === 'inv') { label = 'NOT'; sub = `${n.nd.gate} -> ${n.nd.stage}`; }
    else { label = (n.nd.op || '').toUpperCase(); sub = n.nd.pull_down ? `pull-down ${n.nd.pull_down}` : ''; }
    const w = n.nd.kind === 'lit' ? 56 : 96;
    grp.appendChild(svgEl('rect', { x: x - w / 2, y: y - 20, width: w, height: 40, rx: n.nd.kind === 'inv' ? 20 : 3, fill: '#0f1c1f', stroke: c, 'stroke-width': n.nd.kind === 'lit' ? 1 : 1.6 }));
    const tx = svgEl('text', { x, y: y + (sub ? -2 : 5), 'text-anchor': 'middle', fill: n.nd.kind === 'lit' ? SAND : c, 'font-size': 14 }); tx.textContent = label; grp.appendChild(tx);
    if (sub) { const st = svgEl('text', { x, y: y + 13, 'text-anchor': 'middle', fill: '#7c9a98', 'font-size': 10 }); st.textContent = sub; grp.appendChild(st); }
    if (n.nd.cut) { const ct = svgEl('text', { x: x + w / 2 + 4, y: y - 12, fill: '#7c9a98', 'font-size': 10 }); ct.textContent = 'cut: ' + n.nd.cut; grp.appendChild(ct); }
    svg.appendChild(grp);
  });
  setTimeout(() => { if (g === gen) done(); }, order.length * 260 + 600);
  const c = net.cost;
  notes.innerHTML = '<h3>Stages</h3>' + net.stages.map(s => `<div class="choice" style="border-left-color:${stageColor(s.output)}"><span class="mono">${esc(s.function)}</span><br><span style="color:var(--muted);font-size:12px">${esc(s.kind)}, ${esc(s.form)}, ${s.transistors} transistors</span></div>`).join('') +
    '<h3>Cost of this decomposition</h3>' + kv([['stages', c.stages], ['inverters', c.inverters], ['transistors', c.transistors], ['max series', Math.max(c.max_series_pull_down, c.max_series_pull_up)]]) +
    '<p>Each CMOS stage inverts. A subtree is cut into its own stage when a stack would exceed three transistors in series, either flipped (built as its dual) or followed by an inverter.</p>';
}

/* ---------------- 05 transistors ---------------- */
function renderTransistors(vis, notes, g, done) {
  const net = data.network;
  const boxes = [];
  net.stages.forEach(st => {
    const card = el('div', 'stagecard fade');
    card.innerHTML = `<div class="fn">${esc(st.function)}</div>`;
    const nets = el('div', 'nets');
    [['pull-up (PMOS)', st.pull_up, 'pmos'], ['pull-down (NMOS)', st.pull_down, 'nmos']].forEach(([title, tree, type]) => {
      const devs = net.devices.filter(d => d.stage === st.output && d.type === type).sort((a, b) => a.leaf - b.leaf);
      let k = 0;
      const build = t => {
        if (t.gate) { const d = devs[k++] || {}; const b = el('div', 'tr' + (type === 'pmos' ? ' p' : ''), `${esc(t.gate)}<b>${d.w_um !== undefined ? d.w_um.toFixed(2) + ' um' : ''}${d.fingers > 1 ? ' x' + d.fingers : ''}</b>`); b.title = d.id || ''; boxes.push(b); return b; }
        const c = el('div', 'sp ' + t.op);
        t.children.forEach(ch => c.appendChild(build(ch)));
        return c;
      };
      const col = el('div', 'net', `<h4>${title}</h4>`);
      col.appendChild(build(tree));
      nets.appendChild(col);
    });
    card.appendChild(nets);
    vis.appendChild(card);
  });
  boxes.forEach((b, i) => later(g, 150 + i * 110, () => b.classList.add('in')));
  later(g, 400 + boxes.length * 110, done);
  const s = net.internal_sizing;
  notes.innerHTML = '<h3>Sizing</h3>' + kv([['family', net.sizing], ['transistors', net.cost.transistors]]) +
    (net.sizing === 'opt' && s ? `<div class="choice">Elmore model fitted to HD inv_1. Worst path ${(s.worst_delay_ns.x1 * 1000).toFixed(1)} ps at x1, ${(s.worst_delay_ns.sized * 1000).toFixed(1)} ps sized.</div>` + Object.entries(s.stages).map(([k, v]) => `<p class="mono">${esc(k)}: NMOS ${v.nmos.join('/')} um, PMOS ${v.pmos.join('/')} um</p>`).join('') : '') +
    (net.sizing === 'stack' ? '<div class="choice">Output stage widths are x1 times series depth; wide devices fold into fingers that fit the row.</div>' : '') +
    (net.sizing === 'hd' ? '<div class="choice">Every NMOS 0.65 um and every PMOS 1.0 um, as HD\'s x1 cells.</div>' : '') +
    '<p>Series boxes stack vertically, parallel ones side by side. The pull-up is the dual of the pull-down, leaf for leaf, so each NMOS/PMOS pair can share one poly column.</p>';
}

/* ---------------- 06 euler ---------------- */
function segs(row) { return Array.isArray(row) ? row : [row]; }
function renderEuler(vis, notes, g, done) {
  const spec = data.euler, ep = spec.euler_paths, byId = {};
  spec.devices.forEach(d => byId[d.id] = d);
  const N = segs(ep.nmos), P = segs(ep.pmos);
  const colW = 74, gapW = 40;
  let x = 30;
  const cols = [], nodesN = [], nodesP = [], strips = [];
  N.forEach((sn, s) => {
    const sp = P[s];
    const startX = x;
    for (let i = 0; i <= sn.devices.length; i++) {
      const nx = x + i * colW;
      nodesN.push({ x: nx, net: sn.nodes[i] });
      nodesP.push({ x: nx, net: sp.nodes[i] });
      if (i < sn.devices.length) {
        const dn = sn.devices[i] && byId[sn.devices[i]], dp = sp.devices[i] && byId[sp.devices[i]];
        cols.push({ x: nx + colW / 2, gate: (dn || dp).gate, hasN: !!dn, hasP: !!dp });
      }
    }
    strips.push([startX, x + sn.devices.length * colW]);
    x += sn.devices.length * colW + gapW;
  });
  const W = Math.max(520, x), H = 280, yP = 70, yN = 210;
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%' });
  vis.appendChild(svg);
  const lbl = (tx, ty, t, fill, size, anchor) => { const e = svgEl('text', { x: tx, y: ty, fill, 'font-size': size || 12, 'text-anchor': anchor || 'middle' }); e.textContent = t; svg.appendChild(e); return e; };
  lbl(8, yP - 34, 'PMOS', AMBER, 11, 'start'); lbl(8, yN + 44, 'NMOS', TEAL, 11, 'start');
  strips.forEach(([a, b], i) => {
    svg.appendChild(svgEl('rect', { x: a, y: yP - 18, width: b - a, height: 36, fill: '#ff9e2c1f', stroke: '#ff9e2c55', class: 'pop', style: `animation-delay:${i * 200}ms` }));
    svg.appendChild(svgEl('rect', { x: a, y: yN - 18, width: b - a, height: 36, fill: '#3fb8af1f', stroke: '#3fb8af55', class: 'pop', style: `animation-delay:${i * 200}ms` }));
  });
  cols.forEach((c, i) => {
    const d = 300 + i * 220;
    const len = yN - yP + 60;
    svg.appendChild(svgEl('line', { x1: c.x, x2: c.x, y1: c.hasP ? yP - 30 : 140, y2: c.hasN ? yN + 30 : 140, stroke: '#e36a6a', 'stroke-width': 6, class: 'draw', style: `--len:${len};animation-delay:${d}ms` }));
    const t = lbl(c.x, 145, c.gate, SAND, 13); t.setAttribute('class', 'pop'); t.style.animationDelay = d + 200 + 'ms';
  });
  [[nodesP, yP, AMBER], [nodesN, yN, TEAL]].forEach(([ns, y, c]) => ns.forEach((n, i) => {
    if (!n.net) return;
    const t = lbl(n.x, y + 4, n.net, c, 11); t.setAttribute('class', 'pop'); t.style.animationDelay = 300 + i * 220 + 'ms';
  }));
  const tStart = 400 + cols.length * 220;
  [[nodesP, yP, AMBER], [nodesN, yN, TEAL]].forEach(([ns, y, c]) => {
    const pts = ns.filter(n => n.net).map(n => n.x);
    if (pts.length < 2) return;
    const dot = svgEl('circle', { r: 5, cx: pts[0], cy: y - 24, fill: c, opacity: 0 });
    svg.appendChild(dot);
    pts.forEach((px, k) => later(g, tStart + k * 260, () => { dot.setAttribute('opacity', 1); dot.setAttribute('cx', px); dot.style.transition = 'cx .25s'; }));
  });
  later(g, tStart + Math.max(nodesN.length, nodesP.length) * 260 + 400, done);
  const pl = spec.placement;
  notes.innerHTML = '<h3>Placement</h3>' + kv([['stage order', pl.stage_order.join(' -> ')], ['mirrored', pl.mirrored.join(', ') || 'none'], ['columns', pl.columns], ['diffusion breaks', pl.diffusion_breaks], ['repeated nets in a row', pl.repeated_nets_in_rows], ['wiring slots', pl.wiring_slots], ['alternatives kept', (spec.alternative_euler_paths || []).length]]) +
    '<p>Each column is one poly gate shared by an NMOS and a PMOS. A dual trail visits every transistor once in both rows with the same gate order, so neighbouring transistors share diffusion.</p>' +
    `<div class="choice">${esc(pl.method)}: fewest diffusion breaks first, then the fewest repeated nets in a row, then the least wiring.</div>`;
}

/* ---------------- 07 layout ---------------- */
const LAYERS = [
  ['nwell', '#1d3b40', .6, 'n-well'], ['nsdm', TEAL, .05, 'n implant'], ['psdm', AMBER, .05, 'p implant'], ['hvtp', SAND, .04, 'high Vt'],
  ['diff', TEAL, .7, 'diffusion'], ['poly', '#e36a6a', .85, 'poly'], ['npc', '#e36a6a', .07, 'npc'], ['licon', SAND, 1, 'licon'],
  ['li1', '#7fe0d8', .42, 'li1'], ['mcon', '#ffffff', 1, 'mcon'], ['met1', '#6f8cff', .38, 'met1'], ['li1_pin', AMBER, 0, 'pins'],
];
function renderLayout(vis, notes, g, done) {
  const L = data.layout;
  const shapes = L.shapes.filter(s => LAYERS.some(l => l[0] === s.layer));
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  shapes.forEach(s => { x0 = Math.min(x0, s.box[0]); y0 = Math.min(y0, s.box[1]); x1 = Math.max(x1, s.box[2]); y1 = Math.max(y1, s.box[3]); });
  const pad = .1, S = 200, W = (x1 - x0 + 2 * pad) * S, H = (y1 - y0 + 2 * pad) * S;
  const X = v => (v - x0 + pad) * S, Y = v => (y1 + pad - v) * S;
  const legend = el('div', 'legend'), now = el('div', 'layer-now'), box = el('div', 'layoutbox');
  vis.appendChild(legend); vis.appendChild(now); vis.appendChild(box);
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', style: 'max-height:520px' });
  box.appendChild(svg);
  svg.appendChild(svgEl('rect', { x: X(0), y: Y(L.height_um), width: L.width_um * S, height: L.height_um * S, fill: 'none', stroke: '#ff9e2c66', 'stroke-dasharray': '6 5' }));
  const groups = {};
  LAYERS.forEach(([name, color, op, title]) => {
    const list = shapes.filter(s => s.layer === name).sort((a, b) => a.box[0] - b.box[0] || a.box[1] - b.box[1]);
    if (!list.length) return;
    const grp = svgEl('g', {});
    svg.appendChild(grp);
    groups[name] = { grp, list, color, op, title };
    const b = el('button', null, `<i style="background:${color};opacity:${Math.max(op, .5)}"></i>${title}`);
    b.onclick = () => { b.classList.toggle('off'); grp.style.display = b.classList.contains('off') ? 'none' : ''; };
    legend.appendChild(b);
  });
  let t = 0;
  Object.entries(groups).forEach(([name, G]) => {
    const per = Math.min(90, 1100 / G.list.length);
    later(g, t, () => { now.textContent = `drawing ${G.title}: ${G.list.length} shape${G.list.length > 1 ? 's' : ''}`; });
    G.list.forEach((s, i) => {
      const [a, b, c, d] = s.box;
      const attrs = { x: X(a), y: Y(d), width: (c - a) * S, height: (d - b) * S, class: 'pop', style: `animation-delay:${t + i * per}ms` };
      if (name === 'li1_pin') Object.assign(attrs, { fill: 'none', stroke: AMBER, 'stroke-width': 2, 'stroke-dasharray': '4 3' });
      else Object.assign(attrs, { fill: G.color, 'fill-opacity': G.op, stroke: G.op < .1 ? G.color : 'none', 'stroke-opacity': .35 });
      const r = svgEl('rect', attrs);
      if (s.net) { const tt = svgEl('title', {}); tt.textContent = `${name} ${s.net}`; r.appendChild(tt); }
      G.grp.appendChild(r);
    });
    t += G.list.length * per + 450;
  });
  later(g, t, () => {
    now.textContent = 'labels';
    (L.labels || []).forEach((lb, i) => {
      const [px, py] = lb.position_um;
      const e = svgEl('text', { x: X(px), y: Y(py) + 5, fill: SAND, 'font-size': 18, 'text-anchor': 'middle', class: 'pop', style: `animation-delay:${i * 80}ms` });
      e.textContent = lb.text; svg.appendChild(e);
    });
    later(g, 600, () => { now.textContent = `${L.width_sites} sites, ${L.width_um.toFixed(2)} x ${L.height_um.toFixed(2)} um`; done(); });
  });
  const binding = {};
  (L.gate_gaps || []).forEach(gp => binding[gp.binding] = (binding[gp.binding] || 0) + 1);
  const reasons = {};
  (L.retries || []).forEach(r => { const k = r.reason.replace(/ at .*| \(.*/, '').slice(0, 60); reasons[k] = (reasons[k] || 0) + 1; });
  const licons = (L.contacts || []).filter(c => c.type === 'diffusion').reduce((a, c) => a + (c.licons || 1), 0);
  notes.innerHTML = '<h3>Result</h3>' + kv([['width', `${L.width_sites} sites`], ['narrowest placement', `${L.minimum_width_sites} sites`], ['router', L.router || 'fixed'], ['met1 vias', L.vias], ['diffusion licons', licons], ['attempts before this one', (L.retries || []).length]]) +
    '<h3>What set the gate pitches</h3>' + kv(Object.entries(binding).map(([k, v]) => [k.replace(/_/g, ' '), v + ' gap' + (v > 1 ? 's' : '')])) +
    (Object.keys(reasons).length ? '<h3>Why narrower attempts failed</h3>' + Object.entries(reasons).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([k, v]) => `<div class="choice">${esc(k)} <span style="color:var(--muted)">(${v}x)</span></div>`).join('') : '<div class="choice">The first attempt routed: narrowest placement, li1 only.</div>') +
    (L.router === 'negotiated' ? `<p>The negotiated router placed the diffusion contacts itself and resolved overlaps in ${L.negotiation_iterations} iteration${L.negotiation_iterations > 1 ? 's' : ''}.</p>` : '') +
    '<p>Click a layer in the legend to hide it.</p>';
}

/* ---------------- 08 checks (real) ---------------- */
const CHECKS = [
  { key: 'switch', title: 'Switch level', sub: 'switch_level_check.py on the network and on the Euler order' },
  { key: 'planner', title: 'Planner geometry', sub: 'prepare_layout.py: li.3 / met1.2 spacing, notches, li.1 / met1.1 width' },
  { key: 'drc', title: 'DRC and layer check', sub: 'Magic full DRC in 24 tapped-row abutment contexts, broken control, implant and npc coverage' },
  { key: 'lvs', title: 'LVS and RC-LVS', sub: 'Magic extraction, Netgen against the schematic, then with resistance' },
  { key: 'sim', title: 'Transistor level', sub: 'ngspice on the RC netlist: every row, every single-input edge' },
];
let checksView = null;
function checkState(key) {
  const rp = checksView && checksView.replay;
  if (rp && !rp.revealed.has(key)) return rp.running === key ? 'running' : 'queued';
  const failedHere = job && job.state === 'failed';
  const live = (job && job.live && job.live[key]) || null;
  const res = { switch: data.switch, planner: data.layout, drc: data.drc, lvs: data.lvs, sim: data.sim }[key];
  if (key === 'switch') return res ? (res.network.passed && res.reordered.passed ? 'pass' : 'fail') : 'queued';
  if (key === 'planner') return res ? 'pass' : (failedHere ? 'fail' : 'queued');
  if (res) return res.passed ? 'pass' : 'fail';
  if (live && live.running) return 'running';
  if (key === 'drc' && job && (job.current === 'gds')) return 'running';
  return failedHere ? 'stopped' : 'queued';
}
function renderChecks(vis, notes, g, done) {
  const wrap = el('div', 'cks');
  vis.appendChild(wrap);
  checksView = { g, done, cards: {}, drawn: {}, finished: false, wrap };
  CHECKS.forEach(c => {
    const card = el('div', 'ck queued');
    card.innerHTML = `<div class="ck-head"><div><div class="ck-title">${c.title}</div><div class="ck-sub">${esc(c.sub)}</div></div>` +
      `<span class="ck-time"></span><span class="badge">queued</span></div><div class="ck-bar"><div class="fill"></div></div><pre class="term"></pre><div class="ck-result"></div>`;
    wrap.appendChild(card);
    checksView.cards[c.key] = card;
  });
  if (job.stored) {
    // A stored cell: replay the real report lines of the make run, then show the results.
    const rp = checksView.replay = { revealed: new Set(), running: null, lines: {} };
    const replay = Object.assign({ planner: ['li.3 and met1.2 spacing held', 'same-net notches filled', 'li.1 and met1.1 width held'] }, job.stored.replay);
    let t = 250;
    CHECKS.forEach(c => {
      const lines = replay[c.key] || [];
      const per = Math.max(45, Math.min(160, 1600 / Math.max(1, lines.length)));
      later(g, t, () => { rp.running = c.key; rp.lines[c.key] = []; updateChecks(); });
      lines.forEach((line, k) => later(g, t + 120 + k * per, () => { rp.lines[c.key].push(line); updateChecks(); }));
      t += 120 + lines.length * per + 250;
      later(g, t, () => { rp.revealed.add(c.key); rp.running = null; updateChecks(); });
      t += 200;
    });
    notes.innerHTML = '<h3>Results of the make run</h3>' +
      `<p>These are the reports make wrote for ${esc(job.stored.cell)}: results/switch_level, results/drc, results/lvs and results/transistor_level. Each card replays its report's lines, then shows its results.</p>` +
      '<div class="choice">For a function make has not built, the same cards run the real checks live on the new cell.</div>';
  } else {
    notes.innerHTML = '<h3>These checks really run</h3>' +
      '<p>After the layout, the server draws the GDS and runs the flow\'s own check code on this cell: Magic for DRC (verify_drc.py\'s contexts, in this run\'s own folder), verify_lvs.py for LVS and RC-LVS, transistor_level_check.py for the simulation. Each card streams the tool\'s output while it runs.</p>' +
      '<div class="choice">DRC takes a few minutes: Magic checks the cell in 24 placements beside HD cells, plus a deliberately broken control that must fail.</div>' +
      '<p>Characterization (7 x 7 timing tables) is left to make char.</p>';
  }
  updateChecks();
}
function updateChecks() {
  const v = checksView;
  if (!v || STEPS[step].key !== 'checks' || v.g !== gen) return;
  let final = 0;
  CHECKS.forEach(c => {
    const card = v.cards[c.key], st = checkState(c.key);
    card.className = 'ck ' + st;
    const badge = card.querySelector('.badge');
    badge.textContent = { queued: 'queued', running: 'running', pass: 'pass', fail: 'fail', stopped: 'not run' }[st];
    badge.className = 'badge ' + (st === 'pass' ? 'pass' : st === 'fail' ? 'fail' : st === 'running' ? 'run' : '');
    const rp = v.replay;
    const live = rp ? { lines: rp.lines[c.key] || [], done: (rp.lines[c.key] || []).length, total: c.key === 'drc' ? ((job.stored.replay.drc || []).length || null) : null }
      : (job.live || {})[c.key] || (c.key === 'drc' && job.current === 'gds' ? job.live.gds : null);
    const term = card.querySelector('.term'), fill = card.querySelector('.fill'), bar = card.querySelector('.ck-bar');
    if (live && live.lines && live.lines.length) { term.textContent = live.lines.join('\n'); term.scrollTop = term.scrollHeight; }
    if (st === 'running') {
      if (live && live.total) { bar.classList.remove('indet'); fill.style.width = Math.round(100 * live.done / live.total) + '%'; }
      else bar.classList.add('indet');
    } else if (st === 'pass' || st === 'fail') { bar.classList.remove('indet'); fill.style.width = '100%'; }
    const secs = job.seconds && job.seconds[c.key];
    card.querySelector('.ck-time').textContent = rp ? (st === 'pass' || st === 'fail' ? 'make run' : '') : secs ? `${secs} s` : '';
    if ((st === 'pass' || st === 'fail') && !v.drawn[c.key]) {
      v.drawn[c.key] = true;
      card.querySelector('.ck-result').appendChild(RESULT[c.key]());
    }
    if (['pass', 'fail', 'stopped'].includes(st)) final++;
  });
  if (final === CHECKS.length && !v.finished) { v.finished = true; v.done(); }
}
const tile = (cls, html, title) => { const t = el('div', 'tile ' + cls, html); if (title) t.title = title; return t; };
const RESULT = {
  switch() {
    const box = el('div', 'res');
    [['network', 'network'], ['reordered', 'Euler order']].forEach(([k, label]) => {
      const r = data.switch[k];
      const row = el('div', 'tiles');
      row.appendChild(el('span', 'tl', label));
      (r.rows || []).forEach(x => {
        const bits = req().inputs.map(n => x.inputs[n]).reverse().join('');
        const rail = x.output_connected_to_VPWR ? 'VPWR' : x.output_connected_to_VGND ? 'VGND' : 'float';
        row.appendChild(tile(x.passed ? 'ok' : 'bad', `<b>${bits}</b>${rail === 'VPWR' ? '1' : rail === 'VGND' ? '0' : '?'}`, `inputs ${bits}: Y on ${rail}, expected ${x.expected_rail}`));
      });
      box.appendChild(row);
    });
    box.appendChild(el('div', 'rnote', `${data.switch.network.rows.length} rows: in every one the output connects only to the rail the truth table asks for, with no VPWR-to-VGND path.`));
    return box;
  },
  planner() {
    const L = data.layout, n = L.shapes.length;
    return el('div', 'res', `<div class="rnote">${n} shapes planned. Every net's li1 and met1 keep li.3 and met1.2 from other nets, same-net notches are filled, and no neck is narrower than li.1 or met1.1; an attempt that broke one would have failed in the planner.</div>`);
  },
  drc() {
    const r = data.drc, box = el('div', 'res');
    if (r.error) { box.appendChild(el('div', 'rnote bad', esc(r.error))); return box; }
    const cases = r.cases.filter(c => c.kind === 'abutment');
    const nb = ['sky130_fd_sc_hd__tapvpwrvgnd_1', 'sky130_fd_sc_hd__inv_1', 'sky130_fd_sc_hd__fill_1'];
    const grid = el('div', 'drcgrid');
    grid.appendChild(el('span', 'tl', ''));
    ['R0', 'MY', 'MX above', 'MX below'].forEach(h => grid.appendChild(el('span', 'th2', h)));
    nb.forEach(n => {
      [['left/bottom', 'beside'], ['right/top', 'swapped']].forEach(([pos, lab]) => {
        grid.appendChild(el('span', 'tl', `${n.replace('sky130_fd_sc_hd__', '')} ${lab}`));
        ['R0', 'MY', 'MX_above', 'MX_below'].forEach(how => {
          const c = cases.find(x => x.neighbor === n && x.placement === how && x.generated_position === pos);
          grid.appendChild(c ? tile(c.passed ? 'ok' : 'bad', c.drc_count === 0 ? '0' : String(c.drc_count), `${c.name}: ${c.drc_count} DRC errors`) : tile('', '-'));
        });
      });
    });
    box.appendChild(grid);
    const ctl = r.cases.find(c => c.kind === 'broken_drc');
    const layers = Object.values(r.layers)[0] || { checks: {} };
    const lines = [
      [ctl && ctl.passed, `broken control (a 0.05 um met1 stripe): ${ctl ? ctl.drc_count : '?'} error${ctl && ctl.drc_count === 1 ? '' : 's'}, ${ctl && ctl.passed ? 'caught' : 'NOT caught'}`],
      ...Object.entries(layers.checks).map(([k, x]) => [x.passed, k.replace(/_/g, ' ')]),
      [r.broken_layer_cases.every(b => b.rejected), `layer check rejects ${r.broken_layer_cases.filter(b => b.rejected).length} of ${r.broken_layer_cases.length} copies with a layer removed`],
    ];
    box.appendChild(el('div', 'checklist', lines.map(([ok, t]) => `<div class="${ok ? 'ok' : 'bad'}"><span>${ok ? '&#10003;' : '&times;'}</span>${esc(t)}</div>`).join('')));
    return box;
  },
  lvs() {
    const r = data.lvs, box = el('div', 'res');
    if (r.error) { box.appendChild(el('div', 'rnote bad', esc(r.error))); return box; }
    const rc = r.rc || {};
    const lines = [
      [r.lvs_passed, 'LVS: the extracted devices and nets match schematic.spice'],
      [rc.collapsed_lvs_passed, `RC-LVS: with ${rc.resistors} resistors and ${rc.capacitors} capacitors extracted, the collapsed netlist still matches`],
    ];
    box.appendChild(el('div', 'checklist', lines.map(([ok, t]) => `<div class="${ok ? 'ok' : 'bad'}"><span>${ok ? '&#10003;' : '&times;'}</span>${esc(t)}</div>`).join('')));
    box.appendChild(el('div', 'rnote', `Ports: <span class="mono">${(r.ports || []).join(' ')}</span>. The cell alone, without taps, shows ${r.isolated_drc_count} DRC errors (${(r.isolated_drc_why || []).join(', ')}): well-tap and latch-up rules only, which the tapped rows above satisfy.`));
    return box;
  },
  sim() {
    const r = data.sim, box = el('div', 'res');
    if (r.error) { box.appendChild(el('div', 'rnote bad', esc(r.error))); return box; }
    const row = el('div', 'tiles');
    row.appendChild(el('span', 'tl', 'DC'));
    r.dc.forEach(x => {
      const bits = req().inputs.map(n => x.inputs[n]).reverse().join('');
      row.appendChild(tile(x.passed ? 'ok' : 'bad', `<b>${bits}</b>${x.voltage_v !== undefined ? x.voltage_v.toFixed(2) : '?'}`, `inputs ${bits}: Y = ${x.voltage_v} V, expected ${x.expected}`));
    });
    box.appendChild(row);
    const edges = r.transient || [], ok = edges.filter(e => e.passed).length;
    const glitches = edges.filter(e => e.worst_glitch_v !== null && e.worst_glitch_v !== undefined);
    const worst = glitches.length ? glitches.reduce((w, e) => Math.min(w, e.expected_before ? e.worst_glitch_v : r.supply_v - e.worst_glitch_v), Infinity) : null;
    box.appendChild(el('div', 'checklist',
      `<div class="${r.dc_passed ? 'ok' : 'bad'}"><span>${r.dc_passed ? '&#10003;' : '&times;'}</span>${r.dc.length} operating points within 10 % of the right rail</div>` +
      `<div class="${r.transient_passed ? 'ok' : 'bad'}"><span>${r.transient_passed ? '&#10003;' : '&times;'}</span>${ok} of ${edges.length} single-input edges settle to the right rail${worst !== null ? `; where Y must hold, it stays at least ${worst.toFixed(2)} V from the far rail` : ''}</div>`));
    box.appendChild(el('div', 'rnote', `${r.supply_v} V, ${r.temperature_c} C, ${(r.slew_table_ns * 1000).toFixed(0)} ps input slew, ${(r.load_table_pf * 1000).toFixed(2)} fF load, from the HD Liberty.`));
    return box;
  },
};

/* ---------------- 09 summary ---------------- */
function renderSummary(vis, notes, g, done) {
  const q = req(), net = data.network, L = data.layout;
  const stats = el('div', 'stats');
  const verdict = r => !r ? 'running' : r.passed ? 'pass' : 'fail';
  [['function', q.expression], ['family', q.sizing], ['transistors', net.cost.transistors], ['stages', net.cost.stages], ['width', `${L.width_sites} sites`], ['area', `${(L.width_um * L.height_um).toFixed(2)} um2`], ['met1 vias', L.vias], ['router', L.router || 'fixed'],
   ['DRC', verdict(data.drc)], ['LVS', verdict(data.lvs)], ['transistor level', verdict(data.sim)]]
    .forEach(([l, v], i) => { const s = el('div', 'stat fade', `<div class="l">${esc(l)}</div><div class="v">${esc(v)}</div>`); s.style.animationDelay = i * 120 + 'ms'; stats.appendChild(s); });
  vis.appendChild(stats);
  const again = el('button', null, 'Design another');
  again.style.cssText = 'margin-top:24px;background:var(--amber);color:var(--bg);border:none;font-family:var(--head);letter-spacing:3px;font-size:12px;padding:12px 20px;cursor:pointer';
  again.onclick = () => { $('#expr').focus(); $('#expr').select(); };
  vis.appendChild(again);
  later(g, 1200, done);
  notes.innerHTML = job.stored
    ? '<h3>Files</h3><p class="mono" style="font-size:12px;word-break:break-all">cells/' + esc(job.stored.cell) + '/</p><p>The cell make built (' + esc(job.stored.built) + '), with its reports under results/.</p>'
    : '<h3>Files</h3><p class="mono" style="font-size:12px;word-break:break-all">build/gui/' + esc(job.id) + '/</p><p>network.json, layout_spec.json, the layout plan, the GDS and the check reports of this run stay there. Nothing in cells/ or results/ changed.</p>';
}

init();
