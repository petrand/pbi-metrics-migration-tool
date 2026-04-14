"""
Tests for the FastAPI endpoints in main.py.

Uses httpx TestClient (via fastapi.testclient.TestClient).
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


# ── Health & Ready ────────────────────────────────────────────────────────────

def test_health_returns_200():
    response = client.get("/api/health")
    assert response.status_code == 200


def test_health_status_is_healthy():
    response = client.get("/api/health")
    assert response.json()["status"] == "healthy"


def test_health_version_present():
    response = client.get("/api/health")
    assert "version" in response.json()


def test_ready_returns_200():
    response = client.get("/api/ready")
    assert response.status_code == 200


def test_ready_status_is_ready():
    response = client.get("/api/ready")
    assert response.json()["status"] == "ready"


def test_ready_capabilities_present():
    response = client.get("/api/ready")
    assert "capabilities" in response.json()


def test_ready_sample_models_capability_true():
    response = client.get("/api/ready")
    assert response.json()["capabilities"]["sample_models"] is True


def test_ready_dax_translation_capability_true():
    response = client.get("/api/ready")
    assert response.json()["capabilities"]["dax_translation"] is True


# ── Sample Models ─────────────────────────────────────────────────────────────

def test_list_samples_returns_200():
    response = client.get("/api/samples")
    assert response.status_code == 200


def test_list_samples_returns_three_models():
    response = client.get("/api/samples")
    models = response.json()["models"]
    assert len(models) == 3


def test_list_samples_has_sales_model():
    response = client.get("/api/samples")
    ids = [m["id"] for m in response.json()["models"]]
    assert "sales" in ids


def test_list_samples_has_healthcare_model():
    response = client.get("/api/samples")
    ids = [m["id"] for m in response.json()["models"]]
    assert "healthcare" in ids


def test_list_samples_has_finance_model():
    response = client.get("/api/samples")
    ids = [m["id"] for m in response.json()["models"]]
    assert "finance" in ids


def test_get_sample_sales_returns_200():
    response = client.get("/api/samples/sales")
    assert response.status_code == 200


def test_get_sample_sales_has_correct_name():
    response = client.get("/api/samples/sales")
    assert response.json()["model"]["name"] == "Sales Analytics"


def test_get_sample_sales_has_tables():
    response = client.get("/api/samples/sales")
    model = response.json()["model"]
    assert len(model["tables"]) > 0


def test_get_sample_nonexistent_returns_404():
    response = client.get("/api/samples/nonexistent_model_xyz")
    assert response.status_code == 404


# ── DAX Translation ───────────────────────────────────────────────────────────

def test_translate_returns_200():
    response = client.post("/api/translate", json={"dax": "SUM(Table[Col])", "table_name": "Table"})
    assert response.status_code == 200


def test_translate_sum_returns_sql_sum():
    response = client.post("/api/translate", json={"dax": "SUM(FactSales[SalesAmount])", "table_name": "FactSales"})
    result = response.json()
    assert "SUM" in result["translated_sql"]


def test_translate_returns_original_dax():
    dax = "SUM(FactSales[SalesAmount])"
    response = client.post("/api/translate", json={"dax": dax})
    assert response.json()["original_dax"] == dax


def test_translate_returns_status():
    response = client.post("/api/translate", json={"dax": "SUM(FactSales[SalesAmount])"})
    assert "status" in response.json()


def test_translate_returns_confidence():
    response = client.post("/api/translate", json={"dax": "SUM(FactSales[SalesAmount])"})
    assert "confidence" in response.json()


def test_translate_empty_dax_returns_unsupported():
    response = client.post("/api/translate", json={"dax": ""})
    assert response.json()["status"] == "unsupported"


# ── Batch Translation ─────────────────────────────────────────────────────────

def test_translate_batch_returns_200():
    payload = {
        "measures": [
            {"name": "Revenue", "expression": "SUM(FactSales[SalesAmount])"},
            {"name": "Qty", "expression": "SUM(FactSales[Quantity])"},
        ],
        "table_name": "FactSales",
    }
    response = client.post("/api/translate/batch", json=payload)
    assert response.status_code == 200


def test_translate_batch_returns_results_list():
    payload = {
        "measures": [
            {"name": "Revenue", "expression": "SUM(FactSales[SalesAmount])"},
        ],
        "table_name": "FactSales",
    }
    response = client.post("/api/translate/batch", json=payload)
    assert "results" in response.json()


def test_translate_batch_result_count_matches_input():
    measures = [
        {"name": f"measure_{i}", "expression": f"SUM(FactSales[Col{i}])"}
        for i in range(3)
    ]
    response = client.post("/api/translate/batch", json={"measures": measures, "table_name": "FactSales"})
    assert len(response.json()["results"]) == 3


# ── Validation ────────────────────────────────────────────────────────────────

VALID_YAML = """
version: "1.1"
source: catalog.schema.fact_sales
measures:
  - name: total_revenue
    expr: SUM(source.sales_amount)
"""

def test_dbx_validate_valid_yaml_returns_200():
    response = client.post("/api/dbx/validate", json={"yaml": VALID_YAML})
    assert response.status_code == 200


def test_dbx_validate_valid_yaml_returns_valid_true():
    response = client.post("/api/dbx/validate", json={"yaml": VALID_YAML})
    assert response.json()["valid"] is True


def test_dbx_validate_invalid_yaml_returns_valid_false():
    bad_yaml = "version: '9.9'\nmeasures:\n  - name: m1\n    expr: SUM(x)\n"
    response = client.post("/api/dbx/validate", json={"yaml": bad_yaml})
    assert response.json()["valid"] is False


def test_dbx_validate_no_payload_returns_error():
    response = client.post("/api/dbx/validate", json={})
    data = response.json()
    assert data.get("valid") is False or "error" in data


# ── Migration Pipeline ────────────────────────────────────────────────────────

def test_migrate_with_sample_model_returns_200():
    # Load the sales sample first
    client.get("/api/samples/sales")
    response = client.post("/api/migrate", json={
        "catalog": "main",
        "schema": "default",
        "dry_run": True,
    })
    assert response.status_code == 200


def test_migrate_without_model_returns_400():
    # Ensure no model is in state by passing an empty model field
    # Don't pass a model and ensure state is clear
    from main import _state
    _state["current_model"] = None
    response = client.post("/api/migrate", json={})
    assert response.status_code == 400


def test_migrate_returns_migration_id():
    client.get("/api/samples/sales")
    response = client.post("/api/migrate", json={"dry_run": True})
    assert "migration_id" in response.json()


def test_migrate_returns_status():
    client.get("/api/samples/sales")
    response = client.post("/api/migrate", json={"dry_run": True})
    assert "status" in response.json()


def test_migrate_with_inline_model_returns_200():
    sample = client.get("/api/samples/sales").json()["model"]
    response = client.post("/api/migrate", json={
        "model": sample,
        "catalog": "main",
        "schema": "test",
        "dry_run": True,
    })
    assert response.status_code == 200
