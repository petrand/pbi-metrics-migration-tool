"""Row-Level Security (RLS) migration helper.

Power BI security roles encode row-level filters as DAX ``tablePermission``
predicates. Databricks Metric Views cannot express RLS, so it would otherwise be
silently dropped in migration. This module surfaces that gap and emits
best-effort Unity Catalog row-filter *scaffolding* for manual completion — the
DAX predicate itself is left as a comment for a human to translate to SQL.
"""

import re
from typing import List


def _sanitize(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip().lower())
    return re.sub(r"_+", "_", s).strip("_")


def roles_with_rls(roles: List[dict]) -> List[dict]:
    """Return only the roles that define at least one row-level filter."""
    return [r for r in (roles or []) if r.get("tablePermissions")]


def rls_notes(roles: List[dict]) -> List[str]:
    """Human-readable warnings enumerating RLS that will NOT auto-migrate."""
    notes: List[str] = []
    for r in roles_with_rls(roles):
        tables = ", ".join(sorted(r["tablePermissions"].keys()))
        notes.append(
            f"Role '{r['name']}' defines row-level security on [{tables}] that "
            "Metric Views cannot enforce — reproduce it as a Unity Catalog row "
            "filter (scaffolding generated; DAX predicate needs manual translation)."
        )
    return notes


def generate_rls_scaffolding(roles: List[dict], catalog: str, schema: str) -> str:
    """Emit UC row-filter scaffolding for every RLS predicate across roles.

    One filter function + ALTER TABLE per (table, role) predicate. The DAX
    predicate is preserved as a comment; the SQL body is a safe placeholder
    (``TRUE``) so the DDL is syntactically valid but denies nothing until a
    human translates the predicate.
    """
    scaffold = roles_with_rls(roles)
    if not scaffold:
        return ""

    out: List[str] = [
        "-- Row-Level Security scaffolding migrated from Power BI roles.",
        "-- Metric Views cannot enforce RLS; apply these UC row filters to the",
        "-- underlying tables. Replace the placeholder body with SQL equivalent",
        "-- to the DAX predicate shown in the comment, then grant role members.",
        "",
    ]
    for r in scaffold:
        role = r["name"]
        for table, dax in sorted(r["tablePermissions"].items()):
            fn = f"{catalog}.{schema}.rls_{_sanitize(role)}_{_sanitize(table)}"
            tbl_fqn = f"{catalog}.{schema}.{_sanitize(table)}"
            members = ", ".join(r.get("members", [])) or "(role members)"
            out.extend([
                f"-- Role '{role}' on table '{table}' — members: {members}",
                f"-- DAX predicate: {dax}",
                f"CREATE OR REPLACE FUNCTION {fn}()",
                "  RETURN TRUE;  -- TODO: translate the DAX predicate above to SQL",
                f"ALTER TABLE {tbl_fqn} SET ROW FILTER {fn} ON ();",
                "",
            ])
    return "\n".join(out)
