"""
report_parser.py
================
Extracts Power BI report layout information — pages, visuals, chart types,
field bindings, and positions — from both the REST API responses and the
internal ``Report/Layout`` JSON embedded in a ``.pbix`` ZIP archive.

Public API
----------
    parser = ReportParser()

    # From Power BI REST API responses
    pages   = parser.parse_report_pages(pages_json)
    visuals = parser.parse_page_visuals(visuals_json)

    # From a local .pbix file
    report  = parser.parse_pbix_file("/path/to/report.pbix")
"""

from __future__ import annotations

import json
import logging
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReportParserError(Exception):
    """Raised when the report layout cannot be parsed."""


# ---------------------------------------------------------------------------
# Visual-type constants
# ---------------------------------------------------------------------------

PBI_VISUAL_TYPE_MAP: Dict[str, str] = {
    "barChart": "bar",
    "clusteredBarChart": "bar",
    "clusteredColumnChart": "bar",
    "columnChart": "bar",
    "lineChart": "line",
    "areaChart": "area",
    "stackedAreaChart": "area",
    "pieChart": "pie",
    "donutChart": "pie",
    "card": "counter",
    "multiRowCard": "counter",
    "kpiVisual": "counter",
    "tableEx": "table",
    "pivotTable": "table",
    "matrix": "table",
    "scatterChart": "scatter",
}

UNSUPPORTED_VISUALS: frozenset = frozenset(
    {
        "mapVisual",
        "filledMap",
        "azureMap",
        "gauge",
        "funnel",
        "ribbonChart",
        "waterfallChart",
        "treemap",
        "textbox",
        "image",
        "shape",
        "actionButton",
        "slicerVisual",
    }
)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PBIVisual:
    """A single visual container on a Power BI report page."""

    visual_id: str
    visual_type: str  # barChart, lineChart, card, matrix, slicer, etc.
    title: str = ""
    measures: List[str] = field(default_factory=list)
    dimensions: List[str] = field(default_factory=list)
    filters: List[dict] = field(default_factory=list)
    position: Dict[str, int] = field(default_factory=dict)  # {x, y, width, height}

    def to_dict(self) -> dict:
        return {
            "visual_id": self.visual_id,
            "visual_type": self.visual_type,
            "title": self.title,
            "measures": self.measures,
            "dimensions": self.dimensions,
            "filters": self.filters,
            "position": self.position,
        }


@dataclass
class PBIReportPage:
    """A page inside a Power BI report."""

    name: str           # internal name, e.g. "ReportSection1a2b3c"
    display_name: str   # human-readable label shown in the report tabs
    order: int = 0
    visuals: List[PBIVisual] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "order": self.order,
            "visuals": [v.to_dict() for v in self.visuals],
        }


@dataclass
class PBIReport:
    """Top-level representation of a Power BI report."""

    id: str
    name: str
    dataset_id: str = ""
    pages: List[PBIReportPage] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "dataset_id": self.dataset_id,
            "pages": [p.to_dict() for p in self.pages],
        }


# ---------------------------------------------------------------------------
# ReportParser
# ---------------------------------------------------------------------------


class ReportParser:
    """Parses Power BI report metadata from REST API payloads and .pbix files."""

    # Default canvas width used by Power BI Desktop (in pixels / PBI units)
    _DEFAULT_CANVAS_WIDTH: int = 1280
    # Lakeview grid columns
    _GRID_COLUMNS: int = 6

    # ------------------------------------------------------------------
    # REST API helpers
    # ------------------------------------------------------------------

    def parse_report_pages(self, pages_json: list) -> List[PBIReportPage]:
        """Parse the ``/reports/{id}/pages`` REST API response.

        Expected structure (each element)::

            {
                "name": "ReportSection1a2b",
                "displayName": "Sales Overview",
                "order": 0
            }
        """
        pages: List[PBIReportPage] = []
        for raw in pages_json:
            try:
                page = PBIReportPage(
                    name=raw.get("name", ""),
                    display_name=raw.get("displayName", raw.get("name", "")),
                    order=int(raw.get("order", 0)),
                )
                pages.append(page)
                logger.debug("Parsed page: %s (%s)", page.display_name, page.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping malformed page entry: %s — %s", raw, exc)

        # Sort by the reported order field so callers get a stable sequence
        pages.sort(key=lambda p: p.order)
        return pages

    def parse_page_visuals(self, visuals_json: list) -> List[PBIVisual]:
        """Parse the ``/reports/{id}/pages/{name}/visuals`` REST API response.

        Expected structure (each element)::

            {
                "id": "abc123",
                "visualType": "barChart",
                "title": "Revenue by Region",
                "x": 0, "y": 0, "width": 640, "height": 480
            }
        """
        visuals: List[PBIVisual] = []
        for raw in visuals_json:
            try:
                visual_id = raw.get("id", "")
                visual_type = raw.get("visualType", "unknown")
                title = raw.get("title", "")
                position = self._extract_rest_position(raw)
                visual = PBIVisual(
                    visual_id=visual_id,
                    visual_type=visual_type,
                    title=title,
                    position=position,
                )
                visuals.append(visual)
                logger.debug(
                    "Parsed visual %s (type=%s)", visual_id, visual_type
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Skipping malformed visual entry: %s — %s", raw, exc
                )
        return visuals

    # ------------------------------------------------------------------
    # .pbix layout helpers
    # ------------------------------------------------------------------

    def parse_pbix_layout(self, layout_json: dict) -> List[PBIReportPage]:
        """Parse the ``Report/Layout`` JSON extracted from inside a ``.pbix`` ZIP.

        The top-level object contains a ``sections`` array.  Each section
        represents one report page and carries a ``visualContainers`` array
        whose individual entries have:

        * ``config``   — JSON-stringified object; ``singleVisual.visualType``
                         and ``singleVisual.projectionOrdering`` live here
        * ``x``, ``y``, ``width``, ``height`` — position in PBI units
        * ``filters``  — JSON-stringified array of filter objects
        """
        sections = layout_json.get("sections", [])
        if not sections:
            logger.warning("Layout JSON contains no sections")
            return []

        pages: List[PBIReportPage] = []
        for idx, section in enumerate(sections):
            try:
                page = self._parse_section(section, idx)
                pages.append(page)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Error parsing section %d (%s): %s",
                    idx,
                    section.get("name", "?"),
                    exc,
                )

        pages.sort(key=lambda p: p.order)
        return pages

    def parse_pbix_file(self, file_path: str) -> PBIReport:
        """Open a ``.pbix`` ZIP archive, locate ``Report/Layout``, and parse it.

        Returns a :class:`PBIReport` whose ``id`` and ``name`` are derived
        from the filename (since the .pbix format does not embed a canonical
        report ID).
        """
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                layout_entry = self._find_layout_entry(zf)
                raw_bytes = zf.read(layout_entry)
        except zipfile.BadZipFile as exc:
            raise ReportParserError(
                f"File is not a valid .pbix ZIP archive: {file_path}"
            ) from exc
        except KeyError as exc:
            raise ReportParserError(
                f"Report/Layout not found inside {file_path}"
            ) from exc

        # The Layout file is UTF-16 LE (with or without BOM)
        try:
            layout_text = raw_bytes.decode("utf-16-le").lstrip("\ufeff")
        except UnicodeDecodeError:
            # Fall back to UTF-8 for custom/exported variants
            layout_text = raw_bytes.decode("utf-8", errors="replace")

        try:
            layout_json = json.loads(layout_text)
        except json.JSONDecodeError as exc:
            raise ReportParserError(
                f"Failed to decode Report/Layout JSON from {file_path}: {exc}"
            ) from exc

        import os

        base_name = os.path.splitext(os.path.basename(file_path))[0]
        pages = self.parse_pbix_layout(layout_json)

        report = PBIReport(
            id=base_name,
            name=layout_json.get("reportConfig", {}).get("name", base_name),
            dataset_id=layout_json.get("datasetId", ""),
            pages=pages,
        )
        logger.info(
            "Parsed .pbix '%s': %d page(s)", report.name, len(report.pages)
        )
        return report

    # ------------------------------------------------------------------
    # Field-binding extraction
    # ------------------------------------------------------------------

    def extract_field_bindings(
        self, visual_config: dict
    ) -> Tuple[List[str], List[str]]:
        """Extract measure and dimension field names from a PBI visual config.

        ``visual_config`` is the parsed JSON object found at
        ``singleVisual`` inside each ``visualContainer.config``.

        Returns a ``(measures, dimensions)`` tuple where both lists contain
        plain field name strings (table-qualified names are split to the
        column/measure portion only).

        Strategy
        --------
        1. Inspect ``projectionOrdering`` to understand which roles are
           populated and in what order.
        2. Walk ``prototypeQuery.Select`` (when present) to get the actual
           field references keyed by ``Name``.
        3. Apply data-role heuristics (``Values``/``Y``/``Value`` → measures;
           everything else → dimensions) to classify fields.
        """
        measures: List[str] = []
        dimensions: List[str] = []

        projection_ordering: Dict[str, Any] = visual_config.get(
            "projectionOrdering", {}
        )
        # Build role → [field_names] map from the query select list
        query_select: List[dict] = (
            visual_config.get("prototypeQuery", {}).get("Select", [])
        )

        # Map each select entry by its projected name
        select_by_name: Dict[str, dict] = {}
        for sel in query_select:
            name = sel.get("Name", "")
            if name:
                select_by_name[name] = sel

        measure_roles = {"Values", "Value", "Y", "Y Axis", "Tooltips"}

        for role, ordered_names in projection_ordering.items():
            if not isinstance(ordered_names, list):
                continue
            is_measure_role = role in measure_roles
            for entry in ordered_names:
                # entry is either a string name or a dict with "queryRef"
                if isinstance(entry, str):
                    ref = entry
                elif isinstance(entry, dict):
                    ref = entry.get("queryRef", entry.get("Name", ""))
                else:
                    continue

                # Strip table prefix: "Table.Column" → "Column"
                field_name = ref.split(".")[-1] if "." in ref else ref

                if not field_name:
                    continue

                # Determine measure vs dimension via role heuristic, then
                # fall back to whether the select entry is an aggregation
                if is_measure_role:
                    measures.append(field_name)
                else:
                    # Check the select entry for an Aggregation property
                    sel_entry = select_by_name.get(ref, {})
                    if "Aggregation" in sel_entry or "Measure" in sel_entry:
                        measures.append(field_name)
                    else:
                        dimensions.append(field_name)

        # Deduplicate while preserving order
        measures = _dedup(measures)
        dimensions = _dedup(dimensions)

        logger.debug(
            "Field bindings — measures: %s, dimensions: %s", measures, dimensions
        )
        return measures, dimensions

    # ------------------------------------------------------------------
    # Position normalisation
    # ------------------------------------------------------------------

    def normalize_position(
        self,
        pbi_x: int,
        pbi_y: int,
        pbi_width: int,
        pbi_height: int,
        canvas_width: int = 1280,
    ) -> Dict[str, int]:
        """Convert PBI pixel coordinates to Lakeview 6-column grid positions.

        Power BI Desktop uses a canvas that is typically 1280 px wide.
        Lakeview (Databricks AI/BI Dashboards) uses a 6-column grid.

        Mapping rules
        -------------
        * ``x``     : ``floor(pbi_x / col_px)``  clamped to [0, 5]
        * ``width``  : ``max(1, round(pbi_width / col_px))`` clamped to [1, 6]
        * ``y``     : ``pbi_y`` passed through (Lakeview rows are not fixed-height)
        * ``height`` : ``pbi_height`` passed through
        """
        if canvas_width <= 0:
            canvas_width = self._DEFAULT_CANVAS_WIDTH

        col_px: float = canvas_width / self._GRID_COLUMNS  # pixels per column

        grid_x = max(0, min(5, int(pbi_x / col_px)))
        grid_width = max(1, min(6, round(pbi_width / col_px)))

        # Clamp so the visual doesn't overflow the grid
        if grid_x + grid_width > self._GRID_COLUMNS:
            grid_width = self._GRID_COLUMNS - grid_x

        return {
            "x": grid_x,
            "y": pbi_y,
            "width": grid_width,
            "height": pbi_height,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_layout_entry(self, zf: zipfile.ZipFile) -> str:
        """Return the ZIP entry name for ``Report/Layout`` (case-insensitive)."""
        for name in zf.namelist():
            if name.replace("\\", "/").lower() == "report/layout":
                return name
        raise KeyError("Report/Layout not found in ZIP")

    def _parse_section(self, section: dict, fallback_order: int) -> PBIReportPage:
        """Parse a single section dict from the pbix Layout JSON."""
        page = PBIReportPage(
            name=section.get("name", f"section_{fallback_order}"),
            display_name=section.get("displayName", section.get("name", "")),
            order=int(section.get("ordinal", fallback_order)),
        )

        canvas_settings = section.get("visualContainerLayoutJSON", {})
        if isinstance(canvas_settings, str):
            try:
                canvas_settings = json.loads(canvas_settings)
            except json.JSONDecodeError:
                canvas_settings = {}

        canvas_width: int = (
            canvas_settings.get("width", self._DEFAULT_CANVAS_WIDTH)
            or self._DEFAULT_CANVAS_WIDTH
        )

        for vc in section.get("visualContainers", []):
            visual = self._parse_visual_container(vc, canvas_width)
            if visual is not None:
                page.visuals.append(visual)

        logger.debug(
            "Parsed section '%s': %d visual(s)", page.display_name, len(page.visuals)
        )
        return page

    def _parse_visual_container(
        self, vc: dict, canvas_width: int
    ) -> Optional[PBIVisual]:
        """Parse a single visualContainer dict from a pbix section."""
        # config is a JSON-stringified object
        config_raw = vc.get("config", "{}")
        if isinstance(config_raw, str):
            try:
                config = json.loads(config_raw)
            except json.JSONDecodeError:
                logger.debug("Could not parse visual config JSON; skipping")
                return None
        else:
            config = config_raw if isinstance(config_raw, dict) else {}

        single_visual: dict = config.get("singleVisual", {})
        visual_type: str = single_visual.get("visualType", "unknown")

        # Skip layout-only placeholders and unsupported types
        if visual_type in ("", "unknown") and not single_visual:
            return None

        visual_id: str = config.get("name", vc.get("id", ""))
        if not visual_id:
            visual_id = f"visual_{id(vc)}"

        # Title from vcObjects → title → properties → text → expr → Literal
        title = self._extract_title(single_visual)

        # Position
        pbi_x = int(vc.get("x", 0))
        pbi_y = int(vc.get("y", 0))
        pbi_width = int(vc.get("width", 0))
        pbi_height = int(vc.get("height", 0))
        position = self.normalize_position(
            pbi_x, pbi_y, pbi_width, pbi_height, canvas_width
        )

        # Filters (JSON-stringified array)
        filters_raw = vc.get("filters", "[]")
        if isinstance(filters_raw, str):
            try:
                filters: List[dict] = json.loads(filters_raw)
            except json.JSONDecodeError:
                filters = []
        elif isinstance(filters_raw, list):
            filters = filters_raw
        else:
            filters = []

        # Field bindings
        measures, dimensions = self.extract_field_bindings(single_visual)

        visual = PBIVisual(
            visual_id=visual_id,
            visual_type=visual_type,
            title=title,
            measures=measures,
            dimensions=dimensions,
            filters=filters,
            position=position,
        )

        if visual_type in UNSUPPORTED_VISUALS:
            logger.debug("Visual %s is of unsupported type '%s'", visual_id, visual_type)

        return visual

    def _extract_title(self, single_visual: dict) -> str:
        """Extract the display title from a singleVisual config object."""
        try:
            vc_objects: dict = single_visual.get("vcObjects", {})
            title_obj: dict = vc_objects.get("title", [{}])[0]
            properties: dict = title_obj.get("properties", {})
            text_obj: dict = properties.get("text", {})
            expr_obj: dict = text_obj.get("expr", {})
            literal: dict = expr_obj.get("Literal", {})
            raw_val: str = literal.get("Value", "")
            # Power BI wraps string literals in single quotes
            return raw_val.strip("'")
        except (KeyError, IndexError, TypeError):
            return ""

    @staticmethod
    def _extract_rest_position(raw: dict) -> Dict[str, int]:
        """Build a position dict from a flat REST API visual entry."""
        return {
            "x": int(raw.get("x", 0)),
            "y": int(raw.get("y", 0)),
            "width": int(raw.get("width", 0)),
            "height": int(raw.get("height", 0)),
        }


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _dedup(seq: List[str]) -> List[str]:
    """Return a list with duplicates removed, preserving insertion order."""
    seen: set = set()
    result: List[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
