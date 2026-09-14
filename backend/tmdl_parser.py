"""
tmdl_parser.py
==============
Parser for TMDL (Tabular Model Definition Language) -- the text-based format
Power BI uses to export semantic models.

Supports:
  - Directory layouts: ``tables/`` subdirectory, flat directory, or single-file
  - ZIP archives containing a TMDL export
  - Multi-line DAX expressions (arbitrary nesting depth)
  - Quoted identifiers (single-quoted table/column names)
  - isHidden flag (bare keyword, no value)
  - Graceful handling of unknown properties and malformed content

Public API
----------
    parser = TMDLParser()
    model  = parser.parse("/path/to/tmdl_dir_or_zip")
    model  = parser.parse_string(tmdl_content_string)
    data   = model.to_dict()      # JSON-serialisable dict for the frontend
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TMDLColumn:
    """Represents a single column inside a TMDL table block."""

    name: str
    data_type: Optional[str] = None
    source_column: Optional[str] = None
    is_hidden: bool = False
    format_string: Optional[str] = None
    summarize_by: Optional[str] = None
    description: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"name": self.name}
        if self.data_type is not None:
            d["dataType"] = self.data_type
        if self.source_column is not None:
            d["sourceColumn"] = self.source_column
        if self.is_hidden:
            d["isHidden"] = True
        if self.format_string is not None:
            d["formatString"] = self.format_string
        if self.summarize_by is not None:
            d["summarizeBy"] = self.summarize_by
        if self.description is not None:
            d["description"] = self.description
        return d


@dataclass
class TMDLMeasure:
    """Represents a DAX measure inside a TMDL table block."""

    name: str
    expression: str
    display_folder: Optional[str] = None
    format_string: Optional[str] = None
    description: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"name": self.name, "expression": self.expression}
        if self.display_folder is not None:
            d["displayFolder"] = self.display_folder
        if self.format_string is not None:
            d["formatString"] = self.format_string
        if self.description is not None:
            d["description"] = self.description
        return d


@dataclass
class TMDLTable:
    """Represents a TMDL table block with its columns and measures."""

    name: str
    columns: List[TMDLColumn] = field(default_factory=list)
    measures: List[TMDLMeasure] = field(default_factory=list)
    lineage_tag: Optional[str] = None
    is_hidden: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lineageTag": self.lineage_tag,
            "isHidden": self.is_hidden,
            "columns": [c.to_dict() for c in self.columns],
            "measures": [m.to_dict() for m in self.measures],
        }


@dataclass
class TMDLRelationship:
    """Represents a relationship between two table columns."""

    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    is_active: bool = True
    cross_filtering: Optional[str] = None

    @property
    def relationship_type(self) -> str:
        """Infer a human-readable cardinality label (always manyToOne for PBI defaults)."""
        return "manyToOne"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "from": f"{self.from_table}.{self.from_column}",
            "to": f"{self.to_table}.{self.to_column}",
            "fromTable": self.from_table,
            "fromColumn": self.from_column,
            "toTable": self.to_table,
            "toColumn": self.to_column,
            "isActive": self.is_active,
            "crossFiltering": self.cross_filtering,
            "type": self.relationship_type,
        }


@dataclass
class TMDLRole:
    """A security role with its model permission and row-level filters.

    ``table_permissions`` maps a table name to its DAX row-filter predicate;
    these encode row-level security (RLS) that Metric Views cannot express and
    must be reproduced as Unity Catalog row filters.
    """

    name: str
    model_permission: Optional[str] = None
    table_permissions: Dict[str, str] = field(default_factory=dict)
    members: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "modelPermission": self.model_permission,
            "tablePermissions": dict(self.table_permissions),
            "members": list(self.members),
        }


@dataclass
class SemanticModel:
    """Top-level container for a parsed TMDL semantic model."""

    name: str
    tables: List[TMDLTable] = field(default_factory=list)
    relationships: List[TMDLRelationship] = field(default_factory=list)
    roles: List[TMDLRole] = field(default_factory=list)
    culture: Optional[str] = None

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict in the format the frontend expects."""
        return {
            "name": self.name,
            "culture": self.culture,
            "tables": [t.to_dict() for t in self.tables],
            "relationships": [r.to_dict() for r in self.relationships],
            "roles": [r.to_dict() for r in self.roles],
        }

    def to_json(self, **kwargs) -> str:
        """Convenience wrapper that returns a JSON string."""
        return json.dumps(self.to_dict(), **kwargs)


# ---------------------------------------------------------------------------
# Low-level parsing helpers
# ---------------------------------------------------------------------------

# Matches a simple ``key: value`` property line (value may be quoted or bare)
_PROP_RE = re.compile(r"^(?P<key>\w+):\s*(?P<value>.+)$")

# Matches a measure definition inside a table block:
#   measure 'Total Revenue' = SUM(FactSales[SalesAmount])
#   measure 'YTD Revenue' =          <-- expr on next line(s)
_MEASURE_DEF_RE = re.compile(
    r"^measure\s+(?P<name>'[^']+'|\S+)\s*=\s*(?P<expr>.*)$"
)

# Matches a bare column definition:
#   column SalesAmount
#   column 'Order Date'
_COLUMN_DEF_RE = re.compile(
    r"^column\s+(?P<name>'[^']+'|\S+)$"
)

# Relationship column reference: ``FactSales.ProductKey`` or ``'Dim Date'.DateKey``
_REL_COL_RE = re.compile(
    r"^(?P<table>'[^']+'|[^.\s]+)\.(?P<column>'[^']+'|[^.\s]+)$"
)

# Known measure property keys that appear at measure_depth+1 and are NOT DAX
_MEASURE_PROP_KEYS: FrozenSet[str] = frozenset({
    "displayFolder", "formatString", "description", "lineageTag",
    "isHidden", "kpi", "detailRowsExpression", "dataCategory",
    "annotation", "changedProperty", "dataType",
})


def _strip_quotes(s: str) -> str:
    """Remove surrounding single quotes from a TMDL identifier if present."""
    s = s.strip()
    if s.startswith("'") and s.endswith("'") and len(s) >= 2:
        return s[1:-1]
    return s


def _parse_col_ref(ref: str) -> Tuple[str, str]:
    """
    Parse a ``Table.Column`` column reference, handling single-quoted names.

    Returns ``(table_name, column_name)``.
    Raises ``ValueError`` if the reference cannot be split on a dot.
    """
    ref = ref.strip()
    m = _REL_COL_RE.match(ref)
    if m:
        return _strip_quotes(m.group("table")), _strip_quotes(m.group("column"))
    # Fallback: split on the *last* dot (handles quoted names containing dots)
    dot_idx = ref.rfind(".")
    if dot_idx == -1:
        raise ValueError(f"Cannot parse column reference: {ref!r}")
    return _strip_quotes(ref[:dot_idx]), _strip_quotes(ref[dot_idx + 1:])


# ---------------------------------------------------------------------------
# Line tokeniser
# ---------------------------------------------------------------------------

@dataclass
class _Line:
    """A single logical TMDL line with its indentation depth (tab count)."""

    depth: int   # number of leading tabs
    text: str    # content after stripping leading tabs and trailing whitespace


def _tokenise(content: str) -> List[_Line]:
    """
    Convert raw TMDL text into a list of :class:`_Line` objects.

    Processing rules:
    - Blank lines and comment-only lines (starting with ``//``) are dropped.
    - Leading indentation is measured in tab characters.
    - Groups of 4 spaces are normalised to a single tab (some PBI exports
      use space indentation instead of tabs).
    """
    lines: List[_Line] = []
    for raw in content.splitlines():
        # Normalise 4-space groups to a single tab before measuring depth
        normalised = raw.replace("    ", "\t")
        stripped = normalised.lstrip("\t")
        depth = len(normalised) - len(stripped)
        text = stripped.rstrip()
        if not text or text.startswith("//"):
            continue
        lines.append(_Line(depth=depth, text=text))
    return lines


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

class _SectionParser:
    """Shared utilities for block-level parsers."""

    @staticmethod
    def _read_property(line: _Line) -> Optional[Tuple[str, str]]:
        """
        Attempt to parse ``key: value`` from *line*.

        Returns ``(key, value)`` if successful, ``None`` otherwise.
        """
        m = _PROP_RE.match(line.text)
        if m:
            return m.group("key"), m.group("value").strip()
        return None


class _TableParser(_SectionParser):
    """Parses a single ``table <Name>`` TMDL block."""

    def parse(self, lines: List[_Line], start: int) -> Tuple[TMDLTable, int]:
        """
        Parse the table block beginning at ``lines[start]``.

        ``lines[start]`` must be the ``table <Name>`` header at depth 0 (or
        whatever the enclosing depth is; the parser handles nested files too).

        Returns ``(TMDLTable, next_index)`` where *next_index* is the first
        index after the block that belongs to the caller.
        """
        header = lines[start]
        table_name = _strip_quotes(header.text[len("table "):].strip())
        table = TMDLTable(name=table_name)
        idx = start + 1
        base_depth = header.depth
        child_depth = base_depth + 1

        while idx < len(lines):
            line = lines[idx]

            # A line at or above the table's depth signals a sibling/parent block
            if line.depth <= base_depth:
                break

            if line.depth == child_depth:
                # --- measure definition ---
                mm = _MEASURE_DEF_RE.match(line.text)
                if mm:
                    measure, idx = self._parse_measure(lines, idx, child_depth, mm)
                    if measure is not None:
                        table.measures.append(measure)
                    continue

                # --- column definition ---
                cm = _COLUMN_DEF_RE.match(line.text)
                if cm:
                    col, idx = self._parse_column(lines, idx, child_depth, cm)
                    if col is not None:
                        table.columns.append(col)
                    continue

                # --- partition block (M/Power Query source -- skip) ---
                if line.text.startswith("partition "):
                    idx = self._skip_block(lines, idx, child_depth)
                    continue

                # --- bare isHidden flag ---
                if line.text.strip() == "isHidden":
                    table.is_hidden = True
                else:
                    prop = self._read_property(line)
                    if prop:
                        key, value = prop
                        if key == "lineageTag":
                            table.lineage_tag = value
                        # Unknown table-level properties silently ignored

            idx += 1

        return table, idx

    # ------------------------------------------------------------------
    def _parse_measure(
        self,
        lines: List[_Line],
        start: int,
        measure_depth: int,
        m: re.Match,
    ) -> Tuple[Optional[TMDLMeasure], int]:
        """
        Parse a measure block starting at ``lines[start]``.

        Multi-line DAX handling
        -----------------------
        TMDL stores multi-line DAX by placing the first token (or nothing) on
        the ``measure 'Name' =`` line, then continuation tokens on lines at
        depth ``measure_depth + 1`` or deeper.  The expression section ends as
        soon as a line at exactly ``measure_depth + 1`` is a recognised
        property key (e.g. ``displayFolder``, ``formatString``).

        Relative indentation within the DAX expression is preserved using each
        line's depth relative to ``measure_depth + 1``.
        """
        name = _strip_quotes(m.group("name"))
        first_fragment = m.group("expr").strip()

        # Store (relative_indent, text) so indentation can be reconstructed
        expr_lines: List[Tuple[int, str]] = []
        if first_fragment:
            expr_lines.append((0, first_fragment))

        display_folder: Optional[str] = None
        format_string: Optional[str] = None
        description: Optional[str] = None

        # expr_base: depth that counts as "zero indent" for continuation lines
        expr_base = measure_depth + 1

        # We proceed in two conceptual phases:
        #   Phase 1 (expr)  -- accumulate DAX lines
        #   Phase 2 (props) -- accumulate property lines
        # Transition happens when we see a recognised property key at expr_base.
        in_expr_phase = True

        idx = start + 1
        while idx < len(lines):
            line = lines[idx]

            # Exited the measure block entirely
            if line.depth <= measure_depth:
                break

            if in_expr_phase:
                if line.depth == expr_base:
                    prop = self._read_property(line)
                    if prop and prop[0] in _MEASURE_PROP_KEYS:
                        # Transition: this line is a property, not DAX
                        in_expr_phase = False
                        # Fall through to property handling below
                    else:
                        # DAX at relative indent 0
                        expr_lines.append((0, line.text))
                        idx += 1
                        continue
                else:
                    # Deeper DAX continuation -- preserve relative indentation
                    rel = line.depth - expr_base
                    indent = "\t" * max(0, rel)
                    expr_lines.append((rel, indent + line.text))
                    idx += 1
                    continue

            # Property phase
            if line.depth == expr_base:
                prop = self._read_property(line)
                if prop:
                    key, value = prop
                    if key == "displayFolder":
                        display_folder = value
                    elif key == "formatString":
                        format_string = value
                    elif key == "description":
                        description = value
                    # Unknown property keys silently ignored

            idx += 1

        expression = "\n".join(text for _, text in expr_lines).strip()
        if not expression:
            logger.warning("Measure %r has an empty expression -- skipping.", name)
            return None, idx

        return (
            TMDLMeasure(
                name=name,
                expression=expression,
                display_folder=display_folder,
                format_string=format_string,
                description=description,
            ),
            idx,
        )

    # ------------------------------------------------------------------
    def _parse_column(
        self,
        lines: List[_Line],
        start: int,
        col_depth: int,
        m: re.Match,
    ) -> Tuple[Optional[TMDLColumn], int]:
        """Parse a column block starting at ``lines[start]``."""
        name = _strip_quotes(m.group("name"))
        col = TMDLColumn(name=name)
        idx = start + 1

        while idx < len(lines):
            line = lines[idx]

            if line.depth <= col_depth:
                break

            if line.depth == col_depth + 1:
                if line.text.strip() == "isHidden":
                    col.is_hidden = True
                else:
                    prop = self._read_property(line)
                    if prop:
                        key, value = prop
                        if key == "dataType":
                            col.data_type = value
                        elif key == "sourceColumn":
                            col.source_column = value
                        elif key == "formatString":
                            col.format_string = value
                        elif key == "summarizeBy":
                            col.summarize_by = value
                        elif key == "description":
                            col.description = value
                        # Unknown property keys silently ignored

            idx += 1

        return col, idx

    # ------------------------------------------------------------------
    @staticmethod
    def _skip_block(lines: List[_Line], start: int, block_depth: int) -> int:
        """Advance past all lines strictly deeper than *block_depth*."""
        idx = start + 1
        while idx < len(lines) and lines[idx].depth > block_depth:
            idx += 1
        return idx


class _RelationshipParser(_SectionParser):
    """Parses a single ``relationship <name>`` TMDL block."""

    def parse(
        self, lines: List[_Line], start: int
    ) -> Tuple[Optional[TMDLRelationship], int]:
        """
        Parse the relationship block at ``lines[start]``.

        Returns ``(TMDLRelationship | None, next_index)``.  Returns ``None``
        (with a warning log) if required fields are missing or unparseable.
        """
        header = lines[start]
        rel_name = header.text[len("relationship "):].strip()
        base_depth = header.depth
        child_depth = base_depth + 1

        from_ref: Optional[str] = None
        to_ref: Optional[str] = None
        is_active: bool = True
        cross_filtering: Optional[str] = None

        idx = start + 1
        while idx < len(lines):
            line = lines[idx]
            if line.depth <= base_depth:
                break
            if line.depth == child_depth:
                prop = self._read_property(line)
                if prop:
                    key, value = prop
                    if key == "fromColumn":
                        from_ref = value
                    elif key == "toColumn":
                        to_ref = value
                    elif key == "crossFilteringBehavior":
                        cross_filtering = value
                    elif key == "isActive":
                        is_active = value.lower() != "false"
                    # Unknown keys silently ignored
            idx += 1

        if not from_ref or not to_ref:
            logger.warning(
                "Relationship %r is missing fromColumn/toColumn -- skipping.",
                rel_name,
            )
            return None, idx

        try:
            from_table, from_col = _parse_col_ref(from_ref)
            to_table, to_col = _parse_col_ref(to_ref)
        except ValueError as exc:
            logger.warning("Skipping relationship %r: %s", rel_name, exc)
            return None, idx

        return (
            TMDLRelationship(
                name=rel_name,
                from_table=from_table,
                from_column=from_col,
                to_table=to_table,
                to_column=to_col,
                is_active=is_active,
                cross_filtering=cross_filtering,
            ),
            idx,
        )


# ---------------------------------------------------------------------------
# Main parser class
# ---------------------------------------------------------------------------

class TMDLParser:
    """
    Parse TMDL exports into a :class:`SemanticModel`.

    Accepted inputs for :meth:`parse`:

    - A directory containing ``model.tmdl`` (and optionally ``tables/``,
      ``relationships.tmdl``, ``roles.tmdl``)
    - A ``.zip`` archive containing such a directory structure
    - A single ``.tmdl`` file (treated as a combined model file)

    Example::

        parser = TMDLParser()
        model = parser.parse("/path/to/SalesModel.zip")
        print(model.to_json(indent=2))
    """

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def parse(self, path: str) -> SemanticModel:
        """
        Parse a TMDL directory, ZIP archive, or single ``.tmdl`` file.

        Parameters
        ----------
        path:
            Filesystem path to the TMDL export.

        Returns
        -------
        SemanticModel
            Fully populated semantic model.

        Raises
        ------
        FileNotFoundError
            If *path* does not exist on the filesystem.
        ValueError
            If *path* is not a recognisable TMDL input format.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"TMDL path not found: {path!r}")

        if p.is_file():
            suffix = p.suffix.lower()
            if suffix == ".zip":
                return self._parse_zip(p)
            if suffix == ".tmdl":
                content = p.read_text(encoding="utf-8", errors="replace")
                return self.parse_string(content)
            raise ValueError(
                f"Unrecognised file type {suffix!r} for {path!r}. "
                "Expected .zip or .tmdl."
            )

        if p.is_dir():
            return self._parse_directory(p)

        raise ValueError(
            f"Cannot parse {path!r}: must be a directory, .zip file, or .tmdl file."
        )

    def parse_string(self, content: str) -> SemanticModel:
        """
        Parse TMDL content from a raw string.

        The string may contain model metadata, table blocks, and relationship
        blocks concatenated together, as produced by combining multiple TMDL
        files into one string.

        Useful for unit-testing or when content is already in memory.

        Parameters
        ----------
        content:
            Full TMDL text.

        Returns
        -------
        SemanticModel
        """
        return self._parse_combined(content)

    # ------------------------------------------------------------------
    # ZIP handling
    # ------------------------------------------------------------------

    def _parse_zip(self, zip_path: Path) -> SemanticModel:
        """Extract the ZIP to a temporary directory and delegate to _parse_directory."""
        with tempfile.TemporaryDirectory(prefix="tmdl_parse_") as tmp:
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(tmp)
            except zipfile.BadZipFile as exc:
                raise ValueError(f"Not a valid ZIP file: {zip_path}") from exc

            root = self._find_tmdl_root(Path(tmp))
            return self._parse_directory(root)

    @staticmethod
    def _find_tmdl_root(base: Path) -> Path:
        """
        Locate the directory inside *base* that actually contains ``*.tmdl``
        files.  Handles ZIPs that wrap content in a top-level subdirectory
        (one or two levels deep).
        """
        if any(base.glob("*.tmdl")):
            return base
        # One level deep
        for child in sorted(base.iterdir()):
            if child.is_dir() and any(child.glob("*.tmdl")):
                return child
        # Two levels deep
        for child in sorted(base.iterdir()):
            if child.is_dir():
                for grandchild in sorted(child.iterdir()):
                    if grandchild.is_dir() and any(grandchild.glob("*.tmdl")):
                        return grandchild
        logger.warning(
            "Could not find *.tmdl files under %s; using base directory as root.",
            base,
        )
        return base

    # ------------------------------------------------------------------
    # Directory parsing
    # ------------------------------------------------------------------

    def _parse_directory(self, root: Path) -> SemanticModel:
        """
        Parse a TMDL export directory.

        Handles three layouts:

        1. **Standard** -- ``model.tmdl`` + ``tables/*.tmdl`` +
           ``relationships.tmdl``
        2. **Flat** -- all ``.tmdl`` files in one directory (no ``tables/``
           subdirectory)
        3. **Single combined** -- one ``.tmdl`` file containing everything
        """
        model = SemanticModel(name="Model")

        # Top-level metadata
        model_file = root / "model.tmdl"
        if model_file.exists():
            try:
                raw = model_file.read_text(encoding="utf-8", errors="replace")
                self._parse_model_metadata(raw, model)
            except Exception:
                logger.exception("Error reading model.tmdl -- continuing.")

        # Locate table files
        tables_dir = root / "tables"
        if tables_dir.is_dir():
            table_files = sorted(tables_dir.glob("*.tmdl"))
        else:
            excluded = {"model.tmdl", "relationships.tmdl", "roles.tmdl"}
            table_files = sorted(
                f for f in root.glob("*.tmdl") if f.name not in excluded
            )

        for tf in table_files:
            try:
                raw = tf.read_text(encoding="utf-8", errors="replace")
                model.tables.extend(self._parse_tables(raw))
            except Exception:
                logger.exception("Error parsing table file %s -- skipping.", tf)

        # Relationships (dedicated file)
        rel_file = root / "relationships.tmdl"
        if rel_file.exists():
            try:
                raw = rel_file.read_text(encoding="utf-8", errors="replace")
                model.relationships.extend(self._parse_relationships(raw))
            except Exception:
                logger.exception("Error parsing relationships.tmdl -- skipping.")

        # Roles / row-level security (roles/ subdirectory or roles.tmdl)
        role_files: List[Path] = []
        roles_dir = root / "roles"
        if roles_dir.is_dir():
            role_files = sorted(roles_dir.glob("*.tmdl"))
        elif (root / "roles.tmdl").exists():
            role_files = [root / "roles.tmdl"]
        for rf in role_files:
            try:
                raw = rf.read_text(encoding="utf-8", errors="replace")
                model.roles.extend(self._parse_roles(raw))
            except Exception:
                logger.exception("Error parsing role file %s -- skipping.", rf)

        return model

    def _parse_roles(self, content: str) -> List[TMDLRole]:
        """Parse one or more ``role`` blocks from TMDL content.

        Recognises ``modelPermission``, ``tablePermission <Table> = <DAX>`` (the
        row-level-security predicate), and ``member <principal> = ...`` lines.
        """
        roles: List[TMDLRole] = []
        current: Optional[TMDLRole] = None
        for line in _tokenise(content):
            text = line.text
            if text.startswith("role "):
                current = TMDLRole(name=_strip_quotes(text[len("role "):].strip()))
                roles.append(current)
                continue
            if current is None:
                continue
            if text.startswith("modelPermission:"):
                current.model_permission = text.split(":", 1)[1].strip()
            elif text.startswith("tablePermission "):
                body = text[len("tablePermission "):]
                if "=" in body:
                    tbl, expr = body.split("=", 1)
                    current.table_permissions[_strip_quotes(tbl.strip())] = expr.strip()
            elif text.startswith("member "):
                body = text[len("member "):]
                principal = body.split("=", 1)[0].strip()
                current.members.append(principal)
        return roles

    # ------------------------------------------------------------------
    # Model-level metadata
    # ------------------------------------------------------------------

    def _parse_model_metadata(self, content: str, model: SemanticModel) -> None:
        """Extract model name and ``culture`` from model-level TMDL content."""
        lines = _tokenise(content)
        for line in lines:
            if line.depth == 0 and line.text.startswith("model "):
                candidate = line.text[len("model "):].strip()
                if candidate:
                    model.name = candidate
            prop = _PROP_RE.match(line.text)
            if prop:
                key = prop.group("key")
                value = prop.group("value").strip()
                if key == "culture":
                    model.culture = value

    # ------------------------------------------------------------------
    # Combined string parsing
    # ------------------------------------------------------------------

    def _parse_combined(self, content: str) -> SemanticModel:
        """
        Parse TMDL content that may contain model metadata, tables, and
        relationships all concatenated into a single string.
        """
        model = SemanticModel(name="Model")
        self._parse_model_metadata(content, model)
        model.tables.extend(self._parse_tables(content))
        model.relationships.extend(self._parse_relationships(content))
        return model

    # ------------------------------------------------------------------
    # Table parsing
    # ------------------------------------------------------------------

    def _parse_tables(self, content: str) -> List[TMDLTable]:
        """Extract all ``table`` blocks from *content* and return them."""
        lines = _tokenise(content)
        tables: List[TMDLTable] = []
        parser = _TableParser()
        idx = 0
        while idx < len(lines):
            line = lines[idx]
            if line.depth == 0 and line.text.startswith("table "):
                try:
                    table, idx = parser.parse(lines, idx)
                    tables.append(table)
                except Exception:
                    logger.exception(
                        "Unexpected error parsing table at token index %d -- skipping.",
                        idx,
                    )
                    idx += 1
            else:
                idx += 1
        return tables

    # ------------------------------------------------------------------
    # Relationship parsing
    # ------------------------------------------------------------------

    def _parse_relationships(self, content: str) -> List[TMDLRelationship]:
        """Extract all ``relationship`` blocks from *content* and return them."""
        lines = _tokenise(content)
        relationships: List[TMDLRelationship] = []
        parser = _RelationshipParser()
        idx = 0
        while idx < len(lines):
            line = lines[idx]
            if line.depth == 0 and line.text.startswith("relationship "):
                try:
                    rel, idx = parser.parse(lines, idx)
                    if rel is not None:
                        relationships.append(rel)
                except Exception:
                    logger.exception(
                        "Unexpected error parsing relationship at token index %d -- skipping.",
                        idx,
                    )
                    idx += 1
            else:
                idx += 1
        return relationships


# ---------------------------------------------------------------------------
# CLI convenience (python tmdl_parser.py <path>)
# ---------------------------------------------------------------------------

def _main() -> None:
    """Minimal CLI: ``python tmdl_parser.py <tmdl-dir-or-zip>``."""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python tmdl_parser.py <tmdl-dir-or-zip>", file=sys.stderr)
        sys.exit(1)

    parser = TMDLParser()
    model = parser.parse(sys.argv[1])
    print(model.to_json(indent=2))


if __name__ == "__main__":
    _main()
