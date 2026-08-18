# Operational ↔ Analytical Modeling Lab

An educational FastAPI POC that loads one synthetic Synthea FHIR snapshot into a normalized operational SQLite model and compiles the same data into selectable star or snowflake schemas.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --workers 1
```

Open <http://localhost:8000>. `OPENAI_API_KEY` is optional: without it the complete encounter-grain fallback remains available. The configured model defaults to `gpt-5.4-mini`; override it with `OPENAI_MODEL`.

To load another compatible Synthea R4 bulk export:

```bash
SYNTHEA_FHIR_PATH=/path/to/output/fhir uvicorn app.main:app --workers 1
```

## Docker

```bash
docker build -t modeling-lab .
docker run --rm -p 8000:8000 --env-file .env modeling-lab
```

For an external dataset, mount it read-only and set `SYNTHEA_FHIR_PATH`:

```bash
docker run --rm -p 8000:8000 \
  -v /path/to/fhir:/fhir:ro -e SYNTHEA_FHIR_PATH=/fhir \
  --env-file .env modeling-lab
```

The application intentionally runs one worker because both SQLite databases are named, shared, in-memory databases owned by that process.

## Architecture and safety boundary

```text
Synthea NDJSON -> normalized operational SQLite
                         |
               trusted semantic catalog
                         |
       AI typed ModelSpec -> validator -> deterministic compiler
                                      -> star/snowflake SQLite

business question -> AI QueryPair -> SQL parser/allowlist -> both DBs
```

AI receives schema metadata and aggregate counts only—never patient row values. It can select catalog grain, dimension, and measure identifiers but cannot execute DDL or ETL. Generated query SQL is parsed as SQLite, restricted to one read-only SELECT/CTE, checked against known tables and functions, time-limited, and row-limited. Invalid AI SQL is rejected and replaced by a deterministic fallback.

## Demo talking points

- The operational model optimizes correctness of individual writes: normalized entities, constraints, and minimal redundancy.
- The dimensional model starts with a business process and an explicit **grain**. Facts record events and measures; dimensions provide descriptive context.
- A star flattens dimension attributes for convenient reads. A snowflake normalizes selected hierarchies, trading fewer repeated attributes for more joins.
- Surrogate keys decouple warehouse identifiers from source identifiers.
- This tiny POC demonstrates modeling intent and query shape, not a performance benchmark.

## Tests

```bash
pytest -q
```

Tests reconcile every supported grain, compare star and snowflake aggregates, check foreign keys, exercise offline AI fallback, and attack the SQL read-only boundary.

The bundled records are synthetic Synthea output.

