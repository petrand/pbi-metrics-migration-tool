# Testing Guide

This document walks through how to test the Power BI to Databricks Metric View Migration Tool end-to-end, from installing dependencies to exercising every major code path.

---

## 1. Install Dependencies

### Python (backend)

```bash
# Requires Python 3.11+
python --version

pip install -r requirements.txt
```

### Node.js (frontend)

```bash
# Requires Node 18+
node --version

cd frontend
npm install
cd ..
```

---

## 2. Run Backend Tests

Unit and integration tests live in `tests/`. Run them with pytest:

```bash
pytest tests/ -v
```

Expected output includes tests for:
- DAX tokenizer and parser
- Individual DAX-to-SQL translation functions (30+ cases)
- Relationship graph builder
- Metric view DDL generator
- Override file loading and merging
- FastAPI route handlers (using `httpx` test client)

All tests must pass before opening a pull request.

---

## 3. Run Frontend Tests

```bash
cd frontend
npm test
```

The frontend test suite uses Vitest and React Testing Library. Tests cover:
- Component rendering for each wizard step
- DAX preview panel behavior
- File upload drag-and-drop
- API call mocking

---

## 4. Start the Development Server

```bash
uvicorn main:app --reload --port 8000
```

The `--reload` flag restarts the server automatically when Python files change. The React frontend is served as static files from `frontend/dist/`. Rebuild after frontend changes:

```bash
cd frontend && npm run build && cd ..
```

---

## 5. Test the Health Endpoint

Verify the server is running:

```bash
curl localhost:8000/api/health
```

Expected response:

```json
{"status": "ok"}
```

---

## 6. Test Sample Models

List the built-in sample Power BI models:

```bash
curl localhost:8000/api/samples
```

Expected response: a JSON array of sample model names, for example:

```json
["AdventureWorks", "ContosoRetail", "NorthwindAnalytics"]
```

Load one of the samples for use in subsequent steps:

```bash
curl -X POST localhost:8000/api/load-sample \
  -H "Content-Type: application/json" \
  -d '{"name": "AdventureWorks"}'
```

---

## 7. Test DAX Translation

Translate a single DAX expression to SQL to verify the translation engine:

```bash
curl -X POST localhost:8000/api/translate \
  -H "Content-Type: application/json" \
  -d '{"dax": "SUM(Table[Col])"}'
```

Expected response:

```json
{"sql": "SUM(source.Col)", "confidence": 1.0, "warnings": []}
```

Try more complex expressions:

```bash
# CALCULATE with FILTER
curl -X POST localhost:8000/api/translate \
  -H "Content-Type: application/json" \
  -d '{"dax": "CALCULATE(SUM(FactSales[Amount]), FILTER(DimProduct, DimProduct[Category] = \"Electronics\"))"}'

# DIVIDE with alternate result
curl -X POST localhost:8000/api/translate \
  -H "Content-Type: application/json" \
  -d '{"dax": "DIVIDE([Total Sales], [Transaction Count], 0)"}'

# VAR / RETURN
curl -X POST localhost:8000/api/translate \
  -H "Content-Type: application/json" \
  -d '{"dax": "VAR base = SUM(FactSales[Amount]) RETURN base * 1.1"}'
```

---

## 8. Test the Full Migration Pipeline

After loading a sample (step 6), run the migration pipeline:

```bash
curl -X POST localhost:8000/api/migrate \
  -H "Content-Type: application/json" \
  -d '{"catalog": "main", "schema": "default"}'
```

The response includes a job ID. Poll for status:

```bash
curl localhost:8000/api/migration-status/<job-id>
```

When `"status": "complete"`, retrieve the results:

```bash
curl localhost:8000/api/results/<job-id>
```

Inspect the generated YAML — it should contain one metric view definition per measure group detected in the sample model.

### Testing with overrides

Copy and edit the example overrides file:

```bash
cp overrides.example.yaml overrides.yaml
# Edit overrides.yaml as needed
```

Pass the overrides path in the request body:

```bash
curl -X POST localhost:8000/api/migrate \
  -H "Content-Type: application/json" \
  -d '{"catalog": "main", "schema": "default", "overrides_path": "overrides.yaml"}'
```

---

## 9. Test the Frontend in a Browser

Open the app in a browser:

```
http://localhost:8000
```

Walk through the wizard:

1. **Step 1 — Connect**: Select "Use Sample Model", choose "AdventureWorks", click Next
2. **Step 2 — Review**: Confirm tables, relationships, and measures are listed correctly
3. **Step 3 — Configure**: Set catalog (`main`) and schema (`default`), optionally upload an overrides file
4. **Step 4 — Translate**: Click "Run Translation", verify the DAX preview panel shows generated SQL for each measure
5. **Step 5 — Deploy**: Click "Generate DDL", review the YAML output in the code viewer

Check the browser console and the server terminal for errors at each step.

---

## 10. Test TMDL File Upload via the UI

1. Export a TMDL or BIM file from Power BI Desktop:
   - **File → Save As → Power BI Template (.pbit)** (contains the BIM model)
   - Or enable TMDL export in Power BI Desktop preview features
2. In the app, choose **"Upload TMDL File"** on the Connect step
3. Drag and drop your `.tmdl` or `.bim` file onto the upload zone
4. Confirm the tables and measures are detected correctly before proceeding

To test the upload endpoint directly:

```bash
curl -X POST localhost:8000/api/upload-tmdl \
  -F "file=@path/to/model.tmdl"
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `uvicorn: command not found` | uvicorn not installed | `pip install uvicorn` |
| Frontend shows blank page | `frontend/dist/` missing | `cd frontend && npm run build` |
| `422 Unprocessable Entity` on `/api/migrate` | Missing required fields | Include `catalog` and `schema` in request body |
| Translation returns `"confidence": 0` | Unsupported DAX pattern | Add a `measure_overrides` entry in `overrides.yaml` |
| Deployment fails with 403 | Missing Databricks permissions | Ensure the token has `USE CATALOG` and `CREATE` on the target schema |
