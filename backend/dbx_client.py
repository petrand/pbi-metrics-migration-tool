"""
Databricks API Client

Handles authentication, SQL Statement Execution, Unity Catalog browsing,
and metric-view deployment/validation/rollback.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


@dataclass
class DBXWarehouse:
    id: str
    name: str
    warehouse_type: str
    state: str
    num_clusters: int = 1


@dataclass
class DBXCatalog:
    name: str
    comment: Optional[str] = None
    owner: Optional[str] = None


@dataclass
class DBXSchema:
    name: str
    catalog_name: str
    comment: Optional[str] = None


@dataclass
class DBXStatementResult:
    statement_id: str
    status: str
    error_message: Optional[str] = None
    results: Optional[List[Any]] = None


@dataclass
class DeploymentResult:
    view_name: str
    status: str
    statement_id: Optional[str] = None
    error: Optional[str] = None
    sql_executed: Optional[str] = None
    duration_ms: Optional[int] = None
    describe_output: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}


def _build_session(retries=3, backoff=0.5):
    s = requests.Session()
    retry = Retry(total=retries, backoff_factor=backoff,
                  status_forcelist=[429, 503],
                  allowed_methods=["GET", "POST", "DELETE"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


class DatabricksAuthError(Exception):
    pass


class DatabricksAPIError(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


class StatementTimeoutError(Exception):
    pass


class DatabricksClient:
    """Client for Databricks REST APIs used by the PBI migration tool."""

    def __init__(self, host: str = None, token: str = None):
        self.host = (host or os.environ.get("DATABRICKS_HOST", "")).rstrip("/")
        if not self.host:
            raise ValueError("Databricks host required (arg or DATABRICKS_HOST env)")
        self._token = None
        self._session = _build_session()

        resolved = (
            token
            or os.environ.get("DATABRICKS_APP_TOKEN")
            or self._read_app_secret()
            or os.environ.get("DATABRICKS_TOKEN")
        )
        if resolved:
            self._set_token(resolved)

    @staticmethod
    def _read_app_secret():
        try:
            with open("/var/run/secrets/databricks-app-token") as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError):
            return None

    def _set_token(self, token: str):
        self._token = token
        self._session.headers["Authorization"] = f"Bearer {token}"

    def authenticate(self, token: str = None) -> bool:
        if token:
            self._set_token(token)
        if not self._token:
            raise DatabricksAuthError("No token. Pass PAT or set DATABRICKS_TOKEN.")
        try:
            self._request("GET", "/api/2.0/sql/warehouses")
            return True
        except (DatabricksAuthError, DatabricksAPIError):
            return False

    def test_connection(self) -> dict:
        try:
            wh = self.list_warehouses()
            return {"host": self.host, "warehouse_count": len(wh), "authenticated": True}
        except Exception as e:
            return {"host": self.host, "warehouse_count": 0, "authenticated": False, "error": str(e)}

    def list_warehouses(self) -> List[DBXWarehouse]:
        data = self._request("GET", "/api/2.0/sql/warehouses")
        wh = [DBXWarehouse(id=w["id"], name=w.get("name", ""),
                           warehouse_type=w.get("warehouse_type", ""),
                           state=w.get("state", ""),
                           num_clusters=w.get("num_clusters", 1))
              for w in data.get("warehouses", [])]
        wh.sort(key=lambda x: (0 if x.warehouse_type == "SERVERLESS" else 1, x.name))
        return wh

    def list_catalogs(self) -> List[DBXCatalog]:
        data = self._request("GET", "/api/2.1/unity-catalog/catalogs")
        return [DBXCatalog(name=c["name"], comment=c.get("comment"),
                           owner=c.get("owner"))
                for c in data.get("catalogs", [])]

    def list_schemas(self, catalog: str) -> List[DBXSchema]:
        data = self._request("GET", "/api/2.1/unity-catalog/schemas",
                             params={"catalog_name": catalog})
        return [DBXSchema(name=s["name"], catalog_name=catalog,
                          comment=s.get("comment"))
                for s in data.get("schemas", [])]

    def list_tables(self, catalog: str, schema: str) -> List[str]:
        data = self._request("GET", "/api/2.1/unity-catalog/tables",
                             params={"catalog_name": catalog, "schema_name": schema})
        return [t.get("full_name", f"{catalog}.{schema}.{t['name']}")
                for t in data.get("tables", [])]

    def execute_statement(self, sql: str, warehouse_id: str,
                          catalog: str = None, schema: str = None,
                          timeout: int = 120) -> DBXStatementResult:
        payload = {"statement": sql, "warehouse_id": warehouse_id,
                   "wait_timeout": "0s", "on_wait_timeout": "CANCEL"}
        if catalog:
            payload["catalog"] = catalog
        if schema:
            payload["schema"] = schema

        resp = self._request("POST", "/api/2.0/sql/statements", json=payload)
        sid = resp["statement_id"]
        state = resp.get("status", {}).get("state", "PENDING")
        if state in _TERMINAL:
            return self._parse_stmt(resp)
        return self._poll(sid, timeout)

    def _poll(self, sid: str, timeout: int = 120) -> DBXStatementResult:
        deadline = time.monotonic() + timeout
        sleep = 0.5
        while True:
            if time.monotonic() > deadline:
                raise StatementTimeoutError(f"Statement {sid} timed out after {timeout}s")
            resp = self._request("GET", f"/api/2.0/sql/statements/{sid}")
            state = resp.get("status", {}).get("state", "PENDING")
            if state in _TERMINAL:
                return self._parse_stmt(resp)
            time.sleep(min(sleep, 10))
            sleep *= 1.5

    @staticmethod
    def _parse_stmt(resp) -> DBXStatementResult:
        status = resp.get("status", {})
        state = status.get("state", "UNKNOWN")
        err = status.get("error", {}).get("message") if status.get("error") else None
        results = None
        result_data = resp.get("result", {})
        if result_data:
            cols = [c.get("name") for c in resp.get("manifest", {}).get("schema", {}).get("columns", [])]
            rows = result_data.get("data_array", [])
            if cols and rows:
                results = [dict(zip(cols, r)) for r in rows]
            elif rows:
                results = rows
        return DBXStatementResult(statement_id=resp["statement_id"],
                                  status=state, error_message=err, results=results)

    def deploy_metric_view(self, sql: str, warehouse_id: str,
                           catalog: str, schema: str) -> DeploymentResult:
        view_name = self._extract_view_name(sql, catalog, schema)
        start = int(time.monotonic() * 1000)
        try:
            result = self.execute_statement(sql, warehouse_id, catalog, schema)
        except StatementTimeoutError as e:
            dur = int(time.monotonic() * 1000) - start
            return DeploymentResult(view_name=view_name, status="failed",
                                    sql_executed=sql, error=str(e), duration_ms=dur)
        dur = int(time.monotonic() * 1000) - start
        if result.status != "SUCCEEDED":
            return DeploymentResult(view_name=view_name, status="failed",
                                    statement_id=result.statement_id,
                                    error=result.error_message,
                                    sql_executed=sql, duration_ms=dur)
        desc = None
        try:
            desc = self.describe_view(view_name, warehouse_id)
        except Exception:
            pass
        return DeploymentResult(view_name=view_name, status="success",
                                statement_id=result.statement_id,
                                sql_executed=sql, duration_ms=dur,
                                describe_output=desc)

    def validate_metric_view(self, sql: str, warehouse_id: str,
                             catalog: str, schema: str) -> dict:
        try:
            result = self.execute_statement(f"EXPLAIN {sql}", warehouse_id, catalog, schema)
        except StatementTimeoutError as e:
            return {"valid": False, "error": str(e)}
        if result.status == "SUCCEEDED":
            return {"valid": True, "explain_output": result.results}
        return {"valid": False, "error": result.error_message}

    def rollback_metric_view(self, view_name: str, warehouse_id: str) -> DeploymentResult:
        sql = f"DROP VIEW IF EXISTS {view_name}"
        start = int(time.monotonic() * 1000)
        try:
            result = self.execute_statement(sql, warehouse_id)
        except StatementTimeoutError as e:
            dur = int(time.monotonic() * 1000) - start
            return DeploymentResult(view_name=view_name, status="failed",
                                    sql_executed=sql, error=str(e), duration_ms=dur)
        dur = int(time.monotonic() * 1000) - start
        if result.status == "SUCCEEDED":
            return DeploymentResult(view_name=view_name, status="rolled_back",
                                    statement_id=result.statement_id,
                                    sql_executed=sql, duration_ms=dur)
        return DeploymentResult(view_name=view_name, status="failed",
                                statement_id=result.statement_id,
                                error=result.error_message,
                                sql_executed=sql, duration_ms=dur)

    def describe_view(self, view_name: str, warehouse_id: str) -> dict:
        result = self.execute_statement(f"DESCRIBE TABLE EXTENDED {view_name}", warehouse_id)
        if result.status != "SUCCEEDED":
            raise DatabricksAPIError(f"DESCRIBE failed: {result.error_message}")
        return {"view_name": view_name, "columns": result.results or []}

    def _request(self, method, path, **kwargs):
        url = f"{self.host}{path}"
        timeout = kwargs.pop("timeout", 30)
        resp = self._session.request(method, url, timeout=timeout, **kwargs)
        if resp.status_code == 401:
            raise DatabricksAuthError(f"Auth failed (401) for {method} {path}")
        if resp.status_code == 403:
            raise DatabricksAuthError(f"Forbidden (403) for {method} {path}")
        if not resp.ok:
            raise DatabricksAPIError(f"API error {resp.status_code}: {resp.text[:300]}",
                                     resp.status_code)
        return resp.json() if resp.content else {}

    @staticmethod
    def _extract_view_name(sql, catalog, schema):
        m = re.search(r'CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\S+)', sql, re.IGNORECASE)
        if m:
            name = m.group(1).strip('`"\'')
            return name if '.' in name else f"{catalog}.{schema}.{name}"
        return f"{catalog}.{schema}.<unknown>"
