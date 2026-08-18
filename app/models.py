from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


GrainId = Literal[
    "encounter", "observation", "claim", "claim_line",
    "condition", "procedure", "medication_request",
]


class GrainSuggestion(BaseModel):
    id: GrainId
    name: str
    grain_statement: str
    rationale: str
    candidate_dimensions: list[str]
    candidate_measures: list[str]


class GrainSuggestionSet(BaseModel):
    suggestions: list[GrainSuggestion] = Field(min_length=1, max_length=7)


class DimensionalModelSpec(BaseModel):
    grain_id: GrainId
    fact_name: str
    dimensions: list[str]
    measures: list[str]
    explanation: str


class BusinessQuestion(BaseModel):
    id: str
    question: str
    learning_objective: str


class BusinessQuestionSet(BaseModel):
    questions: list[BusinessQuestion] = Field(min_length=1, max_length=5)


class QueryPair(BaseModel):
    operational_sql: str
    analytical_sql: str
    explanation: str


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[object]]
    plan: list[str]
    tables: list[str]
    join_count: int
    truncated: bool = False

