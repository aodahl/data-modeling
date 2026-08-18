from __future__ import annotations

from dataclasses import dataclass

from .models import BusinessQuestion, DimensionalModelSpec, GrainSuggestion


@dataclass(frozen=True)
class GrainDefinition:
    id: str
    label: str
    source_table: str
    fact_table: str
    event_id: str
    patient_id: str
    date_expr: str
    code_expr: str | None
    code_display_expr: str | None
    amount_expr: str | None = None
    value_expr: str | None = None
    unit_expr: str | None = None
    organization_expr: str | None = None


GRAINS: dict[str, GrainDefinition] = {
    "encounter": GrainDefinition("encounter", "One row per encounter", "encounter e", "fact_encounter", "e.id", "e.patient_id", "substr(e.start_at,1,10)", "e.type_code", "e.type_display", organization_expr="e.organization_id"),
    "observation": GrainDefinition("observation", "One row per observation", "observation e", "fact_observation", "e.id", "e.patient_id", "substr(e.effective_at,1,10)", "e.code", "e.code_display", value_expr="e.value", unit_expr="e.unit"),
    "claim": GrainDefinition("claim", "One row per claim", "claim e", "fact_claim", "e.id", "e.patient_id", "substr(e.created_at,1,10)", "e.claim_type", "e.claim_type", amount_expr="e.total_amount", organization_expr="e.organization_id"),
    "claim_line": GrainDefinition("claim_line", "One row per claim line", "claim_item e JOIN claim c ON c.id=e.claim_id", "fact_claim_line", "e.claim_id || ':' || e.sequence", "c.patient_id", "substr(c.created_at,1,10)", "e.product_code", "e.product_display", amount_expr="e.net_amount", organization_expr="c.organization_id"),
    "condition": GrainDefinition("condition", "One row per condition episode", "condition e", "fact_condition", "e.id", "e.patient_id", "substr(COALESCE(e.onset_at,e.recorded_at),1,10)", "e.code", "e.code_display"),
    "procedure": GrainDefinition("procedure", "One row per procedure", "procedure e", "fact_procedure", "e.id", "e.patient_id", "substr(e.start_at,1,10)", "e.code", "e.code_display"),
    "medication_request": GrainDefinition("medication_request", "One row per medication request", "medication_request e", "fact_medication_request", "e.id", "e.patient_id", "substr(e.authored_at,1,10)", "e.medication_code", "e.medication_display"),
}

ALLOWED_DIMENSIONS = {"patient", "date", "clinical_code", "organization"}
ALLOWED_MEASURES = {"event_count", "amount", "numeric_value"}


def fallback_grains() -> list[GrainSuggestion]:
    return [
        GrainSuggestion(id=k, name=v.label, grain_statement=v.label, rationale=r,
                        candidate_dimensions=d, candidate_measures=m)
        for k, v, r, d, m in [
            ("encounter", GRAINS["encounter"], "A clear event grain for visits and utilization.", ["patient", "date", "clinical_code", "organization"], ["event_count"]),
            ("observation", GRAINS["observation"], "Supports numeric clinical trends while exposing unit concerns.", ["patient", "date", "clinical_code"], ["event_count", "numeric_value"]),
            ("claim", GRAINS["claim"], "Shows financial aggregation at claim header grain.", ["patient", "date", "organization"], ["event_count", "amount"]),
            ("claim_line", GRAINS["claim_line"], "Demonstrates how a finer grain changes totals and dimensions.", ["patient", "date", "clinical_code", "organization"], ["event_count", "amount"]),
            ("procedure", GRAINS["procedure"], "Supports procedure utilization by patient and time.", ["patient", "date", "clinical_code"], ["event_count"]),
        ]
    ]


def fallback_spec(grain_id: str) -> DimensionalModelSpec:
    g = GRAINS[grain_id]
    dimensions = ["patient", "date"]
    if g.code_expr:
        dimensions.append("clinical_code")
    if g.organization_expr:
        dimensions.append("organization")
    measures = ["event_count"]
    if g.amount_expr:
        measures.append("amount")
    if g.value_expr:
        measures.append("numeric_value")
    return DimensionalModelSpec(
        grain_id=grain_id, fact_name=g.fact_table, dimensions=dimensions,
        measures=measures,
        explanation=f"A dimensional model at the grain: {g.label.lower()}.",
    )


def validate_spec(spec: DimensionalModelSpec) -> DimensionalModelSpec:
    if spec.grain_id not in GRAINS:
        raise ValueError("Unsupported grain")
    if not set(spec.dimensions) <= ALLOWED_DIMENSIONS:
        raise ValueError("Unsupported dimension")
    if not set(spec.measures) <= ALLOWED_MEASURES:
        raise ValueError("Unsupported measure")
    if not {"patient", "date"} <= set(spec.dimensions):
        raise ValueError("Patient and date dimensions are required")
    g = GRAINS[spec.grain_id]
    if "amount" in spec.measures and not g.amount_expr:
        raise ValueError("Amount is not available for this grain")
    if "numeric_value" in spec.measures and not g.value_expr:
        raise ValueError("Numeric value is not available for this grain")
    if "organization" in spec.dimensions and not g.organization_expr:
        raise ValueError("Organization is not available for this grain")
    spec.fact_name = g.fact_table
    return spec


def fallback_questions(grain_id: str) -> list[BusinessQuestion]:
    def q(id: str, question: str, objective: str) -> BusinessQuestion:
        return BusinessQuestion(id=id, question=question, learning_objective=objective)

    # Every grain also gets at least one question whose operational answer needs
    # a multi-hop path plus per-row date parsing, so the dimensional model's
    # shorter join path and precomputed attributes are visible in the plans.
    state_quarter = q("events_by_state_and_quarter", "How does volume trend by patient state and calendar quarter?", "See the cost of reaching geography through a child table and deriving quarters per row versus reading dim_date and a denormalized dimension.")
    distinct_state_year = q("distinct_patients_by_state_and_year", "How many distinct patients are active per state each year?", "Compare distinct counting over text patient IDs across joined tables with distinct counting over integer surrogate keys in the fact.")
    organization_state_year = q("events_by_organization_state_and_year", "Which organization and patient-state combinations drive volume each year?", "Watch the operational path grow to four joins while the star answers the same crosstab from the fact and its dimensions.")
    amount_organization_state_year = q("amount_by_organization_state_and_year", "Which organization and patient-state combinations account for the most amount each year?", "Aggregate a financial measure across three dimensions and compare the deep operational join path with the fact-centered one.")

    questions = {
        "encounter": [
            q("events_by_year", "How has encounter volume changed by year?", "Analyze utilization through the date dimension."),
            q("events_by_state", "Which patient states account for the most encounters?", "Compare geographic slicing in star and snowflake layouts."),
            q("events_by_organization", "Which organizations handle the most encounters?", "Aggregate utilization through an organization dimension."),
            q("top_codes", "What are the most common encounter types?", "Use encounter type as descriptive clinical context."),
            q("top_patients", "Which patients have the highest encounter utilization?", "Identify high-utilization patients without changing the encounter grain."),
            state_quarter,
            organization_state_year,
        ],
        "observation": [
            q("events_by_year", "How has observation volume changed by year?", "Separate measurement frequency from measured values."),
            q("avg_value_by_code", "What is the average numeric value by observation and unit?", "Avoid combining values with incompatible meanings or units."),
            q("avg_value_by_year", "How do average values change by observation, year, and unit?", "Aggregate a non-additive clinical measure over time without mixing observations."),
            q("events_by_state", "Which patient states generate the most observations?", "Slice clinical activity geographically."),
            q("distinct_patients_by_code", "How many distinct patients have each observation type?", "Contrast event frequency with population reach."),
            state_quarter,
            distinct_state_year,
        ],
        "claim": [
            q("amount_by_year", "What is the total claimed amount by year?", "Aggregate a claim-header financial measure over time."),
            q("amount_by_organization", "Which organizations account for the most claimed amount?", "Analyze financial totals by organization."),
            q("amount_by_state", "How is claimed amount distributed across patient states?", "Combine financial and geographic dimensions."),
            q("top_patients_by_amount", "Which patients have the highest total claimed amount?", "Rank patients using an additive measure rather than event counts."),
            q("avg_amount_by_code", "What is the average claim amount by claim type?", "Distinguish averages from additive totals."),
            amount_organization_state_year,
            state_quarter,
        ],
        "claim_line": [
            q("amount_by_code", "Which products account for the most claim-line amount?", "Show how product-level grain enables spend analysis."),
            q("avg_amount_by_code", "What is the average line amount for each product?", "Compare average line cost across products."),
            q("amount_by_organization", "Which organizations account for the most claim-line amount?", "Roll a fine-grained financial measure up by organization."),
            q("top_patients_by_amount", "Which patients have the highest total claim-line amount?", "Aggregate fine-grained lines safely to patients."),
            q("events_by_year", "How has claim-line volume changed by year?", "Contrast transaction volume with monetary totals."),
            amount_organization_state_year,
            state_quarter,
        ],
        "condition": [
            q("top_codes", "Which conditions are recorded most often?", "Rank diagnoses using the clinical-code dimension."),
            q("events_by_year", "How many condition episodes begin or are recorded each year?", "Apply the condition grain's date definition."),
            q("events_by_state", "How are condition episodes distributed across patient states?", "Explore geographic variation in recorded conditions."),
            q("distinct_patients_by_code", "How many distinct patients have each condition?", "Measure prevalence without double-counting episodes."),
            q("events_by_code_and_year", "How do condition counts by diagnosis change over time?", "Use two dimensions in one grouped analysis."),
            state_quarter,
            distinct_state_year,
        ],
        "procedure": [
            q("top_codes", "Which procedures are performed most often?", "Analyze procedure mix by clinical code."),
            q("events_by_year", "How has procedure volume changed by year?", "Track utilization trends over time."),
            q("events_by_state", "Which patient states account for the most procedures?", "Slice procedure utilization geographically."),
            q("distinct_patients_by_code", "How many distinct patients receive each procedure?", "Separate patient reach from repeat procedure volume."),
            q("events_by_code_and_year", "How does procedure mix change by year?", "Combine date and procedure dimensions."),
            state_quarter,
            distinct_state_year,
        ],
        "medication_request": [
            q("top_codes", "Which medications are requested most often?", "Analyze medication mix by clinical code."),
            q("events_by_year", "How has medication-request volume changed by year?", "Track prescribing activity over time."),
            q("events_by_state", "How are medication requests distributed across patient states?", "Explore geographic differences in prescribing."),
            q("distinct_patients_by_code", "How many distinct patients receive each medication request?", "Contrast request volume with patient reach."),
            q("events_by_code_and_year", "How does medication-request mix change by year?", "Combine medication and date dimensions."),
            state_quarter,
            distinct_state_year,
        ],
    }
    return questions[grain_id]

