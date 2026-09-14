import { useState, useEffect, useCallback, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";

// Client-side DAX preview (not for production translation — backend handles real translation)
const DAX_TO_SQL_MAP = {
  'SUM': (col) => `SUM(${col})`,
  'COUNT': (col) => `COUNT(${col})`,
  'DISTINCTCOUNT': (col) => `COUNT(DISTINCT ${col})`,
  'AVERAGE': (col) => `AVG(${col})`,
  'MIN': (col) => `MIN(${col})`,
  'MAX': (col) => `MAX(${col})`,
  'COUNTA': (col) => `COUNT(${col})`,
  'COUNTROWS': () => `COUNT(*)`,
};

function translateDAXtoSQL(daxExpr) {
  if (!daxExpr) return daxExpr;
  let sql = daxExpr;
  sql = sql.replace(/SUM\s*\(\s*(\w+)\[(\w+)\]\s*\)/gi, (_, _t, col) => `SUM(source.${col.toLowerCase().replace(/\s+/g,'_')})`);
  sql = sql.replace(/COUNT\s*\(\s*(\w+)\[(\w+)\]\s*\)/gi, (_, _t, col) => `COUNT(source.${col.toLowerCase().replace(/\s+/g,'_')})`);
  sql = sql.replace(/DISTINCTCOUNT\s*\(\s*(\w+)\[(\w+)\]\s*\)/gi, (_, _t, col) => `COUNT(DISTINCT source.${col.toLowerCase().replace(/\s+/g,'_')})`);
  sql = sql.replace(/AVERAGE\s*\(\s*(\w+)\[(\w+)\]\s*\)/gi, (_, _t, col) => `AVG(source.${col.toLowerCase().replace(/\s+/g,'_')})`);
  sql = sql.replace(/DIVIDE\s*\(\s*([^,]+),\s*([^,]+),\s*([^)]+)\)/gi, (_, a, b, alt) => `COALESCE((${a.trim()}) / NULLIF(${b.trim()}, 0), ${alt.trim()})`);
  sql = sql.replace(/IF\s*\(\s*([^,]+),\s*([^,]+),\s*([^)]+)\)/gi, (_, c, t, f) => `CASE WHEN ${c.trim()} THEN ${t.trim()} ELSE ${f.trim()} END`);
  sql = sql.replace(/\[([^\]]+)\]/g, (_, name) => `MEASURE(\`${name}\`)`);
  return sql;
}

// Framer Motion variants
const pageVariants = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0, transition: { duration: 0.4, ease: "easeOut" } },
  exit: { opacity: 0, y: -10, transition: { duration: 0.2 } }
};
const cardVariants = {
  initial: { opacity: 0, scale: 0.95 },
  animate: (i) => ({ opacity: 1, scale: 1, transition: { delay: i * 0.06, duration: 0.3, ease: "easeOut" } }),
  hover: { scale: 1.02, borderColor: "rgba(99,102,241,0.5)", transition: { duration: 0.2 } }
};
const staggerContainer = { animate: { transition: { staggerChildren: 0.05 } } };

// API helpers
async function apiFetch(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || err.message || res.statusText);
  }
  return res.json();
}

// ── Results drill-down: per fact table/view, click to expand measures; ──
//    click a measure to see its full DAX → SQL conversion detail.
const STATUS_COLOR = {
  converted: '#4ade80', manual_override: '#4ade80',
  partial: '#fbbf24', unsupported: '#f87171', excluded: '#94a3b8',
};

// Classify WHY a measure was excluded from the deployed view, into a
// human-readable bucket. Uses the backend's exclusion_reason first, then falls
// back to inspecting the original DAX (e.g. residual-DAX exclusions).
const _TIME_INTEL = /\b(TOTALYTD|TOTALQTD|TOTALMTD|DATESYTD|DATESQTD|DATESMTD|SAMEPERIODLASTYEAR|PREVIOUSYEAR|PREVIOUSMONTH|PREVIOUSQUARTER|PREVIOUSDAY|NEXTYEAR|NEXTMONTH|NEXTQUARTER|PARALLELPERIOD|DATEADD|DATESINPERIOD|DATESBETWEEN|ENDOFMONTH|ENDOFQUARTER|ENDOFYEAR|STARTOFMONTH|STARTOFQUARTER|STARTOFYEAR|OPENINGBALANCE\w*|CLOSINGBALANCE\w*|FIRSTDATE|LASTDATE|FIRSTNONBLANK|LASTNONBLANK)\b/i;
const _FILTER_CTX = /\b(CALCULATE|CALCULATETABLE|FILTER|ALLEXCEPT|ALLSELECTED|ALLCROSSFILTERED|ALLNOBLANKROW|ALL|VALUES|ADDCOLUMNS|SELECTCOLUMNS|SUMMARIZE\w*|GROUPBY|EARLIER|EARLIEST|RELATEDTABLE|USERELATIONSHIP|CROSSFILTER|RANKX|TOPN|GENERATE\w*)\b/i;

function classifyExclusion(m) {
  const reason = (m.exclusion_reason || '').toLowerCase();
  const dax = m.original_dax || '';
  if (reason.includes('non-aggregating')) return 'Non-aggregating';
  if (reason.includes('window-over-window')) return 'Prior-period of windowed measure';
  if (reason.includes('references a measure')) return 'Depends on excluded measure';
  if (reason.includes('excluded upstream')) return 'Excluded upstream';
  // Residual-DAX (or unknown) — inspect the original expression.
  if (_TIME_INTEL.test(dax)) return 'Time intelligence';
  if (_FILTER_CTX.test(dax)) return 'Filter/context DAX';
  if (dax.includes('[')) return 'Unresolved reference';
  return 'No SQL analog';
}

// A measure benefits from a manual override when it didn't cleanly deploy:
// excluded from the view, or only partially / not translated.
function needsOverride(m) {
  return m.deployed === false || m.status === 'partial' || m.status === 'unsupported';
}

// Generate a suggested `measure_override` YAML stub so the user can compare the
// default (auto) conversion with a hand-tunable override.
function suggestedOverride(m) {
  const name = m.name;
  const dax = m.original_dax || '';
  const sql = m.translated_sql || '';
  const cat = m.deployed === false
    ? classifyExclusion(m)
    : (m.status === 'partial' ? 'Partial translation' : 'Untranslatable');
  if ((m.exclusion_reason || '').includes('window-over-window')) {
    return `# ${cat}: rebuild via offset on base components, then compose
- name: <base>_py
  expr: <base aggregate>            # e.g. SUM(source.gross_profit)
  window: [{ order: Date, range: cumulative, offset: -12 month }]
- name: ${name}
  expr: MEASURE(<num>_py) / NULLIF(MEASURE(<den>_py), 0)`;
  }
  if (cat === 'Non-aggregating') {
    return `# ${cat}: wrap in an explicit aggregate
- name: ${name}
  expr: SUM(${sql || '<column>'})
  # original DAX: ${dax}`;
  }
  if (cat === 'Time intelligence') {
    return `# ${cat}: express as a window on the base measure
- name: ${name}
  expr: ${sql || '<base aggregate>'}
  window: [{ order: Date, range: cumulative }]
  # original DAX: ${dax}`;
  }
  return `# ${cat}: hand-translate the DAX to SQL
- name: ${name}
  expr: ${sql || '<SQL expression>'}
  # original DAX: ${dax}`;
}

function OverrideStub({ m }) {
  return (
    <div style={{ margin: '2px 0 8px', padding: '8px 10px', borderRadius: 6, background: 'rgba(99,102,241,0.08)', borderLeft: '3px solid #6366f1' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
        <span style={{ fontSize: 10, textTransform: 'uppercase', color: '#a5b4fc', fontWeight: 700 }}>Suggested measure_override</span>
        <button className="btn-secondary" style={{ fontSize: 10, padding: '2px 8px' }}
          onClick={(e) => { e.stopPropagation(); navigator.clipboard?.writeText(suggestedOverride(m)); }}>Copy</button>
      </div>
      <code style={{ display: 'block', whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: '#c7d2fe', fontSize: 11, lineHeight: 1.5 }}>{suggestedOverride(m)}</code>
    </div>
  );
}

function ConfidenceBar({ value }) {
  const v = Math.max(0, Math.min(100, value ?? 0));
  const c = v >= 70 ? '#4ade80' : v >= 40 ? '#fbbf24' : '#f87171';
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <div style={{ flex: 1, height: 5, background: 'rgba(255,255,255,0.08)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ width: `${v}%`, height: '100%', background: c }} />
      </div>
      <span style={{ fontSize: 11, color: '#94a3b8', width: 32, textAlign: 'right' }}>{value != null ? `${v}%` : '—'}</span>
    </div>
  );
}

// The measures this one references (via MEASURE()) that were themselves
// excluded from the view — computed from the group so it's always accurate.
function excludedDeps(m, groupMeasures) {
  const refs = [...String(m.translated_sql || '').matchAll(/MEASURE\(`([^`]+)`\)/g)].map(x => x[1]);
  const byName = {};
  (groupMeasures || []).forEach(x => { byName[x.name.toLowerCase()] = x; });
  const seen = new Set();
  const out = [];
  refs.forEach(r => {
    const dep = byName[r.toLowerCase()];
    if (dep && dep.deployed === false && !seen.has(r.toLowerCase())) { seen.add(r.toLowerCase()); out.push(dep.name); }
  });
  return out;
}

function MeasureDetail({ m, groupMeasures }) {
  const rows = [
    ['Original DAX', m.original_dax, '#fbbf24'],
    ['Translated SQL', m.translated_sql, '#4ade80'],
  ];
  const deps = excludedDeps(m, groupMeasures);
  const hasWindow = m.window_spec && (Array.isArray(m.window_spec) ? m.window_spec.length > 0 : Object.keys(m.window_spec).length > 0);
  return (
    <div style={{ padding: '10px 14px 14px 34px', background: 'rgba(0,0,0,0.18)', borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
      {m.deployed === false && (
        <div style={{ marginBottom: 10, padding: '8px 10px', borderRadius: 6, background: 'rgba(148,163,184,0.12)', borderLeft: '3px solid #94a3b8' }}>
          <span style={{ fontSize: 11, fontWeight: 700, color: '#cbd5e1' }}>⊘ Excluded from the deployed view</span>
          <div style={{ fontSize: 11.5, color: '#94a3b8', marginTop: 2 }}>{m.exclusion_reason || 'not deployable'} — candidate for a manual measure_override.</div>
          {deps.length > 0 && (
            <div style={{ fontSize: 11.5, marginTop: 4 }}>
              <span style={{ color: '#64748b' }}>Depends on excluded: </span>
              {deps.map((d, i) => (
                <span key={d} style={{ color: '#f0abfc', fontWeight: 600 }}>{d}{i < deps.length - 1 ? ', ' : ''}</span>
              ))}
            </div>
          )}
        </div>
      )}
      {needsOverride(m) && <OverrideStub m={m} />}
      {rows.map(([label, val, color]) => (
        <div key={label} style={{ marginBottom: 8 }}>
          <div style={{ fontSize: 10, textTransform: 'uppercase', color: '#64748b', marginBottom: 3 }}>{label}</div>
          <code style={{ display: 'block', whiteSpace: 'pre-wrap', wordBreak: 'break-word', color, fontSize: 11.5, lineHeight: 1.5 }}>{val || '—'}</code>
        </div>
      ))}
      {hasWindow && (
        <div style={{ marginBottom: 8 }}>
          <div style={{ fontSize: 10, textTransform: 'uppercase', color: '#64748b', marginBottom: 3 }}>Window (applied on top of the SQL)</div>
          <code style={{ color: '#a5b4fc', fontSize: 11.5 }}>{JSON.stringify(m.window_spec)}</code>
        </div>
      )}
      {(m.applied_transformations || []).length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 6 }}>
          {m.applied_transformations.map((t, i) => (
            <span key={i} className="tag tag-info" style={{ fontSize: 9 }}>{t}</span>
          ))}
        </div>
      )}
      {(m.issues || []).map((x, i) => (
        <div key={`i${i}`} style={{ fontSize: 11, color: '#f87171', marginTop: 2 }}>⚠ {x}</div>
      ))}
      {(m.warnings || []).map((x, i) => (
        <div key={`w${i}`} style={{ fontSize: 11, color: '#fbbf24', marginTop: 2 }}>• {x}</div>
      ))}
    </div>
  );
}

// Dependency graph of the "depends on excluded measure" cascades: nodes are
// measures, an edge A→B means A references B. Laid out in columns by dependency
// depth (root causes on the left), color-coded root-cause / cascade / deployed.
function DependencyGraph({ measures, onPick }) {
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const byName = {};
  (measures || []).forEach(m => { byName[m.name.toLowerCase()] = m; });
  const depsOf = (m) => [...String(m.translated_sql || '').matchAll(/MEASURE\(`([^`]+)`\)/g)]
    .map(x => byName[x[1].toLowerCase()]).filter(Boolean);

  // Subgraph: measures excluded because of another excluded measure, plus their
  // transitive dependencies (so the root cause is visible).
  const inGraph = new Map();
  const seed = (measures || []).filter(m => m.deployed === false && depsOf(m).some(d => d.deployed === false));
  const stack = [...seed];
  while (stack.length) {
    const m = stack.pop();
    if (inGraph.has(m.name)) continue;
    inGraph.set(m.name, m);
    depsOf(m).forEach(d => { if (!inGraph.has(d.name)) stack.push(d); });
  }
  const nodes = [...inGraph.values()];
  if (nodes.length === 0) {
    return <div className="glass" style={{ padding: 16, color: '#64748b', fontSize: 13 }}>No cascaded exclusions in this view.</div>;
  }

  // Longest-path level to a sink (a node with no in-graph dependencies).
  const level = {};
  const compute = (name, seen) => {
    if (name in level) return level[name];
    if (seen.has(name)) return 0;
    const ds = depsOf(inGraph.get(name)).filter(d => inGraph.has(d.name));
    const v = ds.length === 0 ? 0 : 1 + Math.max(...ds.map(d => compute(d.name, new Set(seen).add(name))));
    level[name] = v;
    return v;
  };
  nodes.forEach(m => compute(m.name, new Set()));

  // Degree of connectivity: dependencies it has (out) + measures depending on it
  // (in). Used to order each column so the most-connected nodes sit at the top.
  const outDeg = {}, inDeg = {};
  nodes.forEach(m => {
    const ds = depsOf(m).filter(d => inGraph.has(d.name));
    outDeg[m.name] = ds.length;
    ds.forEach(d => { inDeg[d.name] = (inDeg[d.name] || 0) + 1; });
  });
  const degree = (n) => (outDeg[n] || 0) + (inDeg[n] || 0);

  const cols = {};
  nodes.forEach(m => { (cols[level[m.name]] = cols[level[m.name]] || []).push(m); });
  const maxLevel = Math.max(...nodes.map(m => level[m.name]));
  const COLW = 210, ROWH = 44, NODEW = 176, NODEH = 30, PADX = 14, PADY = 14;
  const pos = {};
  let maxRows = 0;
  for (let l = 0; l <= maxLevel; l++) {
    const c = (cols[l] || []).slice().sort(
      (a, b) => degree(b.name) - degree(a.name) || a.name.localeCompare(b.name));
    maxRows = Math.max(maxRows, c.length);
    c.forEach((m, i) => { pos[m.name] = { x: PADX + l * COLW, y: PADY + i * ROWH }; });
  }
  const width = PADX * 2 + maxLevel * COLW + NODEW;
  const height = PADY * 2 + Math.max(1, maxRows) * ROWH;
  const nodeColor = (m) => m.deployed !== false ? '#4ade80'
    : (String(m.exclusion_reason || '').includes('references a measure') ? '#fbbf24' : '#f87171');
  const trunc = (s) => s.length > 24 ? s.slice(0, 23) + '…' : s;

  const edges = [];
  const outAdj = {}, inAdj = {};
  nodes.forEach(m => depsOf(m).forEach(d => {
    if (!inGraph.has(d.name)) return;
    edges.push([m.name, d.name]);
    (outAdj[m.name] = outAdj[m.name] || []).push(d.name);   // m depends on d
    (inAdj[d.name] = inAdj[d.name] || []).push(m.name);     // d is depended on by m
  }));

  // Click-to-highlight: the selected node plus everything it depends on
  // (downstream) and everything that depends on it (upstream) — its whole chain.
  const walk = (start, adj) => {
    const s = new Set(), st = [start];
    while (st.length) {
      const n = st.pop();
      (adj[n] || []).forEach(x => { if (!s.has(x)) { s.add(x); st.push(x); } });
    }
    return s;
  };
  const hi = (selected && inGraph.has(selected))
    ? new Set([selected, ...walk(selected, outAdj), ...walk(selected, inAdj)])
    : null;
  const nodeOpacity = (name) => hi ? (hi.has(name) ? 1 : 0.15) : 1;
  const edgeOn = (a, b) => hi && hi.has(a) && hi.has(b);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', fontSize: 10, color: '#94a3b8', alignItems: 'center' }}>
        <span><span style={{ color: '#f87171' }}>■</span> root cause</span>
        <span><span style={{ color: '#fbbf24' }}>■</span> cascaded exclusion</span>
        <span><span style={{ color: '#4ade80' }}>■</span> deployed dependency</span>
        <span style={{ color: '#64748b' }}>arrow: A → B means A depends on B</span>
        <span style={{ marginLeft: 'auto', color: '#818cf8' }}>{selected ? `Highlighting “${selected}” — click background to clear` : 'Click a node to highlight its chain · double-click for the exclusion reason'}</span>
      </div>
      <div style={{ overflow: 'auto', height: 420, minHeight: 140, resize: 'vertical', border: '1px solid rgba(255,255,255,0.08)', borderRadius: 8, background: 'rgba(0,0,0,0.15)' }}>
        <svg width={width} height={height} style={{ display: 'block' }} onClick={() => setSelected(null)}>
          <defs>
            <marker id="dg-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L8,4 L0,8 z" fill="#64748b" />
            </marker>
            <marker id="dg-arrow-hi" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L8,4 L0,8 z" fill="#a5b4fc" />
            </marker>
          </defs>
          {edges.map(([a, b], i) => {
            const pa = pos[a], pb = pos[b];
            if (!pa || !pb) return null;
            const x1 = pa.x, y1 = pa.y + NODEH / 2;              // dependent left edge
            const x2 = pb.x + NODEW, y2 = pb.y + NODEH / 2;      // dependency right edge
            const mx = (x1 + x2) / 2;
            const on = edgeOn(a, b);
            const stroke = hi ? (on ? '#a5b4fc' : 'rgba(148,163,184,0.12)') : 'rgba(148,163,184,0.5)';
            return <path key={i} d={`M${x2},${y2} C${mx},${y2} ${mx},${y1} ${x1},${y1}`}
              fill="none" stroke={stroke} strokeWidth={on ? 2 : 1.2}
              markerStart={`url(#${on ? 'dg-arrow-hi' : 'dg-arrow'})`} />;
          })}
          {nodes.map(m => {
            const p = pos[m.name];
            const c = nodeColor(m);
            const isSel = selected === m.name;
            return (
              <g key={m.name} transform={`translate(${p.x},${p.y})`} style={{ cursor: 'pointer' }}
                opacity={nodeOpacity(m.name)}
                onClick={(e) => { e.stopPropagation(); setSelected(m.name); onPick && onPick(m); }}
                onDoubleClick={(e) => { e.stopPropagation(); setDetail(m); }}>
                <title>{`${m.name}\n${m.deployed === false ? (m.exclusion_reason || 'excluded') : 'deployed'}\n(double-click for details)`}</title>
                <rect width={NODEW} height={NODEH} rx="6" fill={`${c}1a`} stroke={isSel ? '#c7d2fe' : c} strokeWidth={isSel ? 2.4 : 1.3} />
                <text x="9" y={NODEH / 2 + 3.5} fontSize="11" fill="#e2e8f0">{trunc(m.name)}</text>
              </g>
            );
          })}
        </svg>
      </div>
      {detail && (
        <div className="glass" style={{ padding: '10px 12px', borderLeft: `3px solid ${nodeColor(detail)}` }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <strong style={{ fontSize: 13 }}>{detail.name}</strong>
            <span className="tag" style={{ fontSize: 9, background: `${nodeColor(detail)}22`, color: nodeColor(detail) }}>{detail.status}</span>
            {detail.deployed === false && <span style={{ fontSize: 10, color: '#94a3b8' }}>⊘ {classifyExclusion(detail)}</span>}
            <button className="btn-secondary" style={{ marginLeft: 'auto', fontSize: 10, padding: '2px 8px' }} onClick={() => setDetail(null)}>Close</button>
          </div>
          <div style={{ fontSize: 11.5, color: detail.deployed === false ? '#fbbf24' : '#4ade80', marginTop: 4 }}>
            {detail.deployed === false ? (detail.exclusion_reason || 'excluded from the view') : 'Deployed in the view.'}
          </div>
          {excludedDeps(detail, measures).length > 0 && (
            <div style={{ fontSize: 11, marginTop: 3 }}>
              <span style={{ color: '#64748b' }}>Depends on excluded: </span>
              <span style={{ color: '#f0abfc', fontWeight: 600 }}>{excludedDeps(detail, measures).join(', ')}</span>
            </div>
          )}
          <div style={{ marginTop: 6, fontSize: 10, textTransform: 'uppercase', color: '#64748b' }}>Original DAX</div>
          <code style={{ display: 'block', whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: '#fbbf24', fontSize: 11 }}>{detail.original_dax || '—'}</code>
          <div style={{ marginTop: 4, fontSize: 10, textTransform: 'uppercase', color: '#64748b' }}>Translated SQL</div>
          <code style={{ display: 'block', whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: '#4ade80', fontSize: 11 }}>{detail.translated_sql || '—'}</code>
        </div>
      )}
    </div>
  );
}

function ResultsExplorer({ groups, totals }) {
  const [openGroup, setOpenGroup] = useState(null);
  const [openMeasure, setOpenMeasure] = useState(null);
  const [openIssues, setOpenIssues] = useState(null);
  const [measureTab, setMeasureTab] = useState('converted');
  const [excludedCat, setExcludedCat] = useState('all');
  const [showOverrides, setShowOverrides] = useState(false);
  const [showGraph, setShowGraph] = useState(false);
  const sorted = [...(groups || [])].sort((a, b) => (b.total_measures || 0) - (a.total_measures || 0));
  const totalMeasures = sorted.reduce((s, g) => s + (g.total_measures || 0), 0);

  if (sorted.length === 0) {
    return <div className="glass" style={{ padding: 16, color: '#64748b', fontSize: 13 }}>No results yet — run a migration first.</div>;
  }

  const chip = (label, value, color) => (
    <div className="glass" style={{ padding: '10px 14px', flex: 1 }}>
      <div style={{ fontSize: 10, color: '#94a3b8', textTransform: 'uppercase' }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color, marginTop: 2 }}>{value}</div>
    </div>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', gap: 10 }}>
        {chip('Fact tables / views', sorted.length, '#a5b4fc')}
        {chip('Measures', totalMeasures, '#6366f1')}
        {chip('In view', totals?.deployed ?? '—', '#4ade80')}
        {chip('Not in view', totals?.not_deployed ?? '—', '#94a3b8')}
        {chip('Conversion', totals?.overall_conversion_rate != null ? `${totals.overall_conversion_rate}%` : '—', '#a78bfa')}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 11, color: '#64748b' }}>Click a fact table to see its measures; click a measure for the DAX → SQL detail.</span>
        <label style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#94a3b8', cursor: 'pointer' }}>
          <input type="checkbox" checked={showOverrides} onChange={e => setShowOverrides(e.target.checked)} />
          Show suggested overrides
        </label>
      </div>

      {sorted.map(g => {
        const isOpen = openGroup === g.name;
        const rate = g.conversion_rate ?? 0;
        const vClass = g.validation_status === 'OK' ? 'tag-success' : g.validation_status === 'WARNINGS' ? 'tag-warning' : 'tag-error';
        // Excluded is a deployment axis (matches the "not in view" badge);
        // conversion quality is a status axis (matches the partial/converted
        // badges). They overlap on purpose — a partial measure that was also
        // excluded from the view shows in both its status tab and Excluded.
        const excludedMeasures = (g.measures || []).filter(x => x.deployed === false);
        const convertedMeasures = (g.measures || []).filter(m => m.deployed !== false && (m.status === 'converted' || m.status === 'manual_override'));
        const partialMeasures = (g.measures || []).filter(m => m.status === 'partial');
        const failedMeasures = (g.measures || []).filter(m => !['converted', 'manual_override', 'partial', 'excluded'].includes(m.status));
        const notInView = excludedMeasures.length;
        const inView = (g.total_measures || 0) - notInView;
        // Aggregate the group's logs so they can be seen in one click, rather
        // than expanding every measure: hard errors, warnings, and the reasons
        // measures were excluded from the deployed view ("not in view").
        const errLogs = (g.measures || []).flatMap(m => (m.issues || []).map(t => ({ measure: m.name, text: t })));
        const warnLogs = (g.measures || []).flatMap(m =>
          (m.warnings || [])
            // The "Excluded from deployed view" warning is already shown, with
            // its reason, in the excluded section below — don't repeat it here.
            .filter(t => !String(t).startsWith('Excluded from deployed view'))
            .map(t => ({ measure: m.name, text: t }))
        );
        const issuesOpen = openIssues === g.name;
        const logCount = errLogs.length + warnLogs.length + notInView;
        const toggleIssues = (e) => { e.stopPropagation(); setOpenIssues(issuesOpen ? null : g.name); };
        return (
          <div key={g.name} className="glass" style={{ overflow: 'hidden' }}>
            <div onClick={() => { setOpenGroup(isOpen ? null : g.name); setOpenMeasure(null); }}
              style={{ padding: '12px 16px', cursor: 'pointer', display: 'grid', gridTemplateColumns: '18px minmax(160px,1.4fr) 1fr 120px 90px', gap: 12, alignItems: 'center' }}>
              <span style={{ color: '#6366f1', transition: 'transform .15s', transform: isOpen ? 'rotate(90deg)' : 'none' }}>▶</span>
              <div>
                <div style={{ fontWeight: 600, fontSize: 14 }}>{g.name}</div>
                <div style={{ fontSize: 10, color: '#64748b' }}>{g.source_table}</div>
              </div>
              <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
                <span className="tag tag-success" style={{ fontSize: 10 }}>{inView} in view</span>
                {notInView > 0 && <span className="tag" title="Click to see why these were excluded from the deployed view" onClick={toggleIssues} style={{ fontSize: 10, background: 'rgba(148,163,184,0.15)', color: '#94a3b8', cursor: 'pointer', textDecoration: issuesOpen ? 'underline' : 'none' }}>⊘ {notInView} not in view</span>}
                {(g.dimensions || []).length > 0 && <span className="tag tag-info" style={{ fontSize: 10 }}>{g.dimensions.length} dims</span>}
                {g.partial > 0 && <span className="tag tag-warning" style={{ fontSize: 10 }}>{g.partial} partial</span>}
                {g.unsupported > 0 && <span className="tag tag-error" style={{ fontSize: 10 }}>{g.unsupported} unsupported</span>}
              </div>
              <div style={{ minWidth: 100 }}><ConfidenceBar value={rate} /></div>
              <span className={`tag ${vClass}`} onClick={logCount ? toggleIssues : undefined}
                title={logCount ? 'Click to see errors, warnings and excluded-measure reasons' : 'No issues'}
                style={{ fontSize: 10, justifySelf: 'end', cursor: logCount ? 'pointer' : 'default', textDecoration: issuesOpen ? 'underline' : 'none' }}>
                {g.validation_status}{logCount ? ` (${logCount})` : ''}
              </span>
            </div>

            {issuesOpen && (
              <div style={{ borderTop: '1px solid rgba(255,255,255,0.08)', background: 'rgba(0,0,0,0.18)', padding: '12px 16px 14px 34px' }}>
                {errLogs.length > 0 && (
                  <div style={{ marginBottom: warnLogs.length || notInView ? 12 : 0 }}>
                    <div style={{ fontSize: 10, textTransform: 'uppercase', color: '#f87171', marginBottom: 6 }}>Errors ({errLogs.length})</div>
                    {errLogs.map((l, i) => (
                      <div key={`e${i}`} style={{ fontSize: 11.5, color: '#fca5a5', marginBottom: 3 }}>
                        <span style={{ fontWeight: 700, color: '#e2e8f0' }}>{l.measure}</span> — {l.text}
                      </div>
                    ))}
                  </div>
                )}
                {warnLogs.length > 0 && (
                  <div style={{ marginBottom: notInView ? 12 : 0 }}>
                    <div style={{ fontSize: 10, textTransform: 'uppercase', color: '#fbbf24', marginBottom: 6 }}>Warnings ({warnLogs.length})</div>
                    {warnLogs.map((l, i) => (
                      <div key={`w${i}`} style={{ fontSize: 11.5, color: '#fcd34d', marginBottom: 3 }}>
                        <span style={{ fontWeight: 700, color: '#e2e8f0' }}>{l.measure}</span> — {l.text}
                      </div>
                    ))}
                  </div>
                )}
                {notInView > 0 && (
                  <div>
                    <div style={{ fontSize: 10, textTransform: 'uppercase', color: '#94a3b8', marginBottom: 6 }}>Excluded from the deployed view ({notInView})</div>
                    {excludedMeasures.map((m, i) => (
                      <div key={`x${i}`} style={{ fontSize: 11.5, color: '#cbd5e1', marginBottom: 3 }}>
                        <span style={{ fontWeight: 700 }}>{m.name}</span> — <span style={{ color: '#94a3b8' }}>{m.exclusion_reason || 'not deployable'}</span>
                      </div>
                    ))}
                    <div style={{ fontSize: 10.5, color: '#64748b', marginTop: 6 }}>These are candidates for a manual measure_override. Click a measure below for its full DAX → SQL detail.</div>
                  </div>
                )}
              </div>
            )}

            {isOpen && (() => {
              const tabs = [
                ['converted', 'Successfully converted', convertedMeasures, '#4ade80'],
                ['partial', 'Partial conversion', partialMeasures, '#fbbf24'],
                ['failed', 'Failed conversion', failedMeasures, '#f87171'],
                ['excluded', 'Excluded from view', excludedMeasures, '#94a3b8'],
              ];
              const active = tabs.find(t => t[0] === measureTab) || tabs[0];
              let rows = active[2];
              // For the Excluded tab, sub-classify by exclusion type and let the
              // user filter by category (Time intelligence, No SQL analog, etc.).
              let subFilter = null;
              if (measureTab === 'excluded' && excludedMeasures.length > 0) {
                const cats = {};
                excludedMeasures.forEach(m => { const c = classifyExclusion(m); (cats[c] = cats[c] || []).push(m); });
                const catNames = Object.keys(cats).sort((a, b) => cats[b].length - cats[a].length);
                rows = excludedCat === 'all' ? excludedMeasures : (cats[excludedCat] || []);
                const catChip = (label, count, activeCat) => (
                  <span key={label} onClick={(e) => { e.stopPropagation(); setExcludedCat(label); setOpenMeasure(null); }}
                    className="tag" style={{ cursor: 'pointer', fontSize: 10, background: activeCat ? 'rgba(99,102,241,0.3)' : 'rgba(255,255,255,0.06)', color: activeCat ? '#c7d2fe' : '#cbd5e1', border: activeCat ? '1px solid rgba(99,102,241,0.6)' : '1px solid transparent' }}>
                    {label} {count}
                  </span>
                );
                subFilter = (
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', padding: '8px 14px 8px 30px', borderBottom: '1px solid rgba(255,255,255,0.06)', alignItems: 'center' }}>
                    <span style={{ fontSize: 10, textTransform: 'uppercase', color: '#64748b', marginRight: 4 }}>Reason</span>
                    {catChip('all', excludedMeasures.length, excludedCat === 'all')}
                    {catNames.map(c => catChip(c, cats[c].length, excludedCat === c))}
                    <button className="btn-secondary" style={{ marginLeft: 'auto', fontSize: 10, padding: '3px 10px' }}
                      onClick={(e) => { e.stopPropagation(); setShowGraph(v => !v); }}>
                      {showGraph ? 'Hide' : 'Show'} dependency graph
                    </button>
                  </div>
                );
              }
              return (
                <div style={{ borderTop: '1px solid rgba(255,255,255,0.08)' }}>
                  <div style={{ display: 'flex', gap: 4, padding: '8px 14px 0 30px', borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
                    {tabs.map(([id, label, list, color]) => (
                      <div key={id} onClick={(e) => { e.stopPropagation(); setMeasureTab(id); setOpenMeasure(null); setExcludedCat('all'); }}
                        className={`tab ${measureTab === id ? 'tab-active' : 'tab-inactive'}`} style={{ fontSize: 12 }}>
                        {label} <span style={{ color, fontWeight: 700 }}>{list.length}</span>
                      </div>
                    ))}
                  </div>
                  {subFilter}
                  {measureTab === 'excluded' && showGraph && (
                    <div style={{ padding: '12px 14px 12px 30px', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                      <div style={{ fontSize: 10, textTransform: 'uppercase', color: '#64748b', marginBottom: 6 }}>Dependency graph — cascaded exclusions</div>
                      <DependencyGraph measures={g.measures} />
                    </div>
                  )}
                  <div style={{ padding: '6px 14px 6px 30px', display: 'grid', gridTemplateColumns: 'minmax(150px,1fr) minmax(180px,1.6fr) minmax(180px,1.6fr) 90px', gap: 12, fontSize: 10, textTransform: 'uppercase', color: '#64748b', position: 'sticky', top: 0, background: '#202039' }}>
                    <span>Measure</span><span>Original DAX (Power BI)</span><span>Migrated SQL</span><span>Confidence</span>
                  </div>
                  <div style={{ maxHeight: 460, overflowY: 'auto' }}>
                    {rows.length === 0 && (
                      <div style={{ padding: '14px 30px', color: '#64748b', fontSize: 12 }}>No measures in this category.</div>
                    )}
                    {rows.map((m, i) => {
                      const key = `${g.name}::${measureTab}::${m.name}::${i}`;
                      const mOpen = openMeasure === key;
                      const color = STATUS_COLOR[m.status] || '#94a3b8';
                      const notInView = m.deployed === false;
                      // Clamp long code to 2 lines but keep it visible for every
                      // measure — errored / unconverted ones included.
                      const codeClamp = { fontSize: 11, lineHeight: 1.4, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden', whiteSpace: 'pre-wrap', wordBreak: 'break-word' };
                      return (
                        <div key={key}>
                          <div onClick={() => setOpenMeasure(mOpen ? null : key)}
                            style={{ padding: '9px 14px 9px 30px', cursor: 'pointer', display: 'grid', gridTemplateColumns: 'minmax(150px,1fr) minmax(180px,1.6fr) minmax(180px,1.6fr) 90px', gap: 12, alignItems: 'start', fontSize: 12, borderBottom: '1px solid rgba(255,255,255,0.04)', borderLeft: `3px solid ${color}`, background: mOpen ? 'rgba(99,102,241,0.06)' : notInView ? 'rgba(148,163,184,0.05)' : 'transparent' }}>
                            <div style={{ minWidth: 0 }}>
                              <div style={{ fontWeight: 600, opacity: notInView ? 0.75 : 1, wordBreak: 'break-word' }}>{m.name}</div>
                              <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', marginTop: 3, alignItems: 'center' }}>
                                <span className="tag" style={{ fontSize: 9, background: `${color}22`, color }}>{m.status}</span>
                                {notInView && <span title={m.exclusion_reason} className="tag" style={{ fontSize: 9, background: 'rgba(148,163,184,0.15)', color: '#cbd5e1' }}>⊘ {classifyExclusion(m)}</span>}
                                {notInView && excludedDeps(m, g.measures).length > 0 && (
                                  <span title={m.exclusion_reason} style={{ fontSize: 9, color: '#f0abfc' }}>→ {excludedDeps(m, g.measures).join(', ')}</span>
                                )}
                              </div>
                            </div>
                            <code title={m.original_dax} style={{ ...codeClamp, color: '#fbbf24' }}>{m.original_dax || '—'}</code>
                            <code title={m.translated_sql} style={{ ...codeClamp, color: notInView ? '#94a3b8' : '#4ade80' }}>{m.translated_sql || (notInView ? '(excluded from view)' : '—')}</code>
                            <ConfidenceBar value={m.confidence} />
                          </div>
                          {!mOpen && showOverrides && needsOverride(m) && (
                            <div style={{ padding: '0 14px 0 30px' }}><OverrideStub m={m} /></div>
                          )}
                          {mOpen && <MeasureDetail m={m} groupMeasures={g.measures} />}
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })()}
          </div>
        );
      })}
    </div>
  );
}

export default function App() {
  const [page, setPage] = useState('connect');
  const [connectTab, setConnectTab] = useState('pbi');
  const [selectedModel, setSelectedModel] = useState(null);
  const [history, setHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [migrationState, setMigrationState] = useState('idle');
  const [migrationProgress, setMigrationProgress] = useState(0);
  const [migrationLog, setMigrationLog] = useState([]);
  const [migrationId, setMigrationId] = useState(null);
  const [generatedYAML, setGeneratedYAML] = useState('');
  const [generatedSQL, setGeneratedSQL] = useState('');
  const [migrationReport, setMigrationReport] = useState(null);
  const [activeTab, setActiveTab] = useState('yaml');
  const [deployStatus, setDeployStatus] = useState('idle');
  const [deployLog, setDeployLog] = useState([]);
  const [rollbackTarget, setRollbackTarget] = useState('');

  // Connect state
  const [pbiAuth, setPbiAuth] = useState({ tenant_id: '', client_id: '', client_secret: '' });
  const [pbiConnected, setPbiConnected] = useState(false);
  const [pbiWorkspaces, setPbiWorkspaces] = useState([]);
  const [pbiLoading, setPbiLoading] = useState(false);
  const [pbiError, setPbiError] = useState('');

  const [dbxAuth, setDbxAuth] = useState({ host: '', token: '' });
  const [dbxConnected, setDbxConnected] = useState(false);
  const [dbxWarehouses, setDbxWarehouses] = useState([]);
  const [dbxCatalogs, setDbxCatalogs] = useState([]);
  const [dbxLoading, setDbxLoading] = useState(false);
  const [dbxError, setDbxError] = useState('');

  // Migrate config
  const [dbxConfig, setDbxConfig] = useState({ catalog: 'hls_amer_catalog', schema: 'metrics', warehouse_id: '' });
  const [deployDryRun, setDeployDryRun] = useState(false);
  const [convertNestedWindows, setConvertNestedWindows] = useState(true);

  // TMDL upload
  const [tmdlFile, setTmdlFile] = useState(null);
  const [tmdlLoading, setTmdlLoading] = useState(false);
  const [tmdlError, setTmdlError] = useState('');
  const [tmdlDragOver, setTmdlDragOver] = useState(false);
  const tmdlInputRef = useRef();

  // Explore: DAX translate
  const [daxInput, setDaxInput] = useState('');
  const [daxResult, setDaxResult] = useState('');
  const [daxLoading, setDaxLoading] = useState(false);

  const addLog = useCallback((msg, type = 'info') => {
    setMigrationLog(prev => [...prev, { time: new Date().toLocaleTimeString(), msg, type }]);
  }, []);

  const addDeployLog = useCallback((msg, type = 'info') => {
    setDeployLog(prev => [...prev, { time: new Date().toLocaleTimeString(), msg, type }]);
  }, []);

  // Load persisted migration history (past uploads + migration results).
  const loadHistory = useCallback(() => {
    setHistoryLoading(true);
    apiFetch('/api/migrations')
      .then(data => setHistory(data.migrations || []))
      .catch(() => {})
      .finally(() => setHistoryLoading(false));
  }, []);

  useEffect(() => { loadHistory(); }, [loadHistory]);

  // Poll migration status
  useEffect(() => {
    if (!migrationId || migrationState === 'complete' || migrationState === 'error') return;
    const interval = setInterval(async () => {
      try {
        const data = await apiFetch(`/api/migrate/${migrationId}/status`);
        const status = data.status || data.state;
        const uiState = status === 'failed' ? 'error'
          : ['complete', 'validated', 'dry_run'].includes(status) ? 'complete'
          : status;
        if (data.progress !== undefined) setMigrationProgress(data.progress);
        if (uiState) setMigrationState(uiState);
        if (data.logs?.length) {
          const newLogs = data.logs.slice(migrationLog.length);
          newLogs.forEach(l => addLog(l.message || l.msg, l.level || 'info'));
        }
        if (data.yaml) setGeneratedYAML(data.yaml);
        if (data.sql) setGeneratedSQL(data.sql);
        if (uiState === 'complete' || uiState === 'error') {
          clearInterval(interval);
          if (uiState === 'complete') {
            apiFetch(`/api/migrate/${migrationId}/report`).then(r => {
              setGeneratedYAML(Object.values(r.generated_yaml || {}).join('\n\n'));
              setGeneratedSQL(Object.values(r.generated_sql || {}).join('\n\n'));
              setMigrationReport(r);
            }).catch(() => {});
          }
        }
      } catch (_) {}
    }, 1500);
    return () => clearInterval(interval);
  }, [migrationId, migrationState, migrationLog.length, addLog]);

  // PBI Connect
  const connectPBI = async () => {
    setPbiLoading(true); setPbiError('');
    try {
      await apiFetch('/api/pbi/auth', { method: 'POST', body: JSON.stringify(pbiAuth) });
      const ws = await apiFetch('/api/pbi/workspaces');
      setPbiWorkspaces(ws.workspaces || ws || []);
      setPbiConnected(true);
      addLog('Power BI connected via OAuth2', 'success');
    } catch (e) { setPbiError(e.message); }
    finally { setPbiLoading(false); }
  };

  // DBX Connect
  const connectDBX = async () => {
    setDbxLoading(true); setDbxError('');
    try {
      await apiFetch('/api/dbx/auth', { method: 'POST', body: JSON.stringify(dbxAuth) });
      const [wh, cats] = await Promise.all([apiFetch('/api/dbx/warehouses'), apiFetch('/api/dbx/catalogs')]);
      setDbxWarehouses(wh.warehouses || wh || []);
      setDbxCatalogs(cats.catalogs || cats || []);
      const firstWh = (wh.warehouses || wh || [])[0];
      if (firstWh) setDbxConfig(c => ({ ...c, warehouse_id: firstWh.id || firstWh.warehouse_id || '' }));
      setDbxConnected(true);
      addLog('Databricks connected — warehouse active', 'success');
    } catch (e) { setDbxError(e.message); }
    finally { setDbxLoading(false); }
  };

  // TMDL Upload
  const uploadTMDL = async (file) => {
    setTmdlLoading(true); setTmdlError('');
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch('/api/tmdl/upload', { method: 'POST', body: fd });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
      const data = await res.json();
      setSelectedModel(data.model || data);
      setPage('explore');
    } catch (e) { setTmdlError(e.message); }
    finally { setTmdlLoading(false); }
  };

  // Run migration via real API
  const runMigration = async (model) => {
    setMigrationState('extracting'); setMigrationProgress(0); setMigrationLog([]); setMigrationReport(null);
    setGeneratedYAML(''); setGeneratedSQL(''); setMigrationId(null);
    addLog(`Starting migration: ${model.name}`, 'info');
    try {
      const payload = {
        model,
        catalog: dbxConfig.catalog,
        schema: dbxConfig.schema,
        warehouse_id: dbxConfig.warehouse_id,
        deploy: !deployDryRun,
        dry_run: deployDryRun,
        convert_nested_windows: convertNestedWindows,
      };
      const data = await apiFetch('/api/migrate', { method: 'POST', body: JSON.stringify(payload) });
      setMigrationId(data.migration_id || data.id);
      setGeneratedYAML(Object.values(data.generated_yaml || {}).join('\n\n'));
      setGeneratedSQL(Object.values(data.generated_sql || {}).join('\n\n'));
      setMigrationReport(data);
      (data.steps || []).forEach(step => addLog(`${step.name}: ${step.message}`, step.status === 'failed' ? 'error' : 'success'));

      const status = data.status;
      const uiState = status === 'failed' ? 'error'
        : ['complete', 'validated', 'dry_run'].includes(status) ? 'complete'
        : status || 'complete';
      setMigrationState(uiState);
      setMigrationProgress(['complete', 'error'].includes(uiState) ? 100 : 50);
      addLog(
        uiState === 'error'
          ? `Migration failed (ID: ${data.migration_id || data.id})`
          : `Migration complete (ID: ${data.migration_id || data.id})`,
        uiState === 'error' ? 'error' : 'success'
      );
      loadHistory();  // the run is now persisted — refresh the History list
    } catch (e) {
      addLog(`Error: ${e.message}`, 'error');
      setMigrationState('error');
    }
  };

  // Reopen a persisted migration (its uploaded model + results) from History.
  const openMigration = async (id) => {
    try {
      const rec = await apiFetch(`/api/migrations/${id}`);
      setSelectedModel(rec.source_model || null);
      setMigrationId(rec.migration_id || id);
      setMigrationReport(rec);
      setGeneratedYAML(Object.values(rec.generated_yaml || {}).join('\n\n'));
      setGeneratedSQL(Object.values(rec.generated_sql || {}).join('\n\n'));
      setMigrationState(rec.status === 'failed' ? 'error' : 'complete');
      setMigrationProgress(100);
      setMigrationLog([]);
      (rec.steps || []).forEach(s => addLog(`${s.name}: ${s.message}`, s.status === 'failed' ? 'error' : 'success'));
      setActiveTab('results');
      setPage('migrate');
    } catch (_) { /* ignore — record may have been removed */ }
  };

  // Translate single DAX measure
  const translateDAX = async () => {
    if (!daxInput.trim()) return;
    setDaxLoading(true);
    try {
      const table = selectedModel?.tables?.[0]?.name || 'FactTable';
      const data = await apiFetch('/api/translate', { method: 'POST', body: JSON.stringify({ dax: daxInput, table_name: table }) });
      setDaxResult(data.sql || translateDAXtoSQL(daxInput));
    } catch (_) { setDaxResult(translateDAXtoSQL(daxInput)); }
    finally { setDaxLoading(false); }
  };

  // Deploy SQL
  const deploySql = async () => {
    if (!generatedSQL) return;
    setDeployStatus('deploying'); setDeployLog([]);
    addDeployLog('Validating SQL...', 'info');
    try {
      await apiFetch('/api/dbx/validate', { method: 'POST', body: JSON.stringify({ sql: generatedSQL }) });
      addDeployLog('Validation passed', 'success');
      addDeployLog('Deploying to Databricks...', 'info');
      const res = await apiFetch('/api/dbx/deploy', { method: 'POST', body: JSON.stringify({ sql: generatedSQL, warehouse_id: dbxConfig.warehouse_id, catalog: dbxConfig.catalog, schema: dbxConfig.schema }) });
      addDeployLog(`Deployed: ${res.view_name || 'metric view created'}`, 'success');
      setRollbackTarget(res.view_name || '');
      setDeployStatus('success');
    } catch (e) {
      addDeployLog(`Error: ${e.message}`, 'error');
      setDeployStatus('error');
    }
  };

  const rollback = async () => {
    if (!rollbackTarget) return;
    try {
      await apiFetch('/api/dbx/rollback', { method: 'POST', body: JSON.stringify({ view_name: rollbackTarget, warehouse_id: dbxConfig.warehouse_id }) });
      addDeployLog(`Rolled back: ${rollbackTarget}`, 'warning');
      setDeployStatus('idle');
    } catch (e) { addDeployLog(`Rollback failed: ${e.message}`, 'error'); }
  };

  const navItems = [
    { id: 'connect', icon: '⚡', label: 'Connect' },
    { id: 'explore', icon: '🔍', label: 'Explore' },
    { id: 'migrate', icon: '🔄', label: 'Migrate' },
    { id: 'deploy', icon: '🚀', label: 'Deploy' },
    { id: 'history', icon: '🕘', label: 'History' },
  ];

  const pipelineSummary = migrationReport?.pipeline_summary || migrationReport;
  const factGroups = pipelineSummary?.fact_groups || [];
  const translationResults = factGroups.flatMap(group =>
    (group.measures || []).map(measure => ({ ...measure, fact_group: group.name }))
  );
  const validationResult = migrationReport?.validation_result || {};
  const validationIssues = validationResult.issues || [];

  return (
    <div style={{ display: 'flex', height: '100vh', fontFamily: '-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif', background: 'linear-gradient(135deg,#0f0f23 0%,#1a1a3e 50%,#0d1117 100%)', color: '#e2e8f0', overflow: 'hidden' }}>
      <style>{`
        @keyframes pulse { 0%,100%{opacity:1}50%{opacity:0.5} }
        @keyframes progressGlow { 0%{box-shadow:0 0 5px #6366f1}50%{box-shadow:0 0 20px #6366f1,0 0 40px #818cf8}100%{box-shadow:0 0 5px #6366f1} }
        .glass { background:rgba(255,255,255,0.05); backdrop-filter:blur(12px); border:1px solid rgba(255,255,255,0.1); border-radius:12px; }
        .glass:hover { border-color:rgba(99,102,241,0.4); }
        .btn-primary { background:linear-gradient(135deg,#6366f1,#8b5cf6); border:none; color:white; padding:10px 20px; border-radius:8px; cursor:pointer; font-weight:600; transition:all 0.2s; }
        .btn-primary:hover { transform:translateY(-1px); box-shadow:0 4px 15px rgba(99,102,241,0.4); }
        .btn-primary:disabled { opacity:0.5; cursor:not-allowed; transform:none; }
        .btn-secondary { background:rgba(255,255,255,0.08); border:1px solid rgba(255,255,255,0.15); color:#e2e8f0; padding:8px 16px; border-radius:8px; cursor:pointer; transition:all 0.2s; }
        .btn-secondary:hover { background:rgba(255,255,255,0.12); border-color:rgba(99,102,241,0.3); }
        .code-block { background:#0d1117; border:1px solid rgba(255,255,255,0.08); border-radius:8px; padding:16px; font-family:'JetBrains Mono',Consolas,monospace; font-size:12px; line-height:1.6; overflow:auto; white-space:pre-wrap; word-break:break-word; color:#c9d1d9; }
        .nav-item { display:flex; align-items:center; gap:10px; padding:10px 16px; border-radius:8px; cursor:pointer; transition:all 0.2s; font-size:14px; }
        .nav-item:hover { background:rgba(99,102,241,0.15); }
        .nav-active { background:rgba(99,102,241,0.2); border-left:3px solid #6366f1; }
        .tag { display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }
        .tag-success { background:rgba(34,197,94,0.15); color:#4ade80; }
        .tag-warning { background:rgba(251,191,36,0.15); color:#fbbf24; }
        .tag-error { background:rgba(248,113,113,0.15); color:#f87171; }
        .tag-info { background:rgba(99,102,241,0.15); color:#818cf8; }
        .progress-bar { height:8px; border-radius:4px; background:rgba(255,255,255,0.08); overflow:hidden; }
        .progress-fill { height:100%; border-radius:4px; background:linear-gradient(90deg,#6366f1,#8b5cf6,#a78bfa); transition:width 0.6s cubic-bezier(0.4,0,0.2,1); animation:progressGlow 2s infinite; }
        .input { background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.12); border-radius:8px; padding:10px 14px; color:#e2e8f0; font-size:14px; width:100%; outline:none; transition:border-color 0.2s; box-sizing:border-box; }
        .input:focus { border-color:#6366f1; }
        .scroll-area { overflow-y:auto; scrollbar-width:thin; scrollbar-color:rgba(255,255,255,0.1) transparent; }
        .tab { padding:8px 16px; border-radius:6px 6px 0 0; cursor:pointer; font-size:13px; font-weight:500; transition:all 0.2s; }
        .tab-active { background:rgba(99,102,241,0.2); color:#a5b4fc; border-bottom:2px solid #6366f1; }
        .tab-inactive { background:transparent; color:#94a3b8; }
        .tab-inactive:hover { color:#e2e8f0; }
        .drop-zone { border:2px dashed rgba(99,102,241,0.4); border-radius:12px; padding:40px; text-align:center; cursor:pointer; transition:all 0.2s; }
        .drop-zone.drag-over { border-color:#6366f1; background:rgba(99,102,241,0.08); }
        .drop-zone:hover { border-color:rgba(99,102,241,0.6); background:rgba(99,102,241,0.04); }
        select.input { appearance:none; }
      `}</style>

      {/* Sidebar */}
      <div style={{ width: 220, borderRight: '1px solid rgba(255,255,255,0.06)', padding: '20px 12px', display: 'flex', flexDirection: 'column', gap: 4, flexShrink: 0 }}>
        <div style={{ padding: '0 16px 20px', borderBottom: '1px solid rgba(255,255,255,0.06)', marginBottom: 12 }}>
          <div style={{ fontSize: 18, fontWeight: 700, background: 'linear-gradient(135deg,#6366f1,#a78bfa)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>PBI → DBX</div>
          <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>Metrics Migration Tool</div>
        </div>
        {navItems.map(n => (
          <motion.div key={n.id} className={`nav-item ${page === n.id ? 'nav-active' : ''}`}
            onClick={() => { setPage(n.id); if (n.id === 'history') loadHistory(); }} whileHover={{ x: 4 }} whileTap={{ scale: 0.97 }}>
            <span style={{ fontSize: 16 }}>{n.icon}</span> {n.label}
          </motion.div>
        ))}
        <div style={{ marginTop: 'auto', padding: '12px 16px', borderTop: '1px solid rgba(255,255,255,0.06)' }}>
          <div style={{ fontSize: 11, color: '#64748b' }}>Connection Status</div>
          <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
            <span className={`tag ${pbiConnected ? 'tag-success' : 'tag-info'}`} style={{ fontSize: 10 }}>{pbiConnected ? '● PBI' : '○ PBI'}</span>
            <span className={`tag ${dbxConnected ? 'tag-success' : 'tag-info'}`} style={{ fontSize: 10 }}>{dbxConnected ? '● DBX' : '○ DBX'}</span>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
        <div style={{ padding: '16px 24px', borderBottom: '1px solid rgba(255,255,255,0.06)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h1 style={{ fontSize: 20, fontWeight: 600, margin: 0 }}>
            {page === 'connect' && 'Connect Services'}
            {page === 'explore' && 'Explore Semantic Models'}
            {page === 'migrate' && 'DAX → YAML Migration'}
            {page === 'deploy' && 'Deploy to Databricks'}
            {page === 'history' && 'Migration History'}
          </h1>
          <div style={{ fontSize: 12, color: '#64748b' }}>Port 8000 • Serverless SQL Warehouse</div>
        </div>

        <div className="scroll-area" style={{ flex: 1, padding: 24, overflowY: 'auto' }}>
          <AnimatePresence mode="wait">

            {/* CONNECT */}
            {page === 'connect' && (
              <motion.div key="connect" variants={pageVariants} initial="initial" animate="animate" exit="exit" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
                <div style={{ display: 'flex', gap: 4 }}>
                  {[['pbi', '📊 Power BI OAuth2'], ['tmdl', '📁 TMDL Upload']].map(([id, label]) => (
                    <div key={id} className={`tab ${connectTab === id ? 'tab-active' : 'tab-inactive'}`} onClick={() => setConnectTab(id)}>{label}</div>
                  ))}
                </div>

                <AnimatePresence mode="wait">
                  {connectTab === 'pbi' && (
                    <motion.div key="pbi" initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }} className="glass" style={{ padding: 24, maxWidth: 480 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 20 }}>
                        <span style={{ fontSize: 24 }}>📊</span>
                        <h3 style={{ margin: 0 }}>Power BI Connection</h3>
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                        <div><label style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4, display: 'block' }}>Tenant ID</label><input className="input" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" value={pbiAuth.tenant_id} onChange={e => setPbiAuth(a => ({ ...a, tenant_id: e.target.value }))} /></div>
                        <div><label style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4, display: 'block' }}>Client ID</label><input className="input" placeholder="App Registration Client ID" value={pbiAuth.client_id} onChange={e => setPbiAuth(a => ({ ...a, client_id: e.target.value }))} /></div>
                        <div><label style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4, display: 'block' }}>Client Secret</label><input className="input" type="password" placeholder="Client secret (optional if using token)" value={pbiAuth.client_secret} onChange={e => setPbiAuth(a => ({ ...a, client_secret: e.target.value }))} /></div>
                        {pbiError && <div className="tag tag-error">{pbiError}</div>}
                        <motion.button className="btn-primary" disabled={pbiLoading || !pbiAuth.tenant_id} whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }} onClick={connectPBI}>
                          {pbiLoading ? 'Connecting...' : pbiConnected ? '✓ Connected' : 'Connect via OAuth2'}
                        </motion.button>
                        {pbiConnected && pbiWorkspaces.length > 0 && (
                          <div>
                            <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 6 }}>Workspaces ({pbiWorkspaces.length})</div>
                            {pbiWorkspaces.slice(0, 5).map((ws, i) => (
                              <div key={i} style={{ fontSize: 12, padding: '4px 8px', background: 'rgba(99,102,241,0.08)', borderRadius: 6, marginBottom: 4 }}>{ws.name || ws.displayName || ws.id}</div>
                            ))}
                          </div>
                        )}
                      </div>
                    </motion.div>
                  )}

                  {connectTab === 'tmdl' && (
                    <motion.div key="tmdl" initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }} className="glass" style={{ padding: 24, maxWidth: 520 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 20 }}>
                        <span style={{ fontSize: 24 }}>📁</span>
                        <h3 style={{ margin: 0 }}>Upload TMDL / PBIX Archive</h3>
                      </div>
                      <div
                        className={`drop-zone ${tmdlDragOver ? 'drag-over' : ''}`}
                        onDragOver={e => { e.preventDefault(); setTmdlDragOver(true); }}
                        onDragLeave={() => setTmdlDragOver(false)}
                        onDrop={e => { e.preventDefault(); setTmdlDragOver(false); const f = e.dataTransfer.files[0]; if (f) { setTmdlFile(f); uploadTMDL(f); } }}
                        onClick={() => tmdlInputRef.current?.click()}
                      >
                        <input ref={tmdlInputRef} type="file" accept=".zip,.pbix,.tmdl" style={{ display: 'none' }} onChange={e => { const f = e.target.files[0]; if (f) { setTmdlFile(f); uploadTMDL(f); } }} />
                        {tmdlLoading ? (
                          <div style={{ color: '#818cf8' }}>⏳ Uploading and parsing...</div>
                        ) : tmdlFile ? (
                          <div style={{ color: '#4ade80' }}>✓ {tmdlFile.name}</div>
                        ) : (
                          <div>
                            <div style={{ fontSize: 32, marginBottom: 12 }}>📦</div>
                            <div style={{ color: '#94a3b8', fontSize: 14 }}>Drag & drop a ZIP / PBIX file here</div>
                            <div style={{ color: '#64748b', fontSize: 12, marginTop: 4 }}>or click to browse</div>
                            <div style={{ marginTop: 12 }}><span className="tag tag-info">.zip</span> <span className="tag tag-info">.pbix</span> <span className="tag tag-info">.tmdl</span></div>
                          </div>
                        )}
                      </div>
                      {tmdlError && <div className="tag tag-error" style={{ marginTop: 12 }}>{tmdlError}</div>}
                      <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginTop: 16, fontSize: 13, color: '#94a3b8', cursor: 'pointer' }}>
                        <input type="checkbox" style={{ marginTop: 2 }} checked={convertNestedWindows} onChange={e => setConvertNestedWindows(e.target.checked)} />
                        <span>
                          Convert nested window measures
                          <div style={{ fontSize: 11, color: '#64748b' }}>Some measures layer one time calculation on top of another — for example last year's month-to-date, which compares a running total to the same point a year earlier. When enabled, these are rebuilt so they land in the view; when off, they're left out and flagged for manual review.</div>
                        </span>
                      </label>
                    </motion.div>
                  )}

                </AnimatePresence>

                {/* DBX Auth below tabs */}
                <motion.div className="glass" style={{ padding: 24, maxWidth: 480 }} initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 20 }}>
                    <span style={{ fontSize: 24 }}>🔧</span>
                    <h3 style={{ margin: 0 }}>Databricks Connection</h3>
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                    <div><label style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4, display: 'block' }}>Workspace URL</label><input className="input" placeholder="https://your-workspace.cloud.databricks.com" value={dbxAuth.host} onChange={e => setDbxAuth(a => ({ ...a, host: e.target.value }))} /></div>
                    <div><label style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4, display: 'block' }}>Access Token (PAT)</label><input className="input" type="password" placeholder="dapi..." value={dbxAuth.token} onChange={e => setDbxAuth(a => ({ ...a, token: e.target.value }))} /></div>
                    {dbxError && <div className="tag tag-error">{dbxError}</div>}
                    <motion.button className="btn-primary" disabled={dbxLoading || !dbxAuth.host} whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }} onClick={connectDBX}>
                      {dbxLoading ? 'Connecting...' : dbxConnected ? '✓ Connected' : 'Connect to Workspace'}
                    </motion.button>
                    {dbxConnected && dbxWarehouses.length > 0 && (
                      <div>
                        <label style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4, display: 'block' }}>SQL Warehouse</label>
                        <select className="input" value={dbxConfig.warehouse_id} onChange={e => setDbxConfig(c => ({ ...c, warehouse_id: e.target.value }))}>
                          {dbxWarehouses.map(w => <option key={w.id || w.warehouse_id} value={w.id || w.warehouse_id}>{w.name} ({w.id || w.warehouse_id})</option>)}
                        </select>
                      </div>
                    )}
                  </div>
                </motion.div>
              </motion.div>
            )}

            {/* EXPLORE */}
            {page === 'explore' && (
              <motion.div key="explore" variants={pageVariants} initial="initial" animate="animate" exit="exit" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
                {!selectedModel ? (
                  <div className="glass" style={{ padding: 20 }}>
                    <p style={{ color: '#94a3b8' }}>No model loaded. Upload a TMDL export or connect Power BI in <b>Connect</b>, or reopen a past run from <b>History</b>.</p>
                    <div style={{ display: 'flex', gap: 12, marginTop: 12, flexWrap: 'wrap' }}>
                      <motion.button className="btn-secondary" whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }} onClick={() => setPage('connect')}>Go to Connect</motion.button>
                      <motion.button className="btn-secondary" whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }} onClick={() => { setPage('history'); loadHistory(); }}>Go to History</motion.button>
                    </div>
                  </div>
                ) : (
                  <>
                    <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
                      <h2 style={{ margin: 0, fontSize: 18 }}>{selectedModel.name}</h2>
                      <span className="tag tag-info">{(selectedModel.tables || []).length} tables</span>
                      <motion.button className="btn-primary" style={{ marginLeft: 'auto', fontSize: 13 }} whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}
                        onClick={() => { setPage('migrate'); runMigration(selectedModel); }}>🔄 Migrate This Model</motion.button>
                    </div>

                    {(selectedModel.tables || []).map((table, ti) => (
                      <motion.div key={ti} className="glass" style={{ padding: 20 }} initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }} transition={{ delay: ti * 0.07 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                          <span style={{ fontSize: 14 }}>{table.measures?.length > 0 ? '📋' : '📦'}</span>
                          <span style={{ fontWeight: 600 }}>{table.name}</span>
                          <span className="tag tag-info" style={{ fontSize: 10 }}>{(table.columns || []).length} cols</span>
                          {table.measures?.length > 0 && <span className="tag tag-success" style={{ fontSize: 10 }}>{table.measures.length} measures</span>}
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 8, marginBottom: table.measures?.length ? 16 : 0 }}>
                          {(table.columns || []).map((col, ci) => (
                            <div key={ci} style={{ fontSize: 12, padding: '6px 10px', background: 'rgba(255,255,255,0.03)', borderRadius: 6, display: 'flex', justifyContent: 'space-between' }}>
                              <span>{col.name}</span><span style={{ color: '#64748b', fontSize: 10 }}>{col.dataType}</span>
                            </div>
                          ))}
                        </div>
                        {table.measures?.length > 0 && (
                          <div>
                            <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 8, fontWeight: 600 }}>DAX Measures</div>
                            {table.measures.map((m, mi) => (
                              <motion.div key={mi} initial={{ opacity: 0, x: -10 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: mi * 0.04 }}
                                style={{ padding: '10px 12px', background: 'rgba(99,102,241,0.06)', borderRadius: 8, marginBottom: 6, borderLeft: '3px solid #6366f1' }}>
                                <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 4 }}>{m.name}</div>
                                <code style={{ fontSize: 11, color: '#a78bfa', fontFamily: 'JetBrains Mono,Consolas,monospace' }}>{m.expression}</code>
                                <div style={{ fontSize: 11, color: '#64748b', marginTop: 4 }}>→ {translateDAXtoSQL(m.expression)} <span style={{ color: '#475569' }}>(client preview)</span></div>
                              </motion.div>
                            ))}
                          </div>
                        )}
                      </motion.div>
                    ))}

                    {(selectedModel.relationships || []).length > 0 && (
                      <motion.div className="glass" style={{ padding: 20 }} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.3 }}>
                        <h3 style={{ margin: '0 0 12px', fontSize: 15 }}>Relationships</h3>
                        {selectedModel.relationships.map((r, ri) => (
                          <div key={ri} style={{ fontSize: 12, padding: 8, display: 'flex', gap: 8, alignItems: 'center' }}>
                            <code style={{ color: '#a78bfa' }}>{r.from}</code>
                            <span style={{ color: '#6366f1' }}>→</span>
                            <code style={{ color: '#4ade80' }}>{r.to}</code>
                            <span className="tag tag-info" style={{ fontSize: 10 }}>{r.type}</span>
                          </div>
                        ))}
                      </motion.div>
                    )}

                    {/* DAX translate tester */}
                    <motion.div className="glass" style={{ padding: 20 }} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.35 }}>
                      <h3 style={{ margin: '0 0 12px', fontSize: 15 }}>Test DAX Translation (via API)</h3>
                      <div style={{ display: 'flex', gap: 10 }}>
                        <input className="input" placeholder="e.g. SUM(FactSales[SalesAmount])" value={daxInput} onChange={e => setDaxInput(e.target.value)} onKeyDown={e => e.key === 'Enter' && translateDAX()} />
                        <motion.button className="btn-primary" style={{ whiteSpace: 'nowrap' }} disabled={daxLoading} whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }} onClick={translateDAX}>{daxLoading ? '...' : 'Translate'}</motion.button>
                      </div>
                      {daxResult && <div className="code-block" style={{ marginTop: 10, fontSize: 12 }}>{daxResult}</div>}
                    </motion.div>
                  </>
                )}
              </motion.div>
            )}

            {/* MIGRATE */}
            {page === 'migrate' && (
              <motion.div key="migrate" variants={pageVariants} initial="initial" animate="animate" exit="exit" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
                {migrationState === 'idle' && (
                  <div className="glass" style={{ padding: 24 }}>
                    <h3 style={{ margin: '0 0 16px', fontSize: 15 }}>Migration Configuration</h3>
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginBottom: 16 }}>
                      <div><label style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4, display: 'block' }}>Catalog</label><input className="input" value={dbxConfig.catalog} onChange={e => setDbxConfig(c => ({ ...c, catalog: e.target.value }))} /></div>
                      <div><label style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4, display: 'block' }}>Schema</label><input className="input" value={dbxConfig.schema} onChange={e => setDbxConfig(c => ({ ...c, schema: e.target.value }))} /></div>
                      <div><label style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4, display: 'block' }}>Warehouse ID</label><input className="input" value={dbxConfig.warehouse_id} onChange={e => setDbxConfig(c => ({ ...c, warehouse_id: e.target.value }))} /></div>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
                      <input type="checkbox" id="dryrun" checked={deployDryRun} onChange={e => setDeployDryRun(e.target.checked)} />
                      <label htmlFor="dryrun" style={{ fontSize: 13, color: '#94a3b8', cursor: 'pointer' }}>Dry run (translate only, don't deploy)</label>
                    </div>
                    {selectedModel ? (
                      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
                        <motion.button className="btn-primary" whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }} onClick={() => runMigration(selectedModel)}>
                          🔄 Migrate: {selectedModel.name}
                        </motion.button>
                        <span style={{ fontSize: 12, color: '#64748b' }}>{(selectedModel.tables || []).length} tables loaded</span>
                      </div>
                    ) : (
                      <p style={{ color: '#94a3b8', fontSize: 13 }}>
                        No model loaded. Upload a TMDL export or connect Power BI in <b>Connect</b>, then return here — or reopen a past run from <b>History</b>.
                      </p>
                    )}
                  </div>
                )}

                {migrationState !== 'idle' && (
                  <>
                    <motion.div className="glass" style={{ padding: 20 }} layout>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                        <span style={{ fontWeight: 600 }}>
                          {migrationState === 'complete' ? '✅ Migration Complete' : migrationState === 'error' ? '❌ Error' : migrationState === 'extracting' ? '📥 Extracting...' : migrationState === 'transforming' ? '🔄 Transforming...' : migrationState === 'validating' ? '✓ Validating...' : migrationState === 'deploying' ? '🚀 Deploying...' : migrationState + '...'}
                        </span>
                        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                          <span style={{ fontSize: 14, fontWeight: 700, color: '#6366f1' }}>{migrationProgress}%</span>
                          {(migrationState === 'complete' || migrationState === 'error') && (
                            <button className="btn-secondary" style={{ fontSize: 11, padding: '4px 10px' }} onClick={() => { setMigrationState('idle'); setMigrationProgress(0); setMigrationLog([]); }}>Reset</button>
                          )}
                        </div>
                      </div>
                      <div className="progress-bar">
                        <motion.div className="progress-fill" animate={{ width: `${migrationProgress}%` }} transition={{ duration: 0.6, ease: 'easeOut' }} />
                      </div>
                      <div style={{ display: 'flex', gap: 20, marginTop: 16, justifyContent: 'center' }}>
                        {['extracting', 'transforming', 'validating', 'deploying', 'complete'].map((s, i) => {
                          const states = ['extracting', 'transforming', 'validating', 'deploying', 'complete'];
                          const cur = states.indexOf(migrationState);
                          return (
                            <div key={s} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
                              <motion.div animate={{ background: migrationState === s ? '#6366f1' : cur > i ? '#4ade80' : 'rgba(255,255,255,0.1)', boxShadow: migrationState === s ? '0 0 8px #6366f1' : 'none' }} style={{ width: 8, height: 8, borderRadius: '50%' }} />
                              <span style={{ color: migrationState === s ? '#e2e8f0' : '#64748b', textTransform: 'capitalize' }}>{s}</span>
                            </div>
                          );
                        })}
                      </div>
                    </motion.div>

                    <div style={{ display: 'flex', gap: 4, borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
                      {['yaml', 'sql', 'log', 'results', 'report'].map(t => (
                        <div key={t} className={`tab ${activeTab === t ? 'tab-active' : 'tab-inactive'}`} onClick={() => setActiveTab(t)} style={{ textTransform: 'uppercase' }}>{t}</div>
                      ))}
                    </div>

                    <AnimatePresence mode="wait">
                      {activeTab === 'yaml' && (
                        <motion.div key="yaml" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                            <span style={{ fontSize: 13, fontWeight: 600, color: '#a5b4fc' }}>Generated Metric View YAML (v1.1)</span>
                            {generatedYAML && <button className="btn-secondary" style={{ fontSize: 11, padding: '4px 12px' }} onClick={() => navigator.clipboard?.writeText(generatedYAML)}>Copy</button>}
                          </div>
                          <div className="code-block" style={{ maxHeight: 400 }}>{generatedYAML || '// YAML will appear here after migration runs...'}</div>
                        </motion.div>
                      )}
                      {activeTab === 'sql' && (
                        <motion.div key="sql" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                            <span style={{ fontSize: 13, fontWeight: 600, color: '#a5b4fc' }}>SQL DDL — Statement Execution API Payload</span>
                            {generatedSQL && <button className="btn-secondary" style={{ fontSize: 11, padding: '4px 12px' }} onClick={() => navigator.clipboard?.writeText(generatedSQL)}>Copy</button>}
                          </div>
                          <div className="code-block" style={{ maxHeight: 400 }}>{generatedSQL || '// DDL will appear here after migration runs...'}</div>
                        </motion.div>
                      )}
                      {activeTab === 'log' && (
                        <motion.div key="log" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="code-block" style={{ maxHeight: 320, fontSize: 11 }}>
                          {migrationLog.map((l, i) => (
                            <div key={i} style={{ color: l.type === 'success' ? '#4ade80' : l.type === 'error' ? '#f87171' : '#94a3b8' }}>[{l.time}] {l.msg}</div>
                          ))}
                          {migrationState !== 'complete' && migrationState !== 'error' && migrationState !== 'idle' && <div style={{ animation: 'pulse 1s infinite', color: '#6366f1' }}>▌</div>}
                        </motion.div>
                      )}
                      {activeTab === 'results' && migrationReport && (
                        <motion.div key="results" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                          <ResultsExplorer groups={factGroups} totals={pipelineSummary} />
                        </motion.div>
                      )}
                      {activeTab === 'report' && migrationReport && (
                        <motion.div key="report" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12 }}>
                            {[
                              { label: 'Total Measures', value: pipelineSummary?.total_measures ?? '—', color: '#6366f1' },
                              { label: 'In view (deployed)', value: pipelineSummary?.deployed ?? '—', color: '#4ade80' },
                              { label: 'Excluded from view', value: pipelineSummary?.not_deployed ?? '—', color: '#94a3b8' },
                              { label: 'Conversion Rate', value: pipelineSummary?.overall_conversion_rate != null ? `${pipelineSummary.overall_conversion_rate}%` : '—', color: '#a78bfa' },
                            ].map((c, i) => (
                              <div key={i} className="glass" style={{ padding: 16 }}>
                                <div style={{ fontSize: 11, color: '#94a3b8' }}>{c.label}</div>
                                <div style={{ fontSize: 28, fontWeight: 700, color: c.color, marginTop: 4 }}>{String(c.value)}</div>
                              </div>
                            ))}
                          </div>
                          <div className="glass" style={{ padding: 16 }}>
                            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>Fact Groups</div>
                            <div style={{ maxHeight: 320, overflowY: 'auto' }}>
                              {factGroups.map(group => (
                                <div key={group.name} style={{ display: 'grid', gridTemplateColumns: 'minmax(180px,1fr) repeat(4,100px)', gap: 10, padding: '8px 10px', borderBottom: '1px solid rgba(255,255,255,0.05)', alignItems: 'center', fontSize: 12 }}>
                                  <span style={{ fontWeight: 600 }}>{group.name}</span>
                                  <span>{group.total_measures} total</span>
                                  <span style={{ color: '#4ade80' }}>{group.deployed ?? group.converted} in view</span>
                                  <span style={{ color: '#94a3b8' }}>{group.not_deployed || 0} not in view</span>
                                  <span className={`tag ${group.validation_status === 'OK' ? 'tag-success' : group.validation_status === 'WARNINGS' ? 'tag-warning' : 'tag-error'}`}>{group.validation_status}</span>
                                </div>
                              ))}
                            </div>
                          </div>
                          <div className="glass" style={{ padding: 16, display: 'flex', gap: 12 }}>
                            <span className={`tag ${(validationResult.errors || 0) > 0 ? 'tag-error' : 'tag-success'}`}>{validationResult.errors || 0} validation errors</span>
                            <span className={`tag ${(validationResult.warnings || 0) > 0 ? 'tag-warning' : 'tag-success'}`}>{validationResult.warnings || 0} warnings</span>
                            <span style={{ color: '#64748b', fontSize: 12 }}>{pipelineSummary?.duration_seconds != null ? `${pipelineSummary.duration_seconds}s` : ''}</span>
                          </div>
                          {validationIssues.length > 0 && (
                            <div className="glass" style={{ padding: 16 }}>
                              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>Validation Issues ({validationIssues.length})</div>
                              {validationIssues.slice(0, 50).map((issue, i) => (
                                <div key={i} style={{ fontSize: 12, padding: '6px 10px', marginBottom: 4, background: issue.severity === 'error' ? 'rgba(248,113,113,0.06)' : 'rgba(251,191,36,0.06)', borderRadius: 6, borderLeft: `3px solid ${issue.severity === 'error' ? '#f87171' : '#fbbf24'}`, color: issue.severity === 'error' ? '#f87171' : '#fbbf24' }}>
                                  <strong>{issue.severity}</strong> · {issue.category}: {issue.message}
                                </div>
                              ))}
                              {validationIssues.length > 50 && <div style={{ color: '#64748b', fontSize: 11, marginTop: 8 }}>Showing first 50 of {validationIssues.length} issues.</div>}
                            </div>
                          )}
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </>
                )}
              </motion.div>
            )}

            {/* DEPLOY */}
            {page === 'deploy' && (
              <motion.div key="deploy" variants={pageVariants} initial="initial" animate="animate" exit="exit" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
                <motion.div className="glass" style={{ padding: 24 }} initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }}>
                  <h3 style={{ margin: '0 0 16px', fontSize: 16 }}>Deploy to Databricks Unity Catalog</h3>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginBottom: 16 }}>
                    <div><label style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4, display: 'block' }}>Catalog</label><input className="input" value={dbxConfig.catalog} onChange={e => setDbxConfig(c => ({ ...c, catalog: e.target.value }))} /></div>
                    <div><label style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4, display: 'block' }}>Schema</label><input className="input" value={dbxConfig.schema} onChange={e => setDbxConfig(c => ({ ...c, schema: e.target.value }))} /></div>
                    <div><label style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4, display: 'block' }}>Warehouse ID</label><input className="input" value={dbxConfig.warehouse_id} onChange={e => setDbxConfig(c => ({ ...c, warehouse_id: e.target.value }))} /></div>
                  </div>

                  {!generatedSQL && <div style={{ fontSize: 13, color: '#94a3b8', marginBottom: 12 }}>Run a migration first to generate SQL DDL.</div>}
                  {generatedSQL && <div className="code-block" style={{ fontSize: 11, maxHeight: 180, marginBottom: 16 }}>{generatedSQL}</div>}

                  <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                    <motion.button className="btn-primary" disabled={!generatedSQL || deployStatus === 'deploying'} whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }} onClick={deploySql}>
                      {deployStatus === 'deploying' ? '⏳ Deploying...' : deployStatus === 'success' ? '✓ Deployed' : '🚀 Validate & Deploy'}
                    </motion.button>
                    {rollbackTarget && deployStatus === 'success' && (
                      <motion.button className="btn-secondary" whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }} onClick={rollback}>↩ Rollback {rollbackTarget}</motion.button>
                    )}
                    {generatedSQL && <button className="btn-secondary" onClick={() => navigator.clipboard?.writeText(generatedSQL)}>📋 Copy DDL</button>}
                  </div>
                </motion.div>

                {deployLog.length > 0 && (
                  <motion.div className="glass" style={{ padding: 16 }} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Deploy Log</div>
                    <div className="code-block" style={{ fontSize: 11, maxHeight: 200 }}>
                      {deployLog.map((l, i) => (
                        <div key={i} style={{ color: l.type === 'success' ? '#4ade80' : l.type === 'error' ? '#f87171' : l.type === 'warning' ? '#fbbf24' : '#94a3b8' }}>[{l.time}] {l.msg}</div>
                      ))}
                    </div>
                  </motion.div>
                )}

                <motion.div className="glass" style={{ padding: 20 }} initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.15 }}>
                  <h3 style={{ margin: '0 0 12px', fontSize: 15 }}>API Endpoint Reference</h3>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                    {[
                      ['POST /api/dbx/validate', 'Validate YAML / DDL'],
                      ['POST /api/dbx/deploy', 'Execute DDL via Statement API'],
                      ['POST /api/dbx/rollback', 'Roll back a view'],
                      ['GET /api/migrate/:id/report', 'Full evaluation report'],
                    ].map(([ep, desc], i) => (
                      <motion.div key={i} initial={{ opacity: 0, x: -5 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: i * 0.06 }}
                        style={{ fontSize: 12, padding: '8px 12px', background: 'rgba(99,102,241,0.06)', borderRadius: 8 }}>
                        <code style={{ color: '#a78bfa' }}>{ep}</code>
                        <div style={{ color: '#64748b', marginTop: 2 }}>{desc}</div>
                      </motion.div>
                    ))}
                  </div>
                </motion.div>
              </motion.div>
            )}

            {/* HISTORY */}
            {page === 'history' && (
              <motion.div key="history" variants={pageVariants} initial="initial" animate="animate" exit="exit" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <p style={{ color: '#94a3b8', fontSize: 13, margin: 0 }}>Past uploads and migration runs are saved and can be reopened — including after a reload or server restart.</p>
                  <button className="btn-secondary" style={{ marginLeft: 'auto', fontSize: 12 }} onClick={loadHistory} disabled={historyLoading}>
                    {historyLoading ? '…' : '↻ Refresh'}
                  </button>
                </div>

                {history.length === 0 ? (
                  <div className="glass" style={{ padding: 20, color: '#64748b', fontSize: 13 }}>
                    {historyLoading ? 'Loading history…' : 'No migrations yet — run one from Connect → upload a TMDL export, then Migrate.'}
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                    {history.map((h, i) => {
                      const failed = h.status === 'failed';
                      return (
                        <motion.div key={h.migration_id || i} className="glass" style={{ padding: '14px 16px', cursor: 'pointer', display: 'grid', gridTemplateColumns: 'minmax(160px,1.4fr) 1fr 1fr 120px', gap: 12, alignItems: 'center' }}
                          variants={cardVariants} custom={i} initial="initial" animate="animate" whileHover="hover"
                          onClick={() => openMigration(h.migration_id)}>
                          <div>
                            <div style={{ fontWeight: 600, fontSize: 14 }}>{h.model_name || 'Untitled model'}</div>
                            <div style={{ fontSize: 10, color: '#64748b' }}>{h.migration_id}</div>
                          </div>
                          <div style={{ fontSize: 12, color: '#94a3b8' }}>
                            <div>{h.catalog}.{h.schema}</div>
                            <div style={{ fontSize: 10, color: '#64748b' }}>{h.tables} tables • {h.measures} measures</div>
                          </div>
                          <div style={{ fontSize: 11, color: '#64748b' }}>{h.created_at ? new Date(h.created_at).toLocaleString() : '—'}</div>
                          <div style={{ display: 'flex', gap: 8, alignItems: 'center', justifySelf: 'end' }}>
                            <span className={`tag ${failed ? 'tag-error' : 'tag-success'}`} style={{ fontSize: 10 }}>{h.status || '—'}</span>
                            <span style={{ color: '#6366f1', fontSize: 12 }}>Open →</span>
                          </div>
                        </motion.div>
                      );
                    })}
                  </div>
                )}
              </motion.div>
            )}

          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}
