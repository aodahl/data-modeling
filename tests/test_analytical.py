import pytest

from app.analytical import build_analytical
from app.catalog import GRAINS, fallback_questions, fallback_spec
from app.main import fallback_query_pair
from app.query import execute_select


@pytest.mark.parametrize("grain_id", list(GRAINS))
def test_each_grain_reconciles(op,ana,grain_id):
    spec=fallback_spec(grain_id)
    result=build_analytical(op,ana,spec,"star")
    g=GRAINS[grain_id]
    expected=op.execute(f"SELECT count(*) FROM {g.source_table}").fetchone()[0]
    assert result.fact_rows == expected
    assert ana.execute(f"SELECT count(*) FROM {g.fact_table}").fetchone()[0] == expected
    assert ana.execute(f"SELECT sum(event_count) FROM {g.fact_table}").fetchone()[0] == expected
    assert ana.execute("PRAGMA foreign_key_check").fetchall() == []


def test_star_and_snowflake_preserve_fact_results(op,ana):
    spec=fallback_spec("encounter")
    build_analytical(op,ana,spec,"star")
    star=ana.execute("SELECT d.year,sum(f.event_count) FROM fact_encounter f JOIN dim_date d ON d.date_key=f.date_key GROUP BY d.year ORDER BY d.year").fetchall()
    assert "state" in [r[1] for r in ana.execute("PRAGMA table_info('dim_patient')")]
    build_analytical(op,ana,spec,"snowflake")
    snow=ana.execute("SELECT d.year,sum(f.event_count) FROM fact_encounter f JOIN dim_date d ON d.date_key=f.date_key GROUP BY d.year ORDER BY d.year").fetchall()
    assert star == snow
    assert "geography_key" in [r[1] for r in ana.execute("PRAGMA table_info('dim_patient')")]
    assert ana.execute("SELECT count(*) FROM dim_geography").fetchone()[0] > 0


@pytest.mark.parametrize("grain_id",list(GRAINS))
def test_operational_and_analytical_year_aggregates_match(op,ana,grain_id):
    spec=fallback_spec(grain_id)
    build_analytical(op,ana,spec,"star")
    pair=fallback_query_pair("events_by_year",spec,"star")
    assert execute_select(op,pair.operational_sql).rows == execute_select(ana,pair.analytical_sql).rows
    assert "\nFROM " in pair.operational_sql
    assert "\nFROM " in pair.analytical_sql


def test_patient_dimension_has_one_row_per_patient(op,ana):
    build_analytical(op,ana,fallback_spec("encounter"),"star")
    columns = [row[1] for row in ana.execute("PRAGMA table_info('dim_patient')")]
    assert columns == ["patient_key", "patient_id", "given_name", "family_name", "gender", "birth_date", "marital_status", "line", "city", "state", "postal_code", "country"]
    assert ana.execute("SELECT count(*) FROM dim_patient").fetchone()[0] == ana.execute("SELECT count(DISTINCT patient_id) FROM dim_patient").fetchone()[0]
    assert {row[0] for row in ana.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"dim_patient", "dim_date", "dim_clinical_code", "dim_organization", "fact_encounter"}


@pytest.mark.parametrize("grain_id", list(GRAINS))
@pytest.mark.parametrize("style", ["star", "snowflake"])
def test_every_grain_has_five_distinct_reconciled_questions(op,ana,grain_id,style):
    spec = fallback_spec(grain_id)
    build_analytical(op,ana,spec,style)
    questions = fallback_questions(grain_id)
    assert len(questions) == 5
    assert len({question.id for question in questions}) == 5
    for question in questions:
        pair = fallback_query_pair(question.id,spec,style)
        assert execute_select(op,pair.operational_sql).rows == execute_select(ana,pair.analytical_sql).rows
