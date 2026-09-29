"""
Trace-shape tests for Foundry Learn Agent.

Author  : dcodev1702 & M365 Copilot / Cowork
Created : 2026-09-29

The trace is the deliverable of this project, so these tests assert the trace: which spans exist, how they nest,
what they carry - plus the two contracts the README promises around it (the `trace_id` in every traced body and
`X-Trace-Id` header, and that /healthz creates no span at all). No network, no API key, no tokens.

Run:  pytest -q          (from the repository root; pytest.ini puts the root on the import path)
"""

# pylint: disable=missing-function-docstring  # each test's name states the behaviour it checks

from __future__ import annotations

import re
from types import SimpleNamespace

from opentelemetry.trace import SpanKind, StatusCode

from conftest import LEARN_URL, SEARCH_RESULT_TEXT, SESSION_ID, final_brief, function_call, llm_response, wait_for_spans

HEX32 = re.compile(r"^[0-9a-f]{32}$")
ASK_BODY = {"topic": "Microsoft Foundry", "paragraphs": 2, "links": 3}


def by_name(spans, name):
    """All finished spans with this exact name."""
    return [s for s in spans if s.name == name]


def server_spans(spans):
    """Only the root SERVER spans (the ASGI receive/send children are INTERNAL)."""
    return [s for s in spans if s.kind == SpanKind.SERVER]


def client_spans(spans):
    """Only the auto-instrumented outbound CLIENT spans."""
    return [s for s in spans if s.kind == SpanKind.CLIENT]


def story_spans(spans):
    """Every span except the ASGI plumbing children (`... http receive` / `... http send`)."""
    return [s for s in spans if " http " not in s.name]


def trace_id_of(span) -> str:
    """The span's trace id in the 32-hex form the API returns."""
    return format(span.context.trace_id, "032x")


def has_server_span(spans) -> bool:
    """Predicate for wait_for_spans: the request's root span has finished."""
    return bool(server_spans(spans))


# --------------------------------------------------------------------------------------------------------------
# /healthz and /ping - the control group and the cheapest span
# --------------------------------------------------------------------------------------------------------------
def test_healthz_is_not_traced(client, spans):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["exporter"] == "none"
    assert "x-trace-id" not in {k.lower() for k in response.headers}, "an excluded URL must not carry a trace id"
    # give the exporter a moment to prove that nothing shows up
    assert server_spans(wait_for_spans(spans, lambda s: False, timeout=0.3)) == [], "/healthz must create no span"


def test_ping_returns_the_trace_id_of_its_server_span(client, spans):
    response = client.get("/ping")

    assert response.status_code == 200
    body = response.json()
    assert body["pong"] is True
    assert HEX32.match(body["trace_id"]), body
    assert body["trace_url"] is None, "no TRACE_UI_URL configured -> no link"
    assert response.headers["x-trace-id"] == body["trace_id"]

    finished = wait_for_spans(spans, has_server_span)
    roots = server_spans(finished)
    assert len(roots) == 1 and roots[0].name == "GET /ping"
    assert trace_id_of(roots[0]) == body["trace_id"]
    assert all(trace_id_of(s) == body["trace_id"] for s in finished), "every span belongs to the same trace"


def test_trace_url_is_built_from_the_template(client, spans, monkeypatch):  # pylint: disable=unused-argument
    monkeypatch.setenv("TRACE_UI_URL", "http://localhost:16686/trace/{trace_id}")

    body = client.get("/ping").json()

    assert body["trace_url"] == f"http://localhost:16686/trace/{body['trace_id']}"


# --------------------------------------------------------------------------------------------------------------
# /tools - outbound tracing with no OpenAI call
# --------------------------------------------------------------------------------------------------------------
def test_tools_trace_has_one_manual_span_per_mcp_call_each_with_a_client_child(client, spans, learn):
    response = client.get("/tools")

    assert response.status_code == 200, response.text
    assert [t["name"] for t in response.json()] == [
        "microsoft_docs_search",
        "microsoft_code_sample_search",
        "microsoft_docs_fetch",
    ]
    assert learn.methods() == ["initialize", "notifications/initialized", "tools/list"]
    assert learn.requests[2]["session_id"] == SESSION_ID, "session id must be echoed back"
    assert "id" not in learn.requests[1]["body"], "a JSON-RPC notification carries no id"
    assert all(r["traceparent"] for r in learn.requests), "the httpx instrumentation injects W3C traceparent"

    finished = wait_for_spans(spans, has_server_span)
    root = server_spans(finished)[0]
    assert root.name == "GET /tools"
    assert trace_id_of(root) == response.headers["x-trace-id"]

    manual = [by_name(finished, n)[0] for n in ("mcp initialize", "mcp notifications/initialized", "mcp tools/list")]
    for span in manual:
        assert span.kind == SpanKind.INTERNAL
        assert span.parent.span_id == root.context.span_id, f"{span.name} must hang off the SERVER span"
        assert span.attributes["rpc.system"] == "jsonrpc"

    outbound = client_spans(finished)
    assert len(outbound) == 3, [s.name for s in outbound]
    assert {s.parent.span_id for s in outbound} == {
        s.context.span_id for s in manual
    }, "each auto CLIENT span is the child of exactly one manual mcp span"


def test_learn_failure_returns_502_with_the_trace_id(client, spans, learn):
    learn.fail_method = "tools/list"

    response = client.get("/tools")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["error"].startswith("Microsoft Learn MCP transport error")
    assert HEX32.match(detail["trace_id"])
    assert detail["trace_id"] == response.headers["x-trace-id"], "a failed request is as findable as a good one"
    assert detail["trace_url"] is None

    finished = wait_for_spans(spans, has_server_span)
    root = server_spans(finished)[0]
    assert trace_id_of(root) == detail["trace_id"]
    assert root.status.status_code == StatusCode.ERROR, "a 5xx marks the SERVER span as ERROR"
    assert by_name(finished, "mcp tools/list")[0].status.status_code == StatusCode.ERROR

    assert client.get("/healthz").json()["errors"].get("/tools", 0) >= 1, "the stats counters saw the 5xx too"


# --------------------------------------------------------------------------------------------------------------
# /ask - the local tool loop
# --------------------------------------------------------------------------------------------------------------
def test_ask_local_tools_trace_shape(client, spans, learn, openai):
    openai.script.extend(
        [
            llm_response([function_call("microsoft_docs_search", {"query": "Microsoft Foundry"})]),
            llm_response([SimpleNamespace(type="message")], text=final_brief(2, 3), input_tokens=300),
        ]
    )

    response = client.post("/ask", json=ASK_BODY)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "local-tools"
    assert body["turns"] == 2
    assert body["usage"] == {"input_tokens": 400, "output_tokens": 40}
    assert len(body["brief"]["paragraphs"]) == 2 and len(body["brief"]["links"]) == 3
    assert body["brief"]["links"][0]["url"].startswith(LEARN_URL)
    assert [t["tool"] for t in body["tool_calls"]] == ["microsoft_docs_search"]
    assert body["tool_calls"][0]["duration_ms"] is not None and body["tool_calls"][0]["error"] is None
    assert body["trace_id"] == response.headers["x-trace-id"]

    # the second LLM turn carried the tool result back; the tools were offered as (loose) function tools
    fed_back = openai.calls[1]["input"][-1]
    assert fed_back["type"] == "function_call_output" and fed_back["call_id"] == "call_1"
    assert fed_back["output"] == SEARCH_RESULT_TEXT
    assert openai.calls[0]["tools"][0] == {
        "type": "function",
        "name": "microsoft_docs_search",
        "description": "Search Microsoft Learn.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        "strict": False,
    }
    assert openai.calls[0]["text"]["format"]["strict"] is True
    assert learn.methods() == ["initialize", "notifications/initialized", "tools/list", "tools/call"]

    finished = wait_for_spans(spans, has_server_span)
    root = server_spans(finished)[0]
    assert root.name == "POST /ask" and trace_id_of(root) == body["trace_id"]

    agent_run = by_name(finished, "agent.run")[0]
    assert agent_run.parent.span_id == root.context.span_id
    assert agent_run.attributes["agent.mode"] == "local-tools"
    assert agent_run.attributes["agent.turns"] == 2
    assert agent_run.attributes["agent.tool_calls"] == 1
    assert agent_run.attributes["gen_ai.usage.input_tokens"] == 400
    assert list(agent_run.attributes["agent.tools"]) == [
        "microsoft_docs_search",
        "microsoft_code_sample_search",
        "microsoft_docs_fetch",
    ]

    turns = sorted(by_name(finished, "llm.turn"), key=lambda s: s.start_time)
    assert [t.attributes["agent.turn"] for t in turns] == [1, 2]
    assert all(t.parent.span_id == agent_run.context.span_id for t in turns)
    assert turns[1].attributes["gen_ai.usage.input_tokens"] == 300

    tool_span = by_name(finished, "mcp tools/call microsoft_docs_search")[0]
    assert tool_span.parent.span_id == agent_run.context.span_id
    assert tool_span.attributes["mcp.tool.name"] == "microsoft_docs_search"
    assert '"query": "Microsoft Foundry"' in tool_span.attributes["mcp.tool.arguments"]

    # one auto CLIENT span under each manual mcp span (the fake OpenAI client makes no HTTP call)
    mcp_spans = [s for s in finished if s.name.startswith("mcp ")]
    assert len(mcp_spans) == 4
    assert {s.parent.span_id for s in client_spans(finished)} == {s.context.span_id for s in mcp_spans}


def test_ask_tool_failure_is_fed_back_to_the_model_and_recorded_on_the_span(client, spans, learn, openai):
    learn.fail_method = "tools/call"
    openai.script.extend(
        [
            llm_response([function_call("microsoft_docs_search", {"query": "x"}, call_id="call_9")]),
            llm_response([SimpleNamespace(type="message")], text=final_brief()),
        ]
    )

    response = client.post("/ask", json=ASK_BODY)

    assert response.status_code == 200, response.text
    record = response.json()["tool_calls"][0]
    assert record["error"] and "500" in record["error"]
    assert openai.calls[1]["input"][-1]["output"].startswith("TOOL ERROR"), "the model sees the failure"

    finished = wait_for_spans(spans, has_server_span)
    agent_run = by_name(finished, "agent.run")[0]
    assert any(e.name == "exception" for e in agent_run.events), "record_exception() leaves an event on agent.run"
    assert agent_run.status.status_code != StatusCode.ERROR, "a tool failure does not fail the run"
    assert by_name(finished, "mcp tools/call microsoft_docs_search")[0].status.status_code == StatusCode.ERROR


# pylint: disable-next=unused-argument  # `learn` is requested only to reset the fake Learn server
def test_ask_schema_violation_returns_502_with_trace_id(client, spans, learn, openai):
    openai.script.append(llm_response([SimpleNamespace(type="message")], text='{"not": "a brief"}'))

    response = client.post("/ask", json=ASK_BODY)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["error"].startswith("The model's answer did not match the expected schema")
    assert detail["trace_id"] == response.headers["x-trace-id"]
    finished = wait_for_spans(spans, has_server_span)
    assert by_name(finished, "agent.run")[0].status.status_code == StatusCode.ERROR


# --------------------------------------------------------------------------------------------------------------
# /ask-hosted - OpenAI runs the tools; the trace collapses
# --------------------------------------------------------------------------------------------------------------
def test_ask_hosted_trace_collapses_to_one_llm_turn(client, spans, learn, openai):
    openai.script.append(
        llm_response(
            [
                SimpleNamespace(
                    type="mcp_list_tools",
                    tools=[SimpleNamespace(name="microsoft_docs_search"), SimpleNamespace(name="microsoft_docs_fetch")],
                ),
                SimpleNamespace(
                    type="mcp_call",
                    name="microsoft_docs_search",
                    arguments='{"query": "Microsoft Foundry"}',
                    output="x" * 50,
                    error=None,
                ),
                SimpleNamespace(
                    type="mcp_call", name="microsoft_docs_fetch", arguments="not json", output=None, error="timeout"
                ),
                SimpleNamespace(type="message"),
            ],
            text=final_brief(2, 3),
            input_tokens=500,
            output_tokens=80,
        )
    )

    response = client.post("/ask-hosted", json=ASK_BODY)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "hosted-mcp" and body["turns"] == 1
    assert [t["tool"] for t in body["tool_calls"]] == ["microsoft_docs_search", "microsoft_docs_fetch"]
    assert body["tool_calls"][0]["duration_ms"] is None and body["tool_calls"][0]["result_chars"] == 50
    assert body["tool_calls"][1]["error"] == "timeout"
    assert body["tool_calls"][1]["arguments"] == {"_raw": "not json"}
    assert openai.calls[0]["tools"] == [
        {
            "type": "mcp",
            "server_label": "microsoft_learn",
            "server_url": "https://learn.microsoft.com/api/mcp",
            "require_approval": "never",
        }
    ]
    assert learn.methods() == [], "hosted mode never talks to Learn from this process"

    finished = wait_for_spans(spans, has_server_span)
    assert sorted(s.name for s in story_spans(finished)) == ["POST /ask-hosted", "agent.run", "llm.turn"]
    assert client_spans(finished) == [], "the fake OpenAI client makes no HTTP call, and Learn is never called"

    agent_run = by_name(finished, "agent.run")[0]
    assert agent_run.attributes["agent.mode"] == "hosted-mcp"
    assert agent_run.attributes["agent.turns"] == 1 and agent_run.attributes["agent.tool_calls"] == 2
    assert list(agent_run.attributes["agent.tools"]) == ["microsoft_docs_search", "microsoft_docs_fetch"]
    assert [e.name for e in agent_run.events] == ["mcp_call.error"]


# --------------------------------------------------------------------------------------------------------------
# The stats heartbeat counts everything - including the probes that never became spans
# --------------------------------------------------------------------------------------------------------------
def test_stats_snapshot_has_the_documented_shape(client):
    snapshot = client.get("/healthz").json()

    assert snapshot["requests"]["/healthz"] >= 1, "this very request was counted before the snapshot was built"
    assert set(snapshot) >= {
        "status", "service", "version", "time", "uptime_s", "exporter", "model", "rss_mb",
        "requests", "errors", "llm_turns", "tool_calls", "tokens",
    }
    assert isinstance(snapshot["llm_turns"], int) and isinstance(snapshot["tool_calls"], int)
    assert set(snapshot["tokens"]) == {"input_tokens", "output_tokens"}
