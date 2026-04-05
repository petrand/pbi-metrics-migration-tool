from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from datetime import datetime
import os

app = FastAPI(title="PBI Metrics Migration Tool")

# API Routes
@app.get("/api/health")
def health():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat(), "service": "pbi-metrics-migration-tool"}

@app.get("/api/ready")
def ready():
    return {"status": "ready", "version": "1.0.0"}

@app.post("/api/pbi/auth")
def pbi_auth():
    return {"authenticated": True, "method": "OAuth2 MSAL", "scope": "Power BI Scan API"}

@app.get("/api/pbi/workspaces")
def pbi_workspaces():
    return {"workspaces": [
        {"id": "ws-001", "name": "Sales Analytics Workspace", "type": "Premium"},
        {"id": "ws-002", "name": "Healthcare KPIs Workspace", "type": "Pro"},
        {"id": "ws-003", "name": "Finance Reporting Workspace", "type": "PPU"}
    ]}

@app.get("/api/pbi/workspaces/{workspace_id}/datasets")
def pbi_datasets(workspace_id: str):
    return {"datasets": [
        {"id": "ds-001", "name": "Sales Analytics Model", "tables": 4, "measures": 8},
        {"id": "ds-002", "name": "Healthcare KPIs", "tables": 3, "measures": 5},
        {"id": "ds-003", "name": "Financial Reporting", "tables": 3, "measures": 5}
    ]}

@app.post("/api/pbi/extract/{dataset_id}")
def pbi_extract(dataset_id: str):
    return {"model": dataset_id, "extractedAt": datetime.utcnow().isoformat(), "tables": 4, "measures": 8, "relationships": 3, "status": "extracted"}

@app.post("/api/dbx/auth")
def dbx_auth():
    return {"authenticated": True, "workspace": "fevm-hls-amer", "method": "PAT"}

@app.get("/api/dbx/warehouses")
def dbx_warehouses():
    return {"warehouses": [
        {"id": "4b28691c780d9875", "name": "Serverless Starter", "type": "SERVERLESS", "state": "RUNNING"},
        {"id": "8e4258d7fe74671b", "name": "HLS Analytics", "type": "SERVERLESS", "state": "RUNNING"}
    ]}

@app.get("/api/dbx/catalogs")
def dbx_catalogs():
    return {"catalogs": [
        {"name": "hls_amer_catalog", "schemas": ["gold", "silver", "metrics"]},
        {"name": "hls_catalog", "schemas": ["metrics", "staging"]}
    ]}

@app.post("/api/dbx/deploy")
def dbx_deploy():
    return {"statementId": f"stmt-{int(datetime.utcnow().timestamp())}", "status": "SUCCEEDED", "message": "Metric view created successfully"}

@app.post("/api/dbx/validate")
def dbx_validate():
    return {"valid": True, "warnings": [], "yamlVersion": "1.1"}

@app.post("/api/dbx/rollback")
def dbx_rollback():
    return {"status": "rolled_back", "message": "View dropped successfully"}

@app.get("/api/dbx/status/{statement_id}")
def dbx_status(statement_id: str):
    return {"statementId": statement_id, "status": "SUCCEEDED"}

@app.post("/api/migrate")
def migrate():
    return {"migrationId": f"mig-{int(datetime.utcnow().timestamp())}", "status": "COMPLETE", "steps": ["extract", "transform", "validate", "deploy"]}

@app.get("/api/migrate/{migration_id}/status")
def migrate_status(migration_id: str):
    return {"migrationId": migration_id, "status": "COMPLETE", "progress": 100}

@app.get("/api/migrate/{migration_id}/report")
def migrate_report(migration_id: str):
    return {"migrationId": migration_id, "success": True, "measuresTranslated": 8, "measuresFailed": 0, "measuresWarning": 2, "duration": "12.4s"}

# Serve static files (Vite build)
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        file_path = os.path.join(static_dir, full_path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(static_dir, "index.html"))
