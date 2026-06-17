"""Internal read-only SQL guard for future database query helpers."""

from __future__ import annotations

import re


ALLOWED_TABLES = {"api_keys", "users", "models", "providers", "provider_models"}
FORBIDDEN_KEYWORDS = {
    "alter",
    "create",
    "delete",
    "drop",
    "grant",
    "insert",
    "replace",
    "revoke",
    "truncate",
    "update",
}
TABLE_RE = re.compile(r"\b(?:from|join)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", re.IGNORECASE)


def ensure_readonly_sql(sql: str, allowed_tables: set[str] | None = None) -> None:
    if not sql or not sql.strip():
        raise ValueError("SQL must not be empty")

    normalized = re.sub(r"\s+", " ", sql.strip()).lower()
    if not normalized.startswith("select "):
        raise ValueError("Only SELECT statements are allowed")

    stripped = normalized.rstrip(";")
    if ";" in stripped:
        raise ValueError("Multiple SQL statements are not allowed")

    found_keywords = set(re.findall(r"\b[a-z_]+\b", stripped))
    forbidden = FORBIDDEN_KEYWORDS.intersection(found_keywords)
    if forbidden:
        raise ValueError(f"Forbidden SQL keyword: {sorted(forbidden)[0]}")

    allowed = allowed_tables or ALLOWED_TABLES
    tables = {match.group(1).lower() for match in TABLE_RE.finditer(sql)}
    unknown = tables - allowed
    if unknown:
        raise ValueError(f"Table is not allowed: {sorted(unknown)[0]}")
