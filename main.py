"""
PBI Metrics Migration Tool — FastAPI Backend

Production API server connecting Power BI extraction, DAX translation,
YAML generation, validation, evaluation, and Databricks deployment.
"""

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import migration_store
from backend.dax_translator import DAXTranslator
from backend.dbx_client import DatabricksClient, DatabricksAuthError, DatabricksAPIError
from backend.evaluator import EvaluationReporter
from backend.overrides import OverridesManager
from backend.pipeline import MigrationConfig, MigrationPipeline
from backend.tmdl_parser import TMDLParser
from backend.validator import MetricViewValidator
from backend.yaml_generator import MetricViewYAMLGenerator

try:
    from backend.pbi_client import PowerBIClient
except ImportError:
    PowerBIClient = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="PBI Metrics Migration Tool",
    description="Power BI Semantic Model to Databricks Metric View Migration Engine",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory state (per-session) ──
_state = {
    "pbi_client": None,
    "dbx_client": None,
    "current_model": None,
    "migrations": {},
}

# ══════════════════════════════════════════════════════════════════
# Health & Info
# ══════════════════════════════════════════════════════════════════

@app.get("/api/health")
def health():
    return {"status": "healthy", "version": "2.0.0",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "pbi-metrics-migration-tool"}


@app.get("/api/ready")
def ready():
    return {"status": "ready", "version": "2.0.0",
            "capabilities": {
                "pbi_api": PowerBIClient is not None,
                "tmdl_upload": True,
                "dax_translation": True,
                "yaml_validation": True,
                "databricks_deploy": True,
            }}


# ══════════════════════════════════════════════════════════════════
# Power BI Integration
# ══════════════════════════════════════════════════════════════════

@app.post("/api/pbi/auth")
def pbi_auth(body: dict = {}):
    tenant_id = body.get("tenant_id", "")
    client_id = body.get("client_id", "")
    client_secret = body.get("client_secret")
    token = body.get("token")

    if token:
        # Direct token mode
        if PowerBIClient:
            client = PowerBIClient(tenant_id=tenant_id or "direct", client_id=client_id or "direct")
            client.authenticate_with_token(token)
            _state["pbi_client"] = client
        return {"authenticated": True, "method": "direct_token"}

    if not PowerBIClient:
        return {"authenticated": False, "error": "msal not installed. Install: pip install msal"}

    try:
        client = PowerBIClient(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
        if client_secret:
            success = client.authenticate_service_principal()
        else:
            success = client.authenticate_device_code()
        if success:
            _state["pbi_client"] = client
        return {"authenticated": success, "method": "service_principal" if client_secret else "device_code"}
    except Exception as e:
        return {"authenticated": False, "error": str(e)}


@app.get("/api/pbi/workspaces")
def pbi_workspaces():
    client = _state.get("pbi_client")
    if not client:
        return {"workspaces": [], "message": "Not authenticated. Use sample models or authenticate first."}
    try:
        ws = client.list_workspaces()
        return {"workspaces": [{"id": w.id, "name": w.name, "type": w.type} for w in ws]}
    except Exception as e:
        return {"workspaces": [], "error": str(e)}


@app.get("/api/pbi/workspaces/{workspace_id}/datasets")
def pbi_datasets(workspace_id: str):
    client = _state.get("pbi_client")
    if not client:
        raise HTTPException(400, "Not authenticated with Power BI")
    try:
        ds = client.list_datasets(workspace_id)
        return {"datasets": [{"id": d.id, "name": d.name, "tables": 0, "measures": 0} for d in ds]}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/pbi/extract/{dataset_id}")
def pbi_extract(dataset_id: str, workspace_id: str = ""):
    client = _state.get("pbi_client")
    if not client:
        raise HTTPException(400, "Not authenticated with Power BI")
    try:
        model = client.extract_semantic_model(workspace_id, dataset_id)
        model_dict = model.to_dict()
        _state["current_model"] = model_dict
        return {"model": model_dict, "extractedAt": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════════
# TMDL Upload
# ══════════════════════════════════════════════════════════════════

@app.post("/api/tmdl/upload")
async def tmdl_upload(file: UploadFile = File(...)):
    """Upload a TMDL export (ZIP or directory) and parse it."""
    parser = TMDLParser()
    try:
        with tempfile.NamedTemporaryFile(suffix=file.filename, delete=False) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        model = parser.parse(tmp_path)
        model_dict = model.to_dict()
        _state["current_model"] = model_dict
        os.unlink(tmp_path)
        return {"model": model_dict, "source": "tmdl_upload",
                "tables": len(model_dict.get("tables", [])),
                "measures": sum(len(t.get("measures", [])) for t in model_dict.get("tables", []))}
    except Exception as e:
        raise HTTPException(500, f"TMDL parse error: {e}")


# ══════════════════════════════════════════════════════════════════
# Databricks Integration
# ══════════════════════════════════════════════════════════════════

@app.post("/api/dbx/auth")
def dbx_auth(body: dict = {}):
    host = body.get("host", os.environ.get("DATABRICKS_HOST", ""))
    token = body.get("token", os.environ.get("DATABRICKS_TOKEN", ""))
    if not host:
        return {"authenticated": False, "error": "Databricks host URL required"}
    try:
        client = DatabricksClient(host=host, token=token)
        success = client.authenticate()
        if success:
            _state["dbx_client"] = client
        return {"authenticated": success, "workspace": host}
    except DatabricksAuthError as e:
        return {"authenticated": False, "error": str(e)}
    except Exception as e:
        return {"authenticated": False, "error": str(e)}


@app.get("/api/dbx/warehouses")
def dbx_warehouses():
    client = _state.get("dbx_client")
    if not client:
        return {"warehouses": [], "error": "Not authenticated with Databricks"}
    try:
        wh = client.list_warehouses()
        return {"warehouses": [{"id": w.id, "name": w.name, "type": w.warehouse_type,
                                "state": w.state} for w in wh]}
    except Exception as e:
        return {"warehouses": [], "error": str(e)}


@app.get("/api/dbx/catalogs")
def dbx_catalogs():
    client = _state.get("dbx_client")
    if not client:
        return {"catalogs": [], "error": "Not authenticated"}
    try:
        cats = client.list_catalogs()
        return {"catalogs": [{"name": c.name, "comment": c.comment} for c in cats]}
    except Exception as e:
        return {"catalogs": [], "error": str(e)}


@app.get("/api/dbx/schemas/{catalog}")
def dbx_schemas(catalog: str):
    client = _state.get("dbx_client")
    if not client:
        return {"schemas": [], "error": "Not authenticated"}
    try:
        schemas = client.list_schemas(catalog)
        return {"schemas": [{"name": s.name, "catalog": s.catalog_name} for s in schemas]}
    except Exception as e:
        return {"schemas": [], "error": str(e)}


@app.post("/api/dbx/deploy")
def dbx_deploy(body: dict = {}):
    client = _state.get("dbx_client")
    if not client:
        raise HTTPException(400, "Not authenticated with Databricks")
    sql = body.get("sql", "")
    wh = body.get("warehouse_id", "")
    cat = body.get("catalog", "")
    sch = body.get("schema", "")
    if not sql or not wh:
        raise HTTPException(400, "sql and warehouse_id required")
    try:
        result = client.deploy_metric_view(sql, wh, cat, sch)
        return result.to_dict()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/dbx/validate")
def dbx_validate(body: dict = {}):
    yaml_content = body.get("yaml", "")
    ddl = body.get("ddl", "")
    validator = MetricViewValidator()
    if ddl:
        result = validator.validate_ddl(ddl)
    elif yaml_content:
        result = validator.validate_yaml(yaml_content)
    else:
        return {"valid": False, "error": "Provide yaml or ddl"}
    return result.to_dict()


@app.post("/api/dbx/rollback")
def dbx_rollback(body: dict = {}):
    client = _state.get("dbx_client")
    if not client:
        raise HTTPException(400, "Not authenticated with Databricks")
    view = body.get("view_name", "")
    wh = body.get("warehouse_id", "")
    if not view or not wh:
        raise HTTPException(400, "view_name and warehouse_id required")
    result = client.rollback_metric_view(view, wh)
    return result.to_dict()


# ══════════════════════════════════════════════════════════════════
# DAX Translation
# ══════════════════════════════════════════════════════════════════

@app.post("/api/translate")
def translate_dax(body: dict = {}):
    """Translate a single DAX expression to Databricks SQL."""
    dax = body.get("dax", "")
    table = body.get("table_name", "")
    translator = DAXTranslator()
    result = translator.translate(dax, table)
    return result.to_dict()


@app.post("/api/translate/batch")
def translate_batch(body: dict = {}):
    """Translate a batch of DAX measures."""
    measures = body.get("measures", [])
    table = body.get("table_name", "")
    relationships = body.get("relationships", [])
    translator = DAXTranslator(relationships=relationships)
    results = translator.translate_batch(measures, table)
    return {"results": [r.to_dict() for r in results]}


# ══════════════════════════════════════════════════════════════════
# Migration Pipeline
# ══════════════════════════════════════════════════════════════════

@app.post("/api/migrate")
def run_migration(body: dict = {}):
    """Run the full migration pipeline."""
    model = body.get("model") or _state.get("current_model")
    if not model:
        raise HTTPException(400, "No model loaded. Upload a TMDL export or extract from Power BI first.")

    config = MigrationConfig(
        catalog=body.get("catalog", "main"),
        schema=body.get("schema", "default"),
        warehouse_id=body.get("warehouse_id", ""),
        deploy=body.get("deploy", False),
        dry_run=body.get("dry_run", False),
        validate_only=body.get("validate_only", False),
        convert_nested_windows=body.get("convert_nested_windows", True),
        generate_dashboard=body.get("generate_dashboard", False),
        dashboard_name=body.get("dashboard_name", ""),
    )

    # Load overrides if provided
    if body.get("overrides"):
        mgr = OverridesManager()
        config.overrides = mgr.load_from_dict(body["overrides"])

    pipeline = MigrationPipeline()
    dbx_client = _state.get("dbx_client") if config.deploy else None
    result = pipeline.run(model, config, dbx_client)

    _state["migrations"][result.migration_id] = result

    # Persist the run (source model + result) so it can be reopened later —
    # survives page reload and server restart. Never let a persistence error
    # fail the request; the run already succeeded in memory.
    record = result.to_dict()
    record["created_at"] = result.started_at or datetime.now(timezone.utc).isoformat()
    record["catalog"] = config.catalog
    record["schema"] = config.schema
    record["tables"] = len(model.get("tables", []))
    record["measures"] = sum(len(t.get("measures", [])) for t in model.get("tables", []))
    record["source_model"] = model
    migration_store.save(result.migration_id, record)

    return result.to_dict()


@app.get("/api/migrate/{migration_id}/status")
def migration_status(migration_id: str):
    result = _state["migrations"].get(migration_id)
    if result:
        status = result.status
    else:
        record = migration_store.get(migration_id)
        if not record:
            raise HTTPException(404, "Migration not found")
        status = record.get("status", "")
    return {"migration_id": migration_id, "status": status,
            "progress": 100 if status in ("complete", "failed") else 50}


@app.get("/api/migrate/{migration_id}/report")
def migration_report(migration_id: str):
    result = _state["migrations"].get(migration_id)
    if result:
        return result.to_dict()
    # Fall back to the persisted record (e.g. after a server restart).
    record = migration_store.get(migration_id)
    if not record:
        raise HTTPException(404, "Migration not found")
    return record


# ══════════════════════════════════════════════════════════════════
# Migration History (persisted upload + migration results)
# ══════════════════════════════════════════════════════════════════

@app.get("/api/migrations")
def list_migrations():
    """List past migrations (persisted), newest first, for reopening later."""
    return {"migrations": migration_store.list_summaries()}


@app.get("/api/migrations/{migration_id}")
def get_migration(migration_id: str):
    """Return a full persisted migration record — the source model plus the
    migration result — so a previous upload and its results can be reopened."""
    record = migration_store.get(migration_id)
    if not record:
        raise HTTPException(404, "Migration not found")
    return record


# ── Dashboard Endpoints ──────────────────────────────────────────────
@app.post("/api/dashboard/generate")
def dashboard_generate(body: dict = {}):
    """Generate a Lakeview dashboard spec from metric view data."""
    from backend.dashboard_generator import DashboardGenerator
    catalog = body.get("catalog", "main")
    schema = body.get("schema", "default")
    model_name = body.get("model_name", "Migration")
    mv_specs = body.get("metric_view_specs", [])
    if not mv_specs and _state.get("model"):
        # Auto-build from current model state
        mv_specs = [{"fact_group": "default", "yaml": "", "sql": ""}]
    gen = DashboardGenerator()
    spec = gen.generate_from_metric_views(mv_specs, model_name, catalog, schema)
    return spec.to_dict()

@app.post("/api/dashboard/deploy")
def dashboard_deploy(body: dict = {}):
    """Deploy a Lakeview dashboard to Databricks."""
    from backend.lakeview_client import LakeviewClient
    dbx = _state.get("dbx_client")
    if not dbx:
        raise HTTPException(400, "Not connected to Databricks")
    spec_json = body.get("serialized_dashboard", "")
    display_name = body.get("display_name", "PBI Migration Dashboard")
    warehouse_id = body.get("warehouse_id", _state.get("warehouse_id", ""))
    if not spec_json:
        raise HTTPException(400, "serialized_dashboard required")
    lv = LakeviewClient(dbx)
    result = lv.deploy_dashboard(
        display_name=display_name,
        serialized_dashboard=spec_json,
        warehouse_id=warehouse_id,
        publish=body.get("publish", True),
    )
    return result.to_dict()

@app.get("/api/dashboard/{dashboard_id}")
def dashboard_get(dashboard_id: str):
    """Get dashboard info."""
    from backend.lakeview_client import LakeviewClient
    dbx = _state.get("dbx_client")
    if not dbx:
        raise HTTPException(400, "Not connected to Databricks")
    lv = LakeviewClient(dbx)
    return lv.get_dashboard(dashboard_id).to_dict()


# ══════════════════════════════════════════════════════════════════
# Static Files (Vite build)
# ══════════════════════════════════════════════════════════════════

static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.isdir(static_dir):
    static_dir = os.path.join(os.path.dirname(__file__), "frontend", "dist")

if os.path.isdir(static_dir):
    assets_dir = os.path.join(static_dir, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "API endpoint not found")
        file_path = os.path.join(static_dir, full_path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        index = os.path.join(static_dir, "index.html")
        if os.path.isfile(index):
            return FileResponse(index)
        raise HTTPException(404, "Frontend not built. Run: cd frontend && npm run build")
