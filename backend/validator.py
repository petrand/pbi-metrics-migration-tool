"""
Metric View Validator

Validates generated YAML and SQL for Databricks Metric Views.
Catches structural, syntactic, and semantic errors before deployment.
"""

import logging
import re
import textwrap
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    yaml = None


@dataclass
class ValidationIssue:
    severity: str  # "error", "warning", "info"
    category: str  # "yaml_structure", "sql_syntax", "composability", "cross_reference", "residual_dax", "ddl"
    message: str
    location: str = ""


@dataclass
class ValidationResult:
    valid: bool
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def errors_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warnings_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    @property
    def info_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "info")

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": self.errors_count,
            "warnings": self.warnings_count,
            "issues": [{"severity": i.severity, "category": i.category,
                        "message": i.message, "location": i.location}
                       for i in self.issues],
        }


# DAX functions that should NOT appear in translated SQL
RESIDUAL_DAX_FUNCS = [
    "CALCULATE", "CALCULATETABLE", "ADDCOLUMNS", "SELECTCOLUMNS",
    "EARLIER", "EARLIEST", "VALUES", "ALLEXCEPT", "RELATEDTABLE",
    "FILTER", "ALL", "SUMMARIZE", "TOPN", "GENERATE", "CROSSJOIN",
    "DATATABLE", "BLANK", "USERELATIONSHIP",
]

# Table[Column] pattern
RE_TABLE_COL = re.compile(r"'?\w[\w\s]*?'?\[\w[\w\s]*?\]")


class MetricViewValidator:
    """Validates Databricks Metric View YAML and SQL."""

    def validate_yaml(self, yaml_content: str) -> ValidationResult:
        """Validate YAML structure against Metric View v1.1 spec."""
        issues = []

        if yaml is None:
            issues.append(ValidationIssue("warning", "yaml_structure",
                                          "PyYAML not installed; skipping deep YAML validation"))
            return ValidationResult(valid=True, issues=issues)

        try:
            doc = yaml.safe_load(yaml_content)
        except Exception as e:
            issues.append(ValidationIssue("error", "yaml_structure",
                                          f"Invalid YAML syntax: {e}"))
            return ValidationResult(valid=False, issues=issues)

        if not isinstance(doc, dict):
            issues.append(ValidationIssue("error", "yaml_structure",
                                          "YAML root must be a mapping"))
            return ValidationResult(valid=False, issues=issues)

        return self.validate_yaml_dict(doc)

    def validate_yaml_dict(self, doc: dict) -> ValidationResult:
        """Validate a parsed YAML dict against Metric View v1.1 spec."""
        issues = []

        # version
        version = str(doc.get("version", ""))
        if version not in ("1.1", "1"):
            issues.append(ValidationIssue("error", "yaml_structure",
                                          f"version must be '1.1', got '{version}'",
                                          "version"))

        # source
        source = doc.get("source", "")
        if not source:
            issues.append(ValidationIssue("error", "yaml_structure",
                                          "Missing required 'source' field", "source"))
        elif source.count(".") < 2:
            issues.append(ValidationIssue("warning", "yaml_structure",
                                          f"source '{source}' should be fully qualified (catalog.schema.table)",
                                          "source"))

        # joins (recursive — nested joins model snowflake schemas)
        join_names = set()

        def _check_joins(join_list, prefix):
            for idx, j in enumerate(join_list):
                loc = f"{prefix}[{idx}]"
                if not isinstance(j, dict):
                    issues.append(ValidationIssue("error", "yaml_structure",
                                                  "Join entry must be a mapping", loc))
                    continue
                for req in ("name", "source", "on"):
                    if req not in j:
                        issues.append(ValidationIssue("error", "yaml_structure",
                                                      f"Missing required field '{req}'", f"{loc}.{req}"))
                name = j.get("name", "")
                if name in join_names:
                    issues.append(ValidationIssue("error", "yaml_structure",
                                                  f"Duplicate join name '{name}'", loc))
                join_names.add(name)
                nested = j.get("joins")
                if isinstance(nested, list):
                    _check_joins(nested, f"{loc}.joins")

        _check_joins(doc.get("joins", []), "joins")

        # dimensions
        dim_names = set()
        for idx, d in enumerate(doc.get("dimensions", [])):
            loc = f"dimensions[{idx}]"
            if not isinstance(d, dict):
                issues.append(ValidationIssue("error", "yaml_structure",
                                              "Dimension must be a mapping", loc))
                continue
            for req in ("name", "expr"):
                if req not in d:
                    issues.append(ValidationIssue("error", "yaml_structure",
                                                  f"Missing required field '{req}'", f"{loc}.{req}"))
            name = d.get("name", "")
            if name in dim_names:
                issues.append(ValidationIssue("warning", "yaml_structure",
                                              f"Duplicate dimension name '{name}'", loc))
            dim_names.add(name)

            # Dimensions should not have aggregate functions
            expr = str(d.get("expr", ""))
            if re.search(r'\b(SUM|COUNT|AVG|MIN|MAX)\s*\(', expr, re.IGNORECASE):
                issues.append(ValidationIssue("warning", "yaml_structure",
                                              f"Dimension '{name}' contains aggregate function",
                                              f"{loc}.expr"))

        # measures
        measure_names = set()
        for idx, m in enumerate(doc.get("measures", [])):
            loc = f"measures[{idx}]"
            if not isinstance(m, dict):
                issues.append(ValidationIssue("error", "yaml_structure",
                                              "Measure must be a mapping", loc))
                continue
            for req in ("name", "expr"):
                if req not in m:
                    issues.append(ValidationIssue("error", "yaml_structure",
                                                  f"Missing required field '{req}'", f"{loc}.{req}"))
            name = m.get("name", "")
            if name in measure_names:
                issues.append(ValidationIssue("error", "yaml_structure",
                                              f"Duplicate measure name '{name}'", loc))
            measure_names.add(name)

            # Validate the SQL expression
            expr = str(m.get("expr", ""))
            expr_issues = self._validate_expr(expr, f"{loc}.expr")
            issues.extend(expr_issues)

            # Validate window spec. The spec shape is a list of window mappings;
            # a bare mapping is tolerated for backward compatibility.
            window = m.get("window")
            if window:
                window_items = window if isinstance(window, list) else [window]
                for widx, w in enumerate(window_items):
                    if not isinstance(w, dict):
                        issues.append(ValidationIssue("error", "yaml_structure",
                                                      "Window entry must be a mapping",
                                                      f"{loc}.window[{widx}]"))
                        continue
                    if "range" not in w:
                        issues.append(ValidationIssue("warning", "yaml_structure",
                                                      "Window missing 'range'",
                                                      f"{loc}.window[{widx}]"))

        # Cross-reference: join aliases used in expressions
        issues.extend(self._check_join_references(doc, join_names))

        # Composability
        measures_list = doc.get("measures", [])
        if measures_list:
            issues.extend(self._check_circular_measures(measures_list))

        has_errors = any(i.severity == "error" for i in issues)
        return ValidationResult(valid=not has_errors, issues=issues)

    def validate_sql_expression(self, expr: str, context: str = "") -> ValidationResult:
        """Validate a single SQL expression."""
        issues = self._validate_expr(expr, context)
        has_errors = any(i.severity == "error" for i in issues)
        return ValidationResult(valid=not has_errors, issues=issues)

    def validate_ddl(self, ddl: str) -> ValidationResult:
        """Validate a full CREATE VIEW DDL statement."""
        issues = []

        # Plain table DDL is emitted alongside the metric views as a dependency
        # the views read from (CREATE OR REPLACE TABLE ...). It is not a metric
        # view, so metric-view validation does not apply — a well-formed CREATE
        # TABLE is trivially valid here (skip rather than flag "Missing CREATE VIEW").
        # Matches both `CREATE TABLE` and `CREATE OR REPLACE TABLE` (never VIEW).
        if re.match(r'\s*CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\b', ddl or '', re.IGNORECASE):
            return ValidationResult(valid=True, issues=[])

        if not re.search(r'CREATE\s+(OR\s+REPLACE\s+)?VIEW', ddl, re.IGNORECASE):
            issues.append(ValidationIssue("error", "ddl",
                                          "Missing CREATE VIEW statement"))
            return ValidationResult(valid=False, issues=issues)

        if 'WITH METRICS' not in ddl.upper():
            issues.append(ValidationIssue("error", "ddl",
                                          "Missing WITH METRICS clause"))

        if 'LANGUAGE YAML' not in ddl.upper():
            issues.append(ValidationIssue("error", "ddl",
                                          "Missing LANGUAGE YAML clause"))

        # Extract YAML from $$ ... $$. Consume only trailing spaces/tabs after
        # the opening `$$` (not the first content line's indentation) so the
        # captured body keeps a uniform indent that textwrap.dedent can remove.
        yaml_match = re.search(r'\$\$[ \t]*\n(.*?)\n[ \t]*\$\$', ddl, re.DOTALL)
        if not yaml_match:
            issues.append(ValidationIssue("error", "ddl",
                                          "Missing $$ delimited YAML body"))
            return ValidationResult(valid=any(i.severity != "error" for i in issues),
                                    issues=issues)

        yaml_body = textwrap.dedent(yaml_match.group(1))
        yaml_result = self.validate_yaml(yaml_body)
        issues.extend(yaml_result.issues)

        has_errors = any(i.severity == "error" for i in issues)
        return ValidationResult(valid=not has_errors, issues=issues)

    def validate_measure_references(self, measures: List[dict]) -> ValidationResult:
        """Check MEASURE() composability and circular refs."""
        issues = self._check_circular_measures(measures)
        has_errors = any(i.severity == "error" for i in issues)
        return ValidationResult(valid=not has_errors, issues=issues)

    def detect_residual_dax(self, expr: str) -> List[str]:
        """Find remaining DAX patterns that weren't translated."""
        found = []
        for func in RESIDUAL_DAX_FUNCS:
            if func == "FILTER":
                # SQL `FILTER (WHERE ...)` on a measure is valid Databricks
                # syntax, not residual DAX — only flag DAX FILTER( calls.
                if re.search(r'\bFILTER\s*\((?!\s*WHERE\b)', expr, re.IGNORECASE):
                    found.append(func)
                continue
            if re.search(rf'\b{func}\s*\(', expr, re.IGNORECASE):
                found.append(func)
        if RE_TABLE_COL.search(expr):
            found.append("Table[Column]")
        if '&&' in expr:
            found.append("&&")
        if '||' in expr:
            found.append("||")
        return found

    def validate_all(self, yaml_content: str = None, ddl: str = None,
                     yaml_dict: dict = None) -> ValidationResult:
        """Run all validations."""
        all_issues = []

        if ddl:
            r = self.validate_ddl(ddl)
            all_issues.extend(r.issues)
        if yaml_content:
            r = self.validate_yaml(yaml_content)
            all_issues.extend(r.issues)
        if yaml_dict:
            r = self.validate_yaml_dict(yaml_dict)
            all_issues.extend(r.issues)

        has_errors = any(i.severity == "error" for i in all_issues)
        return ValidationResult(valid=not has_errors, issues=all_issues)

    # ── Internal helpers ─────────────────────────────────────────────

    def _validate_expr(self, expr: str, location: str) -> List[ValidationIssue]:
        """Validate a single SQL expression."""
        issues = []
        if not expr.strip():
            return issues

        # Balanced parentheses
        depth = 0
        for ch in expr:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            if depth < 0:
                issues.append(ValidationIssue("error", "sql_syntax",
                                              "Unbalanced parentheses (extra closing)", location))
                break
        if depth > 0:
            issues.append(ValidationIssue("error", "sql_syntax",
                                          f"Unbalanced parentheses ({depth} unclosed)", location))

        # CASE WHEN ... END matching
        case_count = len(re.findall(r'\bCASE\b', expr, re.IGNORECASE))
        end_count = len(re.findall(r'\bEND\b', expr, re.IGNORECASE))
        if case_count != end_count:
            issues.append(ValidationIssue("error", "sql_syntax",
                                          f"Mismatched CASE/END ({case_count} CASE, {end_count} END)",
                                          location))

        # Dangling operators
        stripped = expr.strip()
        if stripped and stripped[-1] in ('+', '-', '*', '/'):
            issues.append(ValidationIssue("warning", "sql_syntax",
                                          "Expression ends with operator", location))

        # Residual DAX
        residual = self.detect_residual_dax(expr)
        for r in residual:
            issues.append(ValidationIssue("warning", "residual_dax",
                                          f"Residual DAX pattern: {r}", location))

        # Nested aggregates
        agg_pattern = re.compile(r'\b(SUM|COUNT|AVG|MIN|MAX)\s*\(', re.IGNORECASE)
        agg_matches = list(agg_pattern.finditer(expr))
        for i, m in enumerate(agg_matches):
            # Check if this aggregate is inside another
            depth = 0
            for j in range(m.start()):
                if expr[j] == '(':
                    depth += 1
                elif expr[j] == ')':
                    depth -= 1
            # Find if there's an enclosing aggregate
            for prev in agg_matches[:i]:
                if prev.start() < m.start():
                    # Check if m is inside prev's parens
                    inner_depth = 0
                    for k in range(prev.end(), m.start()):
                        if expr[k] == '(':
                            inner_depth += 1
                        elif expr[k] == ')':
                            inner_depth -= 1
                    if inner_depth >= 0:
                        issues.append(ValidationIssue("warning", "composability",
                                                      f"Nested aggregate: {m.group(1)} inside {prev.group(1)}",
                                                      location))
                        break

        return issues

    def _check_join_references(self, doc: dict, join_names: set) -> List[ValidationIssue]:
        """Check that expressions reference valid join aliases."""
        issues = []
        alias_pattern = re.compile(r'\b(\w+)\.\w+')

        for section in ("dimensions", "measures"):
            for idx, item in enumerate(doc.get(section, [])):
                expr = str(item.get("expr", ""))
                for m in alias_pattern.finditer(expr):
                    alias = m.group(1).lower()
                    if alias != "source" and alias not in {j.lower() for j in join_names}:
                        issues.append(ValidationIssue("warning", "cross_reference",
                                                      f"Reference to undefined join alias '{alias}'",
                                                      f"{section}[{idx}].expr"))
        return issues

    def _check_circular_measures(self, measures: List[dict]) -> List[ValidationIssue]:
        """Detect circular MEASURE() references using DFS."""
        issues = []
        measure_re = re.compile(r'MEASURE\s*\(\s*`?([^)`]+)`?\s*\)', re.IGNORECASE)

        # Build dependency graph
        deps: Dict[str, List[str]] = {}
        for m in measures:
            name = m.get("name", "")
            expr = str(m.get("expr", ""))
            refs = [match.group(1) for match in measure_re.finditer(expr)]
            deps[name] = refs

        # DFS cycle detection
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {name: WHITE for name in deps}

        def dfs(node, path):
            color[node] = GRAY
            for dep in deps.get(node, []):
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    cycle = path + [node, dep]
                    issues.append(ValidationIssue("error", "composability",
                                                  f"Circular measure reference: {' -> '.join(cycle)}"))
                    return
                if color[dep] == WHITE:
                    dfs(dep, path + [node])
            color[node] = BLACK

        for name in deps:
            if color[name] == WHITE:
                dfs(name, [])

        return issues
