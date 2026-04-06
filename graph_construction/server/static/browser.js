/* browser.js — sidebar list + on-demand graph loading + inline Sankey */

'use strict';

/* =========================================================================
   Shared state
   ========================================================================= */
let allGraphs          = [];
let activeId           = null;   // instance_id of selected graph, or null
let sankeyActive       = false;  // true when Sankey pane is showing
let bayesActive        = false;  // true when Bayesian pane is showing
let dataSourceExpanded = false;

/* =========================================================================
   Bootstrap
   ========================================================================= */
async function init() {
    await loadConfig();
    wireSearch();
    wireToggles();
    wireEnterKey();
    skWireControls();
    byWireControls();
}

/* =========================================================================
   Data source configuration
   ========================================================================= */
async function loadConfig() {
    try {
        const res  = await fetch('/api/config');
        const data = await res.json();
        if (data.trajs) {
            document.getElementById('trajsInput').value  = data.trajs;
            document.getElementById('reportInput').value = data.eval_report;
            updateDataSourceLabel(data.trajs);
            await loadGraphList();
        } else {
            setDataSourceExpanded(true);
        }
    } catch (_) {
        setDataSourceExpanded(true);
    }
}

function toggleDataSource() { setDataSourceExpanded(!dataSourceExpanded); }

function setDataSourceExpanded(open) {
    dataSourceExpanded = open;
    document.getElementById('dataSourceBody').style.display = open ? 'flex' : 'none';
    document.getElementById('dsChevron').textContent = open ? '▴' : '▾';
}

function updateDataSourceLabel(trajsPath) {
    const parts = trajsPath.replace(/\\/g, '/').split('/').filter(Boolean);
    const short = parts.length ? parts[parts.length - 1] : trajsPath;
    document.getElementById('dataSourceLabel').textContent = short || 'Data source';
}

async function applyDataSource() {
    const trajs      = document.getElementById('trajsInput').value.trim();
    const evalReport = document.getElementById('reportInput').value.trim();
    const errEl      = document.getElementById('dsError');
    const btn        = document.getElementById('dsApplyBtn');

    errEl.style.display = 'none';
    if (!trajs) { showDsError('Trajectories path is required.'); return; }

    btn.disabled    = true;
    btn.textContent = 'Loading…';

    try {
        const res  = await fetch('/api/config', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ trajs, eval_report: evalReport }),
        });
        const data = await res.json();
        if (!res.ok) { showDsError(data.error || `Server error (HTTP ${res.status})`); return; }

        updateDataSourceLabel(data.trajs);
        activeId = null;
        clearGraphPane();
        setDataSourceExpanded(false);

        // Invalidate Sankey cache so next view triggers a fresh fetch
        skRawData = null;
        byRawData = null;
        bySelectedFeature = null;

        await loadGraphList();

        // If Sankey is currently showing, reload it with fresh data
        if (sankeyActive) skLoad();
        if (bayesActive) byLoad(true);

    } catch (err) {
        showDsError(`Request failed: ${err.message}`);
    } finally {
        btn.disabled    = false;
        btn.textContent = 'Load';
    }
}

function showDsError(msg) {
    const el = document.getElementById('dsError');
    el.textContent   = msg;
    el.style.display = 'block';
}

function wireEnterKey() {
    ['trajsInput', 'reportInput'].forEach(id => {
        document.getElementById(id).addEventListener('keydown', e => {
            if (e.key === 'Enter') applyDataSource();
        });
    });
}

/* =========================================================================
   Graph list
   ========================================================================= */
async function loadGraphList() {
    document.getElementById('graphList').innerHTML =
        '<div class="placeholder" style="position:relative"><div class="spinner"></div></div>';
    document.getElementById('stats').textContent = 'Loading…';

    try {
        const res = await fetch('/api/graphs');
        allGraphs = await res.json();
        renderStats(allGraphs);
        renderList(allGraphs);
        const q = document.getElementById('searchInput').value.toLowerCase();
        if (q) renderList(allGraphs.filter(g => g.instance_id.toLowerCase().includes(q)));
    } catch (_) {
        document.getElementById('graphList').innerHTML =
            '<div class="placeholder" style="position:relative">Failed to load graphs.</div>';
        document.getElementById('stats').textContent = '—';
    }
}

function renderStats(graphs) {
    const total      = graphs.length;
    const resolved   = graphs.filter(g => g.status === 'resolved').length;
    const unresolved = graphs.filter(g => g.status === 'unresolved').length;
    const hasReport  = graphs.some(g => g.status !== 'none' && g.status !== 'unknown');
    document.getElementById('stats').textContent = hasReport
        ? `${total} total · ${resolved} resolved · ${unresolved} unresolved`
        : `${total} total`;
}

function renderList(graphs) {
    const el = document.getElementById('graphList');
    if (!graphs.length) {
        el.innerHTML = '<div class="placeholder" style="position:relative;padding:30px">No results</div>';
        return;
    }
    el.innerHTML = graphs.map(g => {
        const status     = g.status || 'unknown';
        const badgeClass = ['resolved', 'unresolved', 'unsubmitted'].includes(status) ? status : 'unknown';
        const showBadge  = status !== 'none' && status !== 'unknown';
        const steps      = g.step_count != null ? `${g.step_count} steps` : '';
        const diff       = g.difficulty && g.difficulty !== 'unknown' ? escHtml(g.difficulty) : '';
        const metaParts  = [steps, diff].filter(Boolean).join(' · ');
        return `
        <div class="graph-item${g.instance_id === activeId ? ' active' : ''}"
             data-id="${escHtml(g.instance_id)}"
             onclick="selectGraph('${escHtml(g.instance_id)}')">
            <div class="item-title" title="${escHtml(g.instance_id)}">${escHtml(g.instance_id)}</div>
            <div class="item-meta">
                ${showBadge ? `<span class="badge badge-${badgeClass}">${escHtml(status)}</span>` : ''}
                ${metaParts ? `<span>${metaParts}</span>` : ''}
            </div>
        </div>`;
    }).join('');
}

/* =========================================================================
   Search
   ========================================================================= */
function wireSearch() {
    document.getElementById('searchInput').addEventListener('input', e => {
        const q = e.target.value.toLowerCase();
        renderList(allGraphs.filter(g => g.instance_id.toLowerCase().includes(q)));
    });
}

/* =========================================================================
   Graph view toggles
   ========================================================================= */
function wireToggles() {
    ['filterCdToggle', 'thoughtQuotesToggle', 'nodeVerbosityToggle',
     'observationToggle', 'uniqueThinkToggle'].forEach(id => {
        document.getElementById(id).addEventListener('change', () => {
            if (activeId) loadGraph(activeId);
        });
    });
}

const filterCd        = () => document.getElementById('filterCdToggle').checked;
const thoughtQuotes   = () => document.getElementById('thoughtQuotesToggle').checked;
const nodeVerbosity   = () => document.getElementById('nodeVerbosityToggle').checked;
const showObservation = () => document.getElementById('observationToggle').checked;
const uniqueThink     = () => document.getElementById('uniqueThinkToggle').checked;

/* =========================================================================
   Graph pane
   ========================================================================= */
function selectGraph(instanceId) {
    // Switch away from Sankey if needed
    if (sankeyActive) hideSankeyPane();
    if (bayesActive) hideBayesPane();

    activeId = instanceId;
    document.querySelectorAll('.graph-item').forEach(el =>
        el.classList.toggle('active', el.dataset.id === instanceId)
    );
    document.getElementById('sankeyListItem').classList.remove('active');
    document.getElementById('bayesListItem').classList.remove('active');
    loadGraph(instanceId);
}

function showLoading() {
    const pane   = document.getElementById('graphPane');
    const iframe = pane.querySelector('iframe');
    if (iframe) iframe.remove();
    let ph = pane.querySelector('.placeholder');
    if (!ph) { ph = document.createElement('div'); ph.className = 'placeholder'; pane.appendChild(ph); }
    ph.innerHTML = '<div class="spinner"></div><span>Rendering graph…</span>';
}

function clearGraphPane() {
    document.getElementById('graphPane').innerHTML =
        '<div class="placeholder">'
        + '<span class="placeholder-icon">📊</span>'
        + '<p>Select a graph from the list</p>'
        + '</div>';
}

async function loadGraph(instanceId) {
    showLoading();
    const params = new URLSearchParams({
        id:               instanceId,
        filter_cd:        filterCd(),
        thought_quotes:   thoughtQuotes(),
        node_verbosity:   nodeVerbosity(),
        show_observation: showObservation(),
        unique_think:     uniqueThink(),
    });
    try {
        const res = await fetch(`/api/graph?${params}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        injectGraph(await res.text());
    } catch (err) {
        const ph = document.getElementById('graphPane').querySelector('.placeholder');
        if (ph) ph.innerHTML = `<span style="color:#e74c3c">⚠ ${escHtml(err.message)}</span>`;
    }
}

function injectGraph(html) {
    const pane = document.getElementById('graphPane');
    const ph   = pane.querySelector('.placeholder');
    if (ph) ph.remove();
    let iframe = pane.querySelector('iframe');
    if (!iframe) {
        iframe = document.createElement('iframe');
        iframe.sandbox = 'allow-scripts allow-same-origin';
        pane.appendChild(iframe);
    }
    const doc = iframe.contentDocument || iframe.contentWindow.document;
    doc.open(); doc.write(html); doc.close();
}

/* =========================================================================
   Sankey pane — show/hide
   ========================================================================= */
function selectSankey() {
    // Deselect any active graph item
    activeId = null;
    if (bayesActive) hideBayesPane();
    document.querySelectorAll('.graph-item').forEach(el => el.classList.remove('active'));
    document.getElementById('sankeyListItem').classList.add('active');
    document.getElementById('bayesListItem').classList.remove('active');

    showSankeyPane();
}

function showSankeyPane() {
    sankeyActive = true;
    document.getElementById('graphPane').style.display  = 'none';
    document.getElementById('bayesPane').style.display  = 'none';
    document.getElementById('sankeyPane').style.display = 'flex';
    // Fetch data if not yet loaded, otherwise redraw
    if (!skRawData) {
        skLoad();
    } else {
        skDraw();
    }
}

function hideSankeyPane() {
    sankeyActive = false;
    document.getElementById('sankeyPane').style.display = 'none';
    document.getElementById('graphPane').style.display  = '';
}

/* =========================================================================
   Sankey — data + state
   ========================================================================= */
const SK_PHASE_COLOR = {
    localization: '#C5B3F0',
    patch:        '#FCC9B0',
    validation:   '#A8E6F0',
    general:      '#CFE0F6',
};
const SK_PHASE_STROKE = {
    localization: '#9b7fe8',
    patch:        '#f5956a',
    validation:   '#5bbfd6',
    general:      '#7aaee8',
};
const SK_PHASE_RIBBON = {
    localization: '#b89fe8',
    patch:        '#f5aa80',
    validation:   '#78d0e8',
    general:      '#90bce8',
};
const SK_PHASE_ORDER = ['localization', 'patch', 'validation', 'general'];

let skRawData      = null;
let skActivePhases = new Set(['localization', 'patch', 'validation', 'general']);

/* =========================================================================
   Sankey — controls wiring
   ========================================================================= */
function skWireControls() {
    // Status filter
    document.getElementById('skStatus').addEventListener('change', skDraw);

    // Slider ↔ number sync for maxSteps
    const msSlider = document.getElementById('skMaxStepsSlider');
    const msNum    = document.getElementById('skMaxStepsNum');
    msSlider.addEventListener('input', () => { msNum.value = msSlider.value; skDraw(); });
    msNum.addEventListener('change', () => {
        const v = Math.max(5, Math.min(60, parseInt(msNum.value, 10) || 30));
        msNum.value = msSlider.value = v;
        skDraw();
    });
    msNum.addEventListener('keydown', e => { if (e.key === 'Enter') msNum.dispatchEvent(new Event('change')); });

    // Slider ↔ number sync for minFlow
    const mfSlider = document.getElementById('skMinFlowSlider');
    const mfNum    = document.getElementById('skMinFlowNum');
    mfSlider.addEventListener('input', () => { mfNum.value = mfSlider.value; skDraw(); });
    mfNum.addEventListener('change', () => {
        const v = Math.max(0, Math.min(50, parseInt(mfNum.value, 10) || 0));
        mfNum.value = mfSlider.value = v;
        skDraw();
    });
    mfNum.addEventListener('keydown', e => { if (e.key === 'Enter') mfNum.dispatchEvent(new Event('change')); });

    // Phase chips
    document.querySelectorAll('.sk-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            const ph = chip.dataset.phase;
            if (skActivePhases.has(ph)) {
                if (skActivePhases.size > 1) {
                    skActivePhases.delete(ph);
                    chip.classList.replace('active', 'inactive');
                }
            } else {
                skActivePhases.add(ph);
                chip.classList.replace('inactive', 'active');
            }
            skDraw();
        });
    });
}

/* =========================================================================
   Sankey — fetch
   ========================================================================= */
async function skLoad() {
    skShowPlaceholder('spinner');
    try {
        const res = await fetch('/api/sankey');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        skRawData = await res.json();
        skDraw();
    } catch (err) {
        skShowPlaceholder(`⚠ ${err.message}`);
    }
}

/* =========================================================================
   Sankey — aggregate
   ========================================================================= */
function skBuildFlowData(statusFilter, maxSteps, minFlow) {
    if (!skRawData) return null;

    // Determine whether any trajectory actually has status information
    const hasStatusData = skRawData.trajectories.some(
        t => t.status && t.status !== 'none' && t.status !== 'unknown'
    );

    // If a specific status is requested but no status data exists, warn via a
    // special sentinel so the caller can show a friendly message rather than
    // an empty diagram.
    if (statusFilter !== 'all' && !hasStatusData) {
        return { noStatusData: true };
    }

    const entries = skRawData.trajectories.filter(t => {
        if (statusFilter === 'all') return true;
        return t.status === statusFilter;
    });

    if (entries.length === 0) return { empty: true };

    const stepCounts  = Array.from({ length: maxSteps }, () => ({}));
    const transitions = Array.from({ length: maxSteps }, () => ({}));

    for (const traj of entries) {
        const phases = traj.phases;
        const len    = Math.min(phases.length, maxSteps);
        for (let s = 0; s < len; s++) {
            const ph = phases[s];
            if (!skActivePhases.has(ph)) continue;
            stepCounts[s][ph] = (stepCounts[s][ph] || 0) + 1;
            if (s + 1 < len) {
                const nph = phases[s + 1];
                if (!skActivePhases.has(nph)) continue;
                const key = `${ph}\u2192${nph}`;
                transitions[s][key] = (transitions[s][key] || 0) + 1;
            }
        }
    }

    let lastNonEmpty = -1;
    for (let s = 0; s < maxSteps; s++) {
        if (Object.keys(stepCounts[s]).length > 0) lastNonEmpty = s;
    }
    if (lastNonEmpty < 0) return { empty: true };

    const trimmedSteps = stepCounts.slice(0, lastNonEmpty + 1);
    const trimmedTrans = transitions.slice(0, lastNonEmpty).map(tmap => {
        const out = {};
        for (const [k, v] of Object.entries(tmap)) {
            if (v >= minFlow) out[k] = v;
        }
        return out;
    });

    return { stepCounts: trimmedSteps, transitions: trimmedTrans, total: entries.length };
}

/* =========================================================================
   Sankey — draw
   ========================================================================= */
function skDraw() {
    if (!skRawData) return;

    const statusFilter = document.getElementById('skStatus').value;
    const maxSteps     = parseInt(document.getElementById('skMaxStepsSlider').value, 10);
    const minFlow      = parseInt(document.getElementById('skMinFlowSlider').value,  10);

    const data = skBuildFlowData(statusFilter, maxSteps, minFlow);

    if (data && data.noStatusData) {
        skUpdateStats(0, 0, 0);
        skShowPlaceholder(
            'No status data available — load an eval report to filter by resolved / unresolved.'
        );
        return;
    }
    if (!data || data.empty) {
        skUpdateStats(0, 0, 0);
        skShowPlaceholder('No trajectories match the current filter settings.');
        return;
    }

    skUpdateStats(data.total, data.stepCounts.length, skRawData.trajectories.length);
    skRender(data);
}

function skUpdateStats(filtered, steps, total) {
    const hasStatusData = skRawData && skRawData.trajectories.some(
        t => t.status && t.status !== 'none' && t.status !== 'unknown'
    );
    const resolved   = skRawData ? skRawData.trajectories.filter(t => t.status === 'resolved').length   : 0;
    const unresolved = skRawData ? skRawData.trajectories.filter(t => t.status === 'unresolved').length : 0;

    let html = `<span><b>${filtered}</b> trajectories shown (of <b>${total}</b>)</span>`;
    if (steps) html += `<span>Steps shown: <b>${steps}</b></span>`;
    if (hasStatusData) {
        html += `<span>Resolved: <b style="color:#065f46">${resolved}</b></span>`;
        html += `<span>Unresolved: <b style="color:#991b1b">${unresolved}</b></span>`;
    }
    document.getElementById('skStats').innerHTML = html;
}

/* =========================================================================
   Sankey — render SVG
   ========================================================================= */
function skRender(data) {
    const { stepCounts, transitions } = data;
    const nSteps = stepCounts.length;

    const PAD_L      = 16;
    const PAD_R      = 24;
    const PAD_T      = 44;
    const PAD_B      = 36;
    const NODE_W     = 22;
    const RIBBON_GAP = 80;
    const COL_STRIDE = NODE_W + RIBBON_GAP;
    const CHART_H    = 500;
    const PHASE_GAP  = 10;

    // Use canvas width so the SVG fills the available space
    const canvas  = document.getElementById('skCanvas');
    const availW  = Math.max(canvas.clientWidth - 48, nSteps * COL_STRIDE + PAD_L + PAD_R);
    // Recompute stride to fill available width when there's room
    const stride  = nSteps > 1 ? Math.max(COL_STRIDE, Math.floor((availW - PAD_L - PAD_R - NODE_W) / (nSteps - 1))) : COL_STRIDE;

    const SVG_W = PAD_L + (nSteps - 1) * stride + NODE_W + PAD_R;
    const SVG_H = PAD_T + CHART_H + PAD_B;

    const ns = 'http://www.w3.org/2000/svg';
    function mkEl(tag, attrs = {}, text = '') {
        const e = document.createElementNS(ns, tag);
        for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
        if (text) e.textContent = text;
        return e;
    }

    function colX(s) { return PAD_L + s * stride; }

    // Max count for proportional bar heights
    let maxCount = 0;
    for (const sc of stepCounts)
        for (const v of Object.values(sc))
            if (v > maxCount) maxCount = v;
    if (maxCount === 0) { skShowPlaceholder('No data.'); return; }

    // Bar layout per step
    const layout = stepCounts.map(sc => {
        const phases  = SK_PHASE_ORDER.filter(p => sc[p] > 0 && skActivePhases.has(p));
        const gapTot  = (phases.length - 1) * PHASE_GAP;
        const availH  = CHART_H - gapTot;
        const bars    = {};
        let y = PAD_T;
        for (const ph of phases) {
            const h = Math.max(4, Math.round((sc[ph] / maxCount) * availH));
            bars[ph] = { y0: y, y1: y + h, count: sc[ph] };
            y += h + PHASE_GAP;
        }
        return bars;
    });

    // Slot computation: subdivide bar edges for ribbon endpoints
    function buildSlots(s) {
        const tmap     = transitions[s] || {};
        const outSlots = {};
        const inSlots  = {};

        for (const fromPh of SK_PHASE_ORDER) {
            if (!layout[s]?.[fromPh]) continue;
            const bar      = layout[s][fromPh];
            const outgoing = SK_PHASE_ORDER
                .filter(toPh => (tmap[`${fromPh}\u2192${toPh}`] || 0) > 0)
                .map(toPh => ({ toPh, count: tmap[`${fromPh}\u2192${toPh}`] }));
            const totalOut = outgoing.reduce((a, b) => a + b.count, 0);
            const barH     = bar.y1 - bar.y0;
            let curY = bar.y0;
            outSlots[fromPh] = {};
            for (const { toPh, count } of outgoing) {
                const h = totalOut > 0 ? (count / totalOut) * barH : 0;
                outSlots[fromPh][toPh] = { y0: curY, y1: curY + h };
                curY += h;
            }
        }

        if (layout[s + 1]) {
            for (const toPh of SK_PHASE_ORDER) {
                if (!layout[s + 1]?.[toPh]) continue;
                const bar      = layout[s + 1][toPh];
                const incoming = SK_PHASE_ORDER
                    .filter(fromPh => (tmap[`${fromPh}\u2192${toPh}`] || 0) > 0)
                    .map(fromPh => ({ fromPh, count: tmap[`${fromPh}\u2192${toPh}`] }));
                const totalIn = incoming.reduce((a, b) => a + b.count, 0);
                const barH    = bar.y1 - bar.y0;
                let curY = bar.y0;
                inSlots[toPh] = {};
                for (const { fromPh, count } of incoming) {
                    const h = totalIn > 0 ? (count / totalIn) * barH : 0;
                    inSlots[toPh][fromPh] = { y0: curY, y1: curY + h };
                    curY += h;
                }
            }
        }
        return { outSlots, inSlots };
    }

    const allSlots = transitions.map((_, s) => buildSlots(s));

    /* ── Build SVG ──────────────────────────────────────────── */
    const svg = document.getElementById('sk-svg');
    svg.setAttribute('width',   SVG_W);
    svg.setAttribute('height',  SVG_H);
    svg.setAttribute('viewBox', `0 0 ${SVG_W} ${SVG_H}`);
    svg.style.display = 'block';
    svg.innerHTML = '';

    // 1. Defs — ALL gradients first, before any ribbons reference them
    const defsEl = mkEl('defs');
    for (const fromPh of SK_PHASE_ORDER) {
        for (const toPh of SK_PHASE_ORDER) {
            const lg = mkEl('linearGradient', {
                id: `skrg_${fromPh}_${toPh}`,
                gradientUnits: 'userSpaceOnUse',
                x1: '0', y1: '0', x2: '1', y2: '0',
            });
            lg.appendChild(mkEl('stop', { offset: '0%',   'stop-color': SK_PHASE_RIBBON[fromPh], 'stop-opacity': '0.80' }));
            lg.appendChild(mkEl('stop', { offset: '100%', 'stop-color': SK_PHASE_RIBBON[toPh],   'stop-opacity': '0.80' }));
            defsEl.appendChild(lg);
        }
    }
    svg.appendChild(defsEl);  // in DOM before any querySelector

    // 2. Background
    svg.appendChild(mkEl('rect', { width: SVG_W, height: SVG_H, fill: '#fff', rx: 12 }));

    // 3. Alternating column shading
    const stripeG = mkEl('g');
    for (let s = 0; s < nSteps; s++) {
        if (s % 2 === 1) {
            stripeG.appendChild(mkEl('rect', {
                x: colX(s) - 4, y: PAD_T - 4,
                width: NODE_W + 8, height: CHART_H + 8,
                fill: '#f8f9fb', rx: 4,
            }));
        }
    }
    svg.appendChild(stripeG);

    // 4. Ribbons (filled cubic-bezier area paths)
    const ribbonG = mkEl('g');
    for (let s = 0; s < transitions.length; s++) {
        const tmap = transitions[s];
        if (!tmap || Object.keys(tmap).length === 0) continue;
        const { outSlots, inSlots } = allSlots[s];

        const x0   = colX(s) + NODE_W;   // right edge of bar s
        const x1   = colX(s + 1);        // left edge of bar s+1
        const midX = (x0 + x1) / 2;

        for (const [key, count] of Object.entries(tmap)) {
            const [fromPh, toPh] = key.split('\u2192');
            const srcSlot = outSlots[fromPh]?.[toPh];
            const dstSlot = inSlots[toPh]?.[fromPh];
            if (!srcSlot || !dstSlot) continue;

            const sy0 = srcSlot.y0, sy1 = srcSlot.y1;
            const dy0 = dstSlot.y0, dy1 = dstSlot.y1;

            // Update gradient span to match this ribbon's actual x extents
            const gradEl = svg.querySelector(`#skrg_${fromPh}_${toPh}`);
            if (gradEl) { gradEl.setAttribute('x1', x0); gradEl.setAttribute('x2', x1); }

            const d = [
                `M ${x0} ${sy0}`,
                `C ${midX} ${sy0}, ${midX} ${dy0}, ${x1} ${dy0}`,
                `L ${x1} ${dy1}`,
                `C ${midX} ${dy1}, ${midX} ${sy1}, ${x0} ${sy1}`,
                'Z',
            ].join(' ');

            const isSelf      = fromPh === toPh;
            const baseOpacity = isSelf ? '0.40' : '0.60';

            const path = mkEl('path', {
                d,
                fill:    `url(#skrg_${fromPh}_${toPh})`,
                stroke:  'none',
                opacity: baseOpacity,
                style:   'cursor:pointer; transition:opacity .12s;',
            });
            path.addEventListener('mouseenter', e => {
                path.setAttribute('opacity', '0.90');
                skShowTooltip(e, { from: fromPh, to: toPh, count, step: s });
            });
            path.addEventListener('mousemove',  skMoveTooltip);
            path.addEventListener('mouseleave', () => {
                path.setAttribute('opacity', baseOpacity);
                skHideTooltip();
            });
            ribbonG.appendChild(path);
        }
    }
    svg.appendChild(ribbonG);

    // 5. Phase bars (drawn on top)
    const barG = mkEl('g');
    for (let s = 0; s < nSteps; s++) {
        const x    = colX(s);
        const bars = layout[s];
        for (const [ph, { y0, y1, count }] of Object.entries(bars)) {
            const h = y1 - y0;
            // Shadow
            barG.appendChild(mkEl('rect', {
                x: x + 2, y: y0 + 2, width: NODE_W, height: h, rx: 4,
                fill: '#000', opacity: '0.07',
            }));
            // Body
            const rect = mkEl('rect', {
                x, y: y0, width: NODE_W, height: h, rx: 4,
                fill: SK_PHASE_COLOR[ph], stroke: SK_PHASE_STROKE[ph], 'stroke-width': '1.5',
                style: 'cursor:pointer;',
            });
            rect.addEventListener('mouseenter', e => skShowTooltip(e, { phase: ph, step: s, count }));
            rect.addEventListener('mousemove',  skMoveTooltip);
            rect.addEventListener('mouseleave', skHideTooltip);
            barG.appendChild(rect);
            // Count label
            if (h >= 16) {
                barG.appendChild(mkEl('text', {
                    x: x + NODE_W / 2, y: y0 + h / 2 + 4,
                    'text-anchor': 'middle', 'font-size': '9', 'font-weight': '700',
                    fill: '#2c3e50', 'pointer-events': 'none',
                }, count.toString()));
            }
        }
    }
    svg.appendChild(barG);

    // 6. Step axis labels
    const axisG      = mkEl('g');
    const labelEvery = nSteps <= 20 ? 1 : nSteps <= 40 ? 2 : 5;
    axisG.appendChild(mkEl('text', {
        x: PAD_L - 2, y: PAD_T + CHART_H + 22,
        'text-anchor': 'start', 'font-size': '10', fill: '#94a3b8', 'font-weight': '600',
    }, 'Step:'));
    for (let s = 0; s < nSteps; s++) {
        if (s % labelEvery !== 0 && s !== nSteps - 1) continue;
        axisG.appendChild(mkEl('text', {
            x: colX(s) + NODE_W / 2, y: PAD_T + CHART_H + 22,
            'text-anchor': 'middle', 'font-size': '10', fill: '#94a3b8', 'font-weight': '500',
        }, `${s + 1}`));
    }
    svg.appendChild(axisG);

    // 7. Legend
    const legG      = mkEl('g');
    const legPhases = SK_PHASE_ORDER.filter(p => skActivePhases.has(p));
    legPhases.forEach((ph, i) => {
        const lx = PAD_L + i * 130;
        legG.appendChild(mkEl('rect', {
            x: lx, y: 7, width: 14, height: 14, rx: 3,
            fill: SK_PHASE_COLOR[ph], stroke: SK_PHASE_STROKE[ph], 'stroke-width': '1.5',
        }));
        legG.appendChild(mkEl('text', {
            x: lx + 18, y: 18,
            'font-size': '11', fill: '#444', 'font-weight': '600',
        }, ph.charAt(0).toUpperCase() + ph.slice(1)));
    });
    svg.appendChild(legG);

    skHidePlaceholder();
}

/* =========================================================================
   Sankey — tooltip
   ========================================================================= */
const skTooltipEl = document.getElementById('sk-tooltip');

function skShowTooltip(e, info) {
    let html = '';
    if (info.phase) {
        const pct = skRawData
            ? Math.round(100 * info.count / skRawData.trajectories.length) : '?';
        html  = `<b style="color:${SK_PHASE_RIBBON[info.phase]}">${skCap(info.phase)}</b>`;
        html += `<br>Step <b>${info.step + 1}</b>`;
        html += `<br><b>${info.count}</b> trajectories (${pct}%)`;
    } else {
        html  = `<b style="color:${SK_PHASE_RIBBON[info.from]}">${skCap(info.from)}</b>`;
        html += info.from === info.to
            ? ` <span style="color:#aaa">→ stays same phase</span>`
            : ` → <b style="color:${SK_PHASE_RIBBON[info.to]}">${skCap(info.to)}</b>`;
        html += `<br>Step ${info.step + 1} → ${info.step + 2}`;
        html += `<br><b>${info.count}</b> trajectories`;
    }
    skTooltipEl.innerHTML     = html;
    skTooltipEl.style.display = 'block';
    skMoveTooltip(e);
}
function skMoveTooltip(e) {
    skTooltipEl.style.left = Math.min(e.clientX + 16, window.innerWidth  - 320) + 'px';
    skTooltipEl.style.top  = Math.max(e.clientY - 10, 8)                        + 'px';
}
function skHideTooltip() { skTooltipEl.style.display = 'none'; }

/* =========================================================================
   Sankey — placeholder helpers
   ========================================================================= */
function skShowPlaceholder(msg = '') {
    document.getElementById('sk-svg').style.display = 'none';
    const ph = document.getElementById('skPlaceholder');
    ph.style.display = 'flex';
    if (msg === 'spinner') {
        ph.innerHTML = '<div class="sk-spinner"></div><span>Loading…</span>';
    } else {
        ph.innerHTML = `<span style="font-size:32px;opacity:.3">🌊</span><span style="color:#999;text-align:center;max-width:320px">${msg}</span>`;
    }
}
function skHidePlaceholder() {
    document.getElementById('skPlaceholder').style.display = 'none';
}

/* =========================================================================
   Bayesian pane
   ========================================================================= */
const BY_PHASE_COLOR = {
    localization: '#8c74e6',
    patch: '#ee9463',
    validation: '#4bb8d4',
    general: '#7aa6da',
};

let byRawData = null;
let bySelectedFeature = null;

function byWireControls() {
    ['byStatus', 'byFeatureType', 'bySort'].forEach(id => {
        document.getElementById(id).addEventListener('change', () => byLoad(true));
    });
    ['byMinSupport', 'byTopN'].forEach(id => {
        document.getElementById(id).addEventListener('keydown', e => {
            if (e.key === 'Enter') byLoad(true);
        });
        document.getElementById(id).addEventListener('change', () => byLoad(true));
    });
}

function selectBayes() {
    activeId = null;
    if (sankeyActive) hideSankeyPane();
    document.querySelectorAll('.graph-item').forEach(el => el.classList.remove('active'));
    document.getElementById('sankeyListItem').classList.remove('active');
    document.getElementById('bayesListItem').classList.add('active');
    showBayesPane();
}

function showBayesPane() {
    bayesActive = true;
    document.getElementById('graphPane').style.display = 'none';
    document.getElementById('sankeyPane').style.display = 'none';
    document.getElementById('bayesPane').style.display = 'flex';
    if (!byRawData) byLoad(true);
    else byRender();
}

function hideBayesPane() {
    bayesActive = false;
    document.getElementById('bayesPane').style.display = 'none';
    document.getElementById('graphPane').style.display = '';
}

async function byLoad(force = false) {
    if (!bayesActive && !force) return;

    const btn = document.getElementById('byRefreshBtn');
    const params = new URLSearchParams({
        status: document.getElementById('byStatus').value,
        feature_type: document.getElementById('byFeatureType').value,
        min_support: normalizeIntInput('byMinSupport', 4, 1, 200),
        max_features: normalizeIntInput('byTopN', 36, 5, 200),
    });

    btn.disabled = true;
    document.getElementById('byStats').innerHTML = '<span>Loading Bayesian analysis...</span>';
    document.getElementById('byFeatureList').innerHTML = '<div class="by-empty">Crunching posterior summaries...</div>';
    document.getElementById('byDetail').innerHTML = '<div class="by-empty">Bayesian feature effects are loading.</div>';
    document.getElementById('bySkyline').innerHTML = '';

    try {
        const res = await fetch(`/api/bayes?${params}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        byRawData = await res.json();
        bySelectedFeature = null;
        byRender();
    } catch (err) {
        const msg = `<div class="by-empty">Failed to load analysis: ${escHtml(err.message)}</div>`;
        document.getElementById('byFeatureList').innerHTML = msg;
        document.getElementById('byDetail').innerHTML = msg;
        document.getElementById('byStats').innerHTML = '<span>Analysis unavailable.</span>';
        document.getElementById('bySkyline').innerHTML = '';
    } finally {
        btn.disabled = false;
    }
}

function byRender() {
    if (!byRawData) return;
    const features = [...(byRawData.features || [])];
    const sortMode = document.getElementById('bySort').value;
    features.sort((a, b) => bySortScore(b, sortMode) - bySortScore(a, sortMode));

    document.getElementById('byStats').innerHTML = bySummaryHtml(byRawData.summary, features.length);
    byRenderList(features);
    byRenderSkyline(features);

    if (!features.length) {
        document.getElementById('byFeatureList').innerHTML = '<div class="by-empty">No features meet the current support and filter settings.</div>';
        document.getElementById('byDetail').innerHTML = '<div class="by-empty">Try lowering the minimum support or broadening the status filter.</div>';
        return;
    }

    if (!bySelectedFeature) bySelectedFeature = features[0].feature;
    const selected = features.find(item => item.feature === bySelectedFeature) || features[0];
    bySelectedFeature = selected.feature;
    byRenderDetail(selected);
    highlightBayesSelection();
}

function bySummaryHtml(summary, shownCount) {
    return [
        `<span><b>${summary.trajectory_count}</b> trajectories</span>`,
        `<span><b>${summary.step_count}</b> steps</span>`,
        `<span><b>${shownCount}</b> features shown</span>`,
        `<span>Labeled outcomes: <b>${summary.labeled_trajectories}</b></span>`,
        `<span>Resolved: <b>${summary.resolved_trajectories}</b></span>`,
        `<span>Unresolved: <b>${summary.unresolved_trajectories}</b></span>`,
    ].join('');
}

function bySortScore(item, sortMode) {
    if (sortMode === 'support') return item.trajectory_support;
    if (sortMode === 'process') return item.process.shift_magnitude;
    if (sortMode === 'observation') return Math.abs(item.observation.lift_mean);
    return Math.abs(item.outcome.lift_mean);
}

function byRenderList(features) {
    const root = document.getElementById('byFeatureList');
    root.innerHTML = features.map(item => {
        const outcomeLift = formatSignedPct(item.outcome.lift_mean);
        const obsLift = formatSignedPct(item.observation.lift_mean);
        const processLift = formatSignedPct(item.process.dominant_delta);
        return `
            <div class="by-card${item.feature === bySelectedFeature ? ' active' : ''}" data-feature="${escHtml(item.feature)}" onclick="bySelectFeature('${escHtml(item.feature)}')">
                <div class="by-card-top">
                    <div class="by-card-title">${escHtml(item.label)}</div>
                    <div class="by-pill">${escHtml(item.kind)}</div>
                </div>
                <div class="by-card-metrics">
                    <div><b>${item.trajectory_support}</b>support</div>
                    <div><b>${outcomeLift}</b>outcome</div>
                    <div><b>${processLift}</b>process</div>
                </div>
                <div style="margin-top:8px;font-size:11px;color:#6d786f;">Step-success lift ${obsLift}</div>
            </div>`;
    }).join('');
}

function bySelectFeature(featureName) {
    bySelectedFeature = featureName;
    if (!byRawData) return;
    const current = (byRawData.features || []).find(item => item.feature === featureName);
    if (!current) return;
    byRenderDetail(current);
    highlightBayesSelection();
}

function highlightBayesSelection() {
    document.querySelectorAll('.by-card').forEach(card => {
        card.classList.toggle('active', card.dataset.feature === bySelectedFeature);
    });
}

function byRenderDetail(item) {
    const detail = document.getElementById('byDetail');
    detail.innerHTML = `
        <div class="by-feature-head">
            <div class="by-kicker">${escHtml(item.kind)} feature</div>
            <h2>${escHtml(item.label)}</h2>
            <div class="by-feature-sub">
                Present in <b>${item.trajectory_support}</b> trajectories and seen <b>${item.occurrence_count}</b> times.
                Dominant next-phase pull: <b>${escHtml(item.process.dominant_phase)}</b> (${formatSignedPct(item.process.dominant_delta)}).
            </div>
        </div>
        <div class="by-metric-grid">
            ${byMetricBoxHtml('Resolved lift', formatSignedPct(item.outcome.lift_mean), `Present posterior ${formatPct(item.outcome.present_rate_mean)} · CI90 ${formatInterval(item.outcome.present_rate_ci90)}`)}
            ${byMetricBoxHtml('Step-success lift', formatSignedPct(item.observation.lift_mean), `Present posterior ${formatPct(item.observation.present_rate_mean)} · CI90 ${formatInterval(item.observation.present_rate_ci90)}`)}
            ${byMetricBoxHtml('Support share', formatPct(item.trajectory_share), `Labeled trajectories with feature: ${item.labeled_trajectory_support}`)}
            ${byMetricBoxHtml('Process shift', formatPct(item.process.shift_magnitude), `Compared with the global next-phase baseline.`)}
        </div>
        <div>
            <div class="by-kicker">Outcome posterior</div>
            <div class="by-feature-sub">
                Present: <b>${formatPct(item.outcome.present_rate_mean)}</b> (${formatInterval(item.outcome.present_rate_ci90)})<br>
                Absent: <b>${formatPct(item.outcome.absent_rate_mean)}</b> (${formatInterval(item.outcome.absent_rate_ci90)})
            </div>
        </div>
        <div>
            <div class="by-kicker">Next-phase deltas</div>
            <div class="by-phase-bars">${byPhaseRowsHtml(item.process.deltas)}</div>
        </div>
    `;
}

function byMetricBoxHtml(title, big, small) {
    return `<div class="by-metric-box"><h4>${title}</h4><div class="big">${big}</div><div class="small">${small}</div></div>`;
}

function byPhaseRowsHtml(deltas) {
    return Object.entries(deltas).map(([phase, delta]) => {
        const width = `${Math.min(100, Math.round(Math.abs(delta) * 400))}%`;
        const color = delta >= 0 ? (BY_PHASE_COLOR[phase] || '#7aa6da') : '#d96f6f';
        return `
            <div class="by-phase-row">
                <div>${escHtml(skCap(phase))}</div>
                <div class="by-phase-bar"><div class="by-phase-fill" style="width:${width};background:${color};"></div></div>
                <div>${formatSignedPct(delta)}</div>
            </div>`;
    }).join('');
}

function byRenderSkyline(features) {
    const svg = document.getElementById('bySkyline');
    const wrap = document.getElementById('bySkylineWrap');
    const width = Math.max(420, wrap.clientWidth - 12);
    const height = 360;
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('width', width);
    svg.setAttribute('height', height);
    svg.innerHTML = '';

    if (!features.length) return;

    const ns = 'http://www.w3.org/2000/svg';
    const pad = { left: 48, right: 18, top: 18, bottom: 32 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const maxSupport = Math.max(...features.map(item => item.trajectory_support), 1);

    const axis = document.createElementNS(ns, 'g');
    axis.innerHTML = `
        <line x1="${pad.left}" y1="${pad.top + plotH}" x2="${width - pad.right}" y2="${pad.top + plotH}" stroke="#cbd7ce" />
        <line x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${pad.top + plotH}" stroke="#cbd7ce" />
        <line x1="${pad.left + plotW / 2}" y1="${pad.top}" x2="${pad.left + plotW / 2}" y2="${pad.top + plotH}" stroke="#dfe7e1" stroke-dasharray="4 4" />
        <text x="${pad.left}" y="${height - 8}" font-size="11" fill="#748279">Worse outcome lift</text>
        <text x="${pad.left + plotW / 2 - 18}" y="${height - 8}" font-size="11" fill="#748279">0</text>
        <text x="${width - 108}" y="${height - 8}" font-size="11" fill="#748279">Better outcome lift</text>
        <text x="10" y="${pad.top + 12}" font-size="11" fill="#748279">Support</text>
    `;
    svg.appendChild(axis);

    for (const item of features) {
        const x = pad.left + ((item.outcome.lift_mean + 0.5) / 1.0) * plotW;
        const y = pad.top + plotH - (item.trajectory_support / maxSupport) * plotH;
        const circle = document.createElementNS(ns, 'circle');
        circle.setAttribute('cx', Math.max(pad.left, Math.min(width - pad.right, x)));
        circle.setAttribute('cy', y);
        circle.setAttribute('r', 6 + item.process.shift_magnitude * 26);
        circle.setAttribute('fill', item.outcome.lift_mean >= 0 ? '#2f8f5b' : '#d07070');
        circle.setAttribute('fill-opacity', '0.75');
        circle.setAttribute('stroke', item.feature === bySelectedFeature ? '#173420' : '#ffffff');
        circle.setAttribute('stroke-width', item.feature === bySelectedFeature ? '2' : '1');
        circle.style.cursor = 'pointer';
        circle.addEventListener('click', () => bySelectFeature(item.feature));
        svg.appendChild(circle);

        if (item.feature === bySelectedFeature || item.trajectory_support === maxSupport) {
            const label = document.createElementNS(ns, 'text');
            label.setAttribute('x', Math.max(pad.left, Math.min(width - pad.right - 90, x + 8)));
            label.setAttribute('y', y - 8);
            label.setAttribute('font-size', '10');
            label.setAttribute('fill', '#304236');
            label.textContent = item.label;
            svg.appendChild(label);
        }
    }
}

function normalizeIntInput(id, fallback, min, max) {
    const input = document.getElementById(id);
    const raw = parseInt(input.value, 10);
    const normalized = Number.isFinite(raw) ? Math.max(min, Math.min(max, raw)) : fallback;
    input.value = normalized;
    return normalized;
}

function formatPct(value) {
    return `${Math.round((value || 0) * 100)}%`;
}

function formatSignedPct(value) {
    const pct = Math.round((value || 0) * 100);
    return `${pct > 0 ? '+' : ''}${pct}%`;
}

function formatInterval(ci) {
    if (!Array.isArray(ci) || ci.length !== 2) return 'n/a';
    return `${formatPct(ci[0])} to ${formatPct(ci[1])}`;
}

/* =========================================================================
   Utility
   ========================================================================= */
function skCap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

function escHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

/* ── Start ── */
init();
