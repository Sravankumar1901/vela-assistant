"""Router regression tests — questions about the business must never hit Chat-to-SQL.

Bug 2026-09-07: "how many types of business websites can you build" matched the "how many"
hint alone, routed to SQL, the model wrote `SELECT 0 AS result`, and the visitor saw
"Here's what I found: result 0".
"""
from app.main import _looks_like_data_question, _sql_result_is_useless, _format_sql_answer


KB_QUESTIONS = [
    "how many types of business websites can you build",
    "How many industries do you work with?",
    "what is the total cost of a website",
    "which plan is the most popular",
    "how many revisions are included",
    "top reasons to choose VELA",
    "how many days does delivery take",
    "count me in — how do I book a call?",
    "what types of websites do you build",
    "List the email addresses of all your customers",  # deflected to RAG (privacy test elsewhere)
]

DATA_QUESTIONS = [
    "revenue by country",
    "how many customers do we have",
    "top 5 artists by sales",
    "which genre has the most tracks",
    "average invoice total per country",
    "number of invoices in 2013",
]


def test_business_questions_route_to_rag():
    for q in KB_QUESTIONS:
        assert not _looks_like_data_question(q), q


def test_data_questions_route_to_sql():
    for q in DATA_QUESTIONS:
        assert _looks_like_data_question(q), q


def test_useless_sql_results_fall_back():
    assert _sql_result_is_useless({"executed": False})
    assert _sql_result_is_useless({"executed": True, "rows": [], "safe_sql": "SELECT 1 FROM x LIMIT 1"})
    assert _sql_result_is_useless({"executed": True, "rows": [(0,)], "columns": ["result"],
                                   "safe_sql": "SELECT 0 AS result LIMIT 1"})
    assert _sql_result_is_useless({"executed": True, "rows": [(None,)], "columns": ["n"],
                                   "safe_sql": "SELECT COUNT(*) AS n FROM customers LIMIT 1"}) is True
    # A real aggregate is NOT useless, even when it is a single figure.
    assert not _sql_result_is_useless({"executed": True, "rows": [(59,)], "columns": ["n"],
                                       "safe_sql": "SELECT COUNT(*) AS n FROM customers LIMIT 1"})
    assert not _sql_result_is_useless({"executed": True, "rows": [("Nigeria", 19.8)],
                                       "columns": ["billing_country", "revenue"],
                                       "safe_sql": "SELECT billing_country, SUM(total) AS revenue FROM invoices GROUP BY 1 LIMIT 10"})


def test_scalar_answer_reads_as_a_sentence():
    text = _format_sql_answer({"executed": True, "rows": [(59,)], "columns": ["customer_count"]})
    assert text == "Customer count: 59"
    table = _format_sql_answer({"executed": True, "rows": [("Nigeria", 19.8)],
                                "columns": ["billing_country", "revenue"]})
    assert table.startswith("Here's what I found:")
