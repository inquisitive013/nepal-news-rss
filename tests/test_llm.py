import pytest

from newsroom import llm as llmmod
from newsroom.config import load_settings
from newsroom.images import PICK_SCHEMA
from newsroom.llm import (
    BudgetExceeded,
    MockLLM,
    UsageMeter,
    extract_json,
    strict_schema,
    validate_against_schema,
)
from newsroom.ranking import CASE_SCHEMA, STORY_SCHEMA, VERDICT_SCHEMA
from newsroom.validation import DEFENSE_SCHEMA, RED_TEAM_SCHEMA, RULING_SCHEMA
from newsroom.writing import ARTICLE_SCHEMA


def test_strict_schema_marks_every_object():
    s = strict_schema(STORY_SCHEMA)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == {"stories", "notes"}
    item = s["properties"]["stories"]["items"]
    assert item["additionalProperties"] is False
    assert "candidate_ids" in item["required"]
    # original untouched
    assert "additionalProperties" not in STORY_SCHEMA


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": {"b": [1, 2]}} thanks') == {"a": {"b": [1, 2]}}
    with pytest.raises(llmmod.LLMError):
        extract_json("no json here")


def test_validate_against_schema_reports_problems():
    schema = strict_schema(RULING_SCHEMA)
    good = {"decision": "approve", "rulings": [], "required_edits": [], "reason": "ok", "scores": {"accuracy": 1, "relevance": 1, "defensibility": 1, "virality": 1}}
    assert validate_against_schema(good, schema) == []
    bad = dict(good, decision="maybe")
    assert any("decision" in p for p in validate_against_schema(bad, schema))
    del bad["reason"]
    assert any("reason" in p for p in validate_against_schema(bad, schema))


def test_budget_guard():
    meter = UsageMeter(max_calls=1)
    meter.reserve()
    with pytest.raises(BudgetExceeded):
        meter.reserve()


def _mock():
    settings = load_settings(mock=True)
    return MockLLM(settings, UsageMeter(100))


def test_mock_outputs_match_every_schema():
    m = _mock()
    cands = [
        {"id": "c_1", "title": "Heavy rain puts Bagmati riverside settlements at risk", "summary": "140 households moved", "source": "The Kathmandu Post", "url": "https://kathmandupost.com/x", "published": "2026-09-26T00:00:00+00:00", "language": "en"},
        {"id": "c_2", "title": "काठमाडौंमा भारी वर्षा", "summary": "", "source": "OnlineKhabar", "url": "https://onlinekhabar.com/y", "published": None, "language": "ne"},
        {"id": "c_3", "title": "Nepal Oil Corporation cuts petrol price by Rs 5", "summary": "", "source": "Republica", "url": "https://myrepublica.nagariknetwork.com/z", "published": None, "language": "en"},
    ]
    stories = m.structured("story_clusterer", "", {"run_date": "2026-09-26", "max_stories": 5, "exclude_topics": [], "candidates": cands}, STORY_SCHEMA)
    assert len(stories["stories"]) == 3  # the mock groups by shared title words, so Nepali and English stay apart
    story = stories["stories"][0]
    adv = m.structured("advocate", "", {"story": story, "candidates": cands[:2], "guidance": {}}, CASE_SCHEMA)
    sk = m.structured("skeptic", "", {"story": story, "candidates": cands[:2], "advocate": adv, "guidance": {}}, CASE_SCHEMA)
    assert sk["score"] < adv["score"]
    debates = [{"story_id": story["id"], "advocate": adv, "skeptic": sk}]
    v1 = m.structured("ranking_judge", "", {"judge_position": 1, "stories": [story], "debates": debates, "previous_verdict": None}, VERDICT_SCHEMA)
    v2 = m.structured("ranking_judge", "", {"judge_position": 2, "stories": [story], "debates": debates, "previous_verdict": v1}, VERDICT_SCHEMA)
    assert v2["ranked"][0]["story_id"] == story["id"]
    art = m.structured("writer", "", {"story": story, "candidates": cands[:2], "debate": {}, "language": "en", "run_date": "2026-09-26"}, ARTICLE_SCHEMA)
    assert art["slug"] and art["image_brief"]["search_queries"]
    red = m.structured("red_team", "", {"round": 1, "article": art, "story": story, "candidates": cands}, RED_TEAM_SCHEMA)
    assert red["findings"][0]["severity"] == "high"
    df = m.structured("defense", "", {"article": art, "findings": red["findings"]}, DEFENSE_SCHEMA)
    j1 = m.structured("validation_judge", "", {"judge_position": 1, "article": dict(art, story_id=story["id"]), "red_team": red, "defense": df, "previous_ruling": None, "round": 1, "revisions_left": 2}, RULING_SCHEMA)
    assert j1["decision"] == "revise"
    rev = m.structured("reviser", "", {"article": art, "required_edits": j1["required_edits"], "findings": red["findings"], "defense": df}, ARTICLE_SCHEMA)
    assert "attributed" in rev["body_markdown"]
    pick = m.structured("image_picker", "", {"headline": "h", "dek": "d", "alt_hint": "", "candidates": []}, PICK_SCHEMA)
    assert pick["chosen_index"] == -1


def test_credit_error_detection():
    assert llmmod.is_credit_error("Error code: 400 - Your credit balance is too low to access the Anthropic API.")
    assert not llmmod.is_credit_error("Error code: 400 - messages: field required")


# The spend limit responses, verbatim: this morning's run, and the tier cap and workspace forms from Anthropic's rate limits page.
OWN_LIMIT = "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'You have reached your specified API usage limits. You will regain access on 2026-10-01 at 00:00 UTC.'}}"
WORKSPACE_LIMIT = "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'You have reached your specified workspace API usage limits. You will regain access on 2026-10-01 at 00:00 UTC.'}}"
TIER_CAP = "Error code: 429 - {'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'You have reached your API usage limits: your organization has crossed its monthly API usage threshold, set based on your organization's API tier. You will regain access on 2026-10-01 at 00:00 UTC.', 'details': {'error_code': 'enforced_spend_limit_reached'}}}"


def test_spend_limit_detection_and_the_plain_reason():
    for text in (OWN_LIMIT, WORKSPACE_LIMIT, TIER_CAP):
        assert llmmod.is_spend_limit_error(text)
    assert not llmmod.is_spend_limit_error("Error code: 429 - rate_limit_error: Number of request tokens has exceeded your per-minute rate limit")
    assert not llmmod.is_spend_limit_error("Error code: 400 - Your credit balance is too low to access the Anthropic API.")
    own = llmmod.spend_limit_message(OWN_LIMIT)
    assert "monthly spend limit set in the Claude Console" in own and "2026-10-01 at 00:00 UTC" in own and "Settings > Billing > Spend limits" in own
    assert "workspace spend limit" in llmmod.spend_limit_message(WORKSPACE_LIMIT)
    cap = llmmod.spend_limit_message(TIER_CAP)
    assert "usage tier's monthly spend cap" in cap and "Settings > Limits" in cap and "2026-10-01 at 00:00 UTC" in cap
    assert "Access returns" not in llmmod.spend_limit_message("You have reached your specified API usage limits.")


@pytest.mark.parametrize(
    "error_class,status,text",
    [("BadRequestError", 400, OWN_LIMIT), ("RateLimitError", 429, TIER_CAP)],
)
def test_the_live_client_stops_on_a_spend_limit_instead_of_retrying(monkeypatch, error_class, status, text):
    import anthropic
    httpx2 = pytest.importorskip("httpx2")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    client = llmmod.ClaudeLLM(load_settings(mock=False), llmmod.UsageMeter(10))
    attempts = []

    def refuse(kwargs):
        attempts.append(1)
        response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
        raise getattr(anthropic, error_class)(text, response=response, body=None)

    monkeypatch.setattr(client, "_create", refuse)
    with pytest.raises(llmmod.SpendLimitReached, match="2026-10-01 at 00:00 UTC"):
        client.structured("writer", "Write it.", {"story": "x"}, {"type": "object", "properties": {"headline": {"type": "string"}}})
    assert attempts == [1]  # no retry: nothing else in the run can succeed
