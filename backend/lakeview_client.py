"""
Databricks Lakeview Dashboard API Client

Wraps the /api/2.0/lakeview/* endpoints and delegates HTTP calls
to an existing DatabricksClient instance.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .dbx_client import DatabricksAPIError, DatabricksAuthError  # re-export for callers

logger = logging.getLogger(__name__)

_BASE = "/api/2.0/lakeview/dashboards"


@dataclass
class LakeviewDashboardInfo:
    dashboard_id: str
    display_name: str
    path: str = ""
    warehouse_id: str = ""
    create_time: str = ""
    update_time: str = ""
    lifecycle_state: str = ""
    published_url: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _parse_dashboard(data: dict) -> LakeviewDashboardInfo:
    """Convert a raw API response dict into a LakeviewDashboardInfo."""
    return LakeviewDashboardInfo(
        dashboard_id=data.get("dashboard_id", ""),
        display_name=data.get("display_name", ""),
        path=data.get("path", ""),
        warehouse_id=data.get("warehouse_id", ""),
        create_time=data.get("create_time", ""),
        update_time=data.get("update_time", ""),
        lifecycle_state=data.get("lifecycle_state", ""),
        published_url=data.get("published_url", ""),
    )


class LakeviewClient:
    """Client for Databricks Lakeview Dashboard REST API (api/2.0/lakeview)."""

    def __init__(self, dbx_client):
        """
        Parameters
        ----------
        dbx_client : DatabricksClient
            An authenticated DatabricksClient instance.  All HTTP calls are
            delegated to its ``_request`` method so auth headers, retries, and
            error handling are inherited automatically.
        """
        self._client = dbx_client

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs):
        """Thin wrapper that forwards to the underlying DatabricksClient."""
        return self._client._request(method, path, **kwargs)

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def create_dashboard(
        self,
        display_name: str,
        serialized_dashboard: str,
        warehouse_id: str,
        parent_path: str = "",
    ) -> LakeviewDashboardInfo:
        """
        Create a new Lakeview dashboard.

        Parameters
        ----------
        display_name : str
            Human-readable name for the dashboard.
        serialized_dashboard : str
            JSON-encoded dashboard definition string.
        warehouse_id : str
            SQL warehouse to attach to the dashboard.
        parent_path : str, optional
            Workspace path for the parent folder.

        Returns
        -------
        LakeviewDashboardInfo

        Raises
        ------
        DatabricksAPIError
            409 if a dashboard already exists at the resolved path.
        """
        payload: Dict[str, Any] = {
            "display_name": display_name,
            "serialized_dashboard": serialized_dashboard,
            "warehouse_id": warehouse_id,
        }
        if parent_path:
            payload["parent_path"] = parent_path

        try:
            data = self._request("POST", _BASE, json=payload)
        except DatabricksAPIError as exc:
            if exc.status_code == 409:
                raise DatabricksAPIError(
                    f"Dashboard '{display_name}' already exists at path '{parent_path}': {exc}",
                    status_code=409,
                ) from exc
            raise

        logger.info("Created Lakeview dashboard '%s' (id=%s)", display_name, data.get("dashboard_id"))
        return _parse_dashboard(data)

    def get_dashboard(self, dashboard_id: str) -> LakeviewDashboardInfo:
        """
        Retrieve a dashboard by ID.

        Raises
        ------
        DatabricksAPIError
            404 if the dashboard does not exist.
        """
        try:
            data = self._request("GET", f"{_BASE}/{dashboard_id}")
        except DatabricksAPIError as exc:
            if exc.status_code == 404:
                raise DatabricksAPIError(
                    f"Dashboard '{dashboard_id}' not found.",
                    status_code=404,
                ) from exc
            raise

        return _parse_dashboard(data)

    def update_dashboard(
        self,
        dashboard_id: str,
        display_name: str = None,
        serialized_dashboard: str = None,
    ) -> LakeviewDashboardInfo:
        """
        Update an existing dashboard (PATCH).

        Only fields that are explicitly provided are sent; ``None`` values are
        omitted so the API leaves those fields unchanged.

        Raises
        ------
        DatabricksAPIError
            404 if the dashboard does not exist.
        """
        payload: Dict[str, Any] = {}
        if display_name is not None:
            payload["display_name"] = display_name
        if serialized_dashboard is not None:
            payload["serialized_dashboard"] = serialized_dashboard

        if not payload:
            logger.warning("update_dashboard called with no fields to update; fetching current state.")
            return self.get_dashboard(dashboard_id)

        try:
            data = self._request("PATCH", f"{_BASE}/{dashboard_id}", json=payload)
        except DatabricksAPIError as exc:
            if exc.status_code == 404:
                raise DatabricksAPIError(
                    f"Dashboard '{dashboard_id}' not found.",
                    status_code=404,
                ) from exc
            raise

        logger.info("Updated Lakeview dashboard id=%s", dashboard_id)
        return _parse_dashboard(data)

    def delete_dashboard(self, dashboard_id: str) -> None:
        """
        Delete a dashboard by ID.

        Raises
        ------
        DatabricksAPIError
            404 if the dashboard does not exist.
        """
        try:
            self._request("DELETE", f"{_BASE}/{dashboard_id}")
        except DatabricksAPIError as exc:
            if exc.status_code == 404:
                raise DatabricksAPIError(
                    f"Dashboard '{dashboard_id}' not found.",
                    status_code=404,
                ) from exc
            raise

        logger.info("Deleted Lakeview dashboard id=%s", dashboard_id)

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_dashboard(self, dashboard_id: str, warehouse_id: str = "") -> dict:
        """
        Publish a dashboard so it is accessible via a public URL.

        Parameters
        ----------
        dashboard_id : str
        warehouse_id : str, optional
            Override the warehouse used for published queries.  If omitted the
            dashboard's attached warehouse is used.

        Returns
        -------
        dict
            Raw API response (may include ``published_url`` and related fields).
        """
        payload: Dict[str, Any] = {}
        if warehouse_id:
            payload["warehouse_id"] = warehouse_id

        data = self._request("POST", f"{_BASE}/{dashboard_id}/published", json=payload)
        logger.info("Published Lakeview dashboard id=%s", dashboard_id)
        return data

    def unpublish_dashboard(self, dashboard_id: str) -> None:
        """Remove the published version of a dashboard."""
        self._request("DELETE", f"{_BASE}/{dashboard_id}/published")
        logger.info("Unpublished Lakeview dashboard id=%s", dashboard_id)

    # ------------------------------------------------------------------
    # Listing / discovery
    # ------------------------------------------------------------------

    def list_dashboards(self, page_size: int = 100) -> List[LakeviewDashboardInfo]:
        """
        Return all dashboards visible to the authenticated user.

        Handles pagination automatically.

        Parameters
        ----------
        page_size : int
            Number of items to fetch per page (default 100).

        Returns
        -------
        List[LakeviewDashboardInfo]
        """
        results: List[LakeviewDashboardInfo] = []
        page_token: Optional[str] = None

        while True:
            params: Dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token

            data = self._request("GET", _BASE, params=params)
            for item in data.get("dashboards", []):
                results.append(_parse_dashboard(item))

            page_token = data.get("next_page_token")
            if not page_token:
                break

        return results

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def get_published_url(self, dashboard_id: str) -> str:
        """
        Construct the canonical published URL for a dashboard.

        Returns
        -------
        str
            URL of the form ``{host}/dashboardsv3/{dashboard_id}/published``.
        """
        host = self._client.host.rstrip("/")
        return f"{host}/dashboardsv3/{dashboard_id}/published"

    def deploy_dashboard(
        self,
        display_name: str,
        serialized_dashboard: str,
        warehouse_id: str,
        parent_path: str = "",
        publish: bool = True,
    ) -> LakeviewDashboardInfo:
        """
        Create a dashboard and, optionally, publish it in a single call.

        Parameters
        ----------
        display_name : str
        serialized_dashboard : str
        warehouse_id : str
        parent_path : str, optional
        publish : bool
            If ``True`` (default), the dashboard is published immediately after
            creation.

        Returns
        -------
        LakeviewDashboardInfo
            The dashboard info after creation (and publication if requested).
            ``published_url`` is populated when ``publish=True``.
        """
        info = self.create_dashboard(
            display_name=display_name,
            serialized_dashboard=serialized_dashboard,
            warehouse_id=warehouse_id,
            parent_path=parent_path,
        )

        if publish:
            try:
                self.publish_dashboard(info.dashboard_id, warehouse_id=warehouse_id)
                info.published_url = self.get_published_url(info.dashboard_id)
                logger.info(
                    "Dashboard '%s' deployed and published: %s",
                    display_name,
                    info.published_url,
                )
            except DatabricksAPIError as exc:
                logger.warning(
                    "Dashboard '%s' created (id=%s) but publish failed: %s",
                    display_name,
                    info.dashboard_id,
                    exc,
                )

        return info
