from app.operational import read_resources


def test_import_counts_and_integrity(op, source_path):
    assert op.execute("SELECT count(*) FROM patient").fetchone()[0] == 10
    assert op.execute("SELECT count(*) FROM encounter").fetchone()[0] == 295
    assert op.execute("SELECT count(*) FROM observation").fetchone()[0] == 2000
    assert op.execute("SELECT count(*) FROM claim").fetchone()[0] == 395
    assert op.execute("PRAGMA foreign_key_check").fetchall() == []
    assert op.execute("SELECT count(*) FROM claim_item").fetchone()[0] == sum(len(x.get("item",[])) for x in read_resources(source_path,"Claim"))


def test_nested_arrays_are_relational(op):
    assert op.execute("SELECT count(*) FROM patient_address").fetchone()[0] == 10
    assert op.execute("SELECT count(*) FROM encounter_participant").fetchone()[0] > 0
    assert op.execute("SELECT count(*) FROM claim_diagnosis").fetchone()[0] > 0
    indexes={r[1] for r in op.execute("PRAGMA index_list('encounter')")}
    assert "idx_encounter_patient_date" in indexes

