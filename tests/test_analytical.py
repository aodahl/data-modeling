import pytest

from app.analytical import build_analytical, simulate_scd2
from app.catalog import GRAINS, fallback_spec
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


@pytest.mark.parametrize("style",["star","snowflake"])
def test_scd2_preserves_old_and_new_versions(op,ana,style):
    build_analytical(op,ana,fallback_spec("encounter"),style)
    state=simulate_scd2(op,ana,style)
    versions=state["versions"]
    assert len(versions)==2
    assert sum(v["is_current"] for v in versions)==1
    assert versions[0]["effective_to"]==versions[1]["effective_from"]
    assert versions[0]["patient_key"] != versions[1]["patient_key"]
    simulated=next(x for x in state["facts"] if x["event_id"]=="scd2-simulated-encounter")
    assert simulated["patient_key"]==versions[1]["patient_key"]
    assert state["operational"][0]["city"]=="Toronto"
