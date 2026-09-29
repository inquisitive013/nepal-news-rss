import copy
import dataclasses
from types import SimpleNamespace

import pytest

from newsroom import llm as llmmod
from newsroom.config import load_settings
from newsroom.models import UsageRecord

SCHEMA = {"type": "object", "properties": {"headline": {"type": "string"}}}


def _message(text='{"headline": "Rain"}', stop_reason="end_turn", model="claude-sonnet-5", searches=0):
    usage = SimpleNamespace(
        input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=searches),
    )
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage, stop_reason=stop_reason, model=model)


class FakeBatches:
    """The Message Batches API as the lane uses it. Each created batch follows the next spec."""

    def __init__(self, *specs):
        self.specs = list(specs)
        self.created: list[list[dict]] = []
        self.cancelled: list[str] = []
        self.state: dict[str, dict] = {}

    def create(self, *, requests):
        spec = self.specs.pop(0)
        if "raise" in spec:
            raise spec["raise"]
        bid = f"batch_{len(self.created)}"
        self.created.append(copy.deepcopy(requests))
        self.state[bid] = {"spec": spec, "polls": 0, "cancelled": False}
        return self._status(bid)

    def _status(self, bid):
        st = self.state[bid]
        spec = st["spec"]
        if st["cancelled"]:
            ended = spec.get("ends_after_cancel", True)
        else:
            ended = st["polls"] >= spec.get("polls", 0)
        return SimpleNamespace(id=bid, processing_status="ended" if ended else ("canceling" if st["cancelled"] else "in_progress"))

    def retrieve(self, bid):
        self.state[bid]["polls"] += 1
        return self._status(bid)

    def cancel(self, bid):
        self.state[bid]["cancelled"] = True
        self.cancelled.append(bid)
        return SimpleNamespace(id=bid, processing_status="canceling")

    def results(self, bid):
        st = self.state[bid]
        spec = st["spec"]
        if st["cancelled"] and not spec.get("finished_before_cancel"):
            yield SimpleNamespace(custom_id="x", result=SimpleNamespace(type="canceled"))
            return
        if spec.get("result", "succeeded") == "succeeded":
            yield SimpleNamespace(custom_id="x", result=SimpleNamespace(type="succeeded", message=spec.get("message") or _message()))
        else:
            err = SimpleNamespace(type="error", error=SimpleNamespace(type="invalid_request_error", message=spec.get("error", "tools are not supported")))
            yield SimpleNamespace(custom_id="x", result=SimpleNamespace(type="errored", error=err))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _lane(batches, clock, **kw):
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    opts = {"max_wait": 15 * 60, "run_seconds": 150 * 60, "poll": 15, **kw}
    return llmmod.BatchLane(client, sleep=clock.sleep, clock=clock, **opts)


PARAMS = {"model": "claude-sonnet-5", "max_tokens": 64000, "system": [{"type": "text", "text": "s"}], "messages": [{"role": "user", "content": "u"}]}


def test_an_answered_batch_returns_the_same_request_answered():
    clock = Clock()
    batches = FakeBatches({"polls": 3})
    lane = _lane(batches, clock)
    msg = lane.send("writer", PARAMS)
    assert msg is not None and msg.content[0].text == '{"headline": "Rain"}'
    [request] = batches.created[0]
    assert request["params"] == PARAMS  # nothing about the request changes in the lane
    assert request["custom_id"].startswith("writer-") and len(request["custom_id"]) <= 64
    assert lane.waits == [{"role": "writer", "model": "claude-sonnet-5", "searches": 0, "at": 0, "seconds": 45, "outcome": "answered"}]
    assert lane.open()


def test_a_call_that_waits_too_long_goes_the_normal_way_alone_and_the_lane_stays_open():
    clock = Clock()
    batches = FakeBatches({"polls": 10_000}, {"polls": 2})
    lane = _lane(batches, clock, max_wait=60)
    assert lane.send("red_team", PARAMS) is None
    assert batches.cancelled == ["batch_0"]
    assert lane.open() and lane.off_reason == ""  # one slow call does not shut the queue
    assert lane.send("defense", PARAMS) is not None  # the next call still rides at half price
    assert [(w["role"], w["at"], w["seconds"], w["outcome"]) for w in lane.waits] == [("red_team", 0, 75, "timed out"), ("defense", 75, 30, "answered")]


def test_a_request_that_finished_before_the_cancel_still_counts():
    clock = Clock()
    batches = FakeBatches({"polls": 10_000, "finished_before_cancel": True})
    lane = _lane(batches, clock, max_wait=60)
    assert lane.send("writer", PARAMS) is not None
    assert lane.waits[-1]["outcome"] == "answered as it was cancelled" and lane.open()


def test_a_failed_request_or_a_failed_api_closes_the_lane_without_raising():
    clock = Clock()
    lane = _lane(FakeBatches({"result": "errored", "error": "web search is not available in batches"}), clock)
    assert lane.send("writer", PARAMS) is None and "not available in batches" in lane.off_reason and not lane.open()
    assert lane.waits[-1]["outcome"] == "the request failed"
    lane = _lane(FakeBatches({"raise": RuntimeError("503 overloaded")}), clock)
    assert lane.send("writer", PARAMS) is None and "RuntimeError: 503 overloaded" in lane.off_reason
    assert lane.waits[-1]["outcome"] == "the batch API failed"


def test_the_lane_closes_once_the_run_is_old_enough():
    clock = Clock()
    lane = _lane(FakeBatches(), clock)
    assert lane.open()
    clock.now = 150 * 60
    assert not lane.open() and "past 150 minutes" in lane.off_reason


def test_every_wait_records_the_model_and_the_searches_allowed():
    clock = Clock()
    lane = _lane(FakeBatches({"polls": 1}), clock)
    params = {**PARAMS, "model": "claude-opus-5-5", "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}]}
    clock.now = 600  # the call joins the queue ten minutes into the run
    lane.send("investigator", params)
    assert lane.waits == [{"role": "investigator", "model": "claude-opus-5-5", "searches": 8, "at": 600, "seconds": 15, "outcome": "answered"}]


def test_the_wait_report_names_the_longest_waits():
    waits = [
        {"role": "advocate", "seconds": 200, "at": 170, "outcome": "answered"},
        {"role": "skeptic", "seconds": 1391, "at": 300, "outcome": "answered"},
        {"role": "investigator", "seconds": 900, "at": 3900, "outcome": "timed out"},
    ]
    report = llmmod.wait_report(waits)
    assert report.startswith("3 calls waited, median 15.0 min; 1 hit the wait limit and went the normal way.")
    assert "Longest: skeptic 23.2 min (joined 0:05 into the run, answered); investigator 15.0 min (joined 1:05 into the run, timed out); advocate 3.3 min" in report
    assert llmmod.wait_report([]) == ""


def test_usage_cost_halves_batch_tokens_but_not_searches():
    settings = load_settings(mock=True)
    normal = UsageRecord(role="writer", model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=1_000_000, cache_creation_input_tokens=100_000, web_search_requests=10)
    # 2.00 input + 1.00 output + 0.20 cache reads + 0.25 cache writes + 0.10 searches
    assert llmmod.usage_cost(settings, [normal]) == (pytest.approx(3.55), 0)
    batched = dataclasses.replace(normal, batch=True)
    assert llmmod.usage_cost(settings, [batched]) == (pytest.approx(3.45 / 2 + 0.10), 0)
    judge = UsageRecord(role="validation_judge", model="claude-opus-5-5", input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert llmmod.usage_cost(settings, [judge]) == (pytest.approx(4.20), 0)
    assert llmmod.usage_cost(settings, [UsageRecord(role="x", model="some-other-model")]) == (0.0, 1)


@pytest.fixture
def live(monkeypatch):
    """The live client with a fake batch API and a fake normal path, no network."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings(mock=False)
    raw = copy.deepcopy(settings.raw)
    raw["llm"]["batch"]["enabled"] = True
    settings = dataclasses.replace(settings, raw=raw)

    def make(*specs, normal=None):
        client = llmmod.ClaudeLLM(settings, llmmod.UsageMeter(10))
        assert client.batch is not None  # the setting switches the lane on
        clock = Clock()
        client.batch = _lane(FakeBatches(*specs), clock)
        asked = []

        def create(kwargs):
            asked.append(kwargs)
            return (normal or [_message('{"headline": "Normal"}')]).pop(0)

        monkeypatch.setattr(client, "_create", create)
        return client, asked

    return make


def test_the_live_client_takes_the_batch_answer_and_records_it_at_batch_price(live):
    client, asked = live({"polls": 2})
    assert client.structured("writer", "Write it.", {"story": "x"}, SCHEMA) == {"headline": "Rain"}
    assert asked == [] and [r.batch for r in client.meter.records] == [True]


def test_the_live_client_goes_the_normal_way_when_the_lane_fails(live):
    client, asked = live({"raise": RuntimeError("batches unavailable")})
    assert client.structured("writer", "Write it.", {"story": "x"}, SCHEMA) == {"headline": "Normal"}
    assert len(asked) == 1 and [r.batch for r in client.meter.records] == [False]
    assert client.structured("writer", "Write it again.", {"story": "y"}, SCHEMA) == {"headline": "Normal"}  # the lane stays shut


def test_a_slow_call_goes_the_normal_way_and_the_next_call_still_batches(live):
    client, asked = live({"polls": 10_000}, {"polls": 2})
    assert client.structured("red_team", "Attack it.", {"story": "x"}, SCHEMA) == {"headline": "Normal"}
    assert client.structured("defense", "Defend it.", {"story": "x"}, SCHEMA) == {"headline": "Rain"}
    assert len(asked) == 1 and [r.batch for r in client.meter.records] == [False, True]
    assert [w["outcome"] for w in client.batch.waits] == ["timed out", "answered"] and client.batch.open()


def test_a_refusal_in_the_lane_is_counted_and_asked_again_with_the_fallback(live):
    client, asked = live({"message": _message(text="", stop_reason="refusal")})
    assert client.structured("writer", "Write it.", {"story": "x"}, SCHEMA) == {"headline": "Normal"}
    assert len(asked) == 1 and [r.batch for r in client.meter.records] == [True, False]


def test_a_research_turn_continues_the_normal_way(live):
    client, asked = live({"message": _message(text="", stop_reason="pause_turn", searches=2)})
    assert client.structured("investigator", "Dig.", {"story": "x"}, SCHEMA, web_search_uses=8) == {"headline": "Normal"}
    assert len(asked) == 1 and len(asked[0]["messages"]) == 2  # the paused turn rides along
    assert [r.batch for r in client.meter.records] == [True, False]


class ProbeBatches(FakeBatches):
    """Answers each request the way the real queue would: with a search when it carries the tool."""

    def __init__(self, rounds):
        super().__init__(*[{} for _ in range(2 * rounds)])

    def results(self, bid):
        params = self.created[int(bid.split("_")[1])][0]["params"]
        yield SimpleNamespace(custom_id="x", result=SimpleNamespace(type="succeeded", message=_message(searches=1 if params.get("tools") else 0)))


def test_the_probe_sends_the_editions_shapes_and_reports_the_queue(monkeypatch, capsys):
    from newsroom import __main__ as cli

    batches = ProbeBatches(rounds=2)
    monkeypatch.setattr(llmmod, "build_client", lambda **_: SimpleNamespace(messages=SimpleNamespace(batches=batches)))
    assert cli.main(["batch-probe", "--rounds", "2"]) == 0
    sent = [reqs[0]["params"] for reqs in batches.created]
    assert len(sent) == 4 and sum(1 for p in sent if p.get("tools")) == 2
    settings = load_settings(mock=True)
    for params in sent:
        assert params["max_tokens"] == settings.get("llm.max_tokens") and params["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert params["output_config"]["format"]["type"] == "json_schema"
    out = capsys.readouterr().out
    assert "4 of 4 batch requests answered with valid JSON" in out and "Web search inside a batch request: works" in out


def test_a_spend_limit_met_in_the_queue_still_stops_the_run_at_once(live, monkeypatch):
    import anthropic

    httpx2 = pytest.importorskip("httpx2")
    limit = "You have reached your specified API usage limits. You will regain access on 2026-10-01 at 00:00 UTC."
    client, _ = live({"result": "errored", "error": limit})
    attempts = []

    def refuse(kwargs):
        attempts.append(1)
        response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
        raise anthropic.BadRequestError(limit, response=response, body=None)

    monkeypatch.setattr(client, "_create", refuse)
    with pytest.raises(llmmod.SpendLimitReached, match="2026-10-01 at 00:00 UTC"):
        client.structured("writer", "Write it.", {"story": "x"}, SCHEMA)
    assert attempts == [1] and not client.batch.open()  # one normal attempt names the limit; no retry, no more batches
