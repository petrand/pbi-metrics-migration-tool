# Power BI to Databricks Metric View Migration Tool

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react)
![Databricks](https://img.shields.io/badge/Databricks-Metric%20Views-FF3621?logo=databricks)

Automate the translation of Power BI semantic models (measures, dimensions, relationships) into Databricks [Metric Views](https://docs.databricks.com/aws/en/metric-views/index.html) — enabling governed, reusable analytics defined once and consumed everywhere.

**Live App:** https://pbi-metrics-migration-1602460480284688.aws.databricksapps.com

---

## Overview

Power BI models encode a large amount of business logic in DAX measures and tabular relationships. Re-implementing that logic manually in Databricks is error-prone and time-consuming. This tool:

1. **Extracts** the semantic model from a Power BI dataset (via the REST/Scanner API or a TMDL file upload)
2. **Translates** DAX expressions to standard SQL (30+ DAX functions via 14-pass pipeline)
3. **Validates** every translated expression against Metric View YAML v1.1 spec
4. **Evaluates** translation quality with per-measure confidence scores and audit reports
5. **Generates** Databricks Metric View DDL (YAML + SQL)
6. **Deploys** the metric views to your Databricks workspace via Statement Execution API

**Who it is for:** Data engineers and analytics engineers migrating Power BI workloads to the Databricks Lakehouse.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Migration Pipeline (7 Steps)                       │
│                                                                             │
│  ┌──────────┐  ┌───────────┐  ┌───────────┐  ┌──────────┐  ┌───────────┐  │
│  │ EXTRACT  │─▶│ OVERRIDES │─▶│ TRANSLATE │─▶│ GENERATE │─▶│ VALIDATE  │  │
│  │          │  │           │  │           │  │          │  │           │  │
│  │ PBI API  │  │ Table/col │  │ DAX → SQL │  │ Metric   │  │ YAML+SQL  │  │
│  │ TMDL     │  │ mappings  │  │ 14-pass   │  │ View DDL │  │ + circ.   │  │
│  │ Samples  │  │ exclusions│  │ 30+ funcs │  │ YAML 1.1 │  │ ref check │  │
│  └──────────┘  └───────────┘  └───────────┘  └──────────┘  └─────┬─────┘  │
│                                                                    │        │
│                                           ┌──────────┐    ┌───────▼──────┐ │
│                                           │  DEPLOY  │◀───│  EVALUATE    │ │
│                                           │ Statement│    │  Per-measure │ │
│                                           │ Exec API │    │  confidence  │ │
│                                           └──────────┘    └──────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Features

- **Real Power BI API integration** — connect via MSAL OAuth2 with Scanner API or DAX INFO queries
- **TMDL file upload** — upload `.tmdl` or `.bim` ZIP files when API access is unavailable
- **3 built-in sample models** — Sales Analytics, Healthcare KPIs, Financial Reporting for demo/testing
- **30+ DAX functions translated** — SUM, CALCULATE, FILTER/ALL, DIVIDE, IF, RELATED, VAR/RETURN, time-intelligence, iterators, and more
- **14-pass translation pipeline** — normalize, VAR/RETURN, time intelligence, CALCULATE (balanced-paren), aggregations, conditionals, logical, dates, text, lookups, iterators, table refs, measure refs, cleanup
- **Override system** — table/column mappings, extra joins, exclusions, hand-written SQL via `overrides.yaml`
- **Evaluation & audit reports** — per-measure confidence scores, fact-group conversion rates, pipeline summary
- **YAML v1.1 validation** — structure, SQL expression, circular reference detection, residual DAX detection
- **Databricks deployment** — pushes metric view DDL via Statement Execution API with rollback support
- **React UI** — step-by-step wizard with glass morphism design, Framer Motion animations, drag-drop TMDL upload

---

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/suryasai87/pbi-metrics-migration-tool.git
cd pbi-metrics-migration-tool

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Install and build the frontend
cd frontend
npm install
npm run build
cd ..

# 4. Start the application
uvicorn main:app --port 8000

# 5. Open the UI
open http://localhost:8000
```

> **Tip:** Set `DATABRICKS_HOST` and `DATABRICKS_TOKEN` (or use `--profile` with the Databricks CLI) before deploying metric views.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Liveness check — returns version, timestamp |
| `GET` | `/api/ready` | Readiness check — returns capability flags |
| `POST` | `/api/pbi/auth` | Authenticate with Power BI via MSAL OAuth2 |
| `GET` | `/api/pbi/workspaces` | List accessible Power BI workspaces |
| `POST` | `/api/pbi/extract/{dataset_id}` | Extract semantic model from a dataset |
| `POST` | `/api/tmdl/upload` | Upload a TMDL/BIM ZIP file |
| `GET` | `/api/samples` | List built-in sample models |
| `GET` | `/api/samples/{id}` | Load a sample model by ID |
| `POST` | `/api/translate` | Translate a single DAX expression to SQL |
| `POST` | `/api/translate/batch` | Batch translate multiple DAX expressions |
| `POST` | `/api/dbx/auth` | Authenticate with Databricks workspace |
| `GET` | `/api/dbx/warehouses` | List SQL warehouses |
| `GET` | `/api/dbx/catalogs` | List Unity Catalog catalogs |
| `GET` | `/api/dbx/schemas/{catalog}` | List schemas in a catalog |
| `POST` | `/api/dbx/validate` | Validate metric view YAML or DDL |
| `POST` | `/api/dbx/deploy` | Deploy metric view to Databricks |
| `POST` | `/api/dbx/rollback` | Roll back a deployed view |
| `POST` | `/api/migrate` | Run full migration pipeline |
| `GET` | `/api/migrate/{id}/status` | Poll migration status |
| `GET` | `/api/migrate/{id}/report` | Get evaluation report |

All request/response bodies are JSON.

---

## Backend Modules

| Module | Purpose |
|--------|---------|
| `backend/dax_translator.py` | 14-pass DAX-to-SQL engine, 30+ functions, balanced-paren CALCULATE |
| `backend/pbi_client.py` | Power BI REST API client with MSAL OAuth2 + Scanner API fallback |
| `backend/tmdl_parser.py` | TMDL/BIM ZIP parser for offline semantic model extraction |
| `backend/dbx_client.py` | Databricks Statement Execution + Unity Catalog API client |
| `backend/yaml_generator.py` | Metric View YAML v1.1 + DDL generation |
| `backend/validator.py` | YAML, SQL, DDL, circular-reference, residual-DAX validation |
| `backend/evaluator.py` | Per-measure/fact-group/pipeline evaluation and audit reports |
| `backend/overrides.py` | Table/column mappings, exclusions, join overrides, measure overrides |
| `backend/pipeline.py` | 7-step migration orchestrator with progress callbacks |

---

## DAX Translation Coverage

| Category | Functions | Status |
|----------|-----------|--------|
| Aggregations | SUM, AVERAGE, MIN, MAX, COUNT, DISTINCTCOUNT, COUNTROWS, COUNTBLANK | Supported |
| Logical | IF, AND, OR, NOT, SWITCH, IFERROR, ISBLANK | Supported |
| Math | DIVIDE (2- and 3-arg) | Supported |
| Filter | CALCULATE, FILTER(ALL(...)), simple column filters | Supported |
| Relationship | RELATED, SELECTEDVALUE | Supported |
| Text | CONCATENATE, CONTAINSSTRING | Supported |
| Date | TODAY, EOMONTH, EDATE, DATEDIFF, DATEADD | Supported |
| Time intelligence | TOTALYTD (window spec extraction) | Supported |
| Variables | VAR / RETURN (multi-variable inlining) | Supported |
| Iterators | SUMX, COUNTX, AVERAGEX | Supported |
| Measure refs | [Measure Name] cross-referencing | Supported |
| Table refs | Table[Column] to source.col / join.col | Supported |
| Semi-additive | FIRSTNONBLANK, LASTNONBLANK, OPENINGBALANCE | Unsupported |
| Path hierarchy | PATH, PATHITEM, PATHCONTAINS | Unsupported |

---

## Configuration

Copy and edit the example overrides file:

```bash
cp overrides.example.yaml overrides.yaml
```

Key sections in `overrides.yaml`:

| Section | Purpose |
|---------|---------|
| `target` | Destination catalog and schema |
| `table_mappings` | Power BI table name -> Unity Catalog table |
| `column_mappings` | Per-table column renames |
| `extra_joins` | Additional joins not in the PBI model |
| `exclude_tables` | Tables to skip entirely |
| `exclude_columns` | Columns to drop per table |
| `exclude_measures` | Measures to skip |
| `merge_fact_groups` | Combine multiple fact tables into one source |
| `measure_overrides` | Hand-written SQL expressions for specific measures |

See `overrides.example.yaml` for a fully annotated template.

---

## Testing

### Run all 198 tests

```bash
pytest tests/ -v
```

### Test files

| File | Tests | Coverage |
|------|-------|----------|
| `tests/test_dax_translator.py` | 52 | DAX-to-SQL translation for 30+ patterns |
| `tests/test_validator.py` | 37 | YAML structure, SQL, DDL, circular refs, residual DAX |
| `tests/test_evaluator.py` | 54 | Measure/fact-group/pipeline evaluation, manifest, serialization |
| `tests/test_overrides.py` | 42 | Table/column mappings, exclusions, joins, measure overrides |
| `tests/test_api.py` | 33 | FastAPI endpoints: health, samples, translate, validate, migrate |

### Frontend dev server

```bash
cd frontend && npm run dev
```

See `TESTING.md` for a complete walkthrough including integration tests and manual UI testing steps.

---

## Deployment to Databricks

The tool runs as a [Databricks App](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/index.html):

```bash
# 1. Upload code to workspace
databricks workspace import-dir . /Workspace/Apps/pbi-metrics-migration --overwrite --profile DEFAULT

# 2. Deploy
databricks apps deploy pbi-metrics-migration \
  --source-code-path /Workspace/Apps/pbi-metrics-migration \
  --profile DEFAULT
```

An `app.yaml` is included (uvicorn on port 8000). Set environment variables in the Databricks App settings:

| Variable | Description |
|----------|-------------|
| `DATABRICKS_HOST` | Workspace URL (auto-detected in Databricks Apps) |
| `DATABRICKS_TOKEN` | PAT or OAuth token (auto-detected in Databricks Apps) |
| `PBI_CLIENT_ID` | Power BI service principal client ID |
| `PBI_CLIENT_SECRET` | Power BI service principal client secret |
| `PBI_TENANT_ID` | Azure tenant ID |

---

## Contributing

1. Fork the repository and create a feature branch
2. Make your changes with tests
3. Run `pytest tests/ -v`
4. Open a pull request

For bugs and feature requests, please [open an issue](../../issues/new).
