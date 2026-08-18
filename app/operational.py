from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE patient(id TEXT PRIMARY KEY, given_name TEXT, family_name TEXT, gender TEXT, birth_date TEXT, marital_status TEXT);
CREATE TABLE patient_address(id INTEGER PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patient(id), line TEXT, city TEXT, state TEXT, postal_code TEXT, country TEXT, is_current INTEGER NOT NULL DEFAULT 1);
CREATE TABLE organization(id TEXT PRIMARY KEY, name TEXT NOT NULL, type_code TEXT);
CREATE TABLE location(id TEXT PRIMARY KEY, name TEXT, organization_id TEXT REFERENCES organization(id), city TEXT, state TEXT);
CREATE TABLE practitioner(id TEXT PRIMARY KEY, given_name TEXT, family_name TEXT, gender TEXT);
CREATE TABLE encounter(id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patient(id), organization_id TEXT REFERENCES organization(id), location_id TEXT REFERENCES location(id), status TEXT, class_code TEXT, type_code TEXT, type_display TEXT, start_at TEXT, end_at TEXT);
CREATE TABLE encounter_participant(encounter_id TEXT REFERENCES encounter(id), practitioner_id TEXT REFERENCES practitioner(id), role_code TEXT, PRIMARY KEY(encounter_id, practitioner_id));
CREATE TABLE observation(id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patient(id), encounter_id TEXT REFERENCES encounter(id), status TEXT, category_code TEXT, code TEXT, code_display TEXT, effective_at TEXT, value REAL, unit TEXT);
CREATE TABLE condition(id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patient(id), encounter_id TEXT REFERENCES encounter(id), clinical_status TEXT, code TEXT, code_display TEXT, onset_at TEXT, recorded_at TEXT);
CREATE TABLE procedure(id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patient(id), encounter_id TEXT REFERENCES encounter(id), status TEXT, code TEXT, code_display TEXT, start_at TEXT, end_at TEXT);
CREATE TABLE claim(id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patient(id), organization_id TEXT REFERENCES organization(id), location_id TEXT REFERENCES location(id), status TEXT, claim_type TEXT, start_at TEXT, end_at TEXT, created_at TEXT, total_amount REAL, currency TEXT);
CREATE TABLE claim_item(claim_id TEXT REFERENCES claim(id), sequence INTEGER, product_code TEXT, product_display TEXT, net_amount REAL, currency TEXT, encounter_id TEXT REFERENCES encounter(id), PRIMARY KEY(claim_id, sequence));
CREATE TABLE claim_diagnosis(claim_id TEXT REFERENCES claim(id), sequence INTEGER, condition_id TEXT REFERENCES condition(id), PRIMARY KEY(claim_id, sequence));
CREATE TABLE medication_request(id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patient(id), encounter_id TEXT REFERENCES encounter(id), practitioner_id TEXT REFERENCES practitioner(id), status TEXT, medication_code TEXT, medication_display TEXT, authored_at TEXT, dosage_text TEXT);
CREATE INDEX idx_encounter_patient_date ON encounter(patient_id,start_at);
CREATE INDEX idx_observation_patient_date ON observation(patient_id,effective_at);
CREATE INDEX idx_claim_patient_date ON claim(patient_id,created_at);
CREATE INDEX idx_condition_patient ON condition(patient_id);
CREATE INDEX idx_procedure_patient_date ON procedure(patient_id,start_at);
"""


def _first(items: list[Any] | None, default: Any = None) -> Any:
    return items[0] if items else default


def _coding(value: dict | None) -> tuple[str | None, str | None]:
    c = _first((value or {}).get("coding"), {})
    return c.get("code"), c.get("display") or (value or {}).get("text")


def _ref(value: dict | None) -> str | None:
    raw = (value or {}).get("reference")
    return raw.rsplit("/", 1)[-1] if raw else None


def read_resources(path: Path, resource: str) -> list[dict]:
    file = path / f"{resource}.ndjson"
    if not file.exists():
        raise FileNotFoundError(f"Missing required FHIR resource: {file}")
    with file.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_operational(conn: sqlite3.Connection, path: Path) -> dict[str, int]:
    conn.executescript(SCHEMA)
    counts: dict[str, int] = {}
    patients = read_resources(path, "Patient")
    for x in patients:
        name = _first(x.get("name"), {})
        conn.execute("INSERT INTO patient VALUES(?,?,?,?,?,?)", (x["id"], _first(name.get("given")), name.get("family"), x.get("gender"), x.get("birthDate"), (x.get("maritalStatus") or {}).get("text")))
        for address in x.get("address", []):
            conn.execute("INSERT INTO patient_address(patient_id,line,city,state,postal_code,country) VALUES(?,?,?,?,?,?)", (x["id"], _first(address.get("line")), address.get("city"), address.get("state"), address.get("postalCode"), address.get("country")))
    counts["patient"] = len(patients)

    orgs = read_resources(path, "Organization")
    for x in orgs:
        code, _ = _coding(_first(x.get("type"), {}))
        conn.execute("INSERT OR REPLACE INTO organization VALUES(?,?,?)", (x["id"], x.get("name") or "Unknown", code))
    counts["organization"] = conn.execute("SELECT count(*) FROM organization").fetchone()[0]

    locs = read_resources(path, "Location")
    for x in locs:
        a = x.get("address") or {}
        conn.execute("INSERT OR REPLACE INTO location VALUES(?,?,?,?,?)", (x["id"], x.get("name"), _ref(x.get("managingOrganization")), a.get("city"), a.get("state")))
    counts["location"] = conn.execute("SELECT count(*) FROM location").fetchone()[0]

    practitioners = read_resources(path, "Practitioner")
    for x in practitioners:
        n = _first(x.get("name"), {})
        conn.execute("INSERT OR REPLACE INTO practitioner VALUES(?,?,?,?)", (x["id"], _first(n.get("given")), n.get("family"), x.get("gender")))
    counts["practitioner"] = conn.execute("SELECT count(*) FROM practitioner").fetchone()[0]

    encounters = read_resources(path, "Encounter")
    for x in encounters:
        code, display = _coding(_first(x.get("type"), {}))
        location = _first(x.get("location"), {}).get("location", {})
        period = x.get("period") or {}
        conn.execute("INSERT INTO encounter VALUES(?,?,?,?,?,?,?,?,?,?)", (x["id"], _ref(x.get("subject")), _ref(x.get("serviceProvider")), _ref(location), x.get("status"), (x.get("class") or {}).get("code"), code, display, period.get("start"), period.get("end")))
        for p in x.get("participant", []):
            role, _ = _coding(_first(p.get("type"), {}))
            conn.execute("INSERT OR IGNORE INTO encounter_participant VALUES(?,?,?)", (x["id"], _ref(p.get("individual")), role))
    counts["encounter"] = len(encounters)

    for resource, table in [("Observation", "observation"), ("Condition", "condition"), ("Procedure", "procedure")]:
        rows = read_resources(path, resource)
        for x in rows:
            code, display = _coding(x.get("code"))
            if table == "observation":
                cat, _ = _coding(_first(x.get("category"), {})); q = x.get("valueQuantity") or {}
                conn.execute("INSERT INTO observation VALUES(?,?,?,?,?,?,?,?,?,?)", (x["id"], _ref(x.get("subject")), _ref(x.get("encounter")), x.get("status"), cat, code, display, x.get("effectiveDateTime"), q.get("value"), q.get("unit")))
            elif table == "condition":
                status, _ = _coding(x.get("clinicalStatus"))
                conn.execute("INSERT INTO condition VALUES(?,?,?,?,?,?,?,?)", (x["id"], _ref(x.get("subject")), _ref(x.get("encounter")), status, code, display, x.get("onsetDateTime"), x.get("recordedDate")))
            else:
                period = x.get("performedPeriod") or {}
                conn.execute("INSERT INTO procedure VALUES(?,?,?,?,?,?,?,?)", (x["id"], _ref(x.get("subject")), _ref(x.get("encounter")), x.get("status"), code, display, period.get("start") or x.get("performedDateTime"), period.get("end")))
        counts[table] = len(rows)

    claims = read_resources(path, "Claim")
    for x in claims:
        ctype, _ = _coding(x.get("type")); period = x.get("billablePeriod") or {}; total = x.get("total") or {}
        conn.execute("INSERT INTO claim VALUES(?,?,?,?,?,?,?,?,?,?,?)", (x["id"], _ref(x.get("patient")), _ref(x.get("provider")), _ref(x.get("facility")), x.get("status"), ctype, period.get("start"), period.get("end"), x.get("created"), total.get("value"), total.get("currency")))
        for item in x.get("item", []):
            code, display = _coding(item.get("productOrService")); net = item.get("net") or {}
            conn.execute("INSERT INTO claim_item VALUES(?,?,?,?,?,?,?)", (x["id"], item.get("sequence"), code, display, net.get("value"), net.get("currency"), _ref(_first(item.get("encounter"), {}))))
        for diagnosis in x.get("diagnosis", []):
            conn.execute("INSERT INTO claim_diagnosis VALUES(?,?,?)", (x["id"], diagnosis.get("sequence"), _ref(diagnosis.get("diagnosisReference"))))
    counts["claim"] = len(claims)
    counts["claim_item"] = conn.execute("SELECT count(*) FROM claim_item").fetchone()[0]

    meds = read_resources(path, "MedicationRequest")
    for x in meds:
        code, display = _coding(x.get("medicationCodeableConcept")); dose = _first(x.get("dosageInstruction"), {})
        conn.execute("INSERT INTO medication_request VALUES(?,?,?,?,?,?,?,?,?)", (x["id"], _ref(x.get("subject")), _ref(x.get("encounter")), _ref(x.get("requester")), x.get("status"), code, display, x.get("authoredOn"), dose.get("text")))
    counts["medication_request"] = len(meds)
    conn.commit()
    return counts
