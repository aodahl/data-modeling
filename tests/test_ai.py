from app.ai import AIService
from app.models import GrainSuggestionSet


def test_offline_fallback(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY",raising=False)
    service=AIService()
    grains,used,note=service.grains()
    assert not used
    assert len(grains)>=5
    assert "not configured" in note
    spec,used,_=service.model_spec("encounter")
    assert spec.grain_id=="encounter" and not used
    questions,used,_=service.questions(spec)
    assert questions and not used


def test_valid_structured_response_is_used(monkeypatch):
    service=AIService(); service.enabled=True
    expected=GrainSuggestionSet(suggestions=[{
        "id":"encounter","name":"Encounter","grain_statement":"One row per encounter",
        "rationale":"Utilization","candidate_dimensions":["patient","date"],
        "candidate_measures":["event_count"],
    }])
    monkeypatch.setattr(service,"_parse",lambda *args,**kwargs:expected)
    grains,used,note=service.grains()
    assert used and note is None and grains[0].id=="encounter"


def test_failed_or_refused_response_falls_back(monkeypatch):
    service=AIService(); service.enabled=True
    monkeypatch.setattr(service,"_parse",lambda *args,**kwargs:(_ for _ in ()).throw(ValueError("refused or malformed")))
    grains,used,note=service.grains()
    assert not used and len(grains)>=5 and "malformed" in note
