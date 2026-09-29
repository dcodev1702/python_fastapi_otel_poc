"""
Test fixtures for Foundry Learn Agent.

Author  : dcodev1702 & M365 Copilot / Cowork
Created : 2026-09-29

What the fixtures give every test:

  * A fake Microsoft Learn MCP server, served in-process through `httpx.MockTransport` - so the app's real
    `httpx.AsyncClient` (and therefore the real httpx instrumentation) is exercised, with no network.
  * A fake OpenAI client (`FakeOpenAI`) whose `responses.create` replays a scripted list of responses, so the
    agent loop runs end to end without an API key or a single token.
  * An `InMemorySpanExporter` attached to the app's own TracerProvider, so tests can assert the TRACE SHAPE -
    which spans exist, how they nest, what attributes they carry. The trace is this project's deliverable, so
    it is what the tests check.
  * An `InMemoryMetricReader` and an `InMemoryLogRecordExporter`, installed as the global MeterProvider and
    LoggerProvider BEFORE the app is imported. With OTEL_METRICS_EXPORTER / OTEL_LOGS_EXPORTER=none the app installs
    no providers of its own, so its instruments, the FastAPI and httpx metrics and its logger all land here.

Environment is pinned BEFORE `app` is imported (import-time configuration):
  OPENAI_API_KEY=test            lifespan refuses to start without one
  OTEL_TRACES_EXPORTER=none      no console/OTLP exporter; the tests add their own in-memory processor
  OTEL_METRICS_EXPORTER=none     (and OTEL_LOGS_EXPORTER=none) - the in-memory providers below are used instead
  HEALTHZ_INTERVAL_SECONDS=0     no heartbeat task during tests
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")
os.environ.setdefault("OTEL_TRACES_EXPORTER", "none")
os.environ["OTEL_METRICS_EXPORTER"] = "none"  # forced: a shell with metrics/logs switched on must not leak in
os.environ["OTEL_LOGS_EXPORTER"] = "none"
os.environ.setdefault("HEALTHZ_INTERVAL_SECONDS", "0")
os.environ.pop("TRACE_UI_URL", None)  # individual tests opt in with monkeypatch
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)

# pylint: disable=wrong-import-position  # the environment above must be in place before app.py is imported
from fastapi.testclient import TestClient  # noqa: E402
from opentelemetry import _logs, metrics, trace  # noqa: E402
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: E402
from opentelemetry.sdk._logs import LoggerProvider  # noqa: E402
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor  # noqa: E402
from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

# DELTA temporality: every collection returns only what was measured since the previous one, so each test sees
# exactly its own measurements (the metrics_data fixture collects once up front to start from zero).
METRIC_READER = InMemoryMetricReader(
    preferred_temporality={Counter: AggregationTemporality.DELTA, Histogram: AggregationTemporality.DELTA}
)
metrics.set_meter_provider(MeterProvider(metric_readers=[METRIC_READER]))
LOG_EXPORTER = InMemoryLogRecordExporter()
_log_provider = LoggerProvider()
_log_provider.add_log_record_processor(SimpleLogRecordProcessor(LOG_EXPORTER))
_logs.set_logger_provider(_log_provider)

import app as app_module  # noqa: E402

# --------------------------------------------------------------------------------------------------------------
# Fake Microsoft Learn MCP server (JSON-RPC over HTTP, JSON *and* SSE answers, session id round-trip)
# --------------------------------------------------------------------------------------------------------------
SESSION_ID = "test-session-1"
LEARN_TOOLS = [
    {
        "name": "microsoft_docs_search",
        "description": "Search Microsoft Learn.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "microsoft_code_sample_search",
        "description": "Search code samples.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "language": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "microsoft_docs_fetch",
        "description": "Fetch a Learn page.",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
]
LEARN_URL = "https://learn.microsoft.com/azure/ai-foundry/what-is-azure-ai-foundry"
SEARCH_RESULT_TEXT = f"Microsoft Foundry is a platform for building AI apps and agents. {LEARN_URL}"


class FakeLearn:
    """Records every JSON-RPC request and answers like the Learn MCP server; `fail_method` forces a 500."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.fail_method: str | None = None

    def reset(self) -> None:
        """Forget recorded requests and any forced failure."""
        self.requests.clear()
        self.fail_method = None

    def methods(self) -> list[str]:
        """JSON-RPC methods seen, in order."""
        return [r["method"] for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        """The `httpx.MockTransport` handler."""
        body = json.loads(request.content)
        method = body["method"]
        self.requests.append(
            {
                "method": method,
                "body": body,
                "session_id": request.headers.get("mcp-session-id"),  # httpx header lookup is case-insensitive
                "traceparent": request.headers.get("traceparent"),  # injected by the httpx instrumentation
            }
        )

        if method == self.fail_method:
            return httpx.Response(500, json={"error": "forced failure"})
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "fake-learn"}}
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}, headers={"mcp-session-id": SESSION_ID}
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":  # answered as a one-shot SSE stream, like the real server sometimes does
            message = {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": LEARN_TOOLS}}
            sse = f"event: message\ndata: {json.dumps(message)}\n\n"
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        if method == "tools/call":
            result = {"content": [{"type": "text", "text": SEARCH_RESULT_TEXT}]}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601}})


# --------------------------------------------------------------------------------------------------------------
# Fake OpenAI client: `responses.create` replays a script; each test fills the script it needs
# --------------------------------------------------------------------------------------------------------------
class FakeOpenAI:
    """Stands in for `openai.AsyncOpenAI`: exposes `.responses.create(**kwargs)` and `.close()`."""

    def __init__(self) -> None:
        self.script: deque[Any] = deque()
        self.calls: list[dict[str, Any]] = []

    @property
    def responses(self) -> FakeOpenAI:
        """The SDK nests `create` under `client.responses`; this object plays both roles."""
        return self

    async def create(self, **kwargs: Any) -> Any:
        """Record the call and return (or raise) the next scripted item."""
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("FakeOpenAI: no scripted response left for this call")
        item = self.script.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        """Mirror the real client's close()."""

    def reset(self) -> None:
        """Forget the script and recorded calls."""
        self.script.clear()
        self.calls.clear()


def llm_response(output: list[Any], text: str = "", input_tokens: int = 100, output_tokens: int = 20) -> Any:
    """Build a Responses-API-shaped object: `.output`, `.output_text`, `.usage`."""
    return SimpleNamespace(
        output=output,
        output_text=text,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def function_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> Any:
    """A `function_call` output item as the agent reads it."""
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(arguments), call_id=call_id)


def final_brief(paragraphs: int = 2, links: int = 3) -> str:
    """A JSON answer that satisfies the `Brief` schema."""
    brief = {
        "paragraphs": [f"Paragraph {i + 1} about Microsoft Foundry." for i in range(paragraphs)],
        "links": [{"title": f"Learn page {i + 1}", "url": f"{LEARN_URL}?n={i + 1}"} for i in range(links)],
    }
    return json.dumps(brief)


# --------------------------------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------------------------------
@pytest.fixture(name="span_exporter", scope="session")
def fixture_span_exporter() -> InMemorySpanExporter:
    """Attach an in-memory exporter to the app's TracerProvider (set at import) - once per test session."""
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture(name="fake_learn", scope="session")
def fixture_fake_learn() -> FakeLearn:
    """The fake Learn server (session-scoped; tests call `reset()` through the `learn` fixture)."""
    return FakeLearn()


@pytest.fixture(name="fake_openai", scope="session")
def fixture_fake_openai() -> FakeOpenAI:
    """The fake OpenAI client (session-scoped; tests call `reset()` through the `openai` fixture)."""
    return FakeOpenAI()


@pytest.fixture(scope="session")
def client(span_exporter, fake_learn, fake_openai):  # pylint: disable=unused-argument
    """One TestClient for the whole session.

    Session scope matters: the app's lifespan shutdown calls `TracerProvider.shutdown()`, after which no span
    would reach the exporter, so the lifespan must start once and end once. The fake OpenAI client is swapped
    in BEFORE the lifespan runs (it calls `AsyncOpenAI()`), and the Learn client AFTER (lifespan creates it).
    """
    app_module.AsyncOpenAI = lambda: fake_openai  # type: ignore[assignment]
    with TestClient(app_module.app) as test_client:
        test_client.portal.call(app_module.app.state.http.aclose)  # the lifespan's real client is never used
        learn_client = httpx.AsyncClient(transport=httpx.MockTransport(fake_learn.handler), timeout=httpx.Timeout(5.0))
        # HTTPXClientInstrumentor().instrument() (run by app.py) patches the real transport class,
        # httpx.AsyncHTTPTransport. The fake Learn server sits on a MockTransport instead, so instrument this one
        # client explicitly: the same wrapper, so the same CLIENT spans and the same injected traceparent.
        HTTPXClientInstrumentor.instrument_client(learn_client)
        app_module.app.state.http = learn_client
        yield test_client


@pytest.fixture()
def spans(span_exporter) -> Callable[[], list[Any]]:
    """Per test: start with an empty exporter; return a callable that lists the finished spans so far."""
    span_exporter.clear()
    return span_exporter.get_finished_spans


@pytest.fixture()
def learn(fake_learn) -> FakeLearn:
    """Per test: a reset fake Learn server."""
    fake_learn.reset()
    return fake_learn


@pytest.fixture()
def openai(fake_openai) -> FakeOpenAI:
    """Per test: a reset fake OpenAI client."""
    fake_openai.reset()
    return fake_openai


@pytest.fixture()
def metrics_data() -> Callable[[], dict[str, list[Any]]]:
    """Per test: start from zero; return a callable that collects {metric name: [data points]} measured since."""

    def collect() -> dict[str, list[Any]]:
        points: dict[str, list[Any]] = defaultdict(list)
        data = METRIC_READER.get_metrics_data()
        for resource_metrics in data.resource_metrics if data else []:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    points[metric.name].extend(metric.data.data_points)
        return dict(points)

    collect()  # DELTA temporality: this collection swallows whatever earlier tests measured
    return collect


@pytest.fixture()
def logs() -> Callable[[], list[Any]]:
    """Per test: start with an empty log exporter; return a callable that lists the OTel log records so far."""
    LOG_EXPORTER.clear()
    return lambda: [getattr(item, "log_record", item) for item in LOG_EXPORTER.get_finished_logs()]


def wait_for_spans(get_spans: Callable[[], list[Any]], predicate: Callable[[list[Any]], bool], timeout: float = 2.0):
    """Spans end a hair after the response is sent; poll briefly so assertions never race the exporter."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = list(get_spans())
        if predicate(current):
            return current
        time.sleep(0.02)
    return list(get_spans())
