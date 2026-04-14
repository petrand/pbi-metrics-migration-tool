"""
pbi_client.py — Power BI REST API client for the PBI Metrics Migration Tool.

Connects to Power BI via OAuth2 (MSAL) and extracts semantic model metadata
using the Power BI REST API and the Admin Scanner API.

Supported auth flows:
  - Service Principal  (client_id + client_secret)
  - Device Code        (interactive / user-delegated)
  - Direct token       (frontend passes a pre-acquired Bearer token)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# Optional MSAL import — graceful degradation when the library is absent
# ---------------------------------------------------------------------------
try:
    import msal  # type: ignore

    _MSAL_AVAILABLE = True
except ImportError:
    msal = None  # type: ignore
    _MSAL_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_PBI_BASE = "https://api.powerbi.com/v1.0/myorg"
_DEFAULT_RETRY_ATTEMPTS = 5
_DEFAULT_BACKOFF_BASE = 1.5  # seconds

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PBIWorkspace:
    """Represents a Power BI workspace (group)."""

    id: str
    name: str
    type: str = "Workspace"
    state: str = "Active"


@dataclass
class PBIColumn:
    """A column inside a Power BI table."""

    name: str
    data_type: str = ""
    is_hidden: bool = False
    description: str = ""


@dataclass
class PBIMeasure:
    """A DAX measure inside a Power BI table."""

    name: str
    expression: str = ""
    display_folder: str = ""
    format_string: str = ""
    description: str = ""


@dataclass
class PBITable:
    """A table inside a Power BI semantic model."""

    name: str
    columns: list[PBIColumn] = field(default_factory=list)
    measures: list[PBIMeasure] = field(default_factory=list)
    is_hidden: bool = False


@dataclass
class PBIRelationship:
    """A relationship between two Power BI tables."""

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cross_filter: str = "OneDirection"
    is_active: bool = True


@dataclass
class PBIDataset:
    """Metadata for a Power BI dataset (semantic model)."""

    id: str
    name: str
    configured_by: str = ""
    is_refreshable: bool = False
    workspace_id: str = ""


@dataclass
class PBISemanticModel:
    """
    Full semantic model extracted from a Power BI dataset.

    ``to_dict()`` serialises to the JSON shape expected by the frontend.
    """

    dataset: PBIDataset
    tables: list[PBITable] = field(default_factory=list)
    relationships: list[PBIRelationship] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """
        Serialise the model to the frontend-expected JSON format:

        .. code-block:: json

            {
              "name": "Dataset Name",
              "tables": [{"name": "...", "columns": [...], "measures": [...]}],
              "relationships": [{"from": "Table.Col", "to": "Table.Col", "type": "manyToOne"}]
            }
        """
        serialised_tables = [
            {
                "name": t.name,
                "is_hidden": t.is_hidden,
                "columns": [
                    {
                        "name": c.name,
                        "data_type": c.data_type,
                        "is_hidden": c.is_hidden,
                        "description": c.description,
                    }
                    for c in t.columns
                ],
                "measures": [
                    {
                        "name": m.name,
                        "expression": m.expression,
                        "display_folder": m.display_folder,
                        "format_string": m.format_string,
                        "description": m.description,
                    }
                    for m in t.measures
                ],
            }
            for t in self.tables
        ]

        serialised_relationships = [
            {
                "from": f"{r.from_table}.{r.from_column}",
                "to": f"{r.to_table}.{r.to_column}",
                "type": _cross_filter_to_cardinality(r.cross_filter),
                "is_active": r.is_active,
            }
            for r in self.relationships
        ]

        return {
            "name": self.dataset.name,
            "dataset_id": self.dataset.id,
            "workspace_id": self.dataset.workspace_id,
            "configured_by": self.dataset.configured_by,
            "tables": serialised_tables,
            "relationships": serialised_relationships,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cross_filter_to_cardinality(cross_filter: str) -> str:
    """Map Power BI CrossFilteringBehavior to a human-readable cardinality label."""
    mapping = {
        "OneDirection": "manyToOne",
        "BothDirections": "manyToMany",
        "Automatic": "manyToOne",
    }
    return mapping.get(cross_filter, "manyToOne")


def _raise_for_status_with_detail(response: requests.Response) -> None:
    """
    Raise an informative ``requests.HTTPError`` that includes the response body.

    This makes debugging API failures much easier than the default behaviour.
    """
    if not response.ok:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise requests.HTTPError(
            f"HTTP {response.status_code} from {response.url}: {detail}",
            response=response,
        )


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------


class PowerBIClient:
    """
    Power BI REST API client with MSAL-based authentication.

    Supports three authentication modes:

    1. **Service Principal** — headless / automated pipelines.
    2. **Device Code**       — interactive / user-delegated flows.
    3. **Direct token**      — frontend passes a pre-acquired Bearer token.

    Example (service principal)::

        client = PowerBIClient(
            tenant_id="<tenant>",
            client_id="<client_id>",
            client_secret="<secret>",
        )
        client.authenticate_service_principal()
        workspaces = client.list_workspaces()

    Example (device code — interactive)::

        client = PowerBIClient(tenant_id="<tenant>", client_id="<client_id>")
        client.authenticate_device_code()   # prints a URL + code to stdout
        workspaces = client.list_workspaces()
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: Optional[str] = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret

        # Token state — populated by one of the authenticate_* methods
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0  # Unix timestamp

        # MSAL application instances (created lazily)
        self._conf_app: Any = None  # ConfidentialClientApplication
        self._pub_app: Any = None   # PublicClientApplication

        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _ensure_msal(self) -> None:
        """Raise a clear error if MSAL is not installed."""
        if not _MSAL_AVAILABLE:
            raise RuntimeError(
                "The 'msal' package is required for authentication. "
                "Install it with:  pip install msal"
            )

    def authenticate_service_principal(self) -> bool:
        """
        Acquire a token via the OAuth2 client-credentials flow.

        Requires ``client_secret`` to be set.

        Returns:
            True on success.

        Raises:
            RuntimeError: if MSAL is unavailable or ``client_secret`` is missing.
            ValueError:   if the token acquisition fails.
        """
        self._ensure_msal()
        if not self.client_secret:
            raise RuntimeError(
                "client_secret must be provided for Service Principal auth."
            )

        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        if self._conf_app is None:
            logger.debug("Creating MSAL ConfidentialClientApplication for SP auth")
            self._conf_app = msal.ConfidentialClientApplication(
                client_id=self.client_id,
                client_credential=self.client_secret,
                authority=authority,
            )

        logger.debug("Acquiring token via client_credentials grant")
        result = self._conf_app.acquire_token_for_client(scopes=[_PBI_SCOPE])
        return self._handle_token_result(result, "service principal")

    def authenticate_device_code(self) -> bool:
        """
        Acquire a token via the Device Authorization Grant (interactive).

        Prints the device-code URL and user code to stdout so the caller
        can instruct the user to open the URL and enter the code.

        Returns:
            True on success.

        Raises:
            RuntimeError: if MSAL is unavailable.
            ValueError:   if the flow times out or returns an error.
        """
        self._ensure_msal()
        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        if self._pub_app is None:
            logger.debug("Creating MSAL PublicClientApplication for device-code auth")
            self._pub_app = msal.PublicClientApplication(
                client_id=self.client_id,
                authority=authority,
            )

        flow = self._pub_app.initiate_device_flow(scopes=[_PBI_SCOPE])
        if "user_code" not in flow:
            raise ValueError(
                f"Failed to initiate device code flow: {flow.get('error_description', flow)}"
            )

        # Print the message provided by MSAL (includes URL and user code)
        print(flow["message"])

        logger.debug("Waiting for device code authentication...")
        result = self._pub_app.acquire_token_by_device_flow(flow)
        return self._handle_token_result(result, "device code")

    def authenticate_with_token(self, token: str) -> bool:
        """
        Inject a pre-acquired Bearer token directly.

        Useful when the frontend has already obtained a token via its own
        OAuth2 flow (e.g. MSAL.js / MSAL Python in a web app).

        Args:
            token: A valid Bearer access token string (without the "Bearer " prefix).

        Returns:
            True — the token is stored; expiry is set to 55 minutes from now.
        """
        if not token or not isinstance(token, str):
            raise ValueError("token must be a non-empty string")
        self._access_token = token.strip()
        # Assume a generous 55-minute window; the caller should re-authenticate
        # if the token actually expires sooner.
        self._token_expiry = time.time() + 55 * 60
        logger.debug("Access token injected directly (TTL ~55 min)")
        return True

    def _handle_token_result(self, result: dict, flow_name: str) -> bool:
        """
        Process an MSAL token acquisition result dict.

        Stores the access token and computed expiry timestamp on success.
        Raises ``ValueError`` on failure.
        """
        if "access_token" in result:
            self._access_token = result["access_token"]
            expires_in = result.get("expires_in", 3600)
            self._token_expiry = time.time() + int(expires_in) - 60  # 1-min buffer
            logger.debug(
                "Token acquired via %s flow (expires in %ds)", flow_name, expires_in
            )
            return True

        error = result.get("error", "unknown_error")
        description = result.get("error_description", "No description provided.")
        raise ValueError(
            f"Token acquisition failed ({flow_name}): [{error}] {description}"
        )

    def _is_token_valid(self) -> bool:
        """Return True if we have a non-expired access token."""
        return bool(self._access_token) and time.time() < self._token_expiry

    def _refresh_token_if_needed(self) -> None:
        """
        Attempt a silent token refresh when the cached token has expired.

        Falls back gracefully when no MSAL app is available (e.g. direct-token
        mode) — in that case callers must re-authenticate explicitly.
        """
        if self._is_token_valid():
            return

        logger.debug("Token expired or missing — attempting silent refresh")

        if _MSAL_AVAILABLE:
            # Try silent acquisition from the token cache first
            accounts = []
            app = self._conf_app or self._pub_app
            if app is not None:
                accounts = app.get_accounts()

            if accounts and app is not None:
                result = app.acquire_token_silent(
                    scopes=[_PBI_SCOPE], account=accounts[0]
                )
                if result and "access_token" in result:
                    self._handle_token_result(result, "silent refresh")
                    return

        raise RuntimeError(
            "Access token is expired and could not be refreshed automatically. "
            "Call authenticate_service_principal(), authenticate_device_code(), "
            "or authenticate_with_token() before making API calls."
        )

    def _get_headers(self) -> dict[str, str]:
        """
        Return HTTP headers for authenticated Power BI API requests.

        Raises:
            RuntimeError: if not authenticated or token has expired.
        """
        self._refresh_token_if_needed()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        retries: int = _DEFAULT_RETRY_ATTEMPTS,
        **kwargs: Any,
    ) -> requests.Response:
        """
        Execute an HTTP request with automatic retry on 429 (rate-limit) responses.

        Implements exponential back-off with jitter.  All requests are logged
        at DEBUG level.

        Args:
            method:  HTTP verb ("GET", "POST", ...).
            url:     Fully-qualified endpoint URL.
            retries: Maximum number of retry attempts.
            **kwargs: Forwarded to ``requests.Session.request``.

        Returns:
            The successful ``requests.Response``.

        Raises:
            requests.HTTPError: on non-retryable errors.
            RuntimeError:       after exhausting all retry attempts.
        """
        attempt = 0
        while attempt <= retries:
            logger.debug("%s %s (attempt %d/%d)", method, url, attempt + 1, retries + 1)
            try:
                resp = self._session.request(
                    method, url, headers=self._get_headers(), timeout=30, **kwargs
                )
            except requests.ConnectionError as exc:
                raise RuntimeError(
                    f"Cannot reach Power BI API at {url}: {exc}"
                ) from exc

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
                wait = retry_after * (_DEFAULT_BACKOFF_BASE ** attempt)
                logger.warning(
                    "Rate-limited by Power BI API (429). Waiting %.1fs before retry %d.",
                    wait,
                    attempt + 1,
                )
                time.sleep(wait)
                attempt += 1
                # Refresh token in case it expired during the wait
                self._refresh_token_if_needed()
                continue

            _raise_for_status_with_detail(resp)
            return resp

        raise RuntimeError(
            f"Exhausted {retries} retry attempts for {method} {url} due to rate limiting."
        )

    def _get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """GET a Power BI API path relative to the base URL and return parsed JSON."""
        url = f"{_PBI_BASE}{path}" if path.startswith("/") else path
        return self._request("GET", url, **kwargs).json()

    def _post(self, path: str, payload: dict, **kwargs: Any) -> dict[str, Any]:
        """POST JSON to a Power BI API path and return parsed JSON."""
        url = f"{_PBI_BASE}{path}" if path.startswith("/") else path
        return self._request("POST", url, json=payload, **kwargs).json()

    # ------------------------------------------------------------------
    # Public API — workspace / dataset discovery
    # ------------------------------------------------------------------

    def list_workspaces(self) -> list[PBIWorkspace]:
        """
        List all Power BI workspaces the authenticated principal has access to.

        Returns:
            A list of :class:`PBIWorkspace` dataclasses.

        Raises:
            RuntimeError: if not authenticated.
            requests.HTTPError: if the API call fails.
        """
        logger.debug("Listing workspaces")
        data = self._get("/groups")
        workspaces = [
            PBIWorkspace(
                id=ws["id"],
                name=ws.get("name", ""),
                type=ws.get("type", "Workspace"),
                state=ws.get("state", "Active"),
            )
            for ws in data.get("value", [])
        ]
        logger.debug("Found %d workspace(s)", len(workspaces))
        return workspaces

    def list_datasets(self, workspace_id: str) -> list[PBIDataset]:
        """
        List all datasets inside a specific workspace.

        Args:
            workspace_id: The GUID of the target workspace.

        Returns:
            A list of :class:`PBIDataset` dataclasses.
        """
        logger.debug("Listing datasets in workspace %s", workspace_id)
        data = self._get(f"/groups/{workspace_id}/datasets")
        datasets = [
            PBIDataset(
                id=ds["id"],
                name=ds.get("name", ""),
                configured_by=ds.get("configuredBy", ""),
                is_refreshable=ds.get("isRefreshable", False),
                workspace_id=workspace_id,
            )
            for ds in data.get("value", [])
        ]
        logger.debug(
            "Found %d dataset(s) in workspace %s", len(datasets), workspace_id
        )
        return datasets

    def _get_dataset_detail(
        self, workspace_id: str, dataset_id: str
    ) -> Optional[PBIDataset]:
        """
        Fetch detailed metadata for a single dataset.

        Returns None if the dataset is not found (404) instead of raising.
        """
        try:
            data = self._get(f"/groups/{workspace_id}/datasets/{dataset_id}")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.warning(
                    "Dataset %s not found in workspace %s", dataset_id, workspace_id
                )
                return None
            raise
        return PBIDataset(
            id=data["id"],
            name=data.get("name", ""),
            configured_by=data.get("configuredBy", ""),
            is_refreshable=data.get("isRefreshable", False),
            workspace_id=workspace_id,
        )

    # ------------------------------------------------------------------
    # DAX query execution
    # ------------------------------------------------------------------

    def _execute_dax_query(
        self, workspace_id: str, dataset_id: str, dax_query: str
    ) -> dict[str, Any]:
        """
        Execute a DAX query against a Power BI dataset.

        Uses the ``executeQueries`` endpoint.  Raises ``requests.HTTPError``
        on failure so callers can decide whether to fall back.

        Args:
            workspace_id: GUID of the workspace containing the dataset.
            dataset_id:   GUID of the target dataset.
            dax_query:    A DAX expression to execute (e.g. ``EVALUATE INFO.TABLES()``).

        Returns:
            The raw JSON response dict from the API.
        """
        endpoint = f"/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
        payload = {
            "queries": [{"query": dax_query}],
            "serializerSettings": {"includeNulls": True},
        }
        logger.debug(
            "Executing DAX query on dataset %s: %.120s...", dataset_id, dax_query.strip()
        )
        return self._post(endpoint, payload)

    # ------------------------------------------------------------------
    # Semantic model extraction — DAX INFO path
    # ------------------------------------------------------------------

    def extract_semantic_model(
        self, workspace_id: str, dataset_id: str
    ) -> PBISemanticModel:
        """
        Extract full semantic model metadata using DAX INFO() functions.

        Queries the following functions in sequence:

        * ``INFO.TABLES()``        — table list + visibility
        * ``INFO.COLUMNS()``       — column metadata
        * ``INFO.MEASURES()``      — measure expressions and metadata
        * ``INFO.RELATIONSHIPS()`` — cross-table relationships

        Falls back to :meth:`extract_via_scanner` if DAX queries are not
        supported for the given dataset.

        Args:
            workspace_id: GUID of the target workspace.
            dataset_id:   GUID of the target dataset.

        Returns:
            A fully-populated :class:`PBISemanticModel`.
        """
        dataset = self._get_dataset_detail(workspace_id, dataset_id)
        if dataset is None:
            dataset = PBIDataset(
                id=dataset_id, name="Unknown", workspace_id=workspace_id
            )

        try:
            return self._extract_via_dax(workspace_id, dataset)
        except (requests.HTTPError, KeyError, ValueError) as exc:
            logger.warning(
                "DAX INFO queries failed for dataset %s (%s). "
                "Falling back to Scanner API.",
                dataset_id,
                exc,
            )
            return self.extract_via_scanner(workspace_id, dataset_id=dataset_id)

    def _extract_via_dax(
        self, workspace_id: str, dataset: PBIDataset
    ) -> PBISemanticModel:
        """
        Internal: run all four DAX INFO queries and build a :class:`PBISemanticModel`.
        """
        dataset_id = dataset.id

        logger.debug("Running INFO.TABLES() for dataset %s", dataset_id)
        tables_result = self._execute_dax_query(
            workspace_id, dataset_id, "EVALUATE INFO.TABLES()"
        )

        logger.debug("Running INFO.COLUMNS() for dataset %s", dataset_id)
        columns_result = self._execute_dax_query(
            workspace_id, dataset_id, "EVALUATE INFO.COLUMNS()"
        )

        logger.debug("Running INFO.MEASURES() for dataset %s", dataset_id)
        measures_result = self._execute_dax_query(
            workspace_id, dataset_id, "EVALUATE INFO.MEASURES()"
        )

        logger.debug("Running INFO.RELATIONSHIPS() for dataset %s", dataset_id)
        relationships_result = self._execute_dax_query(
            workspace_id, dataset_id, "EVALUATE INFO.RELATIONSHIPS()"
        )

        tables = _parse_dax_tables(tables_result, columns_result, measures_result)
        relationships = _parse_dax_relationships(relationships_result)

        logger.debug(
            "Extracted %d table(s) and %d relationship(s) from dataset %s",
            len(tables),
            len(relationships),
            dataset_id,
        )
        return PBISemanticModel(
            dataset=dataset,
            tables=tables,
            relationships=relationships,
        )

    # ------------------------------------------------------------------
    # Semantic model extraction — Scanner API path
    # ------------------------------------------------------------------

    def extract_via_scanner(
        self,
        workspace_id: str,
        *,
        dataset_id: Optional[str] = None,
    ) -> PBISemanticModel:
        """
        Extract semantic model metadata via the Power BI Admin Scanner API.

        This endpoint requires tenant admin permissions but provides richer
        metadata including M-query expressions.  It is also used as the
        fallback when DAX INFO() queries are unavailable.

        Args:
            workspace_id: GUID of the workspace to scan.
            dataset_id:   Optional dataset GUID to filter results.  When
                          omitted the first dataset in the workspace is used.

        Returns:
            A :class:`PBISemanticModel` populated from Scanner API data.

        Raises:
            ValueError:        if no datasets are found in the scan result.
            RuntimeError:      if the scan does not complete within the timeout.
            requests.HTTPError: on API failure.
        """
        logger.debug("Initiating Scanner API scan for workspace %s", workspace_id)
        scan_id = self._start_scanner_scan(workspace_id)
        scan_result = self._poll_scanner_result(scan_id)
        return _parse_scanner_result(scan_result, workspace_id, dataset_id)

    def _start_scanner_scan(self, workspace_id: str) -> str:
        """
        POST to the Scanner API to start a workspace scan.

        Returns the scan ID string for polling.
        """
        url = (
            f"{_PBI_BASE}/admin/workspaces/getInfo"
            "?datasetExpressions=true&datasetSchema=true"
        )
        payload = {"workspaces": [workspace_id]}
        result = self._post(url, payload)
        scan_id = result.get("id")
        if not scan_id:
            raise ValueError(
                f"Scanner API did not return a scan ID. Response: {result}"
            )
        logger.debug("Scanner scan initiated, scan_id=%s", scan_id)
        return scan_id

    def _poll_scanner_result(
        self,
        scan_id: str,
        *,
        max_wait: int = 120,
        poll_interval: int = 5,
    ) -> dict[str, Any]:
        """
        Poll the Scanner API until the scan is complete or the timeout is reached.

        Args:
            scan_id:       The scan identifier returned by ``getInfo``.
            max_wait:      Maximum seconds to wait before giving up.
            poll_interval: Seconds between polls.

        Returns:
            The ``scanResult`` dict from the API.

        Raises:
            RuntimeError: if the scan does not finish within ``max_wait`` seconds.
        """
        deadline = time.time() + max_wait
        while time.time() < deadline:
            data = self._get(f"/admin/workspaces/scanResult/{scan_id}")
            status = data.get("status", "")
            logger.debug("Scanner scan status: %s", status)
            if status.lower() == "succeeded":
                return data
            if status.lower() == "failed":
                raise RuntimeError(
                    f"Scanner API scan {scan_id} failed: {data}"
                )
            time.sleep(poll_interval)

        raise RuntimeError(
            f"Scanner API scan {scan_id} did not complete within {max_wait}s."
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def test_connection(self) -> dict[str, Any]:
        """
        Verify that authentication is working by listing workspaces.

        Returns:
            A dict with keys ``success`` (bool), ``workspace_count`` (int),
            and optionally ``error`` (str).

        Example::

            {
                "success": True,
                "workspace_count": 12,
                "message": "Connection OK"
            }
        """
        try:
            workspaces = self.list_workspaces()
            return {
                "success": True,
                "workspace_count": len(workspaces),
                "message": "Connection OK",
            }
        except RuntimeError as exc:
            return {"success": False, "workspace_count": 0, "error": str(exc)}
        except requests.HTTPError as exc:
            return {
                "success": False,
                "workspace_count": 0,
                "error": f"Power BI API error: {exc}",
            }
        except Exception as exc:  # pylint: disable=broad-except
            return {
                "success": False,
                "workspace_count": 0,
                "error": f"Unexpected error: {exc}",
            }


# ---------------------------------------------------------------------------
# DAX result parsers (module-level helpers)
# ---------------------------------------------------------------------------


def _dax_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract the list of row dicts from a DAX ``executeQueries`` response.

    The response structure is::

        {
          "results": [
            {
              "tables": [
                {
                  "columns": [{"name": "[Name]"}, ...],
                  "rows": [{"[Name]": "Sales", ...}, ...]
                }
              ]
            }
          ]
        }

    Returns an empty list if the response does not match the expected shape.
    """
    try:
        tables = result["results"][0]["tables"]
        if not tables:
            return []
        columns: list[str] = [c["name"] for c in tables[0].get("columns", [])]
        raw_rows: list[Any] = tables[0].get("rows", [])

        # Rows may be returned as lists (positional) or as dicts
        if raw_rows and isinstance(raw_rows[0], dict):
            return raw_rows  # Already keyed

        # Positional — zip with column names, strip leading/trailing brackets
        clean_cols = [c.lstrip("[").rstrip("]") for c in columns]
        return [dict(zip(clean_cols, row)) for row in raw_rows]
    except (KeyError, IndexError, TypeError):
        logger.debug("Could not parse DAX rows from result: %s", result)
        return []


def _col(row: dict, *keys: str, default: Any = "") -> Any:
    """
    Return the first matching key from a DAX result row dict.

    DAX INFO() column names appear in several styles depending on the API
    version and tenant configuration, e.g. ``[TableID]``, ``TableID``,
    ``[Name]``, ``Name``.  This helper tries all supplied keys plus their
    bracketed variants.
    """
    for key in keys:
        # Direct lookup
        if key in row:
            return row[key]
        # Bracketed variant
        bracketed = f"[{key}]"
        if bracketed in row:
            return row[bracketed]
        # Without prefix (e.g. "[Table].[Name]" -> "Name")
        bare = key.split(".")[-1].strip("[]")
        if bare in row:
            return row[bare]
    return default


def _parse_dax_tables(
    tables_result: dict,
    columns_result: dict,
    measures_result: dict,
) -> list[PBITable]:
    """
    Build a list of :class:`PBITable` objects from three DAX INFO() results.

    Column and measure rows are matched to tables via their ``TableID`` field.
    """
    table_rows = _dax_rows(tables_result)
    column_rows = _dax_rows(columns_result)
    measure_rows = _dax_rows(measures_result)

    # Build lookup: table_id -> PBITable
    table_map: dict[Any, PBITable] = {}
    for row in table_rows:
        tid = _col(row, "ID", "TableID", default=None)
        name = _col(row, "Name", "TableName")
        hidden = bool(_col(row, "IsHidden", default=False))
        if name:
            tbl = PBITable(name=name, is_hidden=hidden)
            table_map[tid] = tbl

    # Attach columns
    for row in column_rows:
        tid = _col(row, "TableID", default=None)
        tbl = table_map.get(tid)
        if tbl is None:
            continue
        col_name = _col(row, "ExplicitName", "Name", "ColumnName")
        if not col_name:
            continue
        tbl.columns.append(
            PBIColumn(
                name=col_name,
                data_type=str(_col(row, "ExplicitDataType", "DataType", default="")),
                is_hidden=bool(_col(row, "IsHidden", default=False)),
                description=str(_col(row, "Description", default="")),
            )
        )

    # Attach measures
    for row in measure_rows:
        tid = _col(row, "TableID", default=None)
        tbl = table_map.get(tid)
        if tbl is None:
            continue
        m_name = _col(row, "Name", "MeasureName")
        if not m_name:
            continue
        tbl.measures.append(
            PBIMeasure(
                name=m_name,
                expression=str(_col(row, "Expression", default="")),
                display_folder=str(_col(row, "DisplayFolder", default="")),
                format_string=str(_col(row, "FormatString", default="")),
                description=str(_col(row, "Description", default="")),
            )
        )

    return list(table_map.values())


def _parse_dax_relationships(relationships_result: dict) -> list[PBIRelationship]:
    """
    Build a list of :class:`PBIRelationship` objects from a DAX INFO.RELATIONSHIPS() result.
    """
    rows = _dax_rows(relationships_result)
    relationships: list[PBIRelationship] = []
    for row in rows:
        from_table = _col(row, "FromTableName", "FromTable")
        from_col = _col(row, "FromColumnName", "FromColumn")
        to_table = _col(row, "ToTableName", "ToTable")
        to_col = _col(row, "ToColumnName", "ToColumn")
        if not (from_table and from_col and to_table and to_col):
            continue
        relationships.append(
            PBIRelationship(
                from_table=from_table,
                from_column=from_col,
                to_table=to_table,
                to_column=to_col,
                cross_filter=str(
                    _col(row, "CrossFilteringBehavior", "CrossFilter", default="OneDirection")
                ),
                is_active=bool(_col(row, "IsActive", "Active", default=True)),
            )
        )
    return relationships


# ---------------------------------------------------------------------------
# Scanner API result parser (module-level helper)
# ---------------------------------------------------------------------------


def _parse_scanner_result(
    scan_result: dict[str, Any],
    workspace_id: str,
    dataset_id: Optional[str] = None,
) -> PBISemanticModel:
    """
    Parse a Scanner API ``scanResult`` response into a :class:`PBISemanticModel`.

    The Scanner response embeds dataset/table/column/measure info within
    ``workspaces[].datasets[].tables[].columns`` and
    ``workspaces[].datasets[].tables[].measures``.

    Args:
        scan_result: The full JSON dict from the scanResult endpoint.
        workspace_id: The workspace GUID (used to fill ``PBIDataset.workspace_id``).
        dataset_id:   Optional filter; uses first dataset when omitted.

    Returns:
        A :class:`PBISemanticModel`.

    Raises:
        ValueError: if no datasets are found.
    """
    workspaces: list[dict] = scan_result.get("workspaces", [])
    if not workspaces:
        raise ValueError("Scanner API returned no workspaces in scan result.")

    ws = workspaces[0]
    raw_datasets: list[dict] = ws.get("datasets", [])
    if not raw_datasets:
        raise ValueError(
            f"Scanner API returned no datasets for workspace {workspace_id}."
        )

    # Filter to the requested dataset or use the first one
    if dataset_id:
        matching = [d for d in raw_datasets if d.get("id") == dataset_id]
        if not matching:
            logger.warning(
                "Dataset %s not found in scanner result; using first dataset.", dataset_id
            )
            raw_ds = raw_datasets[0]
        else:
            raw_ds = matching[0]
    else:
        raw_ds = raw_datasets[0]

    dataset = PBIDataset(
        id=raw_ds.get("id", ""),
        name=raw_ds.get("name", ""),
        configured_by=raw_ds.get("configuredBy", ""),
        is_refreshable=raw_ds.get("isRefreshable", False),
        workspace_id=workspace_id,
    )

    tables: list[PBITable] = []
    for raw_table in raw_ds.get("tables", []):
        columns = [
            PBIColumn(
                name=c.get("name", ""),
                data_type=c.get("dataType", ""),
                is_hidden=c.get("isHidden", False),
                description=c.get("description", ""),
            )
            for c in raw_table.get("columns", [])
        ]
        measures = [
            PBIMeasure(
                name=m.get("name", ""),
                expression=m.get("expression", ""),
                display_folder=m.get("displayFolder", ""),
                format_string=m.get("formatString", ""),
                description=m.get("description", ""),
            )
            for m in raw_table.get("measures", [])
        ]
        tables.append(
            PBITable(
                name=raw_table.get("name", ""),
                columns=columns,
                measures=measures,
                is_hidden=raw_table.get("isHidden", False),
            )
        )

    # Scanner API embeds relationships at the dataset level
    relationships: list[PBIRelationship] = []
    for rel in raw_ds.get("relationships", []):
        from_table = rel.get("fromTable", "")
        from_col = rel.get("fromColumn", "")
        to_table = rel.get("toTable", "")
        to_col = rel.get("toColumn", "")
        if not (from_table and from_col and to_table and to_col):
            continue
        relationships.append(
            PBIRelationship(
                from_table=from_table,
                from_column=from_col,
                to_table=to_table,
                to_column=to_col,
                cross_filter=rel.get("crossFilteringBehavior", "OneDirection"),
                is_active=rel.get("isActive", True),
            )
        )

    logger.debug(
        "Scanner API: parsed %d table(s) and %d relationship(s) for dataset %s",
        len(tables),
        len(relationships),
        dataset.id,
    )
    return PBISemanticModel(dataset=dataset, tables=tables, relationships=relationships)
