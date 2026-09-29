"""
Metrics and logs tests for Foundry Learn Agent.

Author  : dcodev1702 & M365 Copilot / Cowork
Created : 2026-09-29

Traces show one request; metrics and logs are the other two OpenTelemetry signals. These tests assert what the
app records as metrics (the GenAI semantic-convention histograms, our agent.* instruments, the free http.* metrics,
process memory) and as logs (structured records stamped with the trace they belong to). The conftest installs an
in-memory MeterProvider and LoggerProvider before the app is imported, so no exporter, network or key is needed.

Run:  pytest -q          (from the repository root; pytest.ini puts the root on the import path)
"""

# pylint: disable=missing-function-docstring  # each test's name states the behaviour it checks

from __future__ import annotations

import logging
from types import SimpleNamespace

from conftest import final_brief, function_call, llm_response

import app as app_module

ASK_BODY = {"topic": "Microsoft Foundry", "paragraphs": 2, "links": 3}


def first_of(data, *names):
    """Data points of the first metric name present - the HTTP metrics are renamed under the new semconv."""
    return next((data[name] for name in names if name in data), [])


def hex_trace_id(record) -> str:
    """A log record's trace id in the 32-hex form the API returns."""
    return format(record.trace_id, "032x")


# --------------------------------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------------------------------
# pylint: disable-next=unused-argument  # `learn` is requested only to reset the fake Learn server
def test_ask_records_token_usage_llm_duration_and_tool_metrics(client, learn, openai, metrics_data):
    openai.script.extend(
        [
            llm_response([function_call("microsoft_docs_search", {"query": "Microsoft Foundry"})]),
            llm_response([SimpleNamespace(type="message")], text=final_brief(2, 3), input_tokens=300),
        ]
    )

    assert client.post("/ask", json=ASK_BODY).status_code == 200

    data = metrics_data()
    tokens = {point.attributes["gen_ai.token.type"]: point for point in data["gen_ai.client.token.usage"]}
    assert (tokens["input"].sum, tokens["input"].count) == (400, 2), "one data point per LLM call, per token type"
    assert (tokens["output"].sum, tokens["output"].count) == (40, 2)
    for point in tokens.values():
        assert point.attributes["gen_ai.request.model"] == app_module.OPENAI_MODEL
        assert point.attributes["agent.mode"] == "local-tools"

    (llm,) = data["gen_ai.client.operation.duration"]
    assert llm.count == 2 and "error.type" not in llm.attributes

    (calls,) = data["agent.tool.calls"]
    assert calls.value == 1
    assert dict(calls.attributes) == {"mcp.tool.name": "microsoft_docs_search", "agent.mode": "local-tools"}
    (duration,) = data["agent.tool.duration"]
    assert duration.count == 1 and duration.sum >= 0


# pylint: disable-next=unused-argument  # `learn` is requested only to reset the fake Learn server
def test_hosted_tool_calls_are_counted_but_not_timed(client, learn, openai, metrics_data):
    openai.script.append(
        llm_response(
            [
                SimpleNamespace(type="mcp_call", name="microsoft_docs_search", arguments="{}", output="x", error=None),
                SimpleNamespace(
                    type="mcp_call", name="microsoft_docs_fetch", arguments="{}", output=None, error="timeout"
                ),
                SimpleNamespace(type="message"),
            ],
            text=final_brief(),
        )
    )

    assert client.post("/ask-hosted", json=ASK_BODY).status_code == 200

    data = metrics_data()
    calls = {point.attributes["mcp.tool.name"]: point for point in data["agent.tool.calls"]}
    assert calls["microsoft_docs_search"].value == 1 and "error.type" not in calls["microsoft_docs_search"].attributes
    assert calls["microsoft_docs_fetch"].attributes["error.type"] == "mcp_call.error"
    assert all(point.attributes["agent.mode"] == "hosted-mcp" for point in calls.values())
    assert "agent.tool.duration" not in data, "OpenAI ran these calls; this process never saw how long they took"
    (llm,) = data["gen_ai.client.operation.duration"]
    assert llm.count == 1 and llm.attributes["agent.mode"] == "hosted-mcp"


def test_http_server_metrics_come_free_but_skip_the_excluded_healthz(client, metrics_data):
    client.get("/ping")
    client.get("/ping")
    client.get("/healthz")

    points = first_of(metrics_data(), "http.server.duration", "http.server.request.duration")
    by_route = {point.attributes.get("http.route") or point.attributes.get("http.target"): point for point in points}
    assert by_route["/ping"].count == 2, "the FastAPI instrumentation times every traced request"
    assert "/healthz" not in by_route, "excluded URLs get neither a span nor a metric - the heartbeat still counts them"


# pylint: disable-next=unused-argument  # `learn` is requested only to reset the fake Learn server
def test_learn_calls_land_in_the_http_client_metric(client, learn, metrics_data):
    assert client.get("/tools").status_code == 200

    points = first_of(metrics_data(), "http.client.duration", "http.client.request.duration")
    assert sum(point.count for point in points) == 3, "initialize, notifications/initialized and tools/list"


# pylint: disable-next=unused-argument  # `learn` is requested only to reset the fake Learn server
def test_a_failed_llm_call_is_timed_with_error_type(client, learn, openai, metrics_data):
    openai.script.append(RuntimeError("model unavailable"))

    assert client.post("/ask", json=ASK_BODY).status_code == 502

    (llm,) = metrics_data()["gen_ai.client.operation.duration"]
    assert llm.count == 1 and llm.attributes["error.type"] == "RuntimeError"


def test_process_memory_usage_reports_this_process(metrics_data):
    (point,) = metrics_data()["process.memory.usage"]
    assert point.value > 10 * 1024 * 1024, "resident memory in bytes, read at collection time"


# --------------------------------------------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------------------------------------------
# pylint: disable-next=unused-argument  # `learn` is requested only to reset the fake Learn server
def test_logs_carry_the_trace_id_of_their_request(client, learn, openai, logs):
    openai.script.extend(
        [
            llm_response([function_call("microsoft_docs_search", {"query": "Microsoft Foundry"})]),
            llm_response([SimpleNamespace(type="message")], text=final_brief()),
        ]
    )

    response = client.post("/ask", json=ASK_BODY)

    records = logs()
    bodies = [record.body for record in records]
    assert bodies[0].startswith("agent.run started: local tools")
    assert any(body.startswith("tool microsoft_docs_search returned") for body in bodies)
    assert bodies[-1].startswith("agent.run finished: turns=2 tool_calls=1")
    assert {hex_trace_id(record) for record in records} == {response.json()["trace_id"]}, "every record is correlated"
    tool_record = next(record for record in records if record.body.startswith("tool "))
    assert tool_record.attributes["mcp.tool.name"] == "microsoft_docs_search"
    assert tool_record.attributes["agent.mode"] == "local-tools"


# pylint: disable-next=unused-argument  # `learn` is requested only to reset the fake Learn server
def test_an_upstream_failure_logs_a_warning_with_the_exception(client, learn, openai, logs):
    openai.script.append(RuntimeError("model unavailable"))

    response = client.post("/ask", json=ASK_BODY)

    (failure,) = [record for record in logs() if record.body.startswith("upstream failure")]
    assert failure.severity_number.name.startswith("WARN")
    assert failure.attributes["exception.type"] == "RuntimeError"
    assert "model unavailable" in failure.attributes["exception.message"]
    assert hex_trace_id(failure) == response.json()["detail"]["trace_id"], "the log links to the failed request"


def test_a_failed_tool_call_logs_a_warning(client, learn, openai, logs):
    learn.fail_method = "tools/call"
    openai.script.extend(
        [
            llm_response([function_call("microsoft_docs_search", {"query": "x"})]),
            llm_response([SimpleNamespace(type="message")], text=final_brief()),
        ]
    )

    assert client.post("/ask", json=ASK_BODY).status_code == 200

    (warning,) = [record for record in logs() if record.body.startswith("tool microsoft_docs_search failed")]
    assert warning.attributes["error.type"] == "HTTPStatusError"


def test_stdout_log_lines_end_with_the_trace_id():
    formatter = app_module.TraceIdFormatter("[log] %(levelname)s %(message)s")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)

    assert formatter.format(record) == "[log] INFO hello world trace_id=-", "no span, no trace id"
    with app_module.tracer.start_as_current_span("test") as span:
        line = formatter.format(record)
    assert line == f"[log] INFO hello world trace_id={format(span.get_span_context().trace_id, '032x')}"
