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

# ── Sample models for demo mode ──
SAMPLE_MODELS = [
    {
        "id": "sales", "name": "Sales Analytics",
        "tables": [
            {"name": "FactSales", "columns": [
                {"name": "SalesKey", "dataType": "int64"},
                {"name": "OrderDate", "dataType": "dateTime"},
                {"name": "ProductKey", "dataType": "int64"},
                {"name": "CustomerKey", "dataType": "int64"},
                {"name": "SalesAmount", "dataType": "decimal", "description": "Transaction amount"},
                {"name": "Quantity", "dataType": "int64"},
                {"name": "DiscountAmount", "dataType": "decimal"},
            ], "measures": [
                {"name": "Total Revenue", "expression": "SUM(FactSales[SalesAmount])", "description": "Sum of all sales amounts"},
                {"name": "Total Quantity", "expression": "SUM(FactSales[Quantity])", "description": "Total units sold"},
                {"name": "Avg Order Value", "expression": "DIVIDE(SUM(FactSales[SalesAmount]), DISTINCTCOUNT(FactSales[SalesKey]), 0)", "description": "Average revenue per order"},
                {"name": "Customer Count", "expression": "DISTINCTCOUNT(FactSales[CustomerKey])", "description": "Unique customers"},
                {"name": "Revenue per Customer", "expression": "DIVIDE(SUM(FactSales[SalesAmount]), DISTINCTCOUNT(FactSales[CustomerKey]), 0)", "description": "Revenue per unique customer"},
                {"name": "Net Revenue", "expression": "SUM(FactSales[SalesAmount]) - SUM(FactSales[DiscountAmount])", "description": "Revenue after discounts"},
            ]},
            {"name": "DimProduct", "columns": [
                {"name": "ProductKey", "dataType": "int64"},
                {"name": "ProductName", "dataType": "string"},
                {"name": "Category", "dataType": "string"},
                {"name": "SubCategory", "dataType": "string"},
            ]},
            {"name": "DimCustomer", "columns": [
                {"name": "CustomerKey", "dataType": "int64"},
                {"name": "CustomerName", "dataType": "string"},
                {"name": "Region", "dataType": "string"},
                {"name": "Segment", "dataType": "string"},
            ]},
            {"name": "DimDate", "columns": [
                {"name": "DateKey", "dataType": "int64"},
                {"name": "Date", "dataType": "dateTime"},
                {"name": "Year", "dataType": "int64"},
                {"name": "Quarter", "dataType": "string"},
                {"name": "Month", "dataType": "string"},
            ]},
        ],
        "relationships": [
            {"from": "FactSales.ProductKey", "to": "DimProduct.ProductKey", "type": "manyToOne"},
            {"from": "FactSales.CustomerKey", "to": "DimCustomer.CustomerKey", "type": "manyToOne"},
            {"from": "FactSales.OrderDate", "to": "DimDate.Date", "type": "manyToOne"},
        ],
    },
    {
        "id": "healthcare", "name": "Healthcare KPIs",
        "tables": [
            {"name": "FactClaims", "columns": [
                {"name": "ClaimKey", "dataType": "int64"},
                {"name": "PatientKey", "dataType": "int64"},
                {"name": "ProviderKey", "dataType": "int64"},
                {"name": "ServiceDate", "dataType": "dateTime"},
                {"name": "ClaimAmount", "dataType": "decimal"},
                {"name": "LengthOfStay", "dataType": "int64"},
                {"name": "IsReadmission", "dataType": "boolean"},
            ], "measures": [
                {"name": "Total Claims", "expression": "SUM(FactClaims[ClaimAmount])", "description": "Total claim dollars"},
                {"name": "Claim Count", "expression": "COUNT(FactClaims[ClaimKey])", "description": "Number of claims"},
                {"name": "Avg Length of Stay", "expression": "AVERAGE(FactClaims[LengthOfStay])", "description": "Average patient stay"},
                {"name": "Readmission Rate", "expression": "DIVIDE(SUM(FactClaims[IsReadmission]), COUNT(FactClaims[ClaimKey]), 0)", "description": "Readmission percentage"},
                {"name": "Cost per Encounter", "expression": "DIVIDE(SUM(FactClaims[ClaimAmount]), COUNT(FactClaims[ClaimKey]), 0)", "description": "Average cost per claim"},
            ]},
            {"name": "DimPatient", "columns": [
                {"name": "PatientKey", "dataType": "int64"},
                {"name": "PatientName", "dataType": "string"},
                {"name": "AgeGroup", "dataType": "string"},
                {"name": "Gender", "dataType": "string"},
            ]},
            {"name": "DimProvider", "columns": [
                {"name": "ProviderKey", "dataType": "int64"},
                {"name": "ProviderName", "dataType": "string"},
                {"name": "Specialty", "dataType": "string"},
                {"name": "Facility", "dataType": "string"},
            ]},
        ],
        "relationships": [
            {"from": "FactClaims.PatientKey", "to": "DimPatient.PatientKey", "type": "manyToOne"},
            {"from": "FactClaims.ProviderKey", "to": "DimProvider.ProviderKey", "type": "manyToOne"},
        ],
    },
    {
        "id": "finance", "name": "Financial Reporting",
        "tables": [
            {"name": "FactTransactions", "columns": [
                {"name": "TransactionKey", "dataType": "int64"},
                {"name": "AccountKey", "dataType": "int64"},
                {"name": "PeriodKey", "dataType": "int64"},
                {"name": "Amount", "dataType": "decimal"},
                {"name": "BudgetAmount", "dataType": "decimal"},
                {"name": "TransactionType", "dataType": "string"},
            ], "measures": [
                {"name": "Net Revenue", "expression": "SUM(FactTransactions[Amount])", "description": "Total net revenue"},
                {"name": "Budget Total", "expression": "SUM(FactTransactions[BudgetAmount])", "description": "Total budget"},
                {"name": "Budget Variance", "expression": "SUM(FactTransactions[Amount]) - SUM(FactTransactions[BudgetAmount])", "description": "Actual vs budget"},
                {"name": "Transaction Count", "expression": "COUNT(FactTransactions[TransactionKey])", "description": "Number of transactions"},
                {"name": "Avg Transaction", "expression": "AVERAGE(FactTransactions[Amount])", "description": "Average transaction value"},
            ]},
            {"name": "DimAccount", "columns": [
                {"name": "AccountKey", "dataType": "int64"},
                {"name": "AccountName", "dataType": "string"},
                {"name": "AccountType", "dataType": "string"},
                {"name": "Department", "dataType": "string"},
            ]},
            {"name": "DimPeriod", "columns": [
                {"name": "PeriodKey", "dataType": "int64"},
                {"name": "FiscalYear", "dataType": "int64"},
                {"name": "FiscalQuarter", "dataType": "string"},
                {"name": "FiscalMonth", "dataType": "string"},
            ]},
        ],
        "relationships": [
            {"from": "FactTransactions.AccountKey", "to": "DimAccount.AccountKey", "type": "manyToOne"},
            {"from": "FactTransactions.PeriodKey", "to": "DimPeriod.PeriodKey", "type": "manyToOne"},
        ],
    },
]


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
                "sample_models": True,
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
# Sample Models
# ══════════════════════════════════════════════════════════════════

@app.get("/api/samples")
def list_samples():
    return {"models": [{"id": m["id"], "name": m["name"],
                        "tables": len(m["tables"]),
                        "measures": sum(len(t.get("measures", [])) for t in m["tables"]),
                        "relationships": len(m.get("relationships", []))}
                       for m in SAMPLE_MODELS]}


@app.get("/api/samples/{model_id}")
def get_sample(model_id: str):
    model = next((m for m in SAMPLE_MODELS if m["id"] == model_id), None)
    if not model:
        raise HTTPException(404, f"Sample model '{model_id}' not found")
    _state["current_model"] = model
    return {"model": model}


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
        raise HTTPException(400, "No model loaded. Upload TMDL, extract from PBI, or select a sample.")

    config = MigrationConfig(
        catalog=body.get("catalog", "main"),
        schema=body.get("schema", "default"),
        warehouse_id=body.get("warehouse_id", ""),
        deploy=body.get("deploy", False),
        dry_run=body.get("dry_run", False),
        validate_only=body.get("validate_only", False),
    )

    # Load overrides if provided
    if body.get("overrides"):
        mgr = OverridesManager()
        config.overrides = mgr.load_from_dict(body["overrides"])

    pipeline = MigrationPipeline()
    dbx_client = _state.get("dbx_client") if config.deploy else None
    result = pipeline.run(model, config, dbx_client)

    _state["migrations"][result.migration_id] = result
    return result.to_dict()


@app.get("/api/migrate/{migration_id}/status")
def migration_status(migration_id: str):
    result = _state["migrations"].get(migration_id)
    if not result:
        raise HTTPException(404, "Migration not found")
    return {"migration_id": migration_id, "status": result.status,
            "progress": 100 if result.status in ("complete", "failed") else 50}


@app.get("/api/migrate/{migration_id}/report")
def migration_report(migration_id: str):
    result = _state["migrations"].get(migration_id)
    if not result:
        raise HTTPException(404, "Migration not found")
    return result.to_dict()


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
