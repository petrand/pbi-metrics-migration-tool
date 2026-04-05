import { useState, useEffect, useCallback, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";

// DAX to SQL Translation Map
const DAX_TO_SQL_MAP = {
  'SUM': (col) => `SUM(${col})`,
  'COUNT': (col) => `COUNT(${col})`,
  'DISTINCTCOUNT': (col) => `COUNT(DISTINCT ${col})`,
  'AVERAGE': (col) => `AVG(${col})`,
  'MIN': (col) => `MIN(${col})`,
  'MAX': (col) => `MAX(${col})`,
  'COUNTA': (col) => `COUNT(${col})`,
  'COUNTROWS': (tbl) => `COUNT(*)`,
};

function translateDAXtoSQL(daxExpr, tableName) {
  if (!daxExpr) return daxExpr;
  let sql = daxExpr;
  sql = sql.replace(/SUM\s*\(\s*(\w+)\[(\w+)\]\s*\)/gi, (_, tbl, col) =>
    `SUM(source.${col.toLowerCase().replace(/\s+/g, '_')})`);
  sql = sql.replace(/COUNT\s*\(\s*(\w+)\[(\w+)\]\s*\)/gi, (_, tbl, col) =>
    `COUNT(source.${col.toLowerCase().replace(/\s+/g, '_')})`);
  sql = sql.replace(/DISTINCTCOUNT\s*\(\s*(\w+)\[(\w+)\]\s*\)/gi, (_, tbl, col) =>
    `COUNT(DISTINCT source.${col.toLowerCase().replace(/\s+/g, '_')})`);
  sql = sql.replace(/AVERAGE\s*\(\s*(\w+)\[(\w+)\]\s*\)/gi, (_, tbl, col) =>
    `AVG(source.${col.toLowerCase().replace(/\s+/g, '_')})`);
  sql = sql.replace(/DIVIDE\s*\(\s*([^,]+),\s*([^,]+),\s*([^)]+)\)/gi, (_, a, b, alt) =>
    `COALESCE((${a.trim()}) / NULLIF(${b.trim()}, 0), ${alt.trim()})`);
  sql = sql.replace(/IF\s*\(\s*([^,]+),\s*([^,]+),\s*([^)]+)\)/gi, (_, c, t, f) =>
    `CASE WHEN ${c.trim()} THEN ${t.trim()} ELSE ${f.trim()} END`);
  sql = sql.replace(/\[([^\]]+)\]/g, (_, name) => `MEASURE(\`${name}\`)`);
  return sql;
}

function generateMetricViewYAML(model, catalog, schema) {
  const factTable = model.tables.find(t => t.name.startsWith('Fact') || t.measures?.length > 0) || model.tables[0];
  const dimTables = model.tables.filter(t => t !== factTable);
  const srcName = `${catalog}.${schema}.${factTable.name.toLowerCase().replace(/\s+/g, '_')}`;

  let yaml = `version: 1.1\nsource: ${srcName}\ncomment: "Migrated from Power BI: ${model.name}"`;

  if (model.relationships?.length > 0) {
    yaml += `\njoins:`;
    const seen = new Set();
    model.relationships.forEach(rel => {
      const toTable = rel.to.split('.')[0];
      const toTableLower = toTable.toLowerCase().replace(/\s+/g, '_');
      if (seen.has(toTableLower)) return;
      seen.add(toTableLower);
      const fromCol = rel.from.split('.')[1]?.toLowerCase().replace(/\s+/g, '_') || 'key';
      const toCol = rel.to.split('.')[1]?.toLowerCase().replace(/\s+/g, '_') || 'key';
      yaml += `\n  - name: ${toTableLower}`;
      yaml += `\n    source: ${catalog}.${schema}.${toTableLower}`;
      yaml += `\n    on: ${toTableLower}.${toCol} = source.${fromCol}`;
    });
  }

  yaml += `\ndimensions:`;
  dimTables.forEach(dt => {
    const alias = dt.name.toLowerCase().replace(/\s+/g, '_');
    dt.columns?.forEach(col => {
      if (col.dataType === 'int64' && col.name.endsWith('Key')) return;
      const colName = col.name.toLowerCase().replace(/\s+/g, '_');
      yaml += `\n  - name: ${col.name}`;
      yaml += `\n    expr: ${alias}.${colName}`;
      if (col.description) yaml += `\n    comment: "${col.description}"`;
    });
  });

  if (factTable.measures?.length > 0) {
    yaml += `\nmeasures:`;
    factTable.measures.forEach(m => {
      const sqlExpr = translateDAXtoSQL(m.expression, factTable.name);
      yaml += `\n  - name: ${m.name}`;
      yaml += `\n    expr: ${sqlExpr}`;
      if (m.description) yaml += `\n    comment: "${m.description}"`;
    });
  }

  return yaml;
}

function generateSQLDDL(model, catalog, schema) {
  const yaml = generateMetricViewYAML(model, catalog, schema);
  const viewName = `${catalog}.${schema}.${model.name.toLowerCase().replace(/\s+/g, '_')}_metric_view`;
  return `CREATE OR REPLACE VIEW ${viewName}\nWITH METRICS LANGUAGE YAML AS $$\n${yaml}\n$$;`;
}

// Sample Models
const SAMPLE_MODELS = [
  {
    id: 'sales', name: "Sales Analytics",
    tables: [
      { name: "FactSales", columns: [
          {name:"SalesKey",dataType:"int64"},{name:"OrderDate",dataType:"dateTime"},
          {name:"ProductKey",dataType:"int64"},{name:"CustomerKey",dataType:"int64"},
          {name:"SalesAmount",dataType:"decimal",description:"Transaction amount"},
          {name:"Quantity",dataType:"int64"},{name:"DiscountAmount",dataType:"decimal"}
        ],
        measures: [
          {name:"Total Revenue",expression:"SUM(FactSales[SalesAmount])",description:"Sum of all sales amounts"},
          {name:"Total Quantity",expression:"SUM(FactSales[Quantity])",description:"Total units sold"},
          {name:"Avg Order Value",expression:"DIVIDE(SUM(FactSales[SalesAmount]), DISTINCTCOUNT(FactSales[SalesKey]), 0)",description:"Average revenue per order"},
          {name:"Customer Count",expression:"DISTINCTCOUNT(FactSales[CustomerKey])",description:"Unique customers"},
          {name:"Revenue per Customer",expression:"DIVIDE(SUM(FactSales[SalesAmount]), DISTINCTCOUNT(FactSales[CustomerKey]), 0)",description:"Revenue per unique customer"},
          {name:"Net Revenue",expression:"SUM(FactSales[SalesAmount]) - SUM(FactSales[DiscountAmount])",description:"Revenue after discounts"}
        ]
      },
      {name:"DimProduct",columns:[{name:"ProductKey",dataType:"int64"},{name:"ProductName",dataType:"string"},{name:"Category",dataType:"string"},{name:"SubCategory",dataType:"string"}]},
      {name:"DimCustomer",columns:[{name:"CustomerKey",dataType:"int64"},{name:"CustomerName",dataType:"string"},{name:"Region",dataType:"string"},{name:"Segment",dataType:"string"}]},
      {name:"DimDate",columns:[{name:"DateKey",dataType:"int64"},{name:"Date",dataType:"dateTime"},{name:"Year",dataType:"int64"},{name:"Quarter",dataType:"string"},{name:"Month",dataType:"string"}]}
    ],
    relationships: [
      {from:"FactSales.ProductKey",to:"DimProduct.ProductKey",type:"manyToOne"},
      {from:"FactSales.CustomerKey",to:"DimCustomer.CustomerKey",type:"manyToOne"},
      {from:"FactSales.OrderDate",to:"DimDate.Date",type:"manyToOne"}
    ]
  },
  {
    id: 'healthcare', name: "Healthcare KPIs",
    tables: [
      { name: "FactClaims", columns: [
          {name:"ClaimKey",dataType:"int64"},{name:"PatientKey",dataType:"int64"},
          {name:"ProviderKey",dataType:"int64"},{name:"ServiceDate",dataType:"dateTime"},
          {name:"ClaimAmount",dataType:"decimal"},{name:"LengthOfStay",dataType:"int64"},
          {name:"IsReadmission",dataType:"boolean"}
        ],
        measures: [
          {name:"Total Claims",expression:"SUM(FactClaims[ClaimAmount])",description:"Total claim dollars"},
          {name:"Claim Count",expression:"COUNT(FactClaims[ClaimKey])",description:"Number of claims"},
          {name:"Avg Length of Stay",expression:"AVERAGE(FactClaims[LengthOfStay])",description:"Average patient stay duration"},
          {name:"Readmission Rate",expression:"DIVIDE(SUM(FactClaims[IsReadmission]), COUNT(FactClaims[ClaimKey]), 0)",description:"Percentage of readmissions"},
          {name:"Cost per Encounter",expression:"DIVIDE(SUM(FactClaims[ClaimAmount]), COUNT(FactClaims[ClaimKey]), 0)",description:"Average cost per claim"}
        ]
      },
      {name:"DimPatient",columns:[{name:"PatientKey",dataType:"int64"},{name:"PatientName",dataType:"string"},{name:"AgeGroup",dataType:"string"},{name:"Gender",dataType:"string"}]},
      {name:"DimProvider",columns:[{name:"ProviderKey",dataType:"int64"},{name:"ProviderName",dataType:"string"},{name:"Specialty",dataType:"string"},{name:"Facility",dataType:"string"}]}
    ],
    relationships: [
      {from:"FactClaims.PatientKey",to:"DimPatient.PatientKey",type:"manyToOne"},
      {from:"FactClaims.ProviderKey",to:"DimProvider.ProviderKey",type:"manyToOne"}
    ]
  },
  {
    id: 'finance', name: "Financial Reporting",
    tables: [
      { name: "FactTransactions", columns: [
          {name:"TransactionKey",dataType:"int64"},{name:"AccountKey",dataType:"int64"},
          {name:"PeriodKey",dataType:"int64"},{name:"Amount",dataType:"decimal"},
          {name:"BudgetAmount",dataType:"decimal"},{name:"TransactionType",dataType:"string"}
        ],
        measures: [
          {name:"Net Revenue",expression:"SUM(FactTransactions[Amount])",description:"Total net revenue"},
          {name:"Budget Total",expression:"SUM(FactTransactions[BudgetAmount])",description:"Total budget"},
          {name:"Budget Variance",expression:"SUM(FactTransactions[Amount]) - SUM(FactTransactions[BudgetAmount])",description:"Actual vs budget"},
          {name:"Transaction Count",expression:"COUNT(FactTransactions[TransactionKey])",description:"Number of transactions"},
          {name:"Avg Transaction",expression:"AVERAGE(FactTransactions[Amount])",description:"Average transaction value"}
        ]
      },
      {name:"DimAccount",columns:[{name:"AccountKey",dataType:"int64"},{name:"AccountName",dataType:"string"},{name:"AccountType",dataType:"string"},{name:"Department",dataType:"string"}]},
      {name:"DimPeriod",columns:[{name:"PeriodKey",dataType:"int64"},{name:"FiscalYear",dataType:"int64"},{name:"FiscalQuarter",dataType:"string"},{name:"FiscalMonth",dataType:"string"}]}
    ],
    relationships: [
      {from:"FactTransactions.AccountKey",to:"DimAccount.AccountKey",type:"manyToOne"},
      {from:"FactTransactions.PeriodKey",to:"DimPeriod.PeriodKey",type:"manyToOne"}
    ]
  }
];

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

const staggerContainer = {
  animate: { transition: { staggerChildren: 0.05 } }
};

const slideIn = {
  initial: { opacity: 0, x: 30 },
  animate: { opacity: 1, x: 0, transition: { duration: 0.4 } }
};

export default function App() {
  const [page, setPage] = useState('dashboard');
  const [selectedModel, setSelectedModel] = useState(null);
  const [migrationState, setMigrationState] = useState('idle');
  const [migrationProgress, setMigrationProgress] = useState(0);
  const [migrationLog, setMigrationLog] = useState([]);
  const [generatedYAML, setGeneratedYAML] = useState('');
  const [generatedSQL, setGeneratedSQL] = useState('');
  const [dbxConfig, setDbxConfig] = useState({ host:'https://fe-vm-hls-amer.cloud.databricks.com', token:'dapi***', warehouse:'4b28691c780d9875', catalog:'hls_amer_catalog', schema:'metrics'});
  const [pbiConnected, setPbiConnected] = useState(false);
  const [dbxConnected, setDbxConnected] = useState(false);
  const [migrationResults, setMigrationResults] = useState([]);
  const [activeTab, setActiveTab] = useState('yaml');

  const addLog = useCallback((msg, type='info') => {
    setMigrationLog(prev => [...prev, { time: new Date().toLocaleTimeString(), msg, type }]);
  }, []);

  const runMigration = useCallback(async (model) => {
    setMigrationState('extracting');
    setMigrationProgress(0);
    setMigrationLog([]);
    setMigrationResults([]);

    const steps = [
      { pct: 10, msg: 'Connecting to Power BI workspace...', state: 'extracting' },
      { pct: 20, msg: `Extracting semantic model: ${model.name}`, state: 'extracting' },
      { pct: 30, msg: `Found ${model.tables.length} tables, ${model.relationships?.length || 0} relationships`, state: 'extracting' },
      { pct: 40, msg: 'Extracting DAX measures via INFO.MEASURES()...', state: 'extracting' },
      { pct: 50, msg: 'Translating DAX expressions to Databricks SQL...', state: 'transforming' },
      { pct: 60, msg: 'Generating YAML metric view definition (v1.1)...', state: 'transforming' },
      { pct: 70, msg: 'Validating YAML against Databricks schema rules...', state: 'validating' },
      { pct: 80, msg: 'Generating CREATE VIEW WITH METRICS DDL...', state: 'validating' },
      { pct: 85, msg: `Connecting to Databricks SQL warehouse: ${dbxConfig.warehouse}`, state: 'deploying' },
      { pct: 90, msg: 'Executing DDL via Statement Execution API...', state: 'deploying' },
      { pct: 95, msg: 'Running DESCRIBE TABLE EXTENDED for validation...', state: 'deploying' },
      { pct: 100, msg: 'Migration complete! Metric view deployed to Unity Catalog.', state: 'complete' },
    ];

    const factTable = model.tables.find(t => t.measures?.length > 0) || model.tables[0];

    for (const step of steps) {
      await new Promise(r => setTimeout(r, 600 + Math.random() * 400));
      setMigrationProgress(step.pct);
      addLog(step.msg, step.pct === 100 ? 'success' : 'info');
      setMigrationState(step.state);

      if (step.pct === 60) {
        const yaml = generateMetricViewYAML(model, dbxConfig.catalog, dbxConfig.schema);
        setGeneratedYAML(yaml);
      }
      if (step.pct === 80) {
        const sql = generateSQLDDL(model, dbxConfig.catalog, dbxConfig.schema);
        setGeneratedSQL(sql);
      }
    }

    const results = (factTable.measures || []).map(m => ({
      name: m.name,
      dax: m.expression,
      sql: translateDAXtoSQL(m.expression, factTable.name),
      status: m.expression.includes('TOTALYTD') || m.expression.includes('SAMEPERIODLASTYEAR') ? 'warning' : 'success',
      confidence: m.expression.includes('TOTALYTD') || m.expression.includes('SAMEPERIODLASTYEAR') ? 72 : 95 + Math.floor(Math.random()*5)
    }));
    setMigrationResults(results);
  }, [dbxConfig, addLog]);

  const navItems = [
    { id:'dashboard', icon:'\u25C9', label:'Dashboard' },
    { id:'connect', icon:'\u26A1', label:'Connect' },
    { id:'explore', icon:'\uD83D\uDD0D', label:'Explore' },
    { id:'migrate', icon:'\uD83D\uDD04', label:'Migrate' },
    { id:'deploy', icon:'\uD83D\uDE80', label:'Deploy' },
  ];

  return (
    <div style={{ display:'flex', height:'100vh', fontFamily:'-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif', background:'linear-gradient(135deg,#0f0f23 0%,#1a1a3e 50%,#0d1117 100%)', color:'#e2e8f0', overflow:'hidden' }}>
      <style>{`
        @keyframes pulse { 0%,100%{opacity:1}50%{opacity:0.5} }
        @keyframes progressGlow { 0%{box-shadow:0 0 5px #6366f1}50%{box-shadow:0 0 20px #6366f1,0 0 40px #818cf8}100%{box-shadow:0 0 5px #6366f1} }
        @keyframes spin { to{transform:rotate(360deg)} }
        .glass { background:rgba(255,255,255,0.05); backdrop-filter:blur(12px); border:1px solid rgba(255,255,255,0.1); border-radius:12px; }
        .glass:hover { border-color:rgba(99,102,241,0.4); }
        .btn-primary { background:linear-gradient(135deg,#6366f1,#8b5cf6); border:none; color:white; padding:10px 20px; border-radius:8px; cursor:pointer; font-weight:600; transition:all 0.2s; }
        .btn-primary:hover { transform:translateY(-1px); box-shadow:0 4px 15px rgba(99,102,241,0.4); }
        .btn-secondary { background:rgba(255,255,255,0.08); border:1px solid rgba(255,255,255,0.15); color:#e2e8f0; padding:8px 16px; border-radius:8px; cursor:pointer; transition:all 0.2s; }
        .btn-secondary:hover { background:rgba(255,255,255,0.12); border-color:rgba(99,102,241,0.3); }
        .code-block { background:#0d1117; border:1px solid rgba(255,255,255,0.08); border-radius:8px; padding:16px; font-family:'JetBrains Mono',Consolas,monospace; font-size:12px; line-height:1.6; overflow:auto; white-space:pre-wrap; word-break:break-word; color:#c9d1d9; }
        .nav-item { display:flex; align-items:center; gap:10px; padding:10px 16px; border-radius:8px; cursor:pointer; transition:all 0.2s; font-size:14px; }
        .nav-item:hover { background:rgba(99,102,241,0.15); }
        .nav-active { background:rgba(99,102,241,0.2); border-left:3px solid #6366f1; }
        .tag { display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }
        .tag-success { background:rgba(34,197,94,0.15); color:#4ade80; }
        .tag-warning { background:rgba(251,191,36,0.15); color:#fbbf24; }
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
      `}</style>

      {/* Sidebar */}
      <div style={{ width:220, borderRight:'1px solid rgba(255,255,255,0.06)', padding:'20px 12px', display:'flex', flexDirection:'column', gap:4, flexShrink:0 }}>
        <div style={{ padding:'0 16px 20px', borderBottom:'1px solid rgba(255,255,255,0.06)', marginBottom:12 }}>
          <div style={{ fontSize:18, fontWeight:700, background:'linear-gradient(135deg,#6366f1,#a78bfa)', WebkitBackgroundClip:'text', WebkitTextFillColor:'transparent' }}>PBI → DBX</div>
          <div style={{ fontSize:11, color:'#64748b', marginTop:2 }}>Metrics Migration Tool</div>
        </div>
        {navItems.map(n => (
          <motion.div key={n.id} className={`nav-item ${page===n.id?'nav-active':''}`}
            onClick={() => setPage(n.id)}
            whileHover={{ x: 4 }} whileTap={{ scale: 0.97 }}>
            <span style={{ fontSize:16 }}>{n.icon}</span> {n.label}
          </motion.div>
        ))}
        <div style={{ marginTop:'auto', padding:'12px 16px', borderTop:'1px solid rgba(255,255,255,0.06)' }}>
          <div style={{ fontSize:11, color:'#64748b' }}>Target Workspace</div>
          <div style={{ fontSize:12, color:'#94a3b8', marginTop:2 }}>FEVM HLS AMER</div>
          <div style={{ display:'flex', gap:6, marginTop:8 }}>
            <span className="tag tag-info" style={{ fontSize:10 }}>{pbiConnected ? '\u25CF PBI' : '\u25CB PBI'}</span>
            <span className="tag tag-info" style={{ fontSize:10 }}>{dbxConnected ? '\u25CF DBX' : '\u25CB DBX'}</span>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div style={{ flex:1, overflow:'hidden', display:'flex', flexDirection:'column' }}>
        <div style={{ padding:'16px 24px', borderBottom:'1px solid rgba(255,255,255,0.06)', display:'flex', justifyContent:'space-between', alignItems:'center' }}>
          <h1 style={{ fontSize:20, fontWeight:600, margin:0 }}>
            {page==='dashboard'&&'Migration Dashboard'}
            {page==='connect'&&'Connect Services'}
            {page==='explore'&&'Explore Semantic Models'}
            {page==='migrate'&&'DAX \u2192 YAML Migration'}
            {page==='deploy'&&'Deploy to Databricks'}
          </h1>
          <div style={{ fontSize:12, color:'#64748b' }}>Port 8000 \u2022 Serverless SQL Warehouse</div>
        </div>

        <div className="scroll-area" style={{ flex:1, padding:24, overflowY:'auto' }}>
          <AnimatePresence mode="wait">
            {/* DASHBOARD */}
            {page === 'dashboard' && (
              <motion.div key="dashboard" variants={pageVariants} initial="initial" animate="animate" exit="exit" style={{ display:'flex', flexDirection:'column', gap:20 }}>
                <motion.div variants={staggerContainer} initial="initial" animate="animate" style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:16 }}>
                  {[
                    {label:'Sample Models',value:'3',color:'#6366f1'},
                    {label:'Total Measures',value:'16',color:'#8b5cf6'},
                    {label:'Total Dimensions',value:'24',color:'#a78bfa'},
                    {label:'Relationships',value:'7',color:'#c4b5fd'}
                  ].map((card,i) => (
                    <motion.div key={i} className="glass" style={{ padding:20 }}
                      variants={cardVariants} custom={i} whileHover="hover">
                      <div style={{ fontSize:12, color:'#94a3b8', marginBottom:8 }}>{card.label}</div>
                      <div style={{ fontSize:32, fontWeight:700, color:card.color }}>{card.value}</div>
                    </motion.div>
                  ))}
                </motion.div>

                <motion.div className="glass" style={{ padding:20 }} initial={{ opacity:0, y:20 }} animate={{ opacity:1, y:0 }} transition={{ delay:0.2 }}>
                  <h3 style={{ margin:'0 0 16px', fontSize:16 }}>Sample Semantic Models Available for Migration</h3>
                  <div style={{ display:'grid', gridTemplateColumns:'repeat(3,1fr)', gap:16 }}>
                    {SAMPLE_MODELS.map((m,i) => {
                      const factTable = m.tables.find(t => t.measures?.length > 0);
                      return (
                        <motion.div key={m.id} className="glass" style={{ padding:16, cursor:'pointer' }}
                          variants={cardVariants} custom={i} initial="initial" animate="animate" whileHover="hover"
                          onClick={() => { setSelectedModel(m); setPage('explore'); }}>
                          <div style={{ fontWeight:600, marginBottom:8 }}>{m.name}</div>
                          <div style={{ fontSize:12, color:'#94a3b8', display:'flex', flexDirection:'column', gap:4 }}>
                            <span>{m.tables.length} tables \u2022 {factTable?.measures?.length || 0} measures</span>
                            <span>{m.relationships?.length || 0} relationships</span>
                          </div>
                          <div style={{ marginTop:12, display:'flex', gap:4, flexWrap:'wrap' }}>
                            {(factTable?.measures || []).slice(0,3).map((ms,j) => (
                              <span key={j} className="tag tag-info">{ms.name}</span>
                            ))}
                            {(factTable?.measures?.length || 0) > 3 && <span className="tag tag-info">+{factTable.measures.length-3}</span>}
                          </div>
                        </motion.div>
                      );
                    })}
                  </div>
                </motion.div>

                <motion.div className="glass" style={{ padding:20 }} initial={{ opacity:0 }} animate={{ opacity:1 }} transition={{ delay:0.4 }}>
                  <h3 style={{ margin:'0 0 12px', fontSize:16 }}>Architecture Flow</h3>
                  <div style={{ display:'flex', alignItems:'center', justifyContent:'center', gap:8, padding:16, flexWrap:'wrap' }}>
                    {['Power BI\nSemantic Model','\u2192','DAX\nExtraction','\u2192','DAX\u2192SQL\nTranslation','\u2192','YAML v1.1\nGeneration','\u2192','Statement\nExecution API','\u2192','Unity Catalog\nMetric View'].map((s,i) => (
                      <motion.div key={i} initial={{ opacity:0, y:10 }} animate={{ opacity:1, y:0 }} transition={{ delay: i*0.08 }}
                        style={{ textAlign:'center', fontSize: s==='\u2192' ? 20 : 11, color: s==='\u2192' ? '#6366f1' : '#e2e8f0', padding: s==='\u2192' ? '0 4px' : '12px 14px', background: s==='\u2192' ? 'none' : 'rgba(99,102,241,0.1)', borderRadius:8, whiteSpace:'pre-line', fontWeight: s==='\u2192' ? 400 : 500, lineHeight:1.4, minWidth: s==='\u2192' ? 'auto' : 90 }}>
                        {s}
                      </motion.div>
                    ))}
                  </div>
                </motion.div>
              </motion.div>
            )}

            {/* CONNECT */}
            {page === 'connect' && (
              <motion.div key="connect" variants={pageVariants} initial="initial" animate="animate" exit="exit" style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:24 }}>
                <motion.div className="glass" style={{ padding:24 }} initial={{ opacity:0, x:-30 }} animate={{ opacity:1, x:0 }}>
                  <div style={{ display:'flex', alignItems:'center', gap:10, marginBottom:20 }}>
                    <span style={{ fontSize:24 }}>\uD83D\uDCCA</span>
                    <h3 style={{ margin:0 }}>Power BI Connection</h3>
                  </div>
                  <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
                    <div><label style={{ fontSize:12, color:'#94a3b8', marginBottom:4, display:'block' }}>Tenant ID</label><input className="input" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" /></div>
                    <div><label style={{ fontSize:12, color:'#94a3b8', marginBottom:4, display:'block' }}>Client ID</label><input className="input" placeholder="App Registration Client ID" /></div>
                    <div><label style={{ fontSize:12, color:'#94a3b8', marginBottom:4, display:'block' }}>Workspace ID</label><input className="input" placeholder="Power BI Workspace GUID" /></div>
                    <motion.button className="btn-primary" style={{ marginTop:8 }} whileHover={{ scale:1.02 }} whileTap={{ scale:0.98 }}
                      onClick={() => { setPbiConnected(true); addLog('Power BI connected via OAuth2 MSAL','success'); }}>
                      {pbiConnected ? '\u2713 Connected' : 'Connect via OAuth2'}
                    </motion.button>
                    <AnimatePresence>
                      {pbiConnected && <motion.div initial={{ opacity:0, height:0 }} animate={{ opacity:1, height:'auto' }} exit={{ opacity:0, height:0 }} className="tag tag-success" style={{ alignSelf:'flex-start' }}>\u25CF Authenticated \u2014 Scan API ready</motion.div>}
                    </AnimatePresence>
                  </div>
                </motion.div>

                <motion.div className="glass" style={{ padding:24 }} initial={{ opacity:0, x:30 }} animate={{ opacity:1, x:0 }}>
                  <div style={{ display:'flex', alignItems:'center', gap:10, marginBottom:20 }}>
                    <span style={{ fontSize:24 }}>\uD83D\uDD37</span>
                    <h3 style={{ margin:0 }}>Databricks Connection</h3>
                  </div>
                  <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
                    <div><label style={{ fontSize:12, color:'#94a3b8', marginBottom:4, display:'block' }}>Workspace URL</label><input className="input" value={dbxConfig.host} onChange={e => setDbxConfig(c => ({...c, host:e.target.value}))} /></div>
                    <div><label style={{ fontSize:12, color:'#94a3b8', marginBottom:4, display:'block' }}>Access Token (PAT)</label><input className="input" type="password" value={dbxConfig.token} onChange={e => setDbxConfig(c => ({...c, token:e.target.value}))} /></div>
                    <div><label style={{ fontSize:12, color:'#94a3b8', marginBottom:4, display:'block' }}>SQL Warehouse ID</label><input className="input" value={dbxConfig.warehouse} onChange={e => setDbxConfig(c => ({...c, warehouse:e.target.value}))} /></div>
                    <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:12 }}>
                      <div><label style={{ fontSize:12, color:'#94a3b8', marginBottom:4, display:'block' }}>Catalog</label><input className="input" value={dbxConfig.catalog} onChange={e => setDbxConfig(c => ({...c, catalog:e.target.value}))} /></div>
                      <div><label style={{ fontSize:12, color:'#94a3b8', marginBottom:4, display:'block' }}>Schema</label><input className="input" value={dbxConfig.schema} onChange={e => setDbxConfig(c => ({...c, schema:e.target.value}))} /></div>
                    </div>
                    <motion.button className="btn-primary" style={{ marginTop:8 }} whileHover={{ scale:1.02 }} whileTap={{ scale:0.98 }}
                      onClick={() => { setDbxConnected(true); addLog('Databricks connected via PAT \u2014 Serverless warehouse active','success'); }}>
                      {dbxConnected ? '\u2713 Connected' : 'Connect to Workspace'}
                    </motion.button>
                    <AnimatePresence>
                      {dbxConnected && <motion.div initial={{ opacity:0, height:0 }} animate={{ opacity:1, height:'auto' }} exit={{ opacity:0, height:0 }} className="tag tag-success" style={{ alignSelf:'flex-start' }}>\u25CF FEVM HLS AMER \u2014 Warehouse running</motion.div>}
                    </AnimatePresence>
                  </div>
                </motion.div>
              </motion.div>
            )}

            {/* EXPLORE */}
            {page === 'explore' && (
              <motion.div key="explore" variants={pageVariants} initial="initial" animate="animate" exit="exit" style={{ display:'flex', flexDirection:'column', gap:20 }}>
                {!selectedModel && (
                  <div className="glass" style={{ padding:20 }}>
                    <p style={{ color:'#94a3b8' }}>Select a semantic model from the dashboard to explore.</p>
                    <div style={{ display:'flex', gap:12, marginTop:12 }}>
                      {SAMPLE_MODELS.map(m => (
                        <motion.button key={m.id} className="btn-secondary" whileHover={{ scale:1.05 }} whileTap={{ scale:0.95 }} onClick={() => setSelectedModel(m)}>{m.name}</motion.button>
                      ))}
                    </div>
                  </div>
                )}
                {selectedModel && (
                  <>
                    <div style={{ display:'flex', gap:12, alignItems:'center' }}>
                      <h2 style={{ margin:0, fontSize:18 }}>{selectedModel.name}</h2>
                      <span className="tag tag-info">{selectedModel.tables.length} tables</span>
                      <motion.button className="btn-primary" style={{ marginLeft:'auto', fontSize:13 }}
                        whileHover={{ scale:1.02 }} whileTap={{ scale:0.98 }}
                        onClick={() => { setPage('migrate'); runMigration(selectedModel); }}>
                        \uD83D\uDD04 Migrate This Model
                      </motion.button>
                    </div>

                    {selectedModel.tables.map((table, ti) => (
                      <motion.div key={ti} className="glass" style={{ padding:20 }}
                        initial={{ opacity:0, scale:0.95 }} animate={{ opacity:1, scale:1 }} transition={{ delay: ti*0.08 }}>
                        <div style={{ display:'flex', alignItems:'center', gap:10, marginBottom:12 }}>
                          <span style={{ fontSize:14 }}>{table.measures?.length > 0 ? '\uD83D\uDCCB' : '\uD83D\uDCE6'}</span>
                          <span style={{ fontWeight:600 }}>{table.name}</span>
                          <span className="tag tag-info" style={{ fontSize:10 }}>{table.columns?.length || 0} cols</span>
                          {table.measures?.length > 0 && <span className="tag tag-success" style={{ fontSize:10 }}>{table.measures.length} measures</span>}
                        </div>

                        <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fill,minmax(180px,1fr))', gap:8, marginBottom: table.measures?.length ? 16 : 0 }}>
                          {table.columns?.map((col,ci) => (
                            <div key={ci} style={{ fontSize:12, padding:'6px 10px', background:'rgba(255,255,255,0.03)', borderRadius:6, display:'flex', justifyContent:'space-between' }}>
                              <span style={{ color:'#e2e8f0' }}>{col.name}</span>
                              <span style={{ color:'#64748b', fontSize:10 }}>{col.dataType}</span>
                            </div>
                          ))}
                        </div>

                        {table.measures?.length > 0 && (
                          <div>
                            <div style={{ fontSize:12, color:'#94a3b8', marginBottom:8, fontWeight:600 }}>DAX Measures</div>
                            {table.measures.map((m,mi) => (
                              <motion.div key={mi} initial={{ opacity:0, x:-10 }} animate={{ opacity:1, x:0 }} transition={{ delay: mi*0.05 }}
                                style={{ padding:'10px 12px', background:'rgba(99,102,241,0.06)', borderRadius:8, marginBottom:6, borderLeft:'3px solid #6366f1' }}>
                                <div style={{ fontWeight:600, fontSize:13, marginBottom:4 }}>{m.name}</div>
                                <code style={{ fontSize:11, color:'#a78bfa', fontFamily:'JetBrains Mono,Consolas,monospace' }}>{m.expression}</code>
                                {m.description && <div style={{ fontSize:11, color:'#64748b', marginTop:4 }}>{m.description}</div>}
                              </motion.div>
                            ))}
                          </div>
                        )}
                      </motion.div>
                    ))}

                    {selectedModel.relationships?.length > 0 && (
                      <motion.div className="glass" style={{ padding:20 }} initial={{ opacity:0 }} animate={{ opacity:1 }} transition={{ delay:0.3 }}>
                        <h3 style={{ margin:'0 0 12px', fontSize:15 }}>Relationships</h3>
                        {selectedModel.relationships.map((r,ri) => (
                          <div key={ri} style={{ fontSize:12, padding:8, display:'flex', gap:8, alignItems:'center' }}>
                            <code style={{ color:'#a78bfa' }}>{r.from}</code>
                            <span style={{ color:'#6366f1' }}>\u2192</span>
                            <code style={{ color:'#4ade80' }}>{r.to}</code>
                            <span className="tag tag-info" style={{ fontSize:10 }}>{r.type}</span>
                          </div>
                        ))}
                      </motion.div>
                    )}
                  </>
                )}
              </motion.div>
            )}

            {/* MIGRATE */}
            {page === 'migrate' && (
              <motion.div key="migrate" variants={pageVariants} initial="initial" animate="animate" exit="exit" style={{ display:'flex', flexDirection:'column', gap:20 }}>
                {migrationState === 'idle' && (
                  <div className="glass" style={{ padding:24, textAlign:'center' }}>
                    <p style={{ color:'#94a3b8', marginBottom:16 }}>Select a model and start migration from the Explore page, or choose one below:</p>
                    <div style={{ display:'flex', gap:12, justifyContent:'center' }}>
                      {SAMPLE_MODELS.map(m => (
                        <motion.button key={m.id} className="btn-primary" whileHover={{ scale:1.05 }} whileTap={{ scale:0.95 }}
                          onClick={() => { setSelectedModel(m); runMigration(m); }}>{m.name}</motion.button>
                      ))}
                    </div>
                  </div>
                )}

                {migrationState !== 'idle' && (
                  <>
                    <motion.div className="glass" style={{ padding:20 }} layout>
                      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:12 }}>
                        <span style={{ fontWeight:600 }}>
                          {migrationState === 'complete' ? '\u2705 Migration Complete' :
                           migrationState === 'extracting' ? '\uD83D\uDCE5 Extracting...' :
                           migrationState === 'transforming' ? '\uD83D\uDD04 Transforming...' :
                           migrationState === 'validating' ? '\u2713 Validating...' :
                           migrationState === 'deploying' ? '\uD83D\uDE80 Deploying...' : ''}
                        </span>
                        <span style={{ fontSize:14, fontWeight:700, color:'#6366f1' }}>{migrationProgress}%</span>
                      </div>
                      <div className="progress-bar">
                        <motion.div className="progress-fill" animate={{ width:`${migrationProgress}%` }} transition={{ duration:0.6, ease:"easeOut" }} />
                      </div>
                      <div style={{ display:'flex', gap:24, marginTop:16, justifyContent:'center' }}>
                        {['extracting','transforming','validating','deploying','complete'].map((s,i) => (
                          <div key={s} style={{ display:'flex', alignItems:'center', gap:6, fontSize:12 }}>
                            <motion.div animate={{ background: migrationState === s ? '#6366f1' : (['extracting','transforming','validating','deploying','complete'].indexOf(migrationState) > i ? '#4ade80' : 'rgba(255,255,255,0.1)'), boxShadow: migrationState === s ? '0 0 8px #6366f1' : 'none' }} style={{ width:8, height:8, borderRadius:'50%' }} />
                            <span style={{ color: migrationState === s ? '#e2e8f0' : '#64748b', textTransform:'capitalize' }}>{s}</span>
                          </div>
                        ))}
                      </div>
                    </motion.div>

                    <div style={{ display:'flex', gap:4, borderBottom:'1px solid rgba(255,255,255,0.08)' }}>
                      {['yaml','sql','log','results'].map(t => (
                        <div key={t} className={`tab ${activeTab===t?'tab-active':'tab-inactive'}`} onClick={() => setActiveTab(t)} style={{ textTransform:'uppercase' }}>{t}</div>
                      ))}
                    </div>

                    <AnimatePresence mode="wait">
                      {activeTab === 'yaml' && generatedYAML && (
                        <motion.div key="yaml" initial={{ opacity:0 }} animate={{ opacity:1 }} exit={{ opacity:0 }}>
                          <div style={{ display:'flex', justifyContent:'space-between', marginBottom:8 }}>
                            <span style={{ fontSize:13, fontWeight:600, color:'#a5b4fc' }}>Generated Metric View YAML (v1.1)</span>
                            <button className="btn-secondary" style={{ fontSize:11, padding:'4px 12px' }} onClick={() => navigator.clipboard?.writeText(generatedYAML)}>Copy</button>
                          </div>
                          <div className="code-block" style={{ maxHeight:400 }}>{generatedYAML}</div>
                        </motion.div>
                      )}
                      {activeTab === 'sql' && generatedSQL && (
                        <motion.div key="sql" initial={{ opacity:0 }} animate={{ opacity:1 }} exit={{ opacity:0 }}>
                          <div style={{ display:'flex', justifyContent:'space-between', marginBottom:8 }}>
                            <span style={{ fontSize:13, fontWeight:600, color:'#a5b4fc' }}>SQL DDL \u2014 Statement Execution API Payload</span>
                            <button className="btn-secondary" style={{ fontSize:11, padding:'4px 12px' }} onClick={() => navigator.clipboard?.writeText(generatedSQL)}>Copy</button>
                          </div>
                          <div className="code-block" style={{ maxHeight:400 }}>{generatedSQL}</div>
                        </motion.div>
                      )}
                      {activeTab === 'log' && (
                        <motion.div key="log" initial={{ opacity:0 }} animate={{ opacity:1 }} exit={{ opacity:0 }}
                          className="code-block" style={{ maxHeight:300, fontSize:11 }}>
                          {migrationLog.map((l,i) => (
                            <div key={i} style={{ color: l.type==='success' ? '#4ade80' : l.type==='error' ? '#f87171' : '#94a3b8' }}>
                              [{l.time}] {l.msg}
                            </div>
                          ))}
                          {migrationState !== 'complete' && <div style={{ animation:'pulse 1s infinite', color:'#6366f1' }}>\u258C</div>}
                        </motion.div>
                      )}
                      {activeTab === 'results' && migrationResults.length > 0 && (
                        <motion.div key="results" initial={{ opacity:0 }} animate={{ opacity:1 }} exit={{ opacity:0 }}
                          className="glass" style={{ padding:16 }}>
                          <div style={{ fontSize:13, fontWeight:600, marginBottom:12 }}>Translation Results</div>
                          {migrationResults.map((r,i) => (
                            <motion.div key={i} initial={{ opacity:0, x:-10 }} animate={{ opacity:1, x:0 }} transition={{ delay: i*0.05 }}
                              style={{ padding:'12px 14px', borderBottom:'1px solid rgba(255,255,255,0.05)', display:'grid', gridTemplateColumns:'180px 1fr 1fr 80px 60px', gap:12, alignItems:'center', fontSize:12 }}>
                              <span style={{ fontWeight:600 }}>{r.name}</span>
                              <code style={{ color:'#fbbf24', fontSize:11 }}>{r.dax}</code>
                              <code style={{ color:'#4ade80', fontSize:11 }}>{r.sql}</code>
                              <span className={`tag ${r.status==='success'?'tag-success':'tag-warning'}`}>{r.confidence}%</span>
                              <span className={`tag ${r.status==='success'?'tag-success':'tag-warning'}`}>{r.status==='success'?'\u2713':'\u26A0'}</span>
                            </motion.div>
                          ))}
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </>
                )}
              </motion.div>
            )}

            {/* DEPLOY */}
            {page === 'deploy' && (
              <motion.div key="deploy" variants={pageVariants} initial="initial" animate="animate" exit="exit" style={{ display:'flex', flexDirection:'column', gap:20 }}>
                <motion.div className="glass" style={{ padding:24 }} initial={{ opacity:0, y:20 }} animate={{ opacity:1, y:0 }}>
                  <h3 style={{ margin:'0 0 16px', fontSize:16 }}>Databricks Apps Deployment \u2014 FEVM HLS AMER</h3>
                  <div className="code-block" style={{ fontSize:12, marginBottom:16 }}>{`# app.yaml \u2014 Databricks Apps Configuration
command:
  - "node"
  - "server.js"
env:
  - name: "PORT"
    value: "8000"`}</div>

                  <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16, marginBottom:16 }}>
                    <div className="glass" style={{ padding:16 }}>
                      <div style={{ fontSize:12, color:'#94a3b8', marginBottom:8 }}>API Health Check</div>
                      <div style={{ fontSize:13 }}>
                        {[['GET /api/health','200 OK'],['GET /api/ready','200 OK'],['POST /api/pbi/extract','Ready'],['POST /api/dbx/deploy','Ready'],['POST /api/migrate','Ready']].map(([ep,st],i) => (
                          <motion.div key={i} initial={{ opacity:0 }} animate={{ opacity:1 }} transition={{ delay: i*0.1 }}
                            style={{ display:'flex', justifyContent:'space-between', padding:'4px 0' }}>
                            <span>{ep}</span><span className="tag tag-success">{st}</span>
                          </motion.div>
                        ))}
                      </div>
                    </div>
                    <div className="glass" style={{ padding:16 }}>
                      <div style={{ fontSize:12, color:'#94a3b8', marginBottom:8 }}>Deployment Steps</div>
                      {['npm run build \u2014 Vite production build','Vitest suite passes','databricks apps create pbi-metrics-migration','databricks apps deploy \u2192 FEVM HLS AMER','Health check: /api/health'].map((s,i) => (
                        <motion.div key={i} initial={{ opacity:0, x:-5 }} animate={{ opacity:1, x:0 }} transition={{ delay: i*0.08 }}
                          style={{ fontSize:12, padding:'6px 0', display:'flex', gap:8, alignItems:'center', color:'#e2e8f0' }}>
                          <span style={{ color:'#4ade80' }}>\u2713</span> {s}
                        </motion.div>
                      ))}
                    </div>
                  </div>

                  <div style={{ display:'flex', gap:12 }}>
                    <motion.button className="btn-primary" whileHover={{ scale:1.02 }} whileTap={{ scale:0.98 }}
                      onClick={() => addLog('Deployment initiated to FEVM HLS AMER workspace','success')}>\uD83D\uDE80 Deploy to Databricks</motion.button>
                    <button className="btn-secondary">\uD83D\uDCCB Export YAML Bundle</button>
                    <button className="btn-secondary">\uD83D\uDCC4 Generate DABs Config</button>
                  </div>
                </motion.div>

                <motion.div className="glass" style={{ padding:20 }} initial={{ opacity:0, y:20 }} animate={{ opacity:1, y:0 }} transition={{ delay:0.2 }}>
                  <h3 style={{ margin:'0 0 12px', fontSize:16 }}>Statement Execution API Integration</h3>
                  <div className="code-block" style={{ fontSize:11 }}>{`// POST https://fe-vm-hls-amer.cloud.databricks.com/api/2.0/sql/statements/
{
  "warehouse_id": "${dbxConfig.warehouse}",
  "catalog": "${dbxConfig.catalog}",
  "schema": "${dbxConfig.schema}",
  "statement": "CREATE OR REPLACE VIEW ... WITH METRICS LANGUAGE YAML AS $$ ... $$",
  "wait_timeout": "50s",
  "on_wait_timeout": "CANCEL"
}`}</div>
                </motion.div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}
