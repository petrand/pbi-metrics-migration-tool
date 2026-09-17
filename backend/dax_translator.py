"""
DAX-to-Databricks SQL Translation Engine

Production-grade translator converting Power BI DAX expressions to
Databricks SQL suitable for Metric Views (YAML v1.1 spec).
Handles 30+ DAX functions/patterns with multi-pass resolution.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


class TranslationStatus(Enum):
    CONVERTED = "converted"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    MANUAL_OVERRIDE = "manual_override"
    EXCLUDED = "excluded"


@dataclass
class TranslationResult:
    """Result of translating a single DAX expression to SQL."""
    original_dax: str
    translated_sql: str
    status: str = "converted"
    confidence: int = 100
    applied_transformations: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    window_spec: Optional[Union[dict, list]] = None
    rls_applied: bool = False

    def to_dict(self) -> dict:
        d = {
            "original_dax": self.original_dax,
            "translated_sql": self.translated_sql,
            "status": self.status,
            "confidence": self.confidence,
            "applied_transformations": self.applied_transformations,
            "issues": self.issues,
            "warnings": self.warnings,
            "rls_applied": self.rls_applied,
        }
        if self.window_spec:
            d["window_spec"] = self.window_spec
        return d


# ── Regex patterns for DAX parsing ──────────────────────────────────────

# Table[Column] reference
RE_TABLE_COL = re.compile(
    r"'?(\w[\w\s]*?)'?\[(\w[\w\s]*?)\]", re.IGNORECASE
)

# DAX function call (function name + opening paren)
RE_FUNC_CALL = re.compile(
    r'\b([A-Z_][A-Z_0-9]*)\s*\(', re.IGNORECASE
)

# VAR ... RETURN pattern (multiline)
RE_VAR_RETURN = re.compile(
    r'\bVAR\s+(\w+)\s*=\s*(.*?)\s+RETURN\s+',
    re.IGNORECASE | re.DOTALL
)

# Single VAR assignment
RE_VAR_ASSIGN = re.compile(
    r'\bVAR\s+(\w+)\s*=\s*', re.IGNORECASE
)

# Measure reference [Measure Name]
RE_MEASURE_REF = re.compile(r'\[([^\]]+)\]')

# Logical operators
RE_AND_OP = re.compile(r'&&')
RE_OR_OP = re.compile(r'\|\|')

# SQL keywords that can immediately precede a ``[ref]`` after conditional/logical
# translation. When one of these is captured as a Table[Column] "table" name,
# the reference is really a bare [measure]/column, not a table reference.
_SQL_KEYWORDS = frozenset({
    "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "IN", "LIKE",
    "WHERE", "FILTER", "BY", "ON", "AS", "IS", "NULL", "BETWEEN", "DISTINCT",
})


class DAXTranslator:
    """Translates DAX expressions to Databricks SQL for metric views."""

    def __init__(self, relationships: List[dict] = None, known_measures: Dict[str, str] = None,
                 security_tables: List[str] = None, security_measures: List[str] = None):
        """
        Args:
            relationships: List of relationship dicts with 'from' and 'to' keys.
            known_measures: Dict mapping measure names to their translated SQL.
            security_tables: Names of RLS / measure-security tables. A bare
                CALCULATE filter argument naming one of these (or any table) is a
                context transition with no metric-view equivalent and is dropped;
                when the table is a security table the measure is flagged
                ``rls_applied`` (its in-measure security gate was removed for
                conversion — enforce access via the view's row filters instead).
            security_measures: Names of permission-check measures (home table is
                a security table). A guard ``IF([SecMeasure] <op> n, expr, BLANK())``
                is reduced to ``expr`` and the measure flagged ``rls_applied``.
        """
        self._relationships = relationships or []
        self._known_measures = known_measures or {}
        self._security_tables = {self._norm_name(t) for t in (security_tables or [])}
        self._security_measures = {self._norm_name(m) for m in (security_measures or [])}
        self._rls_applied = False
        # Normalised measure name -> original name, so a qualified reference
        # written as 'Table'[Measure Name] can be recognised as a measure (and
        # rendered MEASURE(`...`)) rather than mistaken for a fact column.
        self._measure_names_norm = {self._norm_name(n): n for n in self._known_measures}
        self._join_map = self._build_join_map()
        self._transformations = []
        self._issues = []
        self._warnings = []

    @staticmethod
    def _norm_name(name: str) -> str:
        """Normalise a measure name for case/whitespace-insensitive matching."""
        return re.sub(r'\s+', ' ', name or '').strip().lower()

    def _build_join_map(self) -> Dict[str, str]:
        """Build a mapping of Table.Column -> join_alias.column from relationships."""
        jmap = {}
        for rel in self._relationships:
            from_parts = rel.get("from", "").split(".")
            to_parts = rel.get("to", "").split(".")
            if len(to_parts) == 2:
                table = to_parts[0].strip("'").lower().replace(" ", "_")
                col = to_parts[1].strip("'").lower().replace(" ", "_")
                key = f"{to_parts[0].strip(chr(39))}.{to_parts[1].strip(chr(39))}".lower()
                jmap[key] = f"{table}.{col}"
        return jmap

    def translate(self, dax_expr: str, table_name: str = "",
                  measures: Dict[str, str] = None) -> TranslationResult:
        """Translate a single DAX expression to Databricks SQL.

        Args:
            dax_expr: The DAX expression to translate.
            table_name: Name of the table this measure belongs to.
            measures: Optional dict of measure_name -> dax_expr for cross-refs.

        Returns:
            TranslationResult with translated SQL and metadata.
        """
        self._transformations = []
        self._issues = []
        self._warnings = []
        self._rls_applied = False

        if not dax_expr or not dax_expr.strip():
            return TranslationResult(
                original_dax=dax_expr or "",
                translated_sql="",
                status="unsupported",
                confidence=0,
                issues=["Empty DAX expression"],
            )

        if measures:
            self._known_measures.update(measures)
            for name in measures:
                self._measure_names_norm[self._norm_name(name)] = name

        sql = dax_expr.strip()

        # Multi-pass translation
        sql = self._pass_normalize(sql)
        sql = self._pass_var_return(sql)
        sql = self._pass_strip_rls_gate(sql)
        sql = self._pass_time_intelligence(sql)
        window = self._extract_window_spec(dax_expr)
        sql = self._pass_calculate(sql, table_name)
        sql = self._pass_aggregations(sql, table_name)
        sql = self._pass_conditional(sql)
        sql = self._pass_logical(sql)
        sql = self._pass_date_functions(sql)
        sql = self._pass_text_functions(sql)
        sql = self._pass_lookups(sql, table_name)
        sql = self._pass_iterators(sql, table_name)
        sql = self._pass_table_col_refs(sql, table_name)
        sql = self._pass_measure_refs(sql)
        sql = self._pass_cleanup(sql)

        # Determine status and confidence
        status, confidence = self._compute_status(sql, dax_expr)

        # A single, uniform note whenever in-measure RLS was removed (either the
        # permission-gate IF or a security-table CALCULATE filter arg).
        if self._rls_applied:
            self._warnings.append(
                "RLS was previously applied in-measure; the security gate was "
                "removed for conversion — enforce access via the view's row filters"
            )

        return TranslationResult(
            original_dax=dax_expr,
            translated_sql=sql,
            status=status,
            confidence=confidence,
            applied_transformations=list(self._transformations),
            issues=list(self._issues),
            warnings=list(self._warnings),
            window_spec=window,
            rls_applied=self._rls_applied,
        )

    def translate_batch(self, measures: List[dict], table_name: str = "") -> List[TranslationResult]:
        """Translate a batch of measures with multi-pass cross-reference resolution.

        Args:
            measures: List of dicts with 'name' and 'expression' keys.
            table_name: Name of the fact table.

        Returns:
            List of TranslationResult objects.
        """
        # Build measure name -> expression map
        measure_map = {m["name"]: m.get("expression", "") for m in measures}

        # Register all measure names (not just resolved ones) so qualified
        # references 'Table'[Measure] resolve to MEASURE(`...`) during ref
        # translation. See _norm_name / _translate_table_col_refs.
        for name in measure_map:
            self._measure_names_norm[self._norm_name(name)] = name

        # Multi-pass: resolve cross-references iteratively
        results = {}
        resolved = {}
        max_passes = 5

        for pass_num in range(max_passes):
            new_resolved = 0
            for m in measures:
                name = m["name"]
                if name in resolved:
                    continue
                result = self.translate(
                    m.get("expression", ""), table_name, measures=resolved
                )
                if result.status in ("converted", "partial"):
                    resolved[name] = result.translated_sql
                    new_resolved += 1
                results[name] = result

            if new_resolved == 0:
                break

        # Final pass for any remaining unresolved
        for m in measures:
            name = m["name"]
            if name not in results:
                result = self.translate(m.get("expression", ""), table_name, measures=resolved)
                results[name] = result

        return [results.get(m["name"], TranslationResult(
            original_dax=m.get("expression", ""),
            translated_sql="",
            status="unsupported",
            confidence=0,
            issues=["Failed to translate"],
        )) for m in measures]

    # ── Translation Passes ──────────────────────────────────────────────

    def _pass_normalize(self, sql: str) -> str:
        """Normalize whitespace and formatting."""
        # Strip TMDL triple-backtick code fences that wrap multi-line measures.
        sql = sql.replace('```', ' ')
        # Drop a trailing measure property (e.g. "isHidden") the parser may have
        # appended to a fenced expression.
        sql = re.sub(r'\bisHidden\b\s*$', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'\s+', ' ', sql).strip()
        # Remove trailing semicolons
        sql = sql.rstrip(';').strip()
        self._transformations.append("normalize")
        return sql

    def _pass_var_return(self, sql: str) -> str:
        """Inline VAR x = expr RETURN result patterns."""
        # Find all VAR assignments
        vars_found = {}
        var_pattern = re.compile(
            r'\bVAR\s+(__\w+|\w+)\s*=\s*',
            re.IGNORECASE
        )

        matches = list(var_pattern.finditer(sql))
        if not matches:
            return sql

        # Parse VAR blocks - find the expression for each VAR
        remaining = sql
        for match in reversed(matches):
            var_name = match.group(1)
            start = match.end()
            # Find the expression end (next VAR or RETURN)
            next_kw = re.search(r'\b(VAR|RETURN)\b', remaining[start:], re.IGNORECASE)
            if next_kw:
                expr = remaining[start:start + next_kw.start()].strip()
            else:
                expr = remaining[start:].strip()
            vars_found[var_name] = expr

        # Find the RETURN expression
        return_match = re.search(r'\bRETURN\s+(.+)$', sql, re.IGNORECASE | re.DOTALL)
        if return_match:
            result = return_match.group(1).strip()
            # Inline all variables
            for var_name, var_expr in vars_found.items():
                result = re.sub(r'\b' + re.escape(var_name) + r'\b', f'({var_expr})', result)
            self._transformations.append("var_return_inline")
            return result

        return sql

    def _pass_strip_rls_gate(self, sql: str) -> str:
        """Remove in-measure row-level-security gating.

        Power BI models often gate a measure behind a permission check, e.g.
        ``IF([Count Measures E] > 0, CALCULATE(SUM([CostE]), 'Measure Security'),
        BLANK())`` — where ``Count Measures E`` counts rows of a security table.
        A metric view can't express per-user security inside a measure (that is
        the job of the view's row filters), so we reduce the guard to its
        then-branch and flag the measure ``rls_applied``. The bare security-table
        CALCULATE filter arg is dropped later in _pass_calculate (also flagged).
        """
        if not self._security_measures:
            return sql

        guard = 0
        if_re = re.compile(r'\bIF\s*\(', re.IGNORECASE)
        while guard < 200:
            guard += 1
            m = if_re.search(sql)
            if not m:
                break
            open_idx = m.end() - 1
            close_idx = self._find_balanced_paren(sql, open_idx)
            if close_idx == -1:
                break
            args = self._split_top_level_args(sql[open_idx + 1:close_idx])
            if len(args) < 2:
                break
            cond = args[0].strip()
            # The condition must be a permission check on a security measure:
            #   [Sec Measure] <op> <number>
            ref = RE_MEASURE_REF.search(cond)
            gate = bool(ref and self._norm_name(ref.group(1)) in self._security_measures
                        and self._CMP_OP.search(cond))
            if not gate:
                # Not an RLS gate — leave this IF for the normal conditional pass.
                # Advance past it so we don't loop forever on the same match.
                marker = "\x00IF\x00"
                sql = sql[:m.start()] + marker + sql[m.start() + 2:]
                continue
            # Reduce the guard to its then-branch (drop the security gate).
            self._rls_applied = True
            self._transformations.append("RLS_gate_stripped")
            sql = sql[:m.start()] + args[1].strip() + sql[close_idx + 1:]

        sql = sql.replace("\x00IF\x00", "IF")
        return sql

    def _pass_time_intelligence(self, sql: str) -> str:
        """Translate time intelligence functions."""
        # TOTALYTD
        m = re.search(r'\bTOTALYTD\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)', sql, re.IGNORECASE)
        if m:
            self._transformations.append("TOTALYTD_to_window")
            self._warnings.append("TOTALYTD converted to window spec - verify date column")
            inner = m.group(1).strip()
            return inner

        # TOTALMTD
        m = re.search(r'\bTOTALMTD\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)', sql, re.IGNORECASE)
        if m:
            self._transformations.append("TOTALMTD_to_window")
            inner = m.group(1).strip()
            return inner

        # TOTALQTD
        m = re.search(r'\bTOTALQTD\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)', sql, re.IGNORECASE)
        if m:
            self._transformations.append("TOTALQTD_to_window")
            inner = m.group(1).strip()
            return inner

        # SAMEPERIODLASTYEAR - flag for manual review
        if re.search(r'\bSAMEPERIODLASTYEAR\b', sql, re.IGNORECASE):
            self._warnings.append("SAMEPERIODLASTYEAR requires manual review for period comparison logic")
            self._transformations.append("SAMEPERIODLASTYEAR_flagged")

        # DATESINPERIOD
        if re.search(r'\bDATESINPERIOD\b', sql, re.IGNORECASE):
            self._warnings.append("DATESINPERIOD requires manual review")
            self._transformations.append("DATESINPERIOD_flagged")

        # PARALLELPERIOD
        if re.search(r'\bPARALLELPERIOD\b', sql, re.IGNORECASE):
            self._warnings.append("PARALLELPERIOD requires manual review")
            self._transformations.append("PARALLELPERIOD_flagged")

        return sql

    # DAX time-intelligence functions, grouped by the Metric View window they map
    # to. Cumulative (*TD) periods become a cumulative window; prior-period
    # comparisons become a window with a period `offset`.
    _TI_CUMULATIVE = {
        "TOTALYTD": "year", "DATESYTD": "year",
        "TOTALQTD": "quarter", "DATESQTD": "quarter",
        "TOTALMTD": "month", "DATESMTD": "month",
    }
    _TI_PRIOR_YEAR = ("SAMEPERIODLASTYEAR", "PREVIOUSYEAR", "PARALLELPERIOD")

    def _extract_window_spec(self, dax_expr: str) -> Optional[dict]:
        """Extract a Metric View window spec from time-intelligence DAX.

        Handles both the wrapper form (``TOTALYTD(m, dates)``) and the
        ``CALCULATE(m, DATESYTD(dates) | SAMEPERIODLASTYEAR(dates))`` form. Returns
        a dict with the spec keys (order / range / semiadditive / offset); the
        YAML generator renders it as a window list. ``None`` when no
        time-intelligence is present.
        """
        # Cumulative period-to-date windows. A bare `range: cumulative` is an
        # unbounded running total; period-to-date semantics (MTD/QTD/YTD) require
        # the accumulation to RESET at the period boundary. That reset is a second,
        # nested window entry: `range: current` ordered by a period-truncated grain
        # (e.g. date_trunc('MONTH', date)). Without it the measure only behaves as
        # *TD when the consuming query happens to group by that period; at a finer
        # grain it silently returns a running total across all periods.
        #
        # The reset entry orders by a synthetic `<date_col>__<period>` dimension;
        # the YAML generator materializes it as date_trunc('<PERIOD>', <date_col>).
        # A prior-year offset (LY MTD/YTD), when built by the generator's offset
        # pushdown, is merged onto BOTH entries — shifting the reset anchor too,
        # which is required or the un-shifted `current` anchor nulls the result.
        for func, period in self._TI_CUMULATIVE.items():
            if re.search(rf'\b{func}\b', dax_expr, re.IGNORECASE):
                date_col = self._find_date_column(dax_expr, func)
                # A fiscal-year anchor (e.g. DATESYTD(dates, \"30-06\")) can't be
                # expressed directly in the window — surface it for review.
                if re.search(r'"\d{1,2}-\d{1,2}"', dax_expr):
                    self._warnings.append(
                        "Fiscal-year anchor detected; confirm the source table's "
                        "date grain reflects the fiscal calendar"
                    )
                return [
                    {"order": date_col, "range": "cumulative", "semiadditive": "last"},
                    {"order": f"{date_col}__{period}", "range": "current",
                     "semiadditive": "last"},
                ]

        # Prior-period comparisons -> window offset. Metric View period offsets
        # are best-effort here; flag for manual verification.
        for func in self._TI_PRIOR_YEAR:
            if re.search(rf'\b{func}\b', dax_expr, re.IGNORECASE):
                date_col = self._find_date_column(dax_expr, func)
                self._warnings.append(
                    f"{func} mapped to a prior-year window offset; verify offset "
                    "semantics or supply a measure_override"
                )
                # Point period-over-period shift: anchor on the current row and
                # slide it back with `offset`. `range: current` keeps the same
                # grain as the base measure and works even under a single-date
                # filter; a size-less `trailing` is invalid on a non-numeric
                # (DATE/TIMESTAMP) order column.
                return {"order": date_col, "range": "current", "offset": "-1 year",
                        "semiadditive": "last"}

        # DATEADD(dates, -n, YEAR|MONTH|QUARTER) -> offset window.
        m = re.search(r'\bDATEADD\s*\(\s*(.+?)\s*,\s*(-?\d+)\s*,\s*(\w+)\s*\)',
                      dax_expr, re.IGNORECASE)
        if m:
            date_col = self._find_date_column(dax_expr, "DATEADD")
            offset = f"{m.group(2)} {m.group(3).lower()}"
            self._warnings.append("DATEADD mapped to a window offset; verify offset semantics")
            # Point shift by a fixed interval -> anchor on the current row and
            # slide it with `offset` (see prior-year branch above).
            return {"order": date_col, "range": "current", "offset": offset,
                    "semiadditive": "last"}

        return None

    @classmethod
    def _find_date_column(cls, dax_expr: str, func: str) -> str:
        """Best-effort extraction of the date column referenced by *func*.

        The date column is the final argument of TOTAL*TD(measure, dates), so we
        parse the balanced argument list and take the last Table[Column] ref.
        """
        m = re.search(rf'\b{func}\s*\(', dax_expr, re.IGNORECASE)
        search_space = dax_expr
        if m:
            open_idx = m.end() - 1
            close_idx = cls._find_balanced_paren(dax_expr, open_idx)
            if close_idx != -1:
                inner = dax_expr[open_idx + 1:close_idx]
                args = cls._split_top_level_args(inner)
                # Prefer the last argument (the date reference).
                search_space = args[-1] if args else inner
        col_m = list(RE_TABLE_COL.finditer(search_space))
        if col_m:
            return col_m[-1].group(2).lower().replace(" ", "_")
        return "date"

    @staticmethod
    def _find_balanced_paren(text: str, start: int) -> int:
        """Return index of the closing ')' that balances the '(' at *start*.
        Returns -1 if no balanced close is found."""
        depth = 0
        i = start
        while i < len(text):
            if text[i] == '(':
                depth += 1
            elif text[i] == ')':
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return -1

    @staticmethod
    def _split_top_level_args(text: str) -> List[str]:
        """Split *text* on commas that are NOT inside nested parentheses."""
        args: List[str] = []
        depth = 0
        current: List[str] = []
        for ch in text:
            if ch == '(':
                depth += 1
                current.append(ch)
            elif ch == ')':
                depth -= 1
                current.append(ch)
            elif ch == ',' and depth == 0:
                args.append(''.join(current).strip())
                current = []
            else:
                current.append(ch)
        tail = ''.join(current).strip()
        if tail:
            args.append(tail)
        return args

    def _pass_calculate(self, sql: str, table_name: str) -> str:
        """Translate CALCULATE with filter arguments to conditional aggregation.

        Uses balanced-parenthesis parsing so nested calls like
        CALCULATE(SUM(T[C]), FILTER(ALL(T), T[X] = 'V')) are handled correctly.
        """
        calc_re = re.compile(r'\bCALCULATE\s*\(', re.IGNORECASE)
        result_parts: List[str] = []
        pos = 0

        while pos < len(sql):
            m = calc_re.search(sql, pos)
            if not m:
                result_parts.append(sql[pos:])
                break

            # Emit text before CALCULATE
            result_parts.append(sql[pos:m.start()])

            # Find the balanced closing ')' for this CALCULATE(
            open_idx = m.end() - 1  # index of the '('
            close_idx = self._find_balanced_paren(sql, open_idx)
            if close_idx == -1:
                # Unbalanced - leave as-is
                result_parts.append(sql[m.start():m.end()])
                pos = m.end()
                continue

            inner = sql[open_idx + 1:close_idx]  # everything inside CALCULATE(...)
            args = self._split_top_level_args(inner)

            # Single-argument CALCULATE(expr) applies no filter — unwrap to expr.
            if len(args) == 1:
                self._transformations.append("CALCULATE_unwrap_single_arg")
                result_parts.append(args[0])
                pos = close_idx + 1
                continue

            if len(args) >= 2:
                measure_expr = args[0]

                # Time-intelligence filter: CALCULATE(m, DATESYTD(...) |
                # SAMEPERIODLASTYEAR(...) | ...). The period logic is carried by
                # the window spec (extracted from the original DAX), so drop the
                # filter argument(s) and keep only the base measure expression.
                rest = " ".join(args[1:])
                if self._is_time_intel(rest):
                    self._transformations.append("CALCULATE_time_intel_to_window")
                    result_parts.append(measure_expr)
                    pos = close_idx + 1
                    continue

                # Translate EVERY filter argument (not just the first) to a SQL
                # predicate and AND them into a single FILTER (WHERE ...). Each
                # predicate is parenthesised so OR-groups (||) and mixed
                # operators keep their precedence. Predicates that can't be
                # expressed (nested table functions, VALUES-iteration, ...) make
                # the whole CALCULATE fall through to the manual-review path.
                preds: List[str] = []
                translatable = True
                for farg in args[1:]:
                    pred = self._calc_predicate(farg, table_name)
                    if pred is None:
                        translatable = False
                        break
                    if pred:  # empty string = a dropped filter (e.g. USERELATIONSHIP)
                        preds.append(pred)

                if translatable:
                    if preds:
                        condition = " AND ".join(preds)
                        self._transformations.append("CALCULATE_filter_to_FILTER_WHERE")
                        result_parts.append(f"{measure_expr} FILTER (WHERE {condition})")
                    else:
                        # All filter args were droppable (context transitions with
                        # no row-filter effect) — the measure stands alone.
                        self._transformations.append("CALCULATE_filters_dropped")
                        result_parts.append(measure_expr)
                    pos = close_idx + 1
                    continue

                # Complex CALCULATE - flag for review
                self._issues.append(f"Complex CALCULATE pattern requires manual review: {inner[:80]}")
                self._transformations.append("CALCULATE_complex_flagged")

            # Could not translate - keep original
            result_parts.append(sql[m.start():close_idx + 1])
            pos = close_idx + 1

        return ''.join(result_parts)

    # Comparison operators that mark a scalar CALCULATE filter predicate.
    _CMP_OP = re.compile(r'(<>|!=|>=|<=|=|<|>)')
    # DAX table functions that must not appear in a FILTER's table argument for
    # us to treat it as a plain row filter (VALUES-iteration etc. is not one).
    _NON_TABLE_FILTER = re.compile(
        r'\b(VALUES|ADDCOLUMNS|SELECTCOLUMNS|SUMMARIZE|TOPN|ALLSELECTED|'
        r'ALLEXCEPT|CROSSJOIN|GENERATE)\s*\(', re.IGNORECASE)
    # DAX calls that can't survive inside a SQL predicate.
    _RESIDUAL_IN_PRED = re.compile(
        r'\b(CALCULATE|VALUES|ALLEXCEPT|EARLIER|ADDCOLUMNS|SELECTCOLUMNS|'
        r'RELATEDTABLE|SUMMARIZE|TOPN)\s*\(', re.IGNORECASE)

    def _calc_predicate(self, arg: str, table_name: str) -> Optional[str]:
        """Translate one CALCULATE filter argument into a SQL WHERE predicate.

        Returns:
            - a parenthesised SQL predicate string, or
            - ``""`` when the argument is a context transition with no row-filter
              effect and should be dropped (e.g. USERELATIONSHIP), or
            - ``None`` when the argument can't be expressed as a plain filter
              (caller then flags the whole CALCULATE for manual review).
        """
        a = arg.strip()

        # USERELATIONSHIP(a, b): activates an alternate relationship. No Metric
        # View equivalent — drop it, keep the other predicates, and warn.
        if re.match(r'USERELATIONSHIP\s*\(', a, re.IGNORECASE):
            self._warnings.append(
                "USERELATIONSHIP dropped — the alternate relationship has no "
                "Metric View equivalent; verify the join path or supply a "
                "measure_override"
            )
            self._transformations.append("USERELATIONSHIP_dropped")
            return ""

        # FILTER(<table|ALL(table)>, <condition>): keep the condition, drop the
        # table scope. Reject FILTER over a table *function* (VALUES(), etc.) —
        # that is an iteration, not a row filter.
        if re.match(r'FILTER\s*\(', a, re.IGNORECASE):
            open_idx = a.index('(')
            close_idx = self._find_balanced_paren(a, open_idx)
            if close_idx == -1:
                return None
            fargs = self._split_top_level_args(a[open_idx + 1:close_idx])
            if len(fargs) != 2:
                return None
            t0 = fargs[0].strip()
            table_ok = bool(
                re.fullmatch(r"ALL\s*\(\s*'?[\w ]+'?\s*\)", t0, re.IGNORECASE)
                or re.fullmatch(r"'?[\w ]+'?", t0)
            )
            if not table_ok or self._NON_TABLE_FILTER.search(t0):
                return None
            cond = fargs[1].strip()
            if re.search(r'\b(FILTER|CALCULATE)\s*\(', cond, re.IGNORECASE):
                return None  # nested filter/calculate — too complex
            return f"({self._to_filter_condition(cond, table_name)})"

        # Scalar predicate: Table[Col] <op> value (any comparison operator).
        if self._CMP_OP.search(a):
            if self._RESIDUAL_IN_PRED.search(a):
                return None
            return f"({self._to_filter_condition(a, table_name)})"

        # A bare table name as a filter argument — CALCULATE(m, Sales) or
        # CALCULATE(m, 'Measure Security') — is a context transition with no
        # metric-view equivalent, so drop it. If it names a security table, the
        # measure carried in-measure RLS: flag it (removed for conversion, to be
        # enforced by the view's row filters instead).
        bare = re.fullmatch(r"'?([A-Za-z_][\w ]*)'?", a)
        if bare and '[' not in a and '(' not in a:
            if self._norm_name(bare.group(1)) in self._security_tables:
                self._rls_applied = True
                self._transformations.append("RLS_table_filter_dropped")
            else:
                self._transformations.append("table_filter_arg_dropped")
            return ""

        return None

    def _is_time_intel(self, expr: str) -> bool:
        """True if *expr* contains a DAX time-intelligence function."""
        funcs = (list(self._TI_CUMULATIVE) + list(self._TI_PRIOR_YEAR) +
                 ["DATEADD", "DATESINPERIOD"])
        return any(re.search(rf'\b{f}\b', expr, re.IGNORECASE) for f in funcs)

    def _pass_aggregations(self, sql: str, table_name: str) -> str:
        """Translate DAX aggregation functions to SQL equivalents."""
        # Bare-column aggregates: SUM([Col]) etc., where [Col] is a fact column
        # (not a measure). Handle before measure-ref resolution so they don't
        # wrongly become SUM(MEASURE(`Col`)). Quoted/unqualified column only.
        bare = {"SUM": "SUM", "COUNT": "COUNT", "AVERAGE": "AVG",
                "MIN": "MIN", "MAX": "MAX", "COUNTA": "COUNT"}
        for dax_fn, sql_fn in bare.items():
            sql = re.sub(
                rf'\b{dax_fn}\s*\(\s*\[(\w[\w\s]*?)\]\s*\)',
                lambda m, f=sql_fn: f"{f}(source.{m.group(1).lower().replace(' ', '_')})",
                sql, flags=re.IGNORECASE,
            )
        sql = re.sub(
            r'\bDISTINCTCOUNT\s*\(\s*\[(\w[\w\s]*?)\]\s*\)',
            lambda m: f"COUNT(DISTINCT source.{m.group(1).lower().replace(' ', '_')})",
            sql, flags=re.IGNORECASE,
        )

        # SUM(Table[Col])
        sql = re.sub(
            r'\bSUM\s*\(\s*\'?(\w[\w\s]*?)\'?\[(\w[\w\s]*?)\]\s*\)',
            lambda m: f"SUM(source.{m.group(2).lower().replace(' ', '_')})",
            sql, flags=re.IGNORECASE
        )
        if 'SUM(source.' in sql:
            self._transformations.append("SUM_translation")

        # COUNT(Table[Col])
        sql = re.sub(
            r'\bCOUNT\s*\(\s*\'?(\w[\w\s]*?)\'?\[(\w[\w\s]*?)\]\s*\)',
            lambda m: f"COUNT(source.{m.group(2).lower().replace(' ', '_')})",
            sql, flags=re.IGNORECASE
        )
        if 'COUNT(source.' in sql:
            self._transformations.append("COUNT_translation")

        # DISTINCTCOUNT(Table[Col])
        sql = re.sub(
            r'\bDISTINCTCOUNT\s*\(\s*\'?(\w[\w\s]*?)\'?\[(\w[\w\s]*?)\]\s*\)',
            lambda m: f"COUNT(DISTINCT source.{m.group(2).lower().replace(' ', '_')})",
            sql, flags=re.IGNORECASE
        )
        if 'COUNT(DISTINCT source.' in sql:
            self._transformations.append("DISTINCTCOUNT_translation")

        # AVERAGE(Table[Col])
        sql = re.sub(
            r'\bAVERAGE\s*\(\s*\'?(\w[\w\s]*?)\'?\[(\w[\w\s]*?)\]\s*\)',
            lambda m: f"AVG(source.{m.group(2).lower().replace(' ', '_')})",
            sql, flags=re.IGNORECASE
        )
        if 'AVG(source.' in sql:
            self._transformations.append("AVERAGE_translation")

        # MIN(Table[Col])
        sql = re.sub(
            r'\bMIN\s*\(\s*\'?(\w[\w\s]*?)\'?\[(\w[\w\s]*?)\]\s*\)',
            lambda m: f"MIN(source.{m.group(2).lower().replace(' ', '_')})",
            sql, flags=re.IGNORECASE
        )

        # MAX(Table[Col])
        sql = re.sub(
            r'\bMAX\s*\(\s*\'?(\w[\w\s]*?)\'?\[(\w[\w\s]*?)\]\s*\)',
            lambda m: f"MAX(source.{m.group(2).lower().replace(' ', '_')})",
            sql, flags=re.IGNORECASE
        )

        # COUNTROWS(VALUES(Table[Col])) -> COUNT(DISTINCT col). This is the
        # canonical DAX distinct-count idiom (equivalent to DISTINCTCOUNT). Must
        # run BEFORE the bare COUNTROWS(Table) rule. Only the COUNTROWS(VALUES(col))
        # wrapper is rewritten — a bare VALUES(col) or FILTER(VALUES(col), ...)
        # iterator is left as residual DAX for manual review, since a table-valued
        # distinct set has no scalar SQL equivalent inside a metric-view measure.
        sql, _n_tv = re.subn(
            r"\bCOUNTROWS\s*\(\s*VALUES\s*\(\s*'?(\w[\w\s]*?)'?\[(\w[\w\s]*?)\]\s*\)\s*\)",
            lambda m: f"COUNT(DISTINCT source.{m.group(2).lower().replace(' ', '_')})",
            sql, flags=re.IGNORECASE,
        )
        sql, _n_bc = re.subn(
            r"\bCOUNTROWS\s*\(\s*VALUES\s*\(\s*\[(\w[\w\s]*?)\]\s*\)\s*\)",
            lambda m: f"COUNT(DISTINCT source.{m.group(1).lower().replace(' ', '_')})",
            sql, flags=re.IGNORECASE,
        )
        if _n_tv or _n_bc:
            self._transformations.append("COUNTROWS_VALUES_to_COUNT_DISTINCT")

        # COUNTROWS(Table)
        sql = re.sub(
            r'\bCOUNTROWS\s*\(\s*\'?(\w[\w\s]*?)\'?\s*\)',
            'COUNT(*)',
            sql, flags=re.IGNORECASE
        )
        if 'COUNT(*)' in sql:
            self._transformations.append("COUNTROWS_translation")

        # COUNTBLANK(Table[Col])
        sql = re.sub(
            r'\bCOUNTBLANK\s*\(\s*\'?(\w[\w\s]*?)\'?\[(\w[\w\s]*?)\]\s*\)',
            lambda m: f"SUM(CASE WHEN source.{m.group(2).lower().replace(' ', '_')} IS NULL THEN 1 ELSE 0 END)",
            sql, flags=re.IGNORECASE
        )

        # COUNTA(Table[Col])
        sql = re.sub(
            r'\bCOUNTA\s*\(\s*\'?(\w[\w\s]*?)\'?\[(\w[\w\s]*?)\]\s*\)',
            lambda m: f"COUNT(source.{m.group(2).lower().replace(' ', '_')})",
            sql, flags=re.IGNORECASE
        )

        # Any remaining DISTINCTCOUNT(x) -> COUNT(DISTINCT x); VALUE(x) -> double(x).
        # Token swaps that preserve the existing parentheses.
        if re.search(r'\bDISTINCTCOUNT\s*\(', sql, re.IGNORECASE):
            sql = re.sub(r'\bDISTINCTCOUNT\s*\(', 'COUNT(DISTINCT ', sql, flags=re.IGNORECASE)
            self._transformations.append("DISTINCTCOUNT_to_COUNT_DISTINCT")
        if re.search(r'\bVALUE\s*\(', sql, re.IGNORECASE):
            sql = re.sub(r'\bVALUE\s*\(', 'double(', sql, flags=re.IGNORECASE)
            self._transformations.append("VALUE_to_double_cast")

        return sql

    def _pass_conditional(self, sql: str) -> str:
        """Translate conditional logic functions."""
        # DIVIDE(num, den [, alt]) -> COALESCE(num / NULLIF(den, 0), alt).
        # Balanced-paren parsing so nested MEASURE()/calls and their commas map
        # to the correct arguments (a non-greedy regex mis-splits them).
        sql = self._translate_divide(sql)

        # BLANK() -> NULL (DAX blank is SQL NULL)
        sql = re.sub(r'\bBLANK\s*\(\s*\)', 'NULL', sql, flags=re.IGNORECASE)

        # IF(cond, true, false) -> CASE WHEN ... THEN ... ELSE ... END
        # Uses balanced-parenthesis parsing (not a non-greedy regex) so nested
        # calls and commas inside arguments — IF(a=0, BLANK(), SUM(T[c])) — pick
        # the correct closing paren and never misplace the emitted END.
        sql = self._translate_if(sql)

        # SWITCH(TRUE(), cond1, result1, cond2, result2, ..., default)
        switch_true = re.compile(
            r'\bSWITCH\s*\(\s*TRUE\s*\(\s*\)\s*,\s*(.+)\)',
            re.IGNORECASE | re.DOTALL
        )
        m = switch_true.search(sql)
        if m:
            args_str = m.group(1)
            args = self._split_args(args_str)
            case_parts = ["CASE"]
            i = 0
            while i < len(args) - 1:
                case_parts.append(f" WHEN {args[i].strip()} THEN {args[i+1].strip()}")
                i += 2
            if len(args) % 2 == 1:
                case_parts.append(f" ELSE {args[-1].strip()}")
            case_parts.append(" END")
            sql = sql[:m.start()] + "".join(case_parts) + sql[m.end():]
            self._transformations.append("SWITCH_TRUE_to_CASE_WHEN")

        # SWITCH(expr, val1, result1, ..., default)
        switch_pattern = re.compile(
            r'\bSWITCH\s*\(\s*(.+?)\s*,\s*(.+)\)',
            re.IGNORECASE | re.DOTALL
        )
        m = switch_pattern.search(sql)
        if m and 'CASE' not in sql:  # Don't double-translate
            switch_expr = m.group(1).strip()
            args_str = m.group(2)
            args = self._split_args(args_str)
            case_parts = [f"CASE {switch_expr}"]
            i = 0
            while i < len(args) - 1:
                case_parts.append(f" WHEN {args[i].strip()} THEN {args[i+1].strip()}")
                i += 2
            if len(args) % 2 == 1:
                case_parts.append(f" ELSE {args[-1].strip()}")
            case_parts.append(" END")
            sql = sql[:m.start()] + "".join(case_parts) + sql[m.end():]
            self._transformations.append("SWITCH_to_CASE")

        # ISBLANK(expr) -> (expr IS NULL)
        sql = re.sub(
            r'\bISBLANK\s*\(\s*(.+?)\s*\)',
            lambda m: f"({m.group(1).strip()} IS NULL)",
            sql, flags=re.IGNORECASE
        )
        if 'IS NULL' in sql and 'ISBLANK' not in sql:
            self._transformations.append("ISBLANK_to_IS_NULL")

        # IFERROR(expr, alt) -> COALESCE(expr, alt). Databricks SQL has no bare
        # TRY() scalar; COALESCE covers the common null-guard intent.
        if re.search(r'\bIFERROR\s*\(', sql, re.IGNORECASE):
            sql = re.sub(
                r'\bIFERROR\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)',
                lambda m: f"COALESCE({m.group(1).strip()}, {m.group(2).strip()})",
                sql, flags=re.IGNORECASE
            )
            self._transformations.append("IFERROR_to_COALESCE")

        return sql

    def _translate_divide(self, sql: str) -> str:
        """Translate DAX DIVIDE(num, den[, alt]) via balanced-paren parsing."""
        div_re = re.compile(r'\bDIVIDE\s*\(', re.IGNORECASE)
        guard = 0
        while guard < 500:
            guard += 1
            m = div_re.search(sql)
            if not m:
                break
            open_idx = m.end() - 1
            close_idx = self._find_balanced_paren(sql, open_idx)
            if close_idx == -1:
                break
            args = self._split_top_level_args(sql[open_idx + 1:close_idx])
            if len(args) < 2:
                break
            num, den = args[0].strip(), args[1].strip()
            alt = args[2].strip() if len(args) >= 3 else "0"
            replacement = f"COALESCE(({num}) / NULLIF({den}, 0), {alt})"
            sql = sql[:m.start()] + replacement + sql[close_idx + 1:]
            self._transformations.append("DIVIDE_to_COALESCE_NULLIF")
        return sql

    def _translate_if(self, sql: str) -> str:
        """Translate DAX ``IF(cond, t, f)`` to ``CASE WHEN cond THEN t ELSE f END``.

        Scans for ``IF(`` (word-boundary guarded so ``IFERROR`` is untouched),
        extracts three top-level arguments via balanced-paren parsing, and
        rewrites in place. Re-scans until no ``IF(`` remains so nested IFs in the
        then/else branches are converted too.
        """
        if_re = re.compile(r'\bIF\s*\(', re.IGNORECASE)
        guard = 0
        while guard < 500:
            guard += 1
            m = if_re.search(sql)
            if not m:
                break
            open_idx = m.end() - 1
            close_idx = self._find_balanced_paren(sql, open_idx)
            if close_idx == -1:
                break
            args = self._split_top_level_args(sql[open_idx + 1:close_idx])
            if len(args) < 2:
                # Malformed / untranslatable IF - leave the expression as-is.
                break
            cond = args[0].strip()
            tval = args[1].strip()
            fval = args[2].strip() if len(args) >= 3 else "NULL"
            replacement = f"CASE WHEN {cond} THEN {tval} ELSE {fval} END"
            sql = sql[:m.start()] + replacement + sql[close_idx + 1:]
            self._transformations.append("IF_to_CASE_WHEN")
        return sql

    def _pass_logical(self, sql: str) -> str:
        """Translate logical operators."""
        # AND(a, b)
        sql = re.sub(
            r'\bAND\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)',
            lambda m: f"({m.group(1).strip()} AND {m.group(2).strip()})",
            sql, flags=re.IGNORECASE
        )

        # OR(a, b)
        sql = re.sub(
            r'\bOR\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)',
            lambda m: f"({m.group(1).strip()} OR {m.group(2).strip()})",
            sql, flags=re.IGNORECASE
        )

        # NOT(expr)
        sql = re.sub(
            r'\bNOT\s*\(\s*(.+?)\s*\)',
            lambda m: f"NOT({m.group(1).strip()})",
            sql, flags=re.IGNORECASE
        )

        # && -> AND, || -> OR
        if '&&' in sql:
            sql = RE_AND_OP.sub(' AND ', sql)
            self._transformations.append("logical_AND_operator")
        if '||' in sql:
            sql = RE_OR_OP.sub(' OR ', sql)
            self._transformations.append("logical_OR_operator")

        return sql

    def _pass_date_functions(self, sql: str) -> str:
        """Translate DAX date functions to Databricks SQL."""
        # TODAY() -> CURRENT_DATE()
        sql = re.sub(r'\bTODAY\s*\(\s*\)', 'CURRENT_DATE()', sql, flags=re.IGNORECASE)
        if 'CURRENT_DATE()' in sql:
            self._transformations.append("TODAY_to_CURRENT_DATE")

        # EOMONTH(date, offset) -> LAST_DAY(ADD_MONTHS(date, offset))
        sql = re.sub(
            r'\bEOMONTH\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)',
            lambda m: f"LAST_DAY(ADD_MONTHS({m.group(1).strip()}, {m.group(2).strip()}))",
            sql, flags=re.IGNORECASE
        )

        # EDATE(date, offset) -> ADD_MONTHS(date, offset)
        sql = re.sub(
            r'\bEDATE\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)',
            lambda m: f"ADD_MONTHS({m.group(1).strip()}, {m.group(2).strip()})",
            sql, flags=re.IGNORECASE
        )

        # DATEDIFF(start, end, unit) -> DATEDIFF(unit, start, end) [param swap]
        datediff_m = re.search(
            r'\bDATEDIFF\s*\(\s*(.+?)\s*,\s*(.+?)\s*,\s*(\w+)\s*\)',
            sql, flags=re.IGNORECASE
        )
        if datediff_m:
            start = datediff_m.group(1).strip()
            end = datediff_m.group(2).strip()
            unit = datediff_m.group(3).strip().upper()
            unit_map = {"DAY": "DAY", "MONTH": "MONTH", "YEAR": "YEAR",
                        "WEEK": "WEEK", "QUARTER": "QUARTER", "HOUR": "HOUR",
                        "MINUTE": "MINUTE", "SECOND": "SECOND"}
            sql_unit = unit_map.get(unit, "DAY")
            sql = sql[:datediff_m.start()] + f"DATEDIFF({sql_unit}, {start}, {end})" + sql[datediff_m.end():]
            self._transformations.append("DATEDIFF_param_reorder")

        # DATEADD(dateCol, offset, unit) -> DATE_ADD / ADD_MONTHS
        dateadd_m = re.search(
            r'\bDATEADD\s*\(\s*(.+?)\s*,\s*(-?\d+)\s*,\s*(\w+)\s*\)',
            sql, flags=re.IGNORECASE
        )
        if dateadd_m:
            date_col = dateadd_m.group(1).strip()
            offset = dateadd_m.group(2)
            unit = dateadd_m.group(3).strip().upper()
            if unit in ("MONTH", "QUARTER", "YEAR"):
                multiplier = {"MONTH": 1, "QUARTER": 3, "YEAR": 12}.get(unit, 1)
                total = int(offset) * multiplier
                replacement = f"ADD_MONTHS({date_col}, {total})"
            else:
                replacement = f"DATE_ADD({date_col}, {offset})"
            sql = sql[:dateadd_m.start()] + replacement + sql[dateadd_m.end():]
            self._transformations.append("DATEADD_translation")

        return sql

    def _pass_text_functions(self, sql: str) -> str:
        """Translate DAX text functions."""
        # CONCATENATE(a, b) -> CONCAT(a, b)
        sql = re.sub(
            r'\bCONCATENATE\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)',
            lambda m: f"CONCAT({m.group(1).strip()}, {m.group(2).strip()})",
            sql, flags=re.IGNORECASE
        )
        if 'CONCAT(' in sql:
            self._transformations.append("CONCATENATE_to_CONCAT")

        # CONTAINSSTRING(str, substr) -> (str LIKE '%' || substr || '%')
        sql = re.sub(
            r'\bCONTAINSSTRING\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)',
            lambda m: f"({m.group(1).strip()} LIKE CONCAT('%%', {m.group(2).strip()}, '%%'))",
            sql, flags=re.IGNORECASE
        )

        # FORMAT - common date patterns
        format_m = re.search(
            r'\bFORMAT\s*\(\s*(.+?)\s*,\s*"(.+?)"\s*\)',
            sql, flags=re.IGNORECASE
        )
        if format_m:
            expr = format_m.group(1).strip()
            fmt = format_m.group(2)
            # Map common DAX format strings to Databricks
            fmt_map = {
                "yyyy": "yyyy", "mm": "MM", "dd": "dd",
                "yyyy-mm-dd": "yyyy-MM-dd",
                "mm/dd/yyyy": "MM/dd/yyyy",
                "#,##0": "#,##0",
                "#,##0.00": "#,##0.00",
                "0%": "0%",
                "0.00%": "0.00%",
            }
            db_fmt = fmt_map.get(fmt.lower(), fmt)
            sql = sql[:format_m.start()] + f"DATE_FORMAT({expr}, '{db_fmt}')" + sql[format_m.end():]
            self._transformations.append("FORMAT_translation")

        return sql

    def _pass_lookups(self, sql: str, table_name: str) -> str:
        """Translate lookup functions (RELATED, SELECTEDVALUE)."""
        # RELATED(DimTable[Col]) -> join_alias.col
        def resolve_related(match):
            full = match.group(0)
            inner = match.group(1).strip()
            col_m = RE_TABLE_COL.search(inner)
            if col_m:
                tbl = col_m.group(1).strip("'").lower().replace(" ", "_")
                col = col_m.group(2).lower().replace(" ", "_")
                self._transformations.append("RELATED_to_join_ref")
                return f"{tbl}.{col}"
            return full

        sql = re.sub(
            r'\bRELATED\s*\(\s*(.+?)\s*\)',
            resolve_related,
            sql, flags=re.IGNORECASE
        )

        # SELECTEDVALUE(Table[Col], alt)
        sql = re.sub(
            r'\bSELECTEDVALUE\s*\(\s*\'?(\w[\w\s]*?)\'?\[(\w[\w\s]*?)\]\s*(?:,\s*(.+?))?\s*\)',
            lambda m: (
                f"COALESCE(source.{m.group(2).lower().replace(' ', '_')}, {m.group(3).strip()})"
                if m.group(3) else f"source.{m.group(2).lower().replace(' ', '_')}"
            ),
            sql, flags=re.IGNORECASE
        )

        # HASONEVALUE(Table[Col]) -> simplified
        sql = re.sub(
            r'\bHASONEVALUE\s*\(\s*\'?(\w[\w\s]*?)\'?\[(\w[\w\s]*?)\]\s*\)',
            lambda m: f"(COUNT(DISTINCT source.{m.group(2).lower().replace(' ', '_')}) = 1)",
            sql, flags=re.IGNORECASE
        )

        return sql

    def _pass_iterators(self, sql: str, table_name: str) -> str:
        """Translate iterator functions (SUMX, COUNTX, AVERAGEX)."""
        # SUMX(table, expr) -> SUM(expr) for simple cases
        sql = re.sub(
            r'\bSUMX\s*\(\s*\'?(\w[\w\s]*?)\'?\s*,\s*(.+?)\s*\)',
            lambda m: f"SUM({m.group(2).strip()})",
            sql, flags=re.IGNORECASE
        )
        if re.search(r'\bSUM\(', sql) and 'SUMX' not in sql:
            self._transformations.append("SUMX_to_SUM")

        # COUNTX
        sql = re.sub(
            r'\bCOUNTX\s*\(\s*\'?(\w[\w\s]*?)\'?\s*,\s*(.+?)\s*\)',
            lambda m: f"COUNT({m.group(2).strip()})",
            sql, flags=re.IGNORECASE
        )

        # AVERAGEX
        sql = re.sub(
            r'\bAVERAGEX\s*\(\s*\'?(\w[\w\s]*?)\'?\s*,\s*(.+?)\s*\)',
            lambda m: f"AVG({m.group(2).strip()})",
            sql, flags=re.IGNORECASE
        )

        return sql

    def _pass_table_col_refs(self, sql: str, table_name: str) -> str:
        """Translate remaining Table[Column] references to source.column."""
        sql = self._translate_table_col_refs(sql, table_name)
        return sql

    def _translate_table_col_refs(self, sql: str, table_name: str) -> str:
        """Replace Table[Column] patterns with source.col or join.col."""
        def replace_ref(match):
            raw = match.group(0)
            tbl_raw = match.group(1)
            # DAX unquoted table names cannot contain spaces; a captured table
            # token with a space that is NOT quoted means the regex greedily
            # swallowed preceding SQL keywords (e.g. "CASE WHEN [Measure]"). In
            # that case this is a bare [ref], not a Table[Column] — leave it
            # intact for the measure-ref pass to resolve.
            if not raw.lstrip().startswith("'"):
                stripped = tbl_raw.strip()
                last_word = stripped.split()[-1] if stripped.split() else stripped
                if " " in stripped or last_word.upper() in _SQL_KEYWORDS:
                    return raw
            # A qualified reference 'Table'[Name] where Name is a known measure
            # is a measure composition, not a fact column — emit MEASURE(`...`).
            measure_name = self._measure_names_norm.get(self._norm_name(match.group(2)))
            if measure_name:
                self._transformations.append("qualified_measure_ref")
                return f"MEASURE(`{measure_name}`)"
            tbl = tbl_raw.strip("'").lower().replace(" ", "_")
            col = match.group(2).lower().replace(" ", "_")
            fact_lower = table_name.lower().replace(" ", "_") if table_name else ""

            # If it's the fact table, use source.col
            if tbl == fact_lower or not fact_lower:
                return f"source.{col}"

            # Check join map
            key = f"{match.group(1).strip(chr(39))}.{match.group(2)}".lower()
            if key in self._join_map:
                return self._join_map[key]

            # Default: use table alias
            return f"{tbl}.{col}"

        result = RE_TABLE_COL.sub(replace_ref, sql)
        if result != sql:
            self._transformations.append("table_column_refs")
        return result

    def _to_filter_condition(self, condition: str, table_name: str) -> str:
        """Prepare a DAX filter predicate for a SQL FILTER (WHERE ...) clause.

        Translates Table[Column] references to source/join columns and converts
        DAX double-quoted string literals to SQL single-quoted literals.
        """
        condition = self._translate_table_col_refs(condition.strip(), table_name)
        # DAX uses double quotes for string literals; SQL uses single quotes.
        condition = re.sub(r'"([^"]*)"', r"'\1'", condition)
        # DAX BLANK() comparisons map to SQL NULL tests.
        condition = re.sub(r'\s*<>\s*BLANK\s*\(\s*\)', ' IS NOT NULL', condition, flags=re.IGNORECASE)
        condition = re.sub(r'\s*!=\s*BLANK\s*\(\s*\)', ' IS NOT NULL', condition, flags=re.IGNORECASE)
        condition = re.sub(r'\s*=\s*BLANK\s*\(\s*\)', ' IS NULL', condition, flags=re.IGNORECASE)
        condition = re.sub(r'\bBLANK\s*\(\s*\)', 'NULL', condition, flags=re.IGNORECASE)
        return condition

    def _pass_measure_refs(self, sql: str) -> str:
        """Translate [Measure Name] references to MEASURE(`slug`)."""
        def replace_measure(match):
            name = match.group(1)
            # Check if this measure has been resolved
            if name in self._known_measures:
                self._transformations.append(f"measure_ref_{name}")
                return f"MEASURE(`{name}`)"
            # Still reference it as MEASURE() for composition
            return f"MEASURE(`{name}`)"

        # Only replace [Name] patterns that look like measure refs
        # (not already inside source.xxx or alias.xxx)
        result = re.sub(r'(?<!source\.)(?<!\.)\[([^\]]+)\]', replace_measure, sql)
        if result != sql:
            self._transformations.append("measure_refs")
        return result

    def _pass_cleanup(self, sql: str) -> str:
        """Final cleanup pass."""
        # Remove double spaces
        sql = re.sub(r'\s+', ' ', sql).strip()
        # Fix double parens
        sql = re.sub(r'\(\(([^()]+)\)\)', r'(\1)', sql)
        return sql

    def _compute_status(self, sql: str, original: str) -> Tuple[str, int]:
        """Compute translation status and confidence score."""
        # Check for residual DAX patterns
        residual_dax = []
        dax_funcs = ['CALCULATE', 'CALCULATETABLE', 'ADDCOLUMNS', 'SELECTCOLUMNS',
                      'EARLIER', 'EARLIEST', 'VALUES', 'ALLEXCEPT', 'RELATEDTABLE']
        for func in dax_funcs:
            if re.search(rf'\b{func}\s*\(', sql, re.IGNORECASE):
                residual_dax.append(func)

        # Check for remaining Table[Col] refs
        if RE_TABLE_COL.search(sql):
            residual_dax.append("Table[Column]")

        if residual_dax:
            self._issues.append(f"Residual DAX patterns: {', '.join(residual_dax)}")

        # Compute confidence
        confidence = 100
        confidence -= len(self._issues) * 20
        confidence -= len(self._warnings) * 5
        if residual_dax:
            confidence -= len(residual_dax) * 15

        confidence = max(0, min(100, confidence))

        # Determine status
        if self._issues and confidence < 30:
            return "unsupported", confidence
        elif self._issues or confidence < 70:
            return "partial", confidence
        else:
            return "converted", confidence

    def _split_args(self, args_str: str) -> List[str]:
        """Split a comma-separated argument string respecting nested parens."""
        args = []
        depth = 0
        current = []
        for ch in args_str:
            if ch == '(':
                depth += 1
                current.append(ch)
            elif ch == ')':
                depth -= 1
                current.append(ch)
            elif ch == ',' and depth == 0:
                args.append(''.join(current).strip())
                current = []
            else:
                current.append(ch)
        if current:
            args.append(''.join(current).strip())
        return args
