from __future__ import annotations

import html
import os
import re
import sqlite3
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .ai import AIService
from .analytical import BuildResult, build_analytical
from .catalog import GRAINS, fallback_questions, fallback_spec
from .models import BusinessQuestion, DimensionalModelSpec, QueryPair
from .operational import load_operational
from .query import execute_select

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = Jinja2Templates(directory=ROOT / "app" / "templates")


def connect(name: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{name}?mode=memory&cache=shared", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def schema_text(conn: sqlite3.Connection) -> str:
    return "\n".join(r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name"))


def schema_tables(conn: sqlite3.Connection) -> list[dict]:
    result = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
        preview=[dict(r) for r in conn.execute(f'SELECT * FROM "{name}" LIMIT 3')]
        result.append({"name":name,"columns":cols,"count":conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0],"preview":preview})
    return result


def diagram_svg(conn: sqlite3.Connection, title: str, *, fact_table: str | None = None, layout: str | None = None) -> str:
    tables = schema_tables(conn)
    width, box_w, gap = 1050, 245, 18
    # Every card uses the same height, sized for the widest field list, which
    # keeps the relationship lines and drag boundaries straightforward.
    box_h = max(100, 52 + 17 * max((len(t["columns"]) for t in tables), default=0))
    is_star = layout == "star" and fact_table in {t["name"] for t in tables}
    is_snowflake = layout == "snowflake" and fact_table in {t["name"] for t in tables}
    if is_star:
        height = 3 * box_h + 180
        # The declared fact is central, with directly joined dimensions radiating around it.
        cx, cy = width / 2, height / 2
        related = [t["name"] for t in tables if t["name"] != fact_table and any(fk[2] == t["name"] for fk in conn.execute(f'PRAGMA foreign_key_list("{fact_table}")'))]
        others = [t["name"] for t in tables if t["name"] not in related and t["name"] != fact_table]
        positions = {fact_table: (cx - box_w / 2, cy - box_h / 2)}
        cardinal_slots = {
            1: [(0, -1)], 2: [(-1, 0), (1, 0)], 3: [(0, -1), (-1, 0), (1, 0)],
            4: [(0, -1), (1, 0), (0, 1), (-1, 0)],
        }.get(len(related), [(0, -1), (1, 0), (0, 1), (-1, 0)])
        for i, name in enumerate(related):
            dx, dy = cardinal_slots[i % len(cardinal_slots)]
            positions[name] = (cx + dx * 360 - box_w / 2, cy + dy * (box_h + 85) - box_h / 2)
        for i, name in enumerate(others):
            positions[name] = (width - box_w - 20, 40 + i * (box_h + gap))
    elif is_snowflake:
        # The fact is central, direct dimensions orbit it, and their normalized
        # lookup tables continue outward along the same branch.
        # A wider canvas leaves room for a branch on each side of the fact.
        width = 1720
        height = 5 * box_h + 400
        cx, cy = width / 2, height / 2
        foreign_keys = {
            table["name"]: [fk[2] for fk in conn.execute(f'PRAGMA foreign_key_list("{table["name"]}")')]
            for table in tables
        }
        related = foreign_keys[fact_table]
        positions = {fact_table: (cx - box_w / 2, cy - box_h / 2)}
        cardinal_slots = {
            1: [(0, -1)], 2: [(-1, 0), (1, 0)], 3: [(0, -1), (-1, 0), (1, 0)],
            4: [(0, -1), (1, 0), (0, 1), (-1, 0)],
        }.get(len(related), [(0, -1), (1, 0), (0, 1), (-1, 0)])
        directions = {}
        for i, name in enumerate(related):
            dx, dy = cardinal_slots[i % len(cardinal_slots)]
            directions[name] = (dx, dy)
            positions[name] = (cx + dx * 360 - box_w / 2, cy + dy * (box_h + 85) - box_h / 2)
        pending = [name for name in related if name in foreign_keys]
        while pending:
            parent = pending.pop(0)
            px, py = positions[parent]
            dx, dy = directions[parent]
            for child in foreign_keys[parent]:
                if child in positions:
                    continue
                directions[child] = (dx, dy)
                positions[child] = (px + dx * 315, py + dy * (box_h + 75))
                pending.append(child)
        for i, table in enumerate(tables):
            if table["name"] not in positions:
                positions[table["name"]] = (width - box_w - 20, 40 + i * (box_h + gap))
    else:
        rows = (len(tables)+3)//4
        height = max(150, 55 + rows*(box_h+gap))
        positions={t["name"]:(10+(i%4)*(box_w+gap),38+(i//4)*(box_h+gap)) for i,t in enumerate(tables)}
    diagram_id = "-".join(c if c.isalnum() else "-" for c in title.lower())
    layout_label = "this star" if is_star else "this snowflake" if is_snowflake else "the schema"
    canvas_class = " snowflake-canvas" if is_snowflake else ""
    chunks = [f'<div class="schema-diagram"><div class="diagram-toolbar"><span>Drag tables to rearrange {layout_label}.</span><div class="diagram-actions"><div class="diagram-zoom" aria-label="Diagram zoom controls"><button type="button" aria-label="Zoom out" data-diagram-zoom="out">−</button><output data-diagram-zoom-level>100%</output><button type="button" aria-label="Zoom in" data-diagram-zoom="in">+</button></div><button type="button" class="diagram-reset" data-diagram-reset="{diagram_id}">Reset layout</button></div></div><div class="diagram-viewport"><svg class="schema-canvas{canvas_class}" data-diagram-id="{diagram_id}" data-box-width="{box_w}" data-box-height="{box_h}" data-base-viewbox="0 0 {width} {height}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)} schema diagram"><style>.b{{fill:#fff;stroke:#4864d7;stroke-width:2}}.fact .b{{fill:#e9edff;stroke:#293fba;stroke-width:3}}.branch .b{{fill:#f4f7ff}}.t{{font:bold 14px system-ui;fill:#17204a}}.c{{font:11px ui-monospace;fill:#4c557a}}.e{{stroke:#aeb8df;stroke-width:2}}</style><text x="10" y="24" class="t">{html.escape(title)}</text><g class="diagram-edges">']
    for table in tables:
        x,y=positions[table["name"]]
        for fk in conn.execute(f'PRAGMA foreign_key_list("{table["name"]}")'):
            if fk[2] in positions:
                tx,ty=positions[fk[2]]
                chunks.append(f'<line class="e" data-from="{html.escape(table["name"])}" data-to="{html.escape(fk[2])}" x1="{x+box_w/2}" y1="{y+box_h/2}" x2="{tx+box_w/2}" y2="{ty+box_h/2}"/>')
    chunks.append('</g><g class="diagram-nodes">')
    for t in tables:
        x,y=positions[t["name"]]
        fact_class = " fact" if t["name"] == fact_table else ""
        branch_class = " branch" if is_snowflake and t["name"] not in related and t["name"] != fact_table else ""
        field_lines = "".join(f'<text class="c" x="10" y="{48 + i * 17}">{html.escape(column)}</text>' for i, column in enumerate(t["columns"]))
        chunks += [f'<g class="diagram-node{fact_class}{branch_class}" data-table="{html.escape(t["name"])}" transform="translate({x} {y})"><rect class="b" rx="8" width="{box_w}" height="{box_h}"/><text class="t" x="10" y="24">{html.escape(t["name"])}</text>{field_lines}<text class="c" x="10" y="{box_h - 12}">{t["count"]} rows</text></g>']
    chunks.append("</g></svg></div></div>")
    return "".join(chunks)


def format_sql(sql: str) -> str:
    """Add clause-level line breaks without changing the query's meaning."""
    formatted = re.sub(r"^SELECT\s+", "SELECT\n  ", sql.strip(), flags=re.IGNORECASE)
    return re.sub(
        r"\s+(FROM|JOIN|WHERE|GROUP BY|ORDER BY|LIMIT)\s+",
        lambda match: f"\n{match.group(1).upper()} ",
        formatted,
        flags=re.IGNORECASE,
    )


# Questions whose operational form must traverse child tables and re-derive
# calendar attributes per row, while the dimensional form reads stored columns
# through surrogate-key lookups. These make the efficiency argument visible.
EFFICIENCY_QUESTION_IDS = {
    "events_by_state_and_quarter", "distinct_patients_by_state_and_year",
    "events_by_organization_state_and_year", "amount_by_organization_state_and_year",
}


def fallback_query_pair(question_id: str, spec: DimensionalModelSpec, style: str) -> QueryPair:
    g=GRAINS[spec.grain_id]; fact=g.fact_table
    op_from=g.source_table
    state_op=f"{op_from} JOIN patient p ON p.id={g.patient_id} JOIN patient_address a ON a.patient_id=p.id AND a.is_current=1"
    state_ana=f"{fact} f JOIN dim_patient p ON p.patient_key=f.patient_key"
    state_col="p.state"
    if style=="snowflake":
        state_ana += " JOIN dim_geography geo ON geo.geography_key=p.geography_key"; state_col="geo.state"
    code_join=f"{fact} f JOIN dim_clinical_code c ON c.clinical_code_key=f.clinical_code_key"
    organization_join=f"{fact} f JOIN dim_organization o ON o.organization_key=f.organization_key"
    # Calendar attributes the operational model must recompute from a text
    # timestamp on every row; dim_date stores them as grouped columns.
    year_op=f"CAST(substr({g.date_expr},1,4) AS INTEGER)"
    quarter_op=f"((CAST(substr({g.date_expr},6,2) AS INTEGER)+2)/3)"
    state_date_ana=f"{state_ana} JOIN dim_date d ON d.date_key=f.date_key"
    pairs = {
        "events_by_year": (f"SELECT CAST(substr({g.date_expr},1,4) AS INTEGER) year, COUNT(*) event_count FROM {op_from} GROUP BY year ORDER BY year", f"SELECT d.year, SUM(f.event_count) event_count FROM {fact} f JOIN dim_date d ON d.date_key=f.date_key GROUP BY d.year ORDER BY d.year"),
        "events_by_state": (f"SELECT a.state,COUNT(*) event_count FROM {state_op} GROUP BY a.state ORDER BY event_count DESC", f"SELECT {state_col} state,SUM(f.event_count) event_count FROM {state_ana} GROUP BY {state_col} ORDER BY event_count DESC"),
        "top_patients": (f"SELECT p.id,p.given_name,p.family_name,COUNT(*) event_count FROM {op_from} JOIN patient p ON p.id={g.patient_id} GROUP BY p.id ORDER BY event_count DESC LIMIT 5", f"SELECT p.patient_id,p.given_name,p.family_name,SUM(f.event_count) event_count FROM {fact} f JOIN dim_patient p ON p.patient_key=f.patient_key GROUP BY p.patient_id,p.given_name,p.family_name ORDER BY event_count DESC LIMIT 5"),
        "top_codes": (f"SELECT {g.code_expr} code,{g.code_display_expr} display,COUNT(*) event_count FROM {op_from} WHERE {g.code_expr} IS NOT NULL GROUP BY {g.code_expr},{g.code_display_expr} ORDER BY event_count DESC LIMIT 10", f"SELECT c.code,c.display,SUM(f.event_count) event_count FROM {code_join} GROUP BY c.code,c.display ORDER BY event_count DESC LIMIT 10"),
        "amount_by_year": (f"SELECT CAST(substr({g.date_expr},1,4) AS INTEGER) year,ROUND(SUM({g.amount_expr}),2) total_amount FROM {op_from} GROUP BY year ORDER BY year", f"SELECT d.year,ROUND(SUM(f.amount),2) total_amount FROM {fact} f JOIN dim_date d ON d.date_key=f.date_key GROUP BY d.year ORDER BY d.year"),
        "avg_value_by_code": (f"SELECT {g.code_expr} code,{g.code_display_expr} display,{g.unit_expr} unit,ROUND(AVG({g.value_expr}),2) average_value FROM {op_from} WHERE {g.value_expr} IS NOT NULL GROUP BY {g.code_expr},{g.code_display_expr},{g.unit_expr} ORDER BY display", f"SELECT c.code,c.display,f.unit,ROUND(AVG(f.numeric_value),2) average_value FROM {code_join} WHERE f.numeric_value IS NOT NULL GROUP BY c.code,c.display,f.unit ORDER BY c.display"),
        "events_by_organization": (f"SELECT o.id organization_id,o.name,COUNT(*) event_count FROM {op_from} JOIN organization o ON o.id={g.organization_expr} GROUP BY o.id,o.name ORDER BY event_count DESC", f"SELECT o.organization_id,o.name,SUM(f.event_count) event_count FROM {organization_join} GROUP BY o.organization_id,o.name ORDER BY event_count DESC"),
        "amount_by_organization": (f"SELECT o.id organization_id,o.name,ROUND(SUM({g.amount_expr}),2) total_amount FROM {op_from} JOIN organization o ON o.id={g.organization_expr} GROUP BY o.id,o.name ORDER BY total_amount DESC", f"SELECT o.organization_id,o.name,ROUND(SUM(f.amount),2) total_amount FROM {organization_join} GROUP BY o.organization_id,o.name ORDER BY total_amount DESC"),
        "amount_by_state": (f"SELECT a.state,ROUND(SUM({g.amount_expr}),2) total_amount FROM {state_op} GROUP BY a.state ORDER BY total_amount DESC", f"SELECT {state_col} state,ROUND(SUM(f.amount),2) total_amount FROM {state_ana} GROUP BY {state_col} ORDER BY total_amount DESC"),
        "top_patients_by_amount": (f"SELECT p.id,p.given_name,p.family_name,ROUND(SUM({g.amount_expr}),2) total_amount FROM {op_from} JOIN patient p ON p.id={g.patient_id} GROUP BY p.id,p.given_name,p.family_name ORDER BY total_amount DESC LIMIT 5", f"SELECT p.patient_id,p.given_name,p.family_name,ROUND(SUM(f.amount),2) total_amount FROM {fact} f JOIN dim_patient p ON p.patient_key=f.patient_key GROUP BY p.patient_id,p.given_name,p.family_name ORDER BY total_amount DESC LIMIT 5"),
        "amount_by_code": (f"SELECT {g.code_expr} code,{g.code_display_expr} display,ROUND(SUM({g.amount_expr}),2) total_amount FROM {op_from} WHERE {g.code_expr} IS NOT NULL GROUP BY {g.code_expr},{g.code_display_expr} ORDER BY total_amount DESC", f"SELECT c.code,c.display,ROUND(SUM(f.amount),2) total_amount FROM {code_join} GROUP BY c.code,c.display ORDER BY total_amount DESC"),
        "avg_amount_by_code": (f"SELECT {g.code_expr} code,{g.code_display_expr} display,ROUND(AVG({g.amount_expr}),2) average_amount FROM {op_from} WHERE {g.code_expr} IS NOT NULL GROUP BY {g.code_expr},{g.code_display_expr} ORDER BY average_amount DESC", f"SELECT c.code,c.display,ROUND(AVG(f.amount),2) average_amount FROM {code_join} GROUP BY c.code,c.display ORDER BY average_amount DESC"),
        "avg_value_by_year": (f"SELECT CAST(substr({g.date_expr},1,4) AS INTEGER) year,{g.code_expr} code,{g.code_display_expr} display,{g.unit_expr} unit,ROUND(AVG({g.value_expr}),2) average_value FROM {op_from} WHERE {g.value_expr} IS NOT NULL GROUP BY year,{g.code_expr},{g.code_display_expr},{g.unit_expr} ORDER BY year,display", f"SELECT d.year,c.code,c.display,f.unit,ROUND(AVG(f.numeric_value),2) average_value FROM {fact} f JOIN dim_date d ON d.date_key=f.date_key JOIN dim_clinical_code c ON c.clinical_code_key=f.clinical_code_key WHERE f.numeric_value IS NOT NULL GROUP BY d.year,c.code,c.display,f.unit ORDER BY d.year,c.display"),
        "distinct_patients_by_code": (f"SELECT {g.code_expr} code,{g.code_display_expr} display,COUNT(DISTINCT {g.patient_id}) patient_count FROM {op_from} WHERE {g.code_expr} IS NOT NULL GROUP BY {g.code_expr},{g.code_display_expr} ORDER BY patient_count DESC", f"SELECT c.code,c.display,COUNT(DISTINCT f.patient_key) patient_count FROM {code_join} GROUP BY c.code,c.display ORDER BY patient_count DESC"),
        "events_by_code_and_year": (f"SELECT CAST(substr({g.date_expr},1,4) AS INTEGER) year,{g.code_expr} code,{g.code_display_expr} display,COUNT(*) event_count FROM {op_from} WHERE {g.code_expr} IS NOT NULL GROUP BY year,{g.code_expr},{g.code_display_expr} ORDER BY year,event_count DESC", f"SELECT d.year,c.code,c.display,SUM(f.event_count) event_count FROM {fact} f JOIN dim_date d ON d.date_key=f.date_key JOIN dim_clinical_code c ON c.clinical_code_key=f.clinical_code_key GROUP BY d.year,c.code,c.display ORDER BY d.year,event_count DESC"),
        "events_by_state_and_quarter": (f"SELECT a.state,{year_op} year,{quarter_op} quarter,COUNT(*) event_count FROM {state_op} GROUP BY a.state,year,quarter ORDER BY year,quarter,event_count DESC", f"SELECT {state_col} state,d.year,d.quarter,SUM(f.event_count) event_count FROM {state_date_ana} GROUP BY {state_col},d.year,d.quarter ORDER BY d.year,d.quarter,event_count DESC"),
        "distinct_patients_by_state_and_year": (f"SELECT a.state,{year_op} year,COUNT(DISTINCT p.id) patient_count,COUNT(*) event_count FROM {state_op} GROUP BY a.state,year ORDER BY year,patient_count DESC", f"SELECT {state_col} state,d.year,COUNT(DISTINCT f.patient_key) patient_count,SUM(f.event_count) event_count FROM {state_date_ana} GROUP BY {state_col},d.year ORDER BY d.year,patient_count DESC"),
        "events_by_organization_state_and_year": (f"SELECT o.name organization,a.state,{year_op} year,COUNT(*) event_count FROM {state_op} JOIN organization o ON o.id={g.organization_expr} GROUP BY o.name,a.state,year ORDER BY event_count DESC LIMIT 20", f"SELECT o.name organization,{state_col} state,d.year,SUM(f.event_count) event_count FROM {state_date_ana} JOIN dim_organization o ON o.organization_key=f.organization_key GROUP BY o.name,{state_col},d.year ORDER BY event_count DESC LIMIT 20"),
        "amount_by_organization_state_and_year": (f"SELECT o.name organization,a.state,{year_op} year,ROUND(SUM({g.amount_expr}),2) total_amount FROM {state_op} JOIN organization o ON o.id={g.organization_expr} GROUP BY o.name,a.state,year ORDER BY total_amount DESC LIMIT 20", f"SELECT o.name organization,{state_col} state,d.year,ROUND(SUM(f.amount),2) total_amount FROM {state_date_ana} JOIN dim_organization o ON o.organization_key=f.organization_key GROUP BY o.name,{state_col},d.year ORDER BY total_amount DESC LIMIT 20"),
    }
    op,ana=pairs.get(question_id,pairs["events_by_year"])
    explanation=("The operational answer walks patient and address child tables and recomputes calendar parts from text on every row; the dimensional answer reads stored dimension columns through surrogate-key lookups, so compare the join counts and query plans."
                 if question_id in EFFICIENCY_QUESTION_IDS
                 else "Equivalent queries expose normalized operational joins versus a fact-centered dimensional path.")
    return QueryPair(operational_sql=format_sql(op),analytical_sql=format_sql(ana),explanation=explanation)


@dataclass
class AppState:
    op: sqlite3.Connection
    ana: sqlite3.Connection
    counts: dict[str,int]
    ai: AIService
    grains: list = field(default_factory=list)
    grain_ai: bool = False
    ai_note: str | None = None
    style: str = "star"
    spec: DimensionalModelSpec = field(default_factory=lambda:fallback_spec("encounter"))
    build: BuildResult | None = None
    questions: list[BusinessQuestion] = field(default_factory=lambda:fallback_questions("encounter"))
    question_ai: bool = False
    question_id: str | None = None
    comparison: dict | None = None
    editor_result: dict | None = None
    error: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


def create_state() -> AppState:
    source=Path(os.getenv("SYNTHEA_FHIR_PATH") or ROOT/"data"/"fhir")
    op=connect("operational_poc"); ana=connect("analytical_poc")
    counts=load_operational(op,source); ai=AIService(); grains,used,note=ai.grains()
    state=AppState(op,ana,counts,ai,grains,used,note)
    state.build=build_analytical(op,ana,state.spec,state.style)
    return state


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.demo=create_state()
    yield
    app.state.demo.op.close(); app.state.demo.ana.close()


app=FastAPI(title="Operational vs Analytical Modeling Lab",lifespan=lifespan)
app.mount("/static",StaticFiles(directory=ROOT/"app"/"static"),name="static")


def view_context(request: Request) -> dict:
    s:AppState=request.app.state.demo
    fact_table = GRAINS[s.build.spec.grain_id].fact_table if s.build else None
    return {"request":request,"s":s,"op_tables":schema_tables(s.op),"ana_tables":schema_tables(s.ana),"op_diagram":diagram_svg(s.op,"Normalized operational model"),"ana_diagram":diagram_svg(s.ana,f"{s.style.title()} analytical model",fact_table=fact_table,layout=s.style),"glossary":GLOSSARY}


GLOSSARY={"Grain":"Exactly what one fact row represents.","Fact":"An event at a declared grain, with foreign keys and measures.","Dimension":"Descriptive context used to filter, group, and label facts.","Measure":"A numeric value that can be aggregated under defined rules.","Surrogate key":"A warehouse-controlled identifier for a dimension row.","ETL / ELT":"Extract, transform, and load—the movement from source to analytical structures.","Normalization":"Separate entities to reduce redundancy and enforce integrity.","Denormalization":"Deliberately repeat or flatten attributes to simplify analytical reads.","Star schema":"A central fact joined directly to denormalized dimensions.","Snowflake schema":"A dimensional model whose hierarchies are normalized into related tables."}


@app.get("/",response_class=HTMLResponse)
async def index(request:Request): return TEMPLATES.TemplateResponse(request,"index.html",view_context(request))


@app.get("/health")
async def health(request:Request):
    s=request.app.state.demo
    return {"status":"ok","operational_patients":s.counts["patient"],"analytical_grain":s.spec.grain_id,"ai_enabled":s.ai.enabled}


@app.post("/grains/generate")
async def generate_grains(request:Request):
    s=request.app.state.demo
    with s.lock: s.grains,s.grain_ai,s.ai_note=s.ai.grains()
    return RedirectResponse("/#model",303)


@app.post("/model/build")
async def build_model(request:Request,grain_id:str=Form(...),style:str=Form(...)):
    s=request.app.state.demo
    try:
        with s.lock:
            s.spec,used,note=s.ai.model_spec(grain_id); s.style=style; s.build=build_analytical(s.op,s.ana,s.spec,style)
            s.questions,s.question_ai,qnote=s.ai.questions(s.spec); s.ai_note=note or qnote; s.comparison=None; s.error=None
    except Exception as exc: s.error=str(exc)
    return RedirectResponse("/#model",303)


@app.post("/questions/generate")
async def generate_questions(request:Request):
    s=request.app.state.demo
    with s.lock: s.questions,s.question_ai,s.ai_note=s.ai.questions(s.spec)
    return RedirectResponse("/#questions",303)


@app.post("/questions/run")
async def run_question(request:Request,question_id:str=Form(...)):
    s=request.app.state.demo; question=next((q for q in s.questions if q.id==question_id),None)
    if not question: raise HTTPException(404,"Unknown question")
    with s.lock:
        # Remember the choice so the redirected page re-renders it as selected.
        s.question_id=question.id
        pair=s.ai.query_pair(question.id,question.question,s.spec,s.style,schema_text(s.op),schema_text(s.ana)) or fallback_query_pair(question.id,s.spec,s.style)
        try:
            s.comparison={"question":question,"pair":pair,"operational":execute_select(s.op,pair.operational_sql),"analytical":execute_select(s.ana,pair.analytical_sql)}; s.error=None
        except ValueError as exc:
            # Never run untrusted invalid SQL; fall back once to deterministic SQL.
            pair=fallback_query_pair(question.id,s.spec,s.style)
            try: s.comparison={"question":question,"pair":pair,"operational":execute_select(s.op,pair.operational_sql),"analytical":execute_select(s.ana,pair.analytical_sql)}; s.error=f"AI SQL was rejected; safe fallback used: {exc}"
            except ValueError as fallback_exc: s.error=str(fallback_exc)
    return RedirectResponse("/#questions",303)


@app.post("/query/run")
async def run_editor(request:Request,target:str=Form(...),sql:str=Form(...)):
    s=request.app.state.demo
    try:
        conn=s.op if target=="operational" else s.ana if target=="analytical" else None
        if conn is None: raise ValueError("Unknown query target")
        result=execute_select(conn,sql); s.editor_result={"target":target,"sql":sql,"result":result}; s.error=None
    except ValueError as exc: s.error=str(exc)
    return RedirectResponse("/#sql",303)


