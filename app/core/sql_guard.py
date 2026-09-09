"""Hard safety gate for Chat-to-SQL — runs BEFORE any execution.

Defense in depth (all must hold):
  1. Parse with sqlglot (Postgres dialect). Reject if it doesn't parse.
  2. Reject multiple statements / stray ';' (only a single statement is allowed).
  3. Reject anything that is not a single SELECT (no INSERT/UPDATE/DELETE/DROP/DDL/etc.,
     no SELECT ... INTO).
  4. Enforce a TABLE allow-list and a COLUMN allow-list (Chinook subset), plus:
       - a PII COLUMN deny-list that is checked with NO exemptions (customer name / email …),
       - no wildcard projections (SELECT * / t.*) — COUNT(*) is still fine,
       - output-alias exemption only where Postgres actually resolves aliases (ORDER BY /
         GROUP BY), so "SELECT email AS email" cannot shadow its way past the allow-list.
  5. Force/clamp a LIMIT (<= SQL_ROW_CAP).
The regenerated SQL from the AST is what gets executed (this strips comments and any
injected trailing statements). Execution additionally runs on a read-only role with a
statement timeout + row cap (see chatsql.py).
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from .config import settings

DIALECT = "postgres"

# Chinook subset exposed to Chat-to-SQL. Extend as you add tables/columns.
ALLOWED_TABLES = {
    "artists", "albums", "tracks", "genres",
    "customers", "invoices", "invoice_items",
}

ALLOWED_COLUMNS = {
    # artists
    "artist_id", "name",
    # albums
    "album_id", "title",
    # tracks
    "track_id", "unit_price", "milliseconds",
    # genres
    "genre_id",
    # customers — ONLY the surrogate key and coarse country. Name/email are PII: see DENIED_COLUMNS.
    "customer_id", "country",
    # invoices
    "invoice_id", "invoice_date", "total", "billing_country",
    # invoice_items
    "invoice_line_id", "quantity",
}

# Person-identifying columns that must NEVER be selectable through Chat-to-SQL, whatever table
# they sit on and however they are referenced (qualified, aliased, wrapped in a function).
# Checked with no exemptions, before the allow-list, so a future "extend the allow-list" edit
# can't silently re-expose them. Anonymous widget visitors reach this path (audit finding:
# "Chat-to-SQL exposes a customer email column to anonymous visitors").
DENIED_COLUMNS = {
    "first_name", "last_name", "full_name", "email", "phone", "fax", "address",
    "city", "state", "postal_code", "company", "support_rep_id",
    # names used by VELA's own PII stores (leads / clients) — never reachable, but belt-and-braces
    "contact", "contact_name", "contact_email", "contact_phone",
}

# Statement types that must never appear.
_FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Command, exp.Merge, exp.Into,
)

# Only these SQL functions may appear. ANYTHING else is rejected — including arbitrary
# functions that execute SQL or touch the system (query_to_xml, dblink, pg_read_file,
# lo_import, pg_sleep) and table-valued functions used in FROM (generate_series, ...).
# This is what closes the "run arbitrary SQL / read files / DoS via a function" bypass
# of the table allow-list (audit finding C1).
ALLOWED_FUNCTIONS = {
    # aggregates
    "count", "sum", "avg", "min", "max", "stddev", "variance", "string_agg",
    # math / numeric
    "round", "abs", "ceil", "ceiling", "floor", "mod", "power", "sqrt", "trunc",
    # null / conditional
    "coalesce", "nullif", "greatest", "least",
    # string
    "lower", "upper", "length", "trim", "ltrim", "rtrim", "substring", "substr",
    "concat", "replace", "left", "right", "initcap",
    # date / time
    "extract", "date_part", "date_trunc", "to_char", "now", "current_date",
    "current_timestamp", "age",
    # casting & expressions (sqlglot canonical names: CASE->case, string_agg->group_concat,
    # -> / ->> -> json_extract / json_extract_scalar). These are safe SQL, not callable
    # system functions — allow-listing them avoids over-blocking legit analytics.
    "cast", "case", "if", "group_concat", "json_extract", "json_extract_scalar",
    # window / ranking
    "rank", "dense_rank", "row_number", "ntile", "lag", "lead",
    "first_value", "last_value",
}


def _func_name(node) -> str:
    """Canonical lowercase name of a function node (typed OR anonymous/unknown)."""
    if isinstance(node, exp.Anonymous):
        return str(node.this or "").lower()
    try:
        return (node.sql_name() or "").lower()
    except Exception:
        return type(node).__name__.lower()


def _is_alias_reference(col: exp.Column) -> bool:
    """True only when `col` sits directly inside a SELECT-level ORDER BY or GROUP BY — the
    places where Postgres resolves a query-defined output alias (e.g. "ORDER BY revenue").

    Anywhere else (the projection, WHERE, JOIN ON, function arguments, window ORDER BY) the
    name is a real column and must go through the allow-list. Without this boundary,
    "SELECT email AS email FROM customers" was skipped as an alias reference.
    """
    node = col.parent
    while node is not None:
        if isinstance(node, exp.Group):
            return True
        if isinstance(node, exp.Order):
            return isinstance(node.parent, exp.Select)  # not an OVER (ORDER BY …)
        if isinstance(node, (exp.Select, exp.Subquery, exp.Func, exp.Window)):
            return False
        node = node.parent
    return False


class SqlRejected(ValueError):
    """Raised when a candidate SQL fails the safety gate."""


def validate_and_fix(sql: str) -> str:
    """Return a safe, LIMIT-bounded single SELECT, or raise SqlRejected.

    The returned string is regenerated from the parsed AST — the original raw text
    (with any comments or extra statements) is discarded.
    """
    if not sql or not sql.strip():
        raise SqlRejected("empty SQL")

    # (2) Multiple statements / stray semicolons.
    try:
        statements = sqlglot.parse(sql, read=DIALECT)
    except Exception as e:
        raise SqlRejected(f"parse error: {e}")
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SqlRejected("only a single statement is allowed")

    tree = statements[0]

    # (3) Must be a SELECT at the root (a leading WITH/CTE still yields a Select root).
    root = tree
    if not isinstance(root, exp.Select):
        # e.g. exp.Drop, exp.Insert, exp.Union, exp.Command...
        raise SqlRejected(f"only SELECT is allowed, got {type(root).__name__}")

    # No forbidden node anywhere in the tree (SELECT INTO, DML in a CTE, etc.).
    for node_type in _FORBIDDEN:
        if root.find(node_type) is not None:
            raise SqlRejected(f"forbidden statement element: {node_type.__name__}")

    # (3b) Function allow-list — reject ANY function not explicitly permitted. This blocks
    # arbitrary-SQL/file/DoS functions (query_to_xml, dblink, pg_read_file, pg_sleep, ...)
    # AND table-valued functions in FROM (generate_series, ...), which the table allow-list
    # alone does not catch (they are exp.Func nodes, not exp.Table). Closes finding C1.
    for fn in root.find_all(exp.Func):
        name = _func_name(fn)
        if name and name not in ALLOWED_FUNCTIONS:
            raise SqlRejected(f"function not allowed: {name}")

    # (3c) DoS guards. LIMIT injection does NOT bound aggregates/sorts over a huge input,
    # so block the constructs that build one: recursive CTEs and self-cross-joins. (Execution
    # also runs under statement_timeout on the read-only role — this is defense in depth.)
    with_node = root.args.get("with")
    if with_node is not None and with_node.args.get("recursive"):
        raise SqlRejected("recursive CTEs are not allowed")
    from collections import Counter
    _tbl_counts = Counter((t.name or "").lower() for t in root.find_all(exp.Table))
    if any(cnt > 3 for cnt in _tbl_counts.values()):
        raise SqlRejected("too many self-joins (possible cross-product)")

    # (4a) Table allow-list — name AND schema/catalog qualifier, so pg_catalog.artists,
    # secret_schema.artists, and otherdb.public.customers are all rejected (finding: the
    # unqualified-name-only check let any relation named like a Chinook table through).
    for table in root.find_all(exp.Table):
        tname = (table.name or "").lower()
        if tname and tname not in ALLOWED_TABLES:
            raise SqlRejected(f"table not allowed: {table.name}")
        schema = (table.db or "").lower()
        if schema and schema != "public":
            raise SqlRejected(f"schema not allowed: {table.db}")
        if table.catalog:
            raise SqlRejected(f"cross-database reference not allowed: {table.catalog}")

    # (4b) No wildcard projections. "SELECT *" / "SELECT c.*" would return every column of
    # the table — including PII columns that are absent from the allow-list — so the column
    # check below could never see them. COUNT(*) (a Star directly under an aggregate) stays legal.
    for star in root.find_all(exp.Star):
        if not isinstance(star.parent, exp.AggFunc):
            raise SqlRejected("wildcard projection not allowed: name each column explicitly")

    # Collect query-defined OUTPUT aliases only (e.g. "SUM(total) AS revenue") so ORDER BY /
    # GROUP BY references to them aren't mistaken for real columns. TABLE aliases are NOT added:
    # doing so let "SELECT api_key_hash FROM artists AS api_key_hash" skip the column allow-list.
    alias_names = set()
    for a in root.find_all(exp.Alias):
        if a.alias:
            alias_names.add(a.alias.lower())

    # (4c) Column deny-list + allow-list.
    for col in root.find_all(exp.Column):
        cname = (col.name or "").lower()
        if cname in ("", "*"):
            continue  # bare/wildcard handled by the Star check above
        # PII hard block — NO exemptions (alias shadowing, qualification, function wrapping).
        if cname in DENIED_COLUMNS:
            raise SqlRejected(f"column not allowed (personal data): {col.name}")
        # Output-alias references are exempt ONLY inside ORDER BY / GROUP BY; a column that is
        # merely *named like* an alias elsewhere ("SELECT email AS email") is a real column.
        if cname in alias_names and _is_alias_reference(col):
            continue
        if cname not in ALLOWED_COLUMNS:
            raise SqlRejected(f"column not allowed: {col.name}")

    # (5) Force / clamp LIMIT.
    cap = settings.SQL_ROW_CAP
    limit = root.args.get("limit")
    if limit is None:
        root = root.limit(cap)
    else:
        try:
            current = int(limit.expression.this)
            if current > cap:
                root = root.limit(cap)
        except Exception:
            root = root.limit(cap)

    return root.sql(dialect=DIALECT)
