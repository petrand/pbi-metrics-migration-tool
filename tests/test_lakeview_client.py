"""
Tests for backend/lakeview_client.py

Covers LakeviewDashboardInfo dataclass and LakeviewClient behaviour
that can be exercised without network access or a real DatabricksClient.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.lakeview_client import LakeviewClient, LakeviewDashboardInfo


# ── Minimal mock DatabricksClient ──────────────────────────────────────────────

class MockDBXClient:
    """Minimal stand-in for DatabricksClient — no network required."""
    host = "https://my-workspace.cloud.databricks.com"

    def _request(self, *args, **kwargs):
        return {}


# ── LakeviewDashboardInfo ──────────────────────────────────────────────────────

def test_lakeview_dashboard_info_dataclass():
    info = LakeviewDashboardInfo(
        dashboard_id="dash-001",
        display_name="Sales Dashboard",
    )
    assert info.dashboard_id == "dash-001"
    assert info.display_name == "Sales Dashboard"


def test_lakeview_dashboard_info_to_dict_has_required_keys():
    info = LakeviewDashboardInfo(
        dashboard_id="dash-001",
        display_name="Sales Dashboard",
    )
    d = info.to_dict()
    assert "dashboard_id" in d
    assert "display_name" in d


def test_lakeview_dashboard_info_defaults():
    info = LakeviewDashboardInfo(
        dashboard_id="dash-001",
        display_name="Sales Dashboard",
    )
    assert info.path == ""
    assert info.warehouse_id == ""
    assert info.create_time == ""
    assert info.update_time == ""
    assert info.lifecycle_state == ""
    assert info.published_url == ""


def test_lakeview_dashboard_info_to_dict_reflects_values():
    info = LakeviewDashboardInfo(
        dashboard_id="dash-001",
        display_name="My Dashboard",
        warehouse_id="wh-123",
        path="/Workspace/Dashboards/my-dash",
    )
    d = info.to_dict()
    assert d["dashboard_id"] == "dash-001"
    assert d["display_name"] == "My Dashboard"
    assert d["warehouse_id"] == "wh-123"


def test_lakeview_dashboard_info_to_dict_path_included_when_set():
    info = LakeviewDashboardInfo(
        dashboard_id="d1",
        display_name="D",
        path="/Workspace/test",
    )
    d = info.to_dict()
    assert d.get("path") == "/Workspace/test"


def test_lakeview_dashboard_info_lifecycle_state_stored():
    info = LakeviewDashboardInfo(
        dashboard_id="d1",
        display_name="D",
        lifecycle_state="ACTIVE",
    )
    assert info.lifecycle_state == "ACTIVE"


def test_lakeview_dashboard_info_published_url_stored():
    url = "https://workspace.cloud.databricks.com/dashboardsv3/d1/published"
    info = LakeviewDashboardInfo(
        dashboard_id="d1",
        display_name="D",
        published_url=url,
    )
    assert info.published_url == url


# ── LakeviewClient initialisation ─────────────────────────────────────────────

def test_lakeview_client_init_stores_dbx_client():
    mock = MockDBXClient()
    client = LakeviewClient(mock)
    assert client._client is mock


def test_lakeview_client_accepts_mock_dbx_client():
    """LakeviewClient can be constructed without a real DatabricksClient."""
    mock = MockDBXClient()
    client = LakeviewClient(mock)
    assert client is not None


# ── get_published_url ──────────────────────────────────────────────────────────

def test_get_published_url_format():
    mock = MockDBXClient()
    client = LakeviewClient(mock)
    url = client.get_published_url("dash-abc-123")
    assert url.startswith("https://my-workspace.cloud.databricks.com")
    assert "dash-abc-123" in url
    assert "published" in url


def test_get_published_url_no_trailing_slash():
    mock = MockDBXClient()
    client = LakeviewClient(mock)
    url = client.get_published_url("dash-001")
    # Should not have double slashes from host trailing slash
    assert "//" not in url.replace("https://", "")


def test_get_published_url_contains_dashboardsv3():
    mock = MockDBXClient()
    client = LakeviewClient(mock)
    url = client.get_published_url("dash-001")
    assert "dashboardsv3" in url


def test_get_published_url_ends_with_published():
    mock = MockDBXClient()
    client = LakeviewClient(mock)
    url = client.get_published_url("dash-001")
    assert url.endswith("/published")


def test_get_published_url_different_dashboard_ids():
    mock = MockDBXClient()
    client = LakeviewClient(mock)
    url1 = client.get_published_url("dash-001")
    url2 = client.get_published_url("dash-999")
    assert url1 != url2
    assert "dash-001" in url1
    assert "dash-999" in url2
