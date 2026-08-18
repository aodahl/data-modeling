from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .catalog import GRAINS, validate_spec
from .models import DimensionalModelSpec


@dataclass
class BuildResult:
    spec: DimensionalModelSpec
    style: str
    ddl: str
    etl: str
    fact_rows: int


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def build_analytical(op: sqlite3.Connection, ana: sqlite3.Connection, spec: DimensionalModelSpec, style: str) -> BuildResult:
    spec = validate_spec(spec)
    if style not in {"star", "snowflake"}:
        raise ValueError("Style must be star or snowflake")
    ana.commit()
    ana.execute("PRAGMA foreign_keys=OFF")
    for name in [r[0] for r in ana.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
        ana.execute(f'DROP TABLE IF EXISTS "{name}"')
    ana.execute("PRAGMA foreign_keys=ON")
    g = GRAINS[spec.grain_id]
    ddl: list[str] = []

    if style == "snowflake":
        sql = "CREATE TABLE dim_geography(geography_key INTEGER PRIMARY KEY, city TEXT, state TEXT, country TEXT, UNIQUE(city,state,country))"
        ana.execute(sql); ddl.append(sql)
        geos = _rows(op, "SELECT DISTINCT city,state,country FROM patient_address WHERE is_current=1")
        ana.executemany("INSERT INTO dim_geography(city,state,country) VALUES(?,?,?)", [tuple(x) for x in geos])
        sql = "CREATE TABLE dim_patient(patient_key INTEGER PRIMARY KEY, patient_id TEXT NOT NULL UNIQUE, given_name TEXT, family_name TEXT, gender TEXT, birth_date TEXT, marital_status TEXT, geography_key INTEGER REFERENCES dim_geography(geography_key), postal_code TEXT)"
    else:
        sql = "CREATE TABLE dim_patient(patient_key INTEGER PRIMARY KEY, patient_id TEXT NOT NULL UNIQUE, given_name TEXT, family_name TEXT, gender TEXT, birth_date TEXT, marital_status TEXT, line TEXT, city TEXT, state TEXT, postal_code TEXT, country TEXT)"
    ana.execute(sql); ddl.append(sql)
    patients = _rows(op, "SELECT p.*,a.line,a.city,a.state,a.postal_code,a.country FROM patient p LEFT JOIN patient_address a ON a.patient_id=p.id AND a.is_current=1")
    if style == "snowflake":
        for p in patients:
            geo = ana.execute("SELECT geography_key FROM dim_geography WHERE city IS ? AND state IS ? AND country IS ?", (p["city"], p["state"], p["country"])).fetchone()
            ana.execute("INSERT INTO dim_patient(patient_id,given_name,family_name,gender,birth_date,marital_status,geography_key,postal_code) VALUES(?,?,?,?,?,?,?,?)", (p["id"], p["given_name"], p["family_name"], p["gender"], p["birth_date"], p["marital_status"], geo[0] if geo else None, p["postal_code"]))
    else:
        ana.executemany("INSERT INTO dim_patient(patient_id,given_name,family_name,gender,birth_date,marital_status,line,city,state,postal_code,country) VALUES(?,?,?,?,?,?,?,?,?,?,?)", [(p["id"],p["given_name"],p["family_name"],p["gender"],p["birth_date"],p["marital_status"],p["line"],p["city"],p["state"],p["postal_code"],p["country"]) for p in patients])

    sql = "CREATE TABLE dim_date(date_key TEXT PRIMARY KEY, full_date TEXT NOT NULL, year INTEGER, quarter INTEGER, month INTEGER, day INTEGER)"
    ana.execute(sql); ddl.append(sql)
    date_sql = f"SELECT DISTINCT {g.date_expr} d FROM {g.source_table} WHERE {g.date_expr} IS NOT NULL UNION SELECT DISTINCT substr(start_at,1,10) FROM encounter WHERE start_at IS NOT NULL"
    for (date,) in _rows(op, date_sql):
        dt = datetime.fromisoformat(date)
        ana.execute("INSERT OR IGNORE INTO dim_date VALUES(?,?,?,?,?,?)", (date,date,dt.year,(dt.month-1)//3+1,dt.month,dt.day))

    if "clinical_code" in spec.dimensions:
        if style == "snowflake":
            sql = "CREATE TABLE dim_code_system(code_system_key INTEGER PRIMARY KEY, system_name TEXT UNIQUE); CREATE TABLE dim_clinical_code(clinical_code_key INTEGER PRIMARY KEY, code TEXT, display TEXT, code_system_key INTEGER REFERENCES dim_code_system(code_system_key), UNIQUE(code,display))"
            ana.executescript(sql); ddl.append(sql)
            ana.execute("INSERT INTO dim_code_system(system_name) VALUES('FHIR source coding')")
        else:
            sql = "CREATE TABLE dim_clinical_code(clinical_code_key INTEGER PRIMARY KEY, code TEXT, display TEXT, code_system TEXT, UNIQUE(code,display))"
            ana.execute(sql); ddl.append(sql)
        codes = _rows(op, f"SELECT DISTINCT {g.code_expr} code,{g.code_display_expr} display FROM {g.source_table} WHERE {g.code_expr} IS NOT NULL")
        if style == "snowflake":
            ana.executemany("INSERT INTO dim_clinical_code(code,display,code_system_key) VALUES(?,?,1)", [tuple(r) for r in codes])
        else:
            ana.executemany("INSERT INTO dim_clinical_code(code,display,code_system) VALUES(?,?,'FHIR source coding')", [tuple(r) for r in codes])

    if "organization" in spec.dimensions:
        sql = "CREATE TABLE dim_organization(organization_key INTEGER PRIMARY KEY, organization_id TEXT UNIQUE, name TEXT, type_code TEXT)"
        ana.execute(sql); ddl.append(sql)
        ana.executemany("INSERT INTO dim_organization(organization_id,name,type_code) VALUES(?,?,?)", [tuple(r) for r in _rows(op,"SELECT id,name,type_code FROM organization")])

    columns = ["fact_key INTEGER PRIMARY KEY", "event_id TEXT UNIQUE", "patient_key INTEGER REFERENCES dim_patient(patient_key)", "date_key TEXT REFERENCES dim_date(date_key)"]
    if "clinical_code" in spec.dimensions: columns.append("clinical_code_key INTEGER REFERENCES dim_clinical_code(clinical_code_key)")
    if "organization" in spec.dimensions: columns.append("organization_key INTEGER REFERENCES dim_organization(organization_key)")
    if "event_count" in spec.measures: columns.append("event_count INTEGER NOT NULL")
    if "amount" in spec.measures: columns.append("amount REAL")
    if "numeric_value" in spec.measures: columns.extend(["numeric_value REAL", "unit TEXT"])
    sql = f"CREATE TABLE {g.fact_table}({', '.join(columns)})"
    ana.execute(sql); ddl.append(sql)

    select = [f"{g.event_id} event_id", f"{g.patient_id} patient_id", f"{g.date_expr} date_key"]
    if "clinical_code" in spec.dimensions: select.extend([f"{g.code_expr} code", f"{g.code_display_expr} display"])
    if "organization" in spec.dimensions: select.append(f"{g.organization_expr} organization_id")
    if "amount" in spec.measures: select.append(f"{g.amount_expr} amount")
    if "numeric_value" in spec.measures: select.extend([f"{g.value_expr} numeric_value", f"{g.unit_expr} unit"])
    source_sql = f"SELECT {', '.join(select)} FROM {g.source_table}"
    source_rows = _rows(op, source_sql)
    insert_cols = ["event_id","patient_key","date_key"]
    if "clinical_code" in spec.dimensions: insert_cols.append("clinical_code_key")
    if "organization" in spec.dimensions: insert_cols.append("organization_key")
    if "event_count" in spec.measures: insert_cols.append("event_count")
    if "amount" in spec.measures: insert_cols.append("amount")
    if "numeric_value" in spec.measures: insert_cols.extend(["numeric_value","unit"])
    for r in source_rows:
        vals: list[object] = [r["event_id"], ana.execute("SELECT patient_key FROM dim_patient WHERE patient_id=?", (r["patient_id"],)).fetchone()[0], r["date_key"]]
        if "clinical_code" in spec.dimensions:
            found = ana.execute("SELECT clinical_code_key FROM dim_clinical_code WHERE code IS ? AND display IS ?",(r["code"],r["display"])).fetchone(); vals.append(found[0] if found else None)
        if "organization" in spec.dimensions:
            found = ana.execute("SELECT organization_key FROM dim_organization WHERE organization_id=?",(r["organization_id"],)).fetchone(); vals.append(found[0] if found else None)
        if "event_count" in spec.measures: vals.append(1)
        if "amount" in spec.measures: vals.append(r["amount"])
        if "numeric_value" in spec.measures: vals.extend([r["numeric_value"],r["unit"]])
        ana.execute(f"INSERT INTO {g.fact_table}({','.join(insert_cols)}) VALUES({','.join('?' for _ in vals)})", vals)

    ana.commit()
    etl = source_sql + ";\n\n-- Trusted compiler resolves natural IDs to surrogate keys and executes:\n" + f"INSERT INTO {g.fact_table}({','.join(insert_cols)}) VALUES({','.join('?' for _ in insert_cols)});"
    return BuildResult(spec, style, ";\n\n".join(ddl)+";", etl, len(source_rows))
