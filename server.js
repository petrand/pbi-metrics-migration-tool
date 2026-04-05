import express from 'express';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const PORT = parseInt(process.env.PORT || '8000', 10);

// Manual CORS middleware (no cors dependency needed)
app.use((req, res, next) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.header('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  if (req.method === 'OPTIONS') return res.sendStatus(200);
  next();
});
app.use(express.json());

// API Routes
app.get('/api/health', (req, res) => {
  res.json({ status: 'healthy', timestamp: new Date().toISOString(), service: 'pbi-metrics-migration-tool' });
});

app.get('/api/ready', (req, res) => {
  res.json({ status: 'ready', version: '1.0.0' });
});

app.post('/api/pbi/auth', (req, res) => {
  res.json({ authenticated: true, method: 'OAuth2 MSAL', scope: 'Power BI Scan API' });
});

app.get('/api/pbi/workspaces', (req, res) => {
  res.json({ workspaces: [
    { id: 'ws-001', name: 'Sales Analytics Workspace', type: 'Premium' },
    { id: 'ws-002', name: 'Healthcare KPIs Workspace', type: 'Pro' },
    { id: 'ws-003', name: 'Finance Reporting Workspace', type: 'PPU' }
  ]});
});

app.get('/api/pbi/workspaces/:id/datasets', (req, res) => {
  res.json({ datasets: [
    { id: 'ds-001', name: 'Sales Analytics Model', tables: 4, measures: 8 },
    { id: 'ds-002', name: 'Healthcare KPIs', tables: 3, measures: 5 },
    { id: 'ds-003', name: 'Financial Reporting', tables: 3, measures: 5 }
  ]});
});

app.post('/api/pbi/extract/:datasetId', (req, res) => {
  res.json({ model: req.params.datasetId, extractedAt: new Date().toISOString(), tables: 4, measures: 8, relationships: 3, status: 'extracted' });
});

app.post('/api/dbx/auth', (req, res) => {
  res.json({ authenticated: true, workspace: 'fevm-hls-amer', method: 'PAT' });
});

app.get('/api/dbx/warehouses', (req, res) => {
  res.json({ warehouses: [
    { id: '4b28691c780d9875', name: 'Serverless Starter', type: 'SERVERLESS', state: 'RUNNING' },
    { id: '8e4258d7fe74671b', name: 'HLS Analytics', type: 'SERVERLESS', state: 'RUNNING' }
  ]});
});

app.get('/api/dbx/catalogs', (req, res) => {
  res.json({ catalogs: [
    { name: 'hls_amer_catalog', schemas: ['gold', 'silver', 'metrics'] },
    { name: 'hls_catalog', schemas: ['metrics', 'staging'] }
  ]});
});

app.post('/api/dbx/deploy', (req, res) => {
  res.json({ statementId: `stmt-${Date.now()}`, status: 'SUCCEEDED', message: 'Metric view created successfully' });
});

app.post('/api/dbx/validate', (req, res) => {
  res.json({ valid: true, warnings: [], yamlVersion: '1.1' });
});

app.post('/api/dbx/rollback', (req, res) => {
  res.json({ status: 'rolled_back', message: 'View dropped successfully' });
});

app.get('/api/dbx/status/:statementId', (req, res) => {
  res.json({ statementId: req.params.statementId, status: 'SUCCEEDED' });
});

app.post('/api/migrate', (req, res) => {
  res.json({ migrationId: `mig-${Date.now()}`, status: 'COMPLETE', steps: ['extract', 'transform', 'validate', 'deploy'] });
});

app.get('/api/migrate/:id/status', (req, res) => {
  res.json({ migrationId: req.params.id, status: 'COMPLETE', progress: 100 });
});

app.get('/api/migrate/:id/report', (req, res) => {
  res.json({ migrationId: req.params.id, success: true, measuresTranslated: 8, measuresFailed: 0, measuresWarning: 2, duration: '12.4s' });
});

// Static Files (Vite build output)
const staticDir = path.join(__dirname, 'frontend', 'dist');
app.use(express.static(staticDir));

// SPA fallback
app.get('*', (req, res) => {
  if (!req.path.startsWith('/api')) {
    res.sendFile(path.join(staticDir, 'index.html'));
  } else {
    res.status(404).json({ error: 'Not found' });
  }
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`PBI Metrics Migration Tool running on port ${PORT}`);
  console.log(`Health: http://localhost:${PORT}/api/health`);
});
