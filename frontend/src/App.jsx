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

function MeasureDetail({ m }) {
  const rows = [
    ['Original DAX', m.original_dax, '#fbbf24'],
    ['Translated SQL', m.translated_sql, '#4ade80'],
  ];
  const hasWindow = m.window_spec && (Array.isArray(m.window_spec) ? m.window_spec.length > 0 : Object.keys(m.window_spec).length > 0);
  return (
    <div style={{ padding: '10px 14px 14px 34px', background: 'rgba(0,0,0,0.18)', borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
      {m.deployed === false && (
        <div style={{ marginBottom: 10, padding: '8px 10px', borderRadius: 6, background: 'rgba(148,163,184,0.12)', borderLeft: '3px solid #94a3b8' }}>
          <span style={{ fontSize: 11, fontWeight: 700, color: '#cbd5e1' }}>⊘ Excluded from the deployed view</span>
          <div style={{ fontSize: 11.5, color: '#94a3b8', marginTop: 2 }}>{m.exclusion_reason || 'not deployable'} — candidate for a manual measure_override.</div>
        </div>
      )}
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

function ResultsExplorer({ groups, totals }) {
  const [openGroup, setOpenGroup] = useState(null);
  const [openMeasure, setOpenMeasure] = useState(null);
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
        {chip('Converted', totals?.converted ?? '—', '#4ade80')}
        {chip('Partial', totals?.partial ?? '—', '#fbbf24')}
        {chip('Conversion', totals?.overall_conversion_rate != null ? `${totals.overall_conversion_rate}%` : '—', '#a78bfa')}
      </div>

      <div style={{ fontSize: 11, color: '#64748b' }}>Click a fact table to see its measures; click a measure for the DAX → SQL detail.</div>

      {sorted.map(g => {
        const isOpen = openGroup === g.name;
        const rate = g.conversion_rate ?? 0;
        const vClass = g.validation_status === 'OK' ? 'tag-success' : g.validation_status === 'WARNINGS' ? 'tag-warning' : 'tag-error';
        const notInView = (g.measures || []).filter(x => x.deployed === false).length;
        const inView = (g.total_measures || 0) - notInView;
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
                {notInView > 0 && <span className="tag" style={{ fontSize: 10, background: 'rgba(148,163,184,0.15)', color: '#94a3b8' }}>⊘ {notInView} not in view</span>}
                {(g.dimensions || []).length > 0 && <span className="tag tag-info" style={{ fontSize: 10 }}>{g.dimensions.length} dims</span>}
                {g.partial > 0 && <span className="tag tag-warning" style={{ fontSize: 10 }}>{g.partial} partial</span>}
                {g.unsupported > 0 && <span className="tag tag-error" style={{ fontSize: 10 }}>{g.unsupported} unsupported</span>}
              </div>
              <div style={{ minWidth: 100 }}><ConfidenceBar value={rate} /></div>
              <span className={`tag ${vClass}`} style={{ fontSize: 10, justifySelf: 'end' }}>{g.validation_status}</span>
            </div>

            {isOpen && (
              <div style={{ borderTop: '1px solid rgba(255,255,255,0.08)' }}>
                {(g.dimensions || []).length > 0 && (
                  <div style={{ padding: '10px 14px 12px 34px', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                    <div style={{ fontSize: 10, textTransform: 'uppercase', color: '#64748b', marginBottom: 6 }}>Converted dimension columns ({g.dimensions.length})</div>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(240px,1fr))', gap: '3px 16px' }}>
                      {g.dimensions.map((d, di) => (
                        <div key={di} style={{ fontSize: 11.5, display: 'flex', gap: 6, alignItems: 'baseline' }} title={`${d.name} = ${d.expr}`}>
                          <span style={{ color: '#e2e8f0', fontWeight: 600 }}>{d.name}</span>
                          <code style={{ color: '#7dd3fc', fontSize: 10.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>← {d.expr}</code>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                <div style={{ padding: '6px 14px 6px 34px', display: 'grid', gridTemplateColumns: 'minmax(160px,1.6fr) minmax(200px,2fr) 120px 100px', gap: 12, fontSize: 10, textTransform: 'uppercase', color: '#64748b', position: 'sticky', top: 0, background: '#202039' }}>
                  <span>Measure</span><span>Translated SQL</span><span>Confidence</span><span>Status</span>
                </div>
                <div style={{ maxHeight: 420, overflowY: 'auto' }}>
                  {(g.measures || []).map((m, i) => {
                    const key = `${g.name}::${m.name}::${i}`;
                    const mOpen = openMeasure === key;
                    const color = STATUS_COLOR[m.status] || '#94a3b8';
                    return (
                      <div key={key}>
                        <div onClick={() => setOpenMeasure(mOpen ? null : key)}
                          style={{ padding: '9px 14px 9px 34px', cursor: 'pointer', display: 'grid', gridTemplateColumns: 'minmax(160px,1.6fr) minmax(200px,2fr) 120px 100px', gap: 12, alignItems: 'center', fontSize: 12, borderBottom: '1px solid rgba(255,255,255,0.04)', background: mOpen ? 'rgba(99,102,241,0.06)' : 'transparent' }}>
                          <span style={{ fontWeight: 600, opacity: m.deployed === false ? 0.6 : 1 }}>
                            {m.name}
                            {m.deployed === false && <span title={m.exclusion_reason} style={{ marginLeft: 6, fontSize: 10, color: '#94a3b8' }}>⊘ not in view</span>}
                          </span>
                          <code title={m.translated_sql} style={{ color: '#4ade80', fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{m.translated_sql || '—'}</code>
                          <ConfidenceBar value={m.confidence} />
                          <span className="tag" style={{ fontSize: 10, background: `${color}22`, color, justifySelf: 'start' }}>{m.status}</span>
                        </div>
                        {mOpen && <MeasureDetail m={m} />}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function App() {
  const [page, setPage] = useState('dashboard');
  const [connectTab, setConnectTab] = useState('pbi');
  const [selectedModel, setSelectedModel] = useState(null);
  const [sampleModels, setSampleModels] = useState([]);
  const [capabilities, setCapabilities] = useState(null);
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

  // Load sample models + capabilities on mount
  useEffect(() => {
    apiFetch('/api/samples')
      .then(async data => {
        const summaries = data.models || data || [];
        const models = await Promise.all(
          summaries.map(async summary => {
            try {
              const detail = await apiFetch(`/api/samples/${summary.id}`);
              return detail.model || detail;
            } catch (_) {
              return summary;
            }
          })
        );
        setSampleModels(models);
      })
      .catch(() => {});
    apiFetch('/api/ready').then(data => setCapabilities(data.capabilities || null)).catch(() => {});
  }, []);

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
    } catch (e) {
      addLog(`Error: ${e.message}`, 'error');
      setMigrationState('error');
    }
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
    { id: 'dashboard', icon: '◉', label: 'Dashboard' },
    { id: 'connect', icon: '⚡', label: 'Connect' },
    { id: 'explore', icon: '🔍', label: 'Explore' },
    { id: 'migrate', icon: '🔄', label: 'Migrate' },
    { id: 'deploy', icon: '🚀', label: 'Deploy' },
  ];

  // Compute dashboard stats from sampleModels
  const totalMeasures = sampleModels.reduce((sum, m) => {
    if (typeof m.measures === 'number') return sum + m.measures;
    if (!Array.isArray(m.tables)) return sum;
    return sum + m.tables.reduce((tableSum, t) => tableSum + (t.measures?.length || 0), 0);
  }, 0);
  const totalRelationships = sampleModels.reduce((sum, m) => {
    if (typeof m.relationships === 'number') return sum + m.relationships;
    return sum + (Array.isArray(m.relationships) ? m.relationships.length : 0);
  }, 0);
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
            onClick={() => setPage(n.id)} whileHover={{ x: 4 }} whileTap={{ scale: 0.97 }}>
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
            {page === 'dashboard' && 'Migration Dashboard'}
            {page === 'connect' && 'Connect Services'}
            {page === 'explore' && 'Explore Semantic Models'}
            {page === 'migrate' && 'DAX → YAML Migration'}
            {page === 'deploy' && 'Deploy to Databricks'}
          </h1>
          <div style={{ fontSize: 12, color: '#64748b' }}>Port 8000 • Serverless SQL Warehouse</div>
        </div>

        <div className="scroll-area" style={{ flex: 1, padding: 24, overflowY: 'auto' }}>
          <AnimatePresence mode="wait">

            {/* DASHBOARD */}
            {page === 'dashboard' && (
              <motion.div key="dashboard" variants={pageVariants} initial="initial" animate="animate" exit="exit" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
                <motion.div variants={staggerContainer} initial="initial" animate="animate" style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 16 }}>
                  {[
                    { label: 'Sample Models', value: String(sampleModels.length || '—'), color: '#6366f1' },
                    { label: 'Total Measures', value: String(totalMeasures || '—'), color: '#8b5cf6' },
                    { label: 'Relationships', value: String(totalRelationships || '—'), color: '#a78bfa' },
                    { label: 'API Status', value: capabilities ? 'Ready' : '...', color: '#4ade80' },
                  ].map((card, i) => (
                    <motion.div key={i} className="glass" style={{ padding: 20 }} variants={cardVariants} custom={i} whileHover="hover">
                      <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 8 }}>{card.label}</div>
                      <div style={{ fontSize: 32, fontWeight: 700, color: card.color }}>{card.value}</div>
                    </motion.div>
                  ))}
                </motion.div>

                {capabilities && (
                  <motion.div className="glass" style={{ padding: 20 }} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.15 }}>
                    <h3 style={{ margin: '0 0 12px', fontSize: 15 }}>Backend Capabilities</h3>
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                      {Object.entries(capabilities).map(([k, v]) => (
                        <span key={k} className={`tag ${v ? 'tag-success' : 'tag-warning'}`}>{v ? '✓' : '✗'} {k.replace(/_/g, ' ')}</span>
                      ))}
                    </div>
                  </motion.div>
                )}

                <motion.div className="glass" style={{ padding: 20 }} initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.2 }}>
                  <h3 style={{ margin: '0 0 16px', fontSize: 16 }}>Sample Semantic Models</h3>
                  {sampleModels.length === 0 ? (
                    <div style={{ color: '#64748b', fontSize: 13 }}>Loading models from API...</div>
                  ) : (
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 16 }}>
                      {sampleModels.map((m, i) => {
                        const factTable = (m.tables || []).find(t => t.measures?.length > 0);
                        return (
                          <motion.div key={m.id || i} className="glass" style={{ padding: 16, cursor: 'pointer' }}
                            variants={cardVariants} custom={i} initial="initial" animate="animate" whileHover="hover"
                            onClick={() => { setSelectedModel(m); setPage('explore'); }}>
                            <div style={{ fontWeight: 600, marginBottom: 8 }}>{m.name}</div>
                            <div style={{ fontSize: 12, color: '#94a3b8', display: 'flex', flexDirection: 'column', gap: 4 }}>
                              <span>{(m.tables || []).length} tables • {factTable?.measures?.length || 0} measures</span>
                              <span>{(m.relationships || []).length} relationships</span>
                            </div>
                            <div style={{ marginTop: 12, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                              {(factTable?.measures || []).slice(0, 3).map((ms, j) => (
                                <span key={j} className="tag tag-info">{ms.name}</span>
                              ))}
                              {(factTable?.measures?.length || 0) > 3 && <span className="tag tag-info">+{factTable.measures.length - 3}</span>}
                            </div>
                          </motion.div>
                        );
                      })}
                    </div>
                  )}
                </motion.div>

                <motion.div className="glass" style={{ padding: 20 }} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.4 }}>
                  <h3 style={{ margin: '0 0 12px', fontSize: 16 }}>Architecture Flow</h3>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, padding: 16, flexWrap: 'wrap' }}>
                    {['Power BI\nSemantic Model', '→', 'DAX\nExtraction', '→', 'DAX→SQL\nTranslation', '→', 'YAML v1.1\nGeneration', '→', 'Statement\nExecution API', '→', 'Unity Catalog\nMetric View'].map((s, i) => (
                      <motion.div key={i} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.07 }}
                        style={{ textAlign: 'center', fontSize: s === '→' ? 20 : 11, color: s === '→' ? '#6366f1' : '#e2e8f0', padding: s === '→' ? '0 4px' : '12px 14px', background: s === '→' ? 'none' : 'rgba(99,102,241,0.1)', borderRadius: 8, whiteSpace: 'pre-line', fontWeight: s === '→' ? 400 : 500, lineHeight: 1.4, minWidth: s === '→' ? 'auto' : 90 }}>{s}</motion.div>
                    ))}
                  </div>
                </motion.div>
              </motion.div>
            )}

            {/* CONNECT */}
            {page === 'connect' && (
              <motion.div key="connect" variants={pageVariants} initial="initial" animate="animate" exit="exit" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
                <div style={{ display: 'flex', gap: 4 }}>
                  {[['pbi', '📊 Power BI OAuth2'], ['tmdl', '📁 TMDL Upload'], ['sample', '🗃 Sample Models']].map(([id, label]) => (
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
                    </motion.div>
                  )}

                  {connectTab === 'sample' && (
                    <motion.div key="sample" initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }} style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                      <div style={{ fontSize: 13, color: '#94a3b8' }}>Select a built-in sample model to explore and migrate:</div>
                      {sampleModels.map((m, i) => (
                        <motion.div key={m.id || i} className="glass" style={{ padding: 16, cursor: 'pointer', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}
                          variants={cardVariants} custom={i} initial="initial" animate="animate" whileHover="hover"
                          onClick={() => { setSelectedModel(m); setPage('explore'); }}>
                          <div>
                            <div style={{ fontWeight: 600 }}>{m.name}</div>
                            <div style={{ fontSize: 12, color: '#64748b', marginTop: 4 }}>{(m.tables || []).length} tables • {(m.relationships || []).length} relationships</div>
                          </div>
                          <button className="btn-secondary" style={{ fontSize: 12 }}>Select →</button>
                        </motion.div>
                      ))}
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
                    <p style={{ color: '#94a3b8' }}>Select a model from Dashboard or Connect → Sample Models.</p>
                    <div style={{ display: 'flex', gap: 12, marginTop: 12, flexWrap: 'wrap' }}>
                      {sampleModels.map(m => (
                        <motion.button key={m.id} className="btn-secondary" whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }} onClick={() => setSelectedModel(m)}>{m.name}</motion.button>
                      ))}
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
                    <p style={{ color: '#94a3b8', marginBottom: 16, fontSize: 13 }}>Select a model to migrate:</p>
                    <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                      {sampleModels.map(m => (
                        <motion.button key={m.id} className="btn-primary" whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }}
                          onClick={() => { setSelectedModel(m); runMigration(m); }}>{m.name}</motion.button>
                      ))}
                      {selectedModel && !sampleModels.find(s => s.id === selectedModel.id) && (
                        <motion.button className="btn-primary" whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }} onClick={() => runMigration(selectedModel)}>
                          Migrate: {selectedModel.name}
                        </motion.button>
                      )}
                    </div>
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
                              { label: 'Converted', value: pipelineSummary?.converted ?? '—', color: '#4ade80' },
                              { label: 'Partial', value: pipelineSummary?.partial ?? '—', color: '#fbbf24' },
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
                                <div key={group.name} style={{ display: 'grid', gridTemplateColumns: 'minmax(180px,1fr) repeat(4,90px)', gap: 10, padding: '8px 10px', borderBottom: '1px solid rgba(255,255,255,0.05)', alignItems: 'center', fontSize: 12 }}>
                                  <span style={{ fontWeight: 600 }}>{group.name}</span>
                                  <span>{group.total_measures} total</span>
                                  <span style={{ color: '#4ade80' }}>{group.converted} converted</span>
                                  <span style={{ color: '#fbbf24' }}>{group.partial} partial</span>
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

          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}
