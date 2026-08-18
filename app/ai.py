from __future__ import annotations

import json
import os
from typing import TypeVar

from pydantic import BaseModel

from .catalog import GRAINS, fallback_grains, fallback_questions, fallback_spec, validate_spec
from .models import BusinessQuestionSet, DimensionalModelSpec, GrainSuggestionSet, QueryPair

T = TypeVar("T", bound=BaseModel)


class AIService:
    def __init__(self) -> None:
        self.model = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
        self.enabled = bool(os.getenv("OPENAI_API_KEY"))

    def _parse(self, schema: type[T], instructions: str, payload: dict) -> T:
        if not self.enabled:
            raise RuntimeError("OpenAI API key not configured")
        from openai import OpenAI
        client = OpenAI(timeout=25.0, max_retries=1)
        last_error: Exception | None = None
        for _ in range(2):
            try:
                response = client.responses.parse(model=self.model, instructions=instructions, input=json.dumps(payload), text_format=schema)
                if response.output_parsed is None:
                    raise ValueError("The model did not return a parsed response")
                return response.output_parsed
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"AI generation failed: {last_error}")

    @property
    def catalog_payload(self) -> dict:
        return {k:{"grain":v.label,"dimensions":["patient","date","clinical_code"] + (["organization"] if v.organization_expr else []),"measures":["event_count"] + (["amount"] if v.amount_expr else []) + (["numeric_value"] if v.value_expr else [])} for k,v in GRAINS.items()}

    def grains(self):
        try:
            result = self._parse(GrainSuggestionSet, "Suggest 5 distinct, pedagogically useful dimensional-model grains. Use only catalog IDs and capabilities. Never request row data.", self.catalog_payload)
            return result.suggestions, True, None
        except Exception as exc:
            return fallback_grains(), False, str(exc)

    def model_spec(self, grain_id: str):
        try:
            result = self._parse(DimensionalModelSpec, "Design a valid dimensional model using only listed identifiers. Include patient and date dimensions. fact_name will be normalized by the trusted compiler.", {"selected_grain":grain_id,"catalog":self.catalog_payload[grain_id]})
            return validate_spec(result), True, None
        except Exception as exc:
            return fallback_spec(grain_id), False, str(exc)

    def questions(self, spec: DimensionalModelSpec):
        try:
            result = self._parse(BusinessQuestionSet, "Write exactly five concise business questions answerable by both the operational and dimensional models. Do not include SQL or patient values.", spec.model_dump())
            return result.questions, True, None
        except Exception as exc:
            return fallback_questions(spec.grain_id), False, str(exc)

    def query_pair(self, question_id: str, question: str, spec: DimensionalModelSpec, style: str, op_schema: str, ana_schema: str) -> QueryPair | None:
        try:
            return self._parse(QueryPair, "Return one SQLite SELECT for each schema that answers the same question. Use only supplied tables/columns. No semicolons, comments, PRAGMA, DDL, DML, ATTACH, or functions that access files. Use LIMIT 20 for detail queries.", {"question_id":question_id,"question":question,"grain":spec.model_dump(),"style":style,"operational_schema":op_schema,"analytical_schema":ana_schema})
        except Exception:
            return None

