import pytest

from app.query import execute_select


def test_select_and_cte_are_allowed(op):
    result=execute_select(op,"WITH totals AS (SELECT count(*) n FROM patient) SELECT n FROM totals")
    assert result.rows==[[10]]
    assert result.tables==["patient","totals"] or result.tables==["patient"]


@pytest.mark.parametrize("sql",[
    "DELETE FROM patient",
    "UPDATE patient SET gender='x'",
    "DROP TABLE patient",
    "PRAGMA table_info(patient)",
    "ATTACH DATABASE '/tmp/x' AS x",
    "SELECT * FROM patient; SELECT * FROM encounter",
    "SELECT * FROM imaginary",
    "SELECT readfile('/etc/passwd')",
])
def test_unsafe_sql_is_rejected(op,sql):
    with pytest.raises(ValueError): execute_select(op,sql)


def test_results_are_limited(op):
    result=execute_select(op,"SELECT a.id,b.id FROM observation a CROSS JOIN observation b",row_limit=20)
    assert len(result.rows)==20
    assert result.truncated

