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
    common = [
        BusinessQuestion(id="events_by_year", question="How many events occurred in each calendar year?", learning_objective="Compare date joins and aggregation."),
        BusinessQuestion(id="events_by_state", question="How many events involve patients in each state?", learning_objective="See flattened versus normalized geography."),
        BusinessQuestion(id="top_patients", question="Which five patients have the most events?", learning_objective="Group facts through a descriptive patient dimension."),
    ]
    if grain_id in {"claim", "claim_line"}:
        common.append(BusinessQuestion(id="amount_by_year", question="What is the total amount by year?", learning_objective="Aggregate an additive measure at its declared grain."))
    if grain_id == "observation":
        common.append(BusinessQuestion(id="avg_value_by_code", question="What is the average numeric value by observation code and unit?", learning_objective="Avoid mixing measures with incompatible units."))
    common.append(BusinessQuestion(id="top_codes", question="What are the most frequent clinical event types?", learning_objective="Slice a fact by a reusable clinical dimension."))
    return common[:5]

