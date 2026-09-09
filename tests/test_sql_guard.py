"""Offline unit tests for the Chat-to-SQL safety gate. No DB / keys needed."""
import pytest

from app.core import sql_guard
from app.core.sql_guard import SqlRejected


def test_accepts_plain_select_and_forces_limit():
    out = sql_guard.validate_and_fix("SELECT customer_id, country FROM customers")
    assert out.lower().startswith("select")
    assert "limit" in out.lower()  # LIMIT injected


def test_accepts_aggregate_join_select():
    sql = (
        "SELECT c.customer_id, SUM(i.total) AS revenue "
        "FROM customers c JOIN invoices i ON i.customer_id = c.customer_id "
        "GROUP BY c.customer_id ORDER BY revenue DESC LIMIT 5"
    )
    out = sql_guard.validate_and_fix(sql)
    assert "limit" in out.lower()


def test_clamps_oversized_limit():
    out = sql_guard.validate_and_fix("SELECT total FROM invoices LIMIT 100000")
    # SQL_ROW_CAP default is 100 -> clamped
    assert "100000" not in out


def test_rejects_drop():
    with pytest.raises(SqlRejected):
        sql_guard.validate_and_fix("DROP TABLE customers")


def test_rejects_delete():
    with pytest.raises(SqlRejected):
        sql_guard.validate_and_fix("DELETE FROM customers WHERE customer_id = 1")


def test_rejects_update():
    with pytest.raises(SqlRejected):
        sql_guard.validate_and_fix("UPDATE customers SET country = 'X'")


def test_rejects_insert():
    with pytest.raises(SqlRejected):
        sql_guard.validate_and_fix("INSERT INTO customers (customer_id) VALUES (99)")


def test_rejects_stacked_statements():
    with pytest.raises(SqlRejected):
        sql_guard.validate_and_fix("SELECT total FROM invoices; DROP TABLE invoices")


def test_rejects_stacked_delete():
    with pytest.raises(SqlRejected):
        sql_guard.validate_and_fix("SELECT 1 FROM invoices; DELETE FROM invoices")


def test_rejects_table_not_in_allowlist():
    with pytest.raises(SqlRejected):
        sql_guard.validate_and_fix("SELECT api_key_hash FROM tenants")


def test_rejects_column_not_in_allowlist():
    with pytest.raises(SqlRejected):
        sql_guard.validate_and_fix("SELECT password FROM customers")


def test_rejects_select_into():
    with pytest.raises(SqlRejected):
        sql_guard.validate_and_fix("SELECT total INTO shadow FROM invoices")


def test_rejects_garbage():
    with pytest.raises(SqlRejected):
        sql_guard.validate_and_fix("not sql at all !!!")


# ---- PII column hardening (audit: "Chat-to-SQL exposes a customer email column") ----

@pytest.mark.parametrize("sql", [
    "SELECT email FROM customers",
    "SELECT c.email FROM customers c",
    "SELECT customers.email FROM customers",
    "SELECT first_name, last_name FROM customers",
    "SELECT country FROM customers WHERE email LIKE '%@%'",
    "SELECT country FROM customers ORDER BY email",
    "SELECT lower(email) FROM customers",
    "SELECT i.total FROM invoices i JOIN customers c ON c.customer_id = i.customer_id AND c.email <> ''",
    "SELECT country FROM (SELECT email, country FROM customers) sub",
])
def test_rejects_pii_columns_however_referenced(sql):
    with pytest.raises(SqlRejected, match="personal data"):
        sql_guard.validate_and_fix(sql)


@pytest.mark.parametrize("sql", [
    # Naming the output alias the same as the column used to skip the allow-list entirely.
    "SELECT email AS email FROM customers",
    "SELECT lower(email) AS email FROM customers",
    "SELECT password AS password FROM customers",
])
def test_rejects_alias_shadowing_bypass(sql):
    with pytest.raises(SqlRejected, match="column not allowed"):
        sql_guard.validate_and_fix(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM customers",
    "SELECT c.* FROM customers c",
    "SELECT customers.* FROM customers",
    "SELECT country, i.* FROM customers c JOIN invoices i ON i.customer_id = c.customer_id",
])
def test_rejects_wildcard_projection(sql):
    with pytest.raises(SqlRejected, match="wildcard"):
        sql_guard.validate_and_fix(sql)


def test_accepts_count_star():
    out = sql_guard.validate_and_fix("SELECT COUNT(*) FROM customers")
    assert "count(*)" in out.lower()


def test_accepts_output_alias_in_order_and_group_by():
    sql = (
        "SELECT country, COUNT(*) AS n, SUM(i.total) AS revenue "
        "FROM customers c JOIN invoices i ON i.customer_id = c.customer_id "
        "GROUP BY country ORDER BY revenue DESC, n"
    )
    out = sql_guard.validate_and_fix(sql)
    assert "revenue" in out.lower() and "limit" in out.lower()


def test_pii_columns_absent_from_allowlist():
    for col in ("first_name", "last_name", "email"):
        assert col not in sql_guard.ALLOWED_COLUMNS
        assert col in sql_guard.DENIED_COLUMNS
    assert not (sql_guard.ALLOWED_COLUMNS & sql_guard.DENIED_COLUMNS)
