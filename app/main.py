from __future__ import annotations

import html
import os
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
from .analytical import BuildResult, build_analytical, reset_scd2, scd2_state, simulate_scd2
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


def diagram_svg(conn: sqlite3.Connection, title: str) -> str:
    tables = schema_tables(conn)
    width, box_w, box_h, gap = 1050, 245, 82, 18
    rows = (len(tables)+3)//4
    height = max(150, 55 + rows*(box_h+gap))
    chunks = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)} schema diagram"><style>.b{{fill:#fff;stroke:#4864d7;stroke-width:2}}.t{{font:bold 14px system-ui;fill:#17204a}}.c{{font:11px ui-monospace;fill:#4c557a}}.e{{stroke:#aeb8df;stroke-width:2}}</style><text x="10" y="24" class="t">{html.escape(title)}</text>']
    positions={t["name"]:(10+(i%4)*(box_w+gap),38+(i//4)*(box_h+gap)) for i,t in enumerate(tables)}
    for table in tables:
        x,y=positions[table["name"]]
        for fk in conn.execute(f'PRAGMA foreign_key_list("{table["name"]}")'):
            if fk[2] in positions:
                tx,ty=positions[fk[2]]
                chunks.append(f'<line class="e" x1="{x+box_w/2}" y1="{y+box_h/2}" x2="{tx+box_w/2}" y2="{ty+box_h/2}"/>')
    for i,t in enumerate(tables):
        x=10+(i%4)*(box_w+gap); y=38+(i//4)*(box_h+gap)
        cols=", ".join(t["columns"][:4]) + ("…" if len(t["columns"])>4 else "")
        chunks += [f'<rect class="b" x="{x}" y="{y}" rx="8" width="{box_w}" height="{box_h}"/>',f'<text class="t" x="{x+10}" y="{y+24}">{html.escape(t["name"])}</text>',f'<text class="c" x="{x+10}" y="{y+45}">{html.escape(cols[:34])}</text>',f'<text class="c" x="{x+10}" y="{y+65}">{t["count"]} rows</text>']
    chunks.append("</svg>")
    return "".join(chunks)


def fallback_query_pair(question_id: str, spec: DimensionalModelSpec, style: str) -> QueryPair:
    g=GRAINS[spec.grain_id]; fact=g.fact_table
    op_from=g.source_table
    state_op=f"{op_from} JOIN patient p ON p.id={g.patient_id} JOIN patient_address a ON a.patient_id=p.id AND a.is_current=1"
    state_ana=f"{fact} f JOIN dim_patient p ON p.patient_key=f.patient_key"
    state_col="p.state"
    if style=="snowflake":
        state_ana += " JOIN dim_geography geo ON geo.geography_key=p.geography_key"; state_col="geo.state"
    code_join=f"{fact} f JOIN dim_clinical_code c ON c.clinical_code_key=f.clinical_code_key"
    pairs = {
        "events_by_year": (f"SELECT CAST(substr({g.date_expr},1,4) AS INTEGER) year, COUNT(*) event_count FROM {op_from} GROUP BY year ORDER BY year", f"SELECT d.year, SUM(f.event_count) event_count FROM {fact} f JOIN dim_date d ON d.date_key=f.date_key GROUP BY d.year ORDER BY d.year"),
        "events_by_state": (f"SELECT a.state,COUNT(*) event_count FROM {state_op} GROUP BY a.state ORDER BY event_count DESC", f"SELECT {state_col} state,SUM(f.event_count) event_count FROM {state_ana} GROUP BY {state_col} ORDER BY event_count DESC"),
        "top_patients": (f"SELECT p.id,p.given_name,p.family_name,COUNT(*) event_count FROM {op_from} JOIN patient p ON p.id={g.patient_id} GROUP BY p.id ORDER BY event_count DESC LIMIT 5", f"SELECT p.patient_id,p.given_name,p.family_name,SUM(f.event_count) event_count FROM {fact} f JOIN dim_patient p ON p.patient_key=f.patient_key GROUP BY p.patient_id,p.given_name,p.family_name ORDER BY event_count DESC LIMIT 5"),
        "top_codes": (f"SELECT {g.code_expr} code,{g.code_display_expr} display,COUNT(*) event_count FROM {op_from} GROUP BY {g.code_expr},{g.code_display_expr} ORDER BY event_count DESC LIMIT 10", f"SELECT c.code,c.display,SUM(f.event_count) event_count FROM {code_join} GROUP BY c.code,c.display ORDER BY event_count DESC LIMIT 10"),
        "amount_by_year": (f"SELECT CAST(substr({g.date_expr},1,4) AS INTEGER) year,ROUND(SUM({g.amount_expr}),2) total_amount FROM {op_from} GROUP BY year ORDER BY year", f"SELECT d.year,ROUND(SUM(f.amount),2) total_amount FROM {fact} f JOIN dim_date d ON d.date_key=f.date_key GROUP BY d.year ORDER BY d.year"),
        "avg_value_by_code": (f"SELECT {g.code_expr} code,{g.code_display_expr} display,{g.unit_expr} unit,ROUND(AVG({g.value_expr}),2) average_value FROM {op_from} WHERE {g.value_expr} IS NOT NULL GROUP BY {g.code_expr},{g.code_display_expr},{g.unit_expr} ORDER BY display", f"SELECT c.code,c.display,f.unit,ROUND(AVG(f.numeric_value),2) average_value FROM {code_join} WHERE f.numeric_value IS NOT NULL GROUP BY c.code,c.display,f.unit ORDER BY c.display"),
    }
    op,ana=pairs.get(question_id,pairs["events_by_year"])
    return QueryPair(operational_sql=op,analytical_sql=ana,explanation="Equivalent queries expose normalized operational joins versus a fact-centered dimensional path.")


@dataclass
class AppState:
    op: sqlite3.Connection
    ana: sqlite3.Connection
    source_path: Path
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
    comparison: dict | None = None
    editor_result: dict | None = None
    error: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


def create_state() -> AppState:
    source=Path(os.getenv("SYNTHEA_FHIR_PATH") or ROOT/"data"/"fhir")
    op=connect("operational_poc"); ana=connect("analytical_poc")
    counts=load_operational(op,source); ai=AIService(); grains,used,note=ai.grains()
    state=AppState(op,ana,source,counts,ai,grains,used,note)
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
    return {"request":request,"s":s,"op_tables":schema_tables(s.op),"ana_tables":schema_tables(s.ana),"op_diagram":diagram_svg(s.op,"Normalized operational model"),"ana_diagram":diagram_svg(s.ana,f"{s.style.title()} analytical model"),"scd":scd2_state(s.op,s.ana),"glossary":GLOSSARY}


GLOSSARY={"Grain":"Exactly what one fact row represents.","Fact":"An event at a declared grain, with foreign keys and measures.","Dimension":"Descriptive context used to filter, group, and label facts.","Measure":"A numeric value that can be aggregated under defined rules.","Surrogate key":"A warehouse-controlled key identifying one dimension version.","ETL / ELT":"Extract, transform, and load—the movement from source to analytical structures.","Normalization":"Separate entities to reduce redundancy and enforce integrity.","Denormalization":"Deliberately repeat or flatten attributes to simplify analytical reads.","Star schema":"A central fact joined directly to denormalized dimensions.","Snowflake schema":"A dimensional model whose hierarchies are normalized into related tables.","SCD Type 2":"Preserve dimension history by expiring a row and inserting a new surrogate-keyed version."}


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


@app.post("/scd2/simulate")
async def scd_simulate(request:Request):
    s=request.app.state.demo
    with s.lock:
        try: simulate_scd2(s.op,s.ana,s.style); s.error=None
        except Exception as exc: s.error=str(exc)
    return RedirectResponse("/#scd2",303)


@app.post("/scd2/reset")
async def scd_reset(request:Request):
    s=request.app.state.demo
    with s.lock: s.build=reset_scd2(s.op,s.ana,s.source_path,s.spec,s.style); s.error=None
    return RedirectResponse("/#scd2",303)
