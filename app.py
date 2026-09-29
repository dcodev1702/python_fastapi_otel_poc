"""
Foundry Learn Agent
===================
Description : A small FastAPI service for learning OpenTelemetry (OTEL) end to end. An LLM agent answers
              questions about Microsoft products using the Microsoft Learn MCP server, and every hop -
              inbound HTTP request, agent loop, Learn tool calls, OpenAI calls - becomes a span in ONE trace.
              The same agent is exposed twice so the two trace shapes can be compared side by side:

                POST /ask         local tools  - the tool loop runs HERE; every Learn call is a span you can see
                POST /ask-hosted  hosted MCP   - OpenAI runs the tool loop; one opaque OpenAI span, far less code

              Traces are always on. Metrics (token usage, LLM and tool latency, request rates, memory) and structured
              logs (correlated to their trace) switch on per viewer: the Aspire override stores all three signals.

Author      : dcodev1702 & M365 Copilot / Cowork
Created     : 2026-09-28
Version     : 0.5.0
Python      : 3.14.7+
Run (local) : uvicorn app:app --reload                                  -> http://127.0.0.1:8000/docs
Run (Docker): docker compose up --build                                 -> http://localhost:8000/docs
              docker compose -f compose.yaml -f compose.jaeger.yaml up --build   (+ Jaeger UI on :16686, traces)
              docker compose -f compose.yaml -f compose.aspire.yaml up --build   (+ Aspire on :18888, all 3 signals)

Endpoints
    GET  /ping         the cheapest possible span: one request, one trace, returns its trace_id
    GET  /healthz      liveness for Docker / Kubernetes. Deliberately NOT traced. The same status JSON is also
                       printed to stdout every HEALTHZ_INTERVAL_SECONDS (default 60) by a background heartbeat
    GET  /tools        lists the Microsoft Learn MCP tools (Learn calls only: no OpenAI, no tokens, free tracing)
    POST /ask          the agent, tools executed locally
    POST /ask-hosted   the agent, tools executed by OpenAI's hosted MCP feature

    Every traced response carries its trace id twice: as the X-Trace-Id header and as `trace_id` in the body,
    plus `trace_url` - a clickable link into the trace UI - when TRACE_UI_URL is set (compose.jaeger.yaml sets
    it). A 502 body carries the same two fields, so a failed request can be found in Jaeger as easily as a
    successful one. /healthz gets neither: it has no span.

Trace shape for POST /ask (local tools)

  client --> FastAPI  POST /ask ........................ SERVER span  (auto: opentelemetry-instrumentation-fastapi)
              +- agent.run (mode=local-tools) ......... manual span  (our code)
                  +- mcp initialize ................... manual span
                  |   +- POST learn.microsoft.com ..... CLIENT span  (auto: opentelemetry-instrumentation-httpx)
                  +- mcp notifications/initialized
                  |   +- POST learn.microsoft.com
                  +- mcp tools/list
                  |   +- POST learn.microsoft.com
                  +- llm.turn  (turn 1)
                  |   +- POST api.openai.com .......... CLIENT span  (the OpenAI SDK's httpx2, instrumented too!)
                  +- mcp tools/call microsoft_docs_search   <- repeats for every tool call the model makes
                  |   +- POST learn.microsoft.com
                  +- llm.turn  (turn n) -> final JSON answer
                      +- POST api.openai.com

Trace shape for POST /ask-hosted (hosted MCP)

  client --> FastAPI  POST /ask-hosted ................ SERVER span
              +- agent.run (mode=hosted-mcp) .......... manual span
                  +- llm.turn ......................... manual span
                      +- POST api.openai.com .......... CLIENT span  <- OpenAI calls Learn on our behalf. Those hops
                                                                        are NOT in our trace; the only evidence is
                                                                        the mcp_call items in the response body.

Layout of this file
    1. OpenTelemetry setup       traces, metrics and logs: API vs SDK vs instrumentation; our metric instruments and
                                 logger. Step 4 (Azure Monitor) is built in, commented out.
    2. Microsoft Learn MCP client
    3. The agent                 prompts, output contract, the local tool loop, the hosted variant
    4. Health + heartbeat        the `stats` counters - deliberately span-free
    5. The API
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Iterable
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from openai import APIError, AsyncOpenAI
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor, HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from pydantic import BaseModel, ConfigDict, Field

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "foundry-learn-agent")
SERVICE_VERSION = "0.5.0"

# =============================================================================
# 1. OPENTELEMETRY SETUP - three signals
#
#    API              = the interface everyone codes against: tracers and spans, meters and instruments, loggers
#    SDK              = the implementation we configure here: Resource -> Provider -> Processor/Reader -> Exporter
#    Instrumentation  = plug-ins that create spans AND metrics for libraries we do not own (FastAPI, httpx)
#
#    Traces  = one request's path, hop by hop. Always on: console, or OTLP when OTEL_EXPORTER_OTLP_ENDPOINT is set.
#    Metrics = numbers aggregated over time: rates, latency distributions, token usage. OTEL_METRICS_EXPORTER.
#    Logs    = timestamped events, each stamped with the trace it happened in. OTEL_LOGS_EXPORTER.
#
#    Metrics and logs are off by default, because Jaeger stores traces only; compose.aspire.yaml switches both on.
#    A provider fans out to EVERY processor added to it, which is why console, OTLP and Azure Monitor can all be
#    switched on at the same time without touching a single line of application code.
# =============================================================================
EXCLUDED_URLS = os.getenv(
    "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS",
    "docs,openapi.json,redoc,healthz",  # comma-separated regexes; Swagger traffic and liveness probes are noise
)
SDK_PROVIDERS: list[Any] = []  # the metrics and logs providers installed below; the lifespan flushes them on exit


def configure_traces(resource: Resource) -> str:
    """TRACES: always collected. Returns where they go - "otlp", "console" or "none" - for the health snapshot."""
    provider = TracerProvider(resource=resource)

    if os.getenv("OTEL_TRACES_EXPORTER", "").lower() == "none":
        # The standard OTEL switch for "collect, but export nowhere". The test suite uses it and attaches its own
        # in-memory processor to this provider, so it can assert the trace shape without console noise.
        mode = "none"
    elif os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        # Ship spans to Jaeger / an OTel Collector / the Aspire Dashboard over OTLP-HTTP, e.g.
        #   OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318          (uvicorn on your machine)
        #   OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318             (inside docker compose)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # reads the env var, appends /v1/traces
        mode = "otlp"
    else:
        # Zero infrastructure: every finished span is printed to stdout as JSON.
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        mode = "console"

    # --------------------------------------------------------------------------------------------------------
    # STEP 4 (optional - commented out on purpose): ship the SAME spans to Azure Monitor / Application Insights.
    #
    # Nothing else in this file changes. The endpoints, the auto-instrumentation and the manual spans are all
    # identical; a second processor + exporter pair is simply added to the provider, so every span fans out to
    # the console (or OTLP) AND to Application Insights, where it shows up in Application Map, the end-to-end
    # transaction view and the requests / dependencies / exceptions tables.
    #
    # To enable:
    #   1. pip install azure-monitor-opentelemetry-exporter   (Docker: uncomment it in requirements/base.txt, make lock)
    #   2. export APPLICATIONINSIGHTS_CONNECTION_STRING="InstrumentationKey=...;IngestionEndpoint=https://...;..."
    #      Portal: Application Insights resource -> Overview -> Connection String. The string carries the
    #      ingestion endpoint, so Azure Government / other sovereign clouds work with no code change.
    #   3. Uncomment the block below and restart.
    #
    # How the portal maps what this file emits:
    #   SERVER spans (FastAPI)                    -> requests table
    #   CLIENT spans (httpx -> Learn, OpenAI)     -> dependencies (type HTTP), drawn as edges on the Application Map
    #   INTERNAL spans (agent.run, llm.turn, mcp) -> dependencies (type InProc)
    #   span attributes                           -> customDimensions   (KQL: customDimensions["agent.turns"])
    #   record_exception() / failed spans         -> exceptions table
    #   Resource service.name                     -> cloud_RoleName, the node label on the Application Map
    #
    # from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
    #
    # if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    #     azure_exporter = AzureMonitorTraceExporter()  # reads APPLICATIONINSIGHTS_CONNECTION_STRING itself
    #     provider.add_span_processor(BatchSpanProcessor(azure_exporter))
    #     mode += "+azure-monitor"
    # --------------------------------------------------------------------------------------------------------

    trace.set_tracer_provider(provider)  # from here on, every API call (ours or a library's) routes into this SDK
    return mode


def configure_metrics(resource: Resource) -> str:
    """METRICS: only when OTEL_METRICS_EXPORTER is "otlp" or "console". Returns that choice, or "none".

    With "none" no metrics SDK is installed and every instrument is a cheap no-op - unless something else installed a
    MeterProvider first (the tests do, with an in-memory reader). Instruments created before a provider exists are
    upgraded when it arrives, so the order does not matter.
    """
    choice = os.getenv("OTEL_METRICS_EXPORTER", "none").lower()
    if choice == "otlp":
        exporter = OTLPMetricExporter()  # same OTEL_EXPORTER_OTLP_ENDPOINT as the spans; appends /v1/metrics
    elif choice == "console":
        exporter = ConsoleMetricExporter()
    else:
        return "none"
    # The reader collects every instrument and exports every OTEL_METRIC_EXPORT_INTERVAL ms (SDK default: 60000).
    provider = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(exporter)])
    metrics.set_meter_provider(provider)
    SDK_PROVIDERS.append(provider)
    return choice


def configure_logs(resource: Resource) -> str:
    """LOGS: only when OTEL_LOGS_EXPORTER is "otlp" or "console". Returns that choice, or "none".

    Our logger (configure_logging below) always writes plain lines to stdout; this adds the OpenTelemetry side, where
    every record carries the trace_id and span_id that were current when it was logged.
    """
    choice = os.getenv("OTEL_LOGS_EXPORTER", "none").lower()
    if choice == "otlp":
        exporter = OTLPLogExporter()  # the same endpoint again; appends /v1/logs
    elif choice == "console":
        exporter = ConsoleLogRecordExporter()
    else:
        return "none"
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)
    SDK_PROVIDERS.append(provider)
    return choice


def configure_opentelemetry() -> dict[str, str]:
    """Wire the SDK for all three signals, then switch on outbound instrumentation.

    Returns where each signal goes, for the health snapshot, e.g. {"traces": "otlp", "metrics": "otlp", "logs": "otlp"}.
    """
    resource = Resource.create({"service.name": SERVICE_NAME, "service.version": SERVICE_VERSION})
    exporters = {
        "traces": configure_traces(resource),
        "metrics": configure_metrics(resource),
        "logs": configure_logs(resource),
    }

    # OUTBOUND: wrap the HTTP clients so every request becomes a CLIENT span, carries a W3C `traceparent` header and
    # (with metrics on) lands in http.client.duration. It takes two instrumentors: our McpClient uses httpx, but the
    # OpenAI SDK (openai 3.x) is built on httpx2, a separate package - with the httpx one alone, every llm.turn span
    # is missing its api.openai.com child. Do this BEFORE any httpx / OpenAI client is created - that is the safest
    # ordering across versions.
    HTTPXClientInstrumentor().instrument()
    HTTPX2ClientInstrumentor().instrument()
    return exporters


class TraceIdFormatter(logging.Formatter):
    """Plain stdout log lines that end with the current trace id, so a log line can be matched to its trace."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        context = trace.get_current_span().get_span_context()
        trace_id = format(context.trace_id, "032x") if context.is_valid else "-"
        return f"{super().formatMessage(record)} trace_id={trace_id}"


def configure_logging() -> logging.Logger:
    """Our application logger: `[log] ...` lines on stdout, plus a bridge that turns each record into an OTel log.

    The LoggingHandler hands every record to the global LoggerProvider - the one configure_logs installed, or a no-op
    when logs are off - stamped with the current trace_id and span_id; `extra={...}` becomes the record's attributes.
    """
    logger = logging.getLogger("foundry_learn_agent")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # uvicorn configures its own loggers; keep ours self-contained
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(TraceIdFormatter("[log] %(levelname)s %(message)s"))
    logger.addHandler(stdout)
    logger.addHandler(LoggingHandler(level=logging.INFO))
    return logger


EXPORTERS = configure_opentelemetry()
tracer = trace.get_tracer(SERVICE_NAME, SERVICE_VERSION)  # our handle for manual spans (this is the API side)
meter = metrics.get_meter(SERVICE_NAME, SERVICE_VERSION)  # ...for our own metrics
log = configure_logging()  # ...and for our own log records

# Our own metrics. Names follow the OpenTelemetry semantic conventions where one exists (gen_ai.*, process.*); the
# agent.* ones are ours. With metrics on, the FastAPI and httpx instrumentations add http.server.* and http.client.*
# for free. The bucket boundaries are the ones the GenAI conventions recommend: token counts run from 1 to millions,
# and LLM calls take seconds rather than the milliseconds the SDK's default buckets are made for.
TOKEN_BUCKETS = [1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864]
SECONDS_BUCKETS = [0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92]
llm_token_usage = meter.create_histogram(
    "gen_ai.client.token.usage",
    unit="{token}",
    description="Tokens per LLM call, by gen_ai.token.type (input or output)",
    explicit_bucket_boundaries_advisory=TOKEN_BUCKETS,
)
llm_duration = meter.create_histogram(
    "gen_ai.client.operation.duration",
    unit="s",
    description="Duration of each LLM call - one per llm.turn span",
    explicit_bucket_boundaries_advisory=SECONDS_BUCKETS,
)
tool_call_count = meter.create_counter(
    "agent.tool.calls", unit="{call}", description="Microsoft Learn tool calls, made here (/ask) or by OpenAI"
)
tool_duration = meter.create_histogram(
    "agent.tool.duration",
    unit="s",
    description="Duration of each tool call made from this process (/ask only: hosted calls are not timed here)",
    explicit_bucket_boundaries_advisory=SECONDS_BUCKETS,
)


def observe_memory(_options: CallbackOptions) -> Iterable[Observation]:
    """Callback for process.memory.usage: the SDK calls it at every collection, so no timer of our own is needed."""
    rss = current_rss_bytes()
    return [] if rss is None else [Observation(rss)]


meter.create_observable_up_down_counter(
    "process.memory.usage", callbacks=[observe_memory], unit="By", description="Resident memory of this process"
)


# GenAI CONTENT CAPTURE (opt-in). HTTP instrumentation never records request or response bodies, and our own spans
# stop at counts - so a trace shows THAT the app talked to OpenAI and Microsoft Learn, not WHAT was said. The GenAI
# semantic conventions define opt-in attributes for the content itself: the system instructions, every message sent
# to the model and returned by it, and each tool call's arguments and result. It is off by default because content
# is large (one /ask records well over 100 KB) and can be sensitive; the Jaeger and Aspire overrides switch it on.
def capture_content() -> bool:
    """OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY (or SPAN_AND_EVENT, or true) records content on
    spans. Read on every call, so the tests can switch it; events (EVENT_ONLY) are not implemented here."""
    value = os.getenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "").strip().lower()
    return value in {"span_only", "span_and_event", "true"}


def as_json(value: Any) -> str:
    """Span attributes cannot hold nested structures yet, so the conventions record content as a JSON string."""
    return json.dumps(value, ensure_ascii=False, default=str)


# =============================================================================
# 2. MICROSOFT LEARN MCP CLIENT
#
#    "MCP over Streamable HTTP" is JSON-RPC 2.0 POSTed to a single URL. Because it rides on httpx, every call
#    below becomes a CLIENT span for free; we add one manual span per call so the trace says WHAT was asked,
#    not just where. (All Learn calls hit the same URL, so the automatic spans alone are indistinguishable.)
# =============================================================================
LEARN_MCP_URL = os.getenv("LEARN_MCP_URL", "https://learn.microsoft.com/api/mcp")


class McpClient:
    """A deliberately minimal MCP client: initialize, list tools, call a tool. One instance per request."""

    def __init__(self, http: httpx.AsyncClient, url: str) -> None:
        self._http = http
        self._url = url
        self._next_id = 0
        self._session_id: str | None = None

    async def initialize(self) -> None:
        """Open the MCP session: `initialize`, then the `notifications/initialized` notification."""
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": SERVICE_NAME, "version": SERVICE_VERSION},
            },
        )
        await self._rpc("notifications/initialized", notification=True)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the server's tool definitions: name, description and `inputSchema` (JSON Schema)."""
        return (await self._rpc("tools/list"))["tools"]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one tool and return its text content blocks joined into one string."""
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        # Tool results are a list of content blocks; we only need the text ones.
        return "\n".join(block.get("text", "") for block in result.get("content", []) if block.get("type") == "text")

    async def _rpc(self, method: str, params: dict[str, Any] | None = None, *, notification: bool = False) -> Any:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:  # notifications carry no id and expect no response
            self._next_id += 1
            payload["id"] = self._next_id

        span_name = f"mcp {method}" + (f" {params['name']}" if method == "tools/call" and params else "")
        with tracer.start_as_current_span(span_name) as span:  # MANUAL span wrapping the AUTO httpx span
            span.set_attribute("rpc.system", "jsonrpc")
            span.set_attribute("rpc.method", method)
            if method == "tools/call" and params:
                span.set_attribute("mcp.tool.name", params["name"])
                span.set_attribute("mcp.tool.arguments", json.dumps(params.get("arguments", {})))
                span.set_attribute("gen_ai.operation.name", "execute_tool")  # GenAI views show it as a tool call
                span.set_attribute("gen_ai.tool.name", params["name"])
                if capture_content():
                    span.set_attribute("gen_ai.tool.call.arguments", as_json(params.get("arguments", {})))

            headers = {"Accept": "application/json, text/event-stream"}
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id

            response = await self._http.post(self._url, json=payload, headers=headers)
            if notification:
                return None  # fire-and-forget (servers answer 202 with an empty body)
            response.raise_for_status()
            self._session_id = response.headers.get("mcp-session-id", self._session_id)

            message = self._parse(response)
            if "error" in message:
                raise RuntimeError(f"MCP {method} failed: {message['error']}")
            if method == "tools/call" and capture_content():
                span.set_attribute("gen_ai.tool.call.result", as_json(message["result"]))  # the data Learn sent back
            return message["result"]

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        """Servers may answer with plain JSON or with a one-shot SSE stream of 'data: {...}' lines."""
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            messages = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
            for message in reversed(messages):  # the response is the last message carrying a result/error
                if "result" in message or "error" in message:
                    return message
            raise RuntimeError("SSE stream contained no JSON-RPC response")
        return response.json()


# =============================================================================
# 3. THE AGENT - prompts, output contract, and two ways to run the tool loop
# =============================================================================
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
MAX_TURNS = 8  # safety valve so a confused model cannot loop (and bill you) forever

SYSTEM_PROMPT = """\
You are a research agent that answers questions about Microsoft products using official Microsoft Learn documentation.

Ground rules
1. Use the tools. Microsoft products are renamed and updated often, so treat the documentation you retrieve as the
   source of truth and your own memory as untrustworthy. Never answer without at least one successful search.
2. Search first (microsoft_docs_search) using the user's topic. If the results are thin or off-topic, search again
   with different wording - product name variants, "overview", "what is".
3. Fetch a full page (microsoft_docs_fetch) only when a search excerpt is not enough to write an accurate paragraph.
   Fetch at most two pages.
4. Only cite URLs that appeared verbatim in tool results. Never invent, shorten, or "fix" a URL.
5. When you have enough material, stop calling tools and answer.

Answer rules
- Return exactly the number of paragraphs and links the user asks for.
- Paragraphs are plain prose (3-5 sentences each), factual and product-accurate - no bullet points, no marketing language.
- Each link has a short title and points to a distinct learn.microsoft.com page that supports something you wrote.
- Return only the JSON structure you were given, with no extra fields or commentary.
"""

USER_PROMPT_TEMPLATE = """\
Topic: {topic}

Please write {paragraphs} paragraph(s) about {topic}. The first paragraph should explain what it is and the problem
it solves; any further paragraphs should cover its key capabilities and how customers typically use it.

Then give me {links} link(s) to the most relevant official Microsoft Learn pages for someone who wants to learn more.
"""

AgentMode = Literal["local-tools", "hosted-mcp"]


class Link(BaseModel):  # pylint: disable=missing-class-docstring  # a docstring would join the schema sent to OpenAI
    model_config = ConfigDict(extra="forbid")  # -> additionalProperties:false, which OpenAI strict mode requires
    title: str
    url: str


class Brief(BaseModel):
    """The shape the LLM must return. The SAME schema is sent to OpenAI as the structured-output contract."""

    model_config = ConfigDict(extra="forbid")
    paragraphs: list[str]
    links: list[Link]


class TokenUsage(BaseModel):
    """Tokens the model consumed, from the Responses API `usage` field."""

    input_tokens: int = 0
    output_tokens: int = 0


class ToolCallRecord(BaseModel):
    """One Microsoft Learn tool call, made by the agent (/ask) or by OpenAI on its behalf (/ask-hosted)."""

    tool: str
    arguments: dict[str, Any]
    result_chars: int
    duration_ms: float | None = Field(
        None,
        description="Wall-clock time measured by THIS service. null for hosted calls: OpenAI made them, so their "
        "latency is hidden inside the single api.openai.com span.",
    )
    error: str | None = None


class AgentResult(BaseModel):
    """An agent run's brief plus how it was produced: LLM turns, token usage and tool calls."""

    mode: AgentMode
    brief: Brief
    turns: int = Field(description="How many times the LLM was called (tool-call rounds + the final answer)")
    usage: TokenUsage
    tool_calls: list[ToolCallRecord]


# One schema, two jobs: FastAPI validates the response with it AND OpenAI is forced to produce exactly this shape.
BRIEF_TEXT_FORMAT = {
    "format": {"type": "json_schema", "name": "brief", "strict": True, "schema": Brief.model_json_schema()}
}

# OpenAI's hosted MCP tool: OpenAI's servers connect to the MCP server, list its tools and execute the tool loop
# for us inside ONE responses.create call. The URL therefore has to be reachable from the public internet
# (a localhost LEARN_MCP_URL override works for /ask but not here).
HOSTED_MCP_TOOL = {
    "type": "mcp",
    "server_label": "microsoft_learn",
    "server_url": LEARN_MCP_URL,
    "require_approval": "never",  # otherwise every tool call pauses for an approval round-trip
    # "allowed_tools": ["microsoft_docs_search", "microsoft_docs_fetch"],  # optional allow-list
}


def build_user_prompt(topic: str, paragraphs: int, links: int) -> str:
    """Fill USER_PROMPT_TEMPLATE with the request's topic and paragraph and link counts."""
    return USER_PROMPT_TEMPLATE.format(topic=topic, paragraphs=paragraphs, links=links)


def to_openai_tools(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MCP tool definitions and OpenAI function tools are both JSON Schema - the translation is one line each."""
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
            "strict": False,  # MCP schemas rarely satisfy OpenAI's strict-mode rules, so validate loosely
        }
        for tool in mcp_tools
    ]


def parse_arguments(raw: str | None) -> dict[str, Any]:
    """Tool arguments arrive as a JSON string; a malformed one must never take the whole request down."""
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def item_parts(item: Any) -> list[dict[str, Any]]:
    """One Responses API item as GenAI message parts: a tool call, a hosted MCP call with its result, text or
    reasoning. Bookkeeping items such as mcp_list_tools carry no content worth recording."""
    if item.type == "function_call":
        arguments = parse_arguments(item.arguments)
        return [{"type": "tool_call", "id": item.call_id, "name": item.name, "arguments": arguments}]
    if item.type == "mcp_call":  # OpenAI ran this tool against Learn for us (/ask-hosted): the call AND its result
        call_id = getattr(item, "id", None)
        call = {"type": "mcp", "arguments": parse_arguments(item.arguments)}
        result = {"type": "mcp", "output": item.output, "error": item.error}
        return [
            {"type": "server_tool_call", "id": call_id, "name": item.name, "server_tool_call": call},
            {"type": "server_tool_call_response", "id": call_id, "server_tool_call_response": result},
        ]
    if item.type == "message":
        return [{"type": "text", "content": part.text} for part in getattr(item, "content", None) or []
                if getattr(part, "text", None)]
    if item.type == "reasoning":
        summary = " ".join(part.text for part in getattr(item, "summary", None) or [] if getattr(part, "text", None))
        return [{"type": "reasoning", "content": summary}] if summary else []
    return []


def input_messages(request_input: Any) -> list[dict[str, Any]]:
    """What the model was sent, as GenAI messages: the user prompt, then each earlier turn's tool calls (assistant)
    and their results (tool) - the conversation the local loop grows turn by turn."""
    if isinstance(request_input, str):  # /ask-hosted sends the prompt as a plain string
        return [{"role": "user", "parts": [{"type": "text", "content": request_input}]}]
    messages: list[dict[str, Any]] = []
    for item in request_input:
        if isinstance(item, dict) and "role" in item:  # the user prompt
            messages.append({"role": item["role"], "parts": [{"type": "text", "content": item["content"]}]})
        elif isinstance(item, dict):  # a function_call_output we appended: a tool result going back to the model
            response = {"type": "tool_call_response", "id": item["call_id"], "response": item["output"]}
            messages.append({"role": "tool", "parts": [response]})
        elif parts := item_parts(item):  # the model's own earlier output, fed back verbatim
            if messages and messages[-1]["role"] == "assistant":
                messages[-1]["parts"].extend(parts)  # one assistant message per turn, not one per item
            else:
                messages.append({"role": "assistant", "parts": parts})
    return messages


def output_messages(response: Any) -> list[dict[str, Any]]:
    """What the model returned, as one GenAI output message: the tool calls it asks for, hosted calls, final text."""
    parts = [part for item in response.output for part in item_parts(item)]
    if response.output_text and not any(part["type"] == "text" for part in parts):
        parts.append({"type": "text", "content": response.output_text})
    finish_reason = "tool_call" if any(part["type"] == "tool_call" for part in parts) else "stop"
    return [{"role": "assistant", "parts": parts, "finish_reason": finish_reason}]


def tool_definitions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tools offered to the model: function tools built from MCP tools/list, or the hosted MCP tool."""
    return [
        {"type": tool["type"], "name": tool.get("name") or tool.get("server_label"),
         **{key: tool[key] for key in ("description", "parameters", "server_url") if key in tool}}
        for tool in tools
    ]


def record_usage(span: trace.Span, response: Any, totals: TokenUsage, mode: AgentMode) -> None:
    """Tokens are money: stamp them on the llm.turn span, total them per request and per process, and record them
    in the gen_ai.client.token.usage histogram - one data point per token type."""
    stats.llm_turns += 1
    if response.usage is None:
        return
    span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)
    totals.input_tokens += response.usage.input_tokens
    totals.output_tokens += response.usage.output_tokens
    stats.tokens.input_tokens += response.usage.input_tokens
    stats.tokens.output_tokens += response.usage.output_tokens
    attributes = {"gen_ai.request.model": OPENAI_MODEL, "agent.mode": mode}
    llm_token_usage.record(response.usage.input_tokens, {**attributes, "gen_ai.token.type": "input"})
    llm_token_usage.record(response.usage.output_tokens, {**attributes, "gen_ai.token.type": "output"})


async def create_response(openai_client: AsyncOpenAI, mode: AgentMode, **request: Any) -> Any:
    """One LLM call with the shared prompt and output contract, timed into gen_ai.client.operation.duration.

    It also describes the call on the current span (llm.turn) in GenAI terms - operation, provider, models, finish
    reason - and, with content capture on, records exactly what was sent and what came back. A failed call is timed
    too, with `error.type` set to the exception's class, so failures show up in the metric.
    """
    attributes = {"gen_ai.request.model": OPENAI_MODEL, "agent.mode": mode}
    span = trace.get_current_span()
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.provider.name", "openai")
    span.set_attribute("gen_ai.request.model", OPENAI_MODEL)
    if capture_content():
        span.set_attribute("gen_ai.system_instructions", as_json([{"type": "text", "content": SYSTEM_PROMPT}]))
        span.set_attribute("gen_ai.input.messages", as_json(input_messages(request["input"])))
        span.set_attribute("gen_ai.tool.definitions", as_json(tool_definitions(request.get("tools", []))))
    started = time.perf_counter()
    try:
        response = await openai_client.responses.create(
            model=OPENAI_MODEL, instructions=SYSTEM_PROMPT, text=BRIEF_TEXT_FORMAT, **request
        )
    except Exception as exc:
        attributes["error.type"] = type(exc).__name__
        raise
    finally:
        llm_duration.record(time.perf_counter() - started, attributes)
    for key, value in (("gen_ai.response.id", getattr(response, "id", None)),
                       ("gen_ai.response.model", getattr(response, "model", None))):
        if value:
            span.set_attribute(key, value)
    messages = output_messages(response)
    span.set_attribute("gen_ai.response.finish_reasons", [messages[0]["finish_reason"]])
    if capture_content():
        span.set_attribute("gen_ai.output.messages", as_json(messages))
    return response


async def run_tool(
    mcp: McpClient, span: trace.Span, name: str, arguments: dict[str, Any]
) -> tuple[str, ToolCallRecord]:
    """One tool call for the local loop: run it, then count, time and log it. Returns the model's input and a record.

    A failed call is not fatal: the error text goes back to the model, which decides what to do next, and the
    exception is recorded on the agent.run span.
    """
    attributes = {"mcp.tool.name": name, "agent.mode": "local-tools"}
    started = time.perf_counter()
    error: str | None = None
    try:
        output = await mcp.call_tool(name, arguments)
    except (httpx.HTTPError, RuntimeError) as exc:
        error = str(exc)
        attributes["error.type"] = type(exc).__name__
        output = f"TOOL ERROR: {exc}"  # let the model see the failure and decide what to do next
        span.record_exception(exc)  # ...but leave a record of it on the agent span
    seconds = time.perf_counter() - started
    tool_call_count.add(1, attributes)
    tool_duration.record(seconds, attributes)
    if error:
        log.warning("tool %s failed after %.0f ms: %s", name, seconds * 1000, error, extra=attributes)
    else:
        log.info("tool %s returned %d chars in %.0f ms", name, len(output), seconds * 1000, extra=attributes)
    record = ToolCallRecord(
        tool=name, arguments=arguments, result_chars=len(output), duration_ms=round(seconds * 1000, 1), error=error
    )
    return output, record


def finish_run(  # pylint: disable=too-many-arguments,too-many-positional-arguments  # six facts per run
    span: trace.Span, mode: AgentMode, brief: Brief, turns: int, usage: TokenUsage, records: list[ToolCallRecord]
) -> AgentResult:
    """Summarise the run on the agent.run span (what a dashboard would chart) and return the API-facing result."""
    span.set_attribute("agent.turns", turns)
    span.set_attribute("agent.tool_calls", len(records))
    span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
    stats.tool_calls += len(records)
    if capture_content():  # the answer that goes back to the API caller, on the span that covers the whole run
        answer = {"role": "assistant", "parts": [{"type": "text", "content": brief.model_dump_json()}]}
        span.set_attribute("gen_ai.output.messages", as_json([{**answer, "finish_reason": "stop"}]))
    log.info(
        "agent.run finished: turns=%d tool_calls=%d input_tokens=%d output_tokens=%d",
        turns, len(records), usage.input_tokens, usage.output_tokens,
        extra={"agent.mode": mode, "agent.turns": turns, "agent.tool_calls": len(records)},
    )
    return AgentResult(mode=mode, brief=brief, turns=turns, usage=usage, tool_calls=records)


def describe_agent_run(span: trace.Span, mode: AgentMode, user_prompt: str) -> None:
    """Stamp the agent.run span: our attributes, the GenAI invoke_agent ones and - with capture on - the prompt."""
    span.set_attribute("agent.mode", mode)
    span.set_attribute("gen_ai.operation.name", "invoke_agent")
    span.set_attribute("gen_ai.provider.name", "openai")
    span.set_attribute("gen_ai.request.model", OPENAI_MODEL)
    if capture_content():
        span.set_attribute("gen_ai.input.messages", as_json(input_messages(user_prompt)))


# pylint: disable-next=too-many-locals  # the whole tool loop reads top to bottom on purpose
async def run_agent(openai_client: AsyncOpenAI, mcp: McpClient, user_prompt: str) -> AgentResult:
    """LOCAL TOOLS. The whole agent is a loop: ask the model -> run the tools it asks for -> ask again -> answer.

    Every step is visible in the trace because every step happens in this process: the Learn round-trips are
    real httpx calls (CLIENT spans) wrapped in our manual `mcp ...` spans, interleaved with the `llm.turn` spans.
    If anything raises inside the `with` block, start_as_current_span records the exception on the span and marks
    it ERROR before re-raising - no extra code needed.
    """
    with tracer.start_as_current_span("agent.run") as span:
        describe_agent_run(span, "local-tools", user_prompt)
        log.info("agent.run started: local tools, model %s", OPENAI_MODEL, extra={"agent.mode": "local-tools"})

        await mcp.initialize()
        tools = to_openai_tools(await mcp.list_tools())
        span.set_attribute("agent.tools", [tool["name"] for tool in tools])

        conversation: list[Any] = [{"role": "user", "content": user_prompt}]
        records: list[ToolCallRecord] = []
        usage = TokenUsage()

        for turn in range(1, MAX_TURNS + 1):
            with tracer.start_as_current_span("llm.turn") as llm_span:
                llm_span.set_attribute("agent.turn", turn)
                response = await create_response(openai_client, "local-tools", input=conversation, tools=tools)
                record_usage(llm_span, response, usage, "local-tools")

            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:  # no tool calls requested -> the model produced its final (JSON) answer
                brief = Brief.model_validate_json(response.output_text)
                return finish_run(span, "local-tools", brief, turn, usage, records)

            conversation.extend(response.output)  # keep the model's turn (incl. its tool calls) in the history
            for call in calls:
                output, record = await run_tool(mcp, span, call.name, parse_arguments(call.arguments))
                records.append(record)
                conversation.append({"type": "function_call_output", "call_id": call.call_id, "output": output})

        raise RuntimeError(f"agent did not finish within {MAX_TURNS} turns")


async def run_agent_hosted(openai_client: AsyncOpenAI, user_prompt: str) -> AgentResult:
    """HOSTED MCP. Same prompts, same output contract, but OpenAI runs the tool loop server-side.

    One request out, one (bigger) response back: no MCP client, no loop, no conversation bookkeeping. The price
    is observability - the entire run is a single api.openai.com CLIENT span, and the Learn calls that happened
    on OpenAI's side are NOT spans in our trace. The response body does list them as `mcp_call` items, which we
    surface as tool_calls (without a duration) so the two modes stay comparable in the API and in Jaeger.
    """
    with tracer.start_as_current_span("agent.run") as span:
        describe_agent_run(span, "hosted-mcp", user_prompt)
        log.info("agent.run started: hosted MCP, model %s", OPENAI_MODEL, extra={"agent.mode": "hosted-mcp"})
        usage = TokenUsage()

        with tracer.start_as_current_span("llm.turn") as llm_span:
            llm_span.set_attribute("agent.turn", 1)
            response = await create_response(openai_client, "hosted-mcp", input=user_prompt, tools=[HOSTED_MCP_TOOL])
            record_usage(llm_span, response, usage, "hosted-mcp")

        # Reconstruct what happened from the output items OpenAI returns alongside the final message.
        records: list[ToolCallRecord] = []
        for item in response.output:
            if item.type == "mcp_list_tools":  # OpenAI did the tools/list for us
                span.set_attribute("agent.tools", [tool.name for tool in item.tools])
            elif item.type == "mcp_call":  # one per tool call OpenAI executed against Learn
                error = str(item.error) if item.error else None
                attributes = {"mcp.tool.name": item.name, "agent.mode": "hosted-mcp"}
                if error:
                    attributes["error.type"] = "mcp_call.error"
                    span.add_event("mcp_call.error", {"mcp.tool.name": item.name, "error": error})
                    log.warning("hosted tool %s failed on OpenAI's side: %s", item.name, error, extra=attributes)
                else:
                    log.info("hosted tool %s ran on OpenAI's side (%d chars, not timed here)", item.name,
                             len(item.output or ""), extra=attributes)
                tool_call_count.add(1, attributes)  # counted, but no tool_duration: we never saw the call happen
                records.append(
                    ToolCallRecord(
                        tool=item.name,
                        arguments=parse_arguments(item.arguments),
                        result_chars=len(item.output or ""),
                        duration_ms=None,
                        error=error,
                    )
                )

        brief = Brief.model_validate_json(response.output_text)
        return finish_run(span, "hosted-mcp", brief, 1, usage, records)


# =============================================================================
# 4. HEALTH + HEARTBEAT - the `stats` counters (deliberately span-free)
#
#    Liveness probes run forever (Docker HEALTHCHECK, Kubernetes) and would flood a trace backend with worthless
#    spans, so /healthz is excluded from instrumentation (see EXCLUDED_URLS). The heartbeat below only reads
#    in-process counters and prints - it never makes a network call, so it cannot create a span either. Think of
#    it as the control group: plain stdout next to the OTEL output, so you can see what each one is good for. The
#    OTEL metrics in section 1 count the same things properly - per model, per tool, as distributions - and can be
#    charted; these counters only reset when the process restarts.
# =============================================================================
HEALTHZ_INTERVAL_SECONDS = float(os.getenv("HEALTHZ_INTERVAL_SECONDS", "60"))  # 0 disables the heartbeat


class Stats:  # pylint: disable=too-few-public-methods  # a plain bag of counters with one read method
    """Plain in-process counters. The middleware in section 5 and the agent functions above feed them."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self.requests: Counter[str] = Counter()  # by path, including the probes that never become spans
        self.errors: Counter[str] = Counter()  # 5xx responses and unhandled exceptions, by path
        self.llm_turns = 0
        self.tool_calls = 0
        self.tokens = TokenUsage()

    def snapshot(self) -> dict[str, Any]:
        """The status JSON that GET /healthz returns and the heartbeat prints."""
        return {
            "status": "ok",
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "uptime_s": round(time.time() - self.started_at),
            "exporter": dict(EXPORTERS),  # where each signal goes: {"traces": ..., "metrics": ..., "logs": ...}
            "model": OPENAI_MODEL,
            "rss_mb": current_rss_mb(),
            "requests": dict(self.requests),
            "errors": dict(self.errors),
            "llm_turns": self.llm_turns,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens.model_dump(),
        }


stats = Stats()


def current_rss_bytes() -> int | None:
    """Resident memory of this process in bytes (Linux only): feeds rss_mb and the process.memory.usage metric."""
    try:
        with open("/proc/self/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024  # kB -> bytes
    except OSError:
        pass
    return None


def current_rss_mb() -> float | None:
    """Resident memory in MB for the status snapshot - worth watching against the 3 GB cap in compose.yaml."""
    rss = current_rss_bytes()
    return None if rss is None else round(rss / 1024 / 1024, 1)


def print_health() -> None:
    """Print one `[healthz] {...}` status line to stdout."""
    print(f"[healthz] {json.dumps(stats.snapshot())}", flush=True)  # flush: reach `docker compose logs` at once


async def heartbeat() -> None:
    """Dump the status snapshot to stdout every HEALTHZ_INTERVAL_SECONDS until the app shuts down."""
    while True:
        await asyncio.sleep(HEALTHZ_INTERVAL_SECONDS)
        print_health()


# =============================================================================
# 5. THE API
# =============================================================================
class AskRequest(BaseModel):
    """Body of POST /ask and POST /ask-hosted: the topic, and how many paragraphs and links to write."""

    topic: str = Field("Microsoft Foundry", examples=["Microsoft Foundry"])
    paragraphs: int = Field(2, ge=1, le=5)
    links: int = Field(3, ge=1, le=10)


class AskResponse(AgentResult):
    """The agent's result plus the request's topic, the model used and the trace_id to look up."""

    topic: str
    model: str
    trace_id: str = Field(description="Paste this into Jaeger / your trace UI to find this exact request")
    trace_url: str | None = Field(
        None, description="Clickable link to this request's trace; present when TRACE_UI_URL is configured"
    )


class ToolInfo(BaseModel):
    """One Microsoft Learn MCP tool, as GET /tools lists it."""

    name: str
    description: str
    parameters: dict[str, Any]


class ErrorDetail(BaseModel):
    """Why an upstream call failed, plus the trace id (and link) that finds the failed request in the trace UI."""

    error: str
    trace_id: str
    trace_url: str | None = None


class ErrorResponse(BaseModel):
    """The 502 body: FastAPI wraps the HTTPException detail in `detail`."""

    detail: ErrorDetail


# Documents the 502 shape in Swagger for every endpoint that talks to Microsoft Learn or OpenAI.
UPSTREAM_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    502: {"model": ErrorResponse, "description": "An upstream call (Microsoft Learn, OpenAI) failed or the model "
          "broke the output schema. `detail.trace_id` finds the failed request in the trace UI."},
}


@asynccontextmanager
async def lifespan(app: FastAPI):  # pylint: disable=redefined-outer-name  # FastAPI passes this same app in
    """Startup: shared clients + heartbeat. Shutdown: stop heartbeat, close clients, flush the last spans."""
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Set OPENAI_API_KEY before starting the app")
    # Created AFTER configure_opentelemetry() ran, so both clients are instrumented.
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    app.state.openai = AsyncOpenAI()  # reads OPENAI_API_KEY; talks to OpenAI through its own httpx client
    heartbeat_task = asyncio.create_task(heartbeat()) if HEALTHZ_INTERVAL_SECONDS > 0 else None
    print_health()  # first status line immediately, then every HEALTHZ_INTERVAL_SECONDS
    log.info(
        "service started: model %s; traces -> %s, metrics -> %s, logs -> %s",
        OPENAI_MODEL, EXPORTERS["traces"], EXPORTERS["metrics"], EXPORTERS["logs"],
    )
    yield
    log.info("service stopping")
    if heartbeat_task:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
    await app.state.http.aclose()
    await app.state.openai.close()
    trace.get_tracer_provider().shutdown()  # flush the batch processor so the last spans are not lost
    for provider in SDK_PROVIDERS:  # ...and the last metrics and log records
        provider.shutdown()


app = FastAPI(
    title="Foundry Learn Agent",
    version=SERVICE_VERSION,
    description=(
        "A small FastAPI service that shows the OpenTelemetry pattern end to end: an inbound request becomes a "
        "server span, and every outbound call the agent makes (Microsoft Learn MCP, OpenAI) becomes a nested "
        "client span. Every traced response carries its trace_id (body and `X-Trace-Id` header) - and a "
        "`trace_url` into the trace UI when TRACE_UI_URL is set - so you can find it in your trace viewer; 502 "
        "bodies carry them too. `/ask` runs the tools locally (every hop visible); `/ask-hosted` lets OpenAI "
        "run them (one opaque hop)."
    ),
    lifespan=lifespan,
)


def current_trace_id() -> str:
    """The active span's trace id as the 32-hex string Jaeger shows. All zeros means 'no span here'."""
    return format(trace.get_current_span().get_span_context().trace_id, "032x")


def trace_url_for(trace_id: str) -> str | None:
    """Turn a trace id into a link into the trace UI, using the TRACE_UI_URL template - or None if unset.

    The template holds a literal `{trace_id}` placeholder: `http://localhost:16686/trace/{trace_id}` for Jaeger.
    It is read per call (not at import) so the tests can set and clear it. All-zero ids mean "no span" -> None.
    """
    template = os.getenv("TRACE_UI_URL")
    if not template or trace_id == "0" * 32:
        return None
    return template.replace("{trace_id}", trace_id)


# The `stats` middleware is registered BEFORE the FastAPI instrumentation on purpose. Starlette runs the
# most recently added middleware outermost, and current opentelemetry-instrumentation-fastapi versions force
# their middleware outermost anyway - either way this one runs INSIDE the SERVER span, so the span is "current"
# here and the X-Trace-Id header can be stamped on every traced response, including the 502s.
@app.middleware("http")
async def count_requests(request: Request, call_next):
    """Feeds the `stats` counters (pure Python, no OTEL) and echoes the trace id as an X-Trace-Id header."""
    path = request.url.path
    stats.requests[path] += 1
    try:
        response = await call_next(request)
    except Exception:
        stats.errors[path] += 1
        raise
    if response.status_code >= 500:
        stats.errors[path] += 1
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:  # excluded URLs (/healthz, Swagger) have no span, so they get no header
        response.headers["X-Trace-Id"] = format(span_context.trace_id, "032x")
    return response


# INBOUND: every request becomes a SERVER span - the root of the trace - except the excluded URLs.
FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS)


def to_http_error(exc: Exception) -> HTTPException:
    """Translate an upstream failure into a 502 whose body carries the reason AND the trace id to look it up.

    The span keeps the full stack (start_as_current_span recorded the exception); the body carries just enough
    for a human: a readable reason, the trace id, and - when TRACE_UI_URL is set - a link straight to the trace.
    """
    if isinstance(exc, APIError):
        reason = f"OpenAI error: {exc}"
    elif isinstance(exc, httpx.HTTPError):
        reason = f"Microsoft Learn MCP transport error: {exc}"
    elif isinstance(exc, ValueError):  # pydantic ValidationError is a ValueError: the model broke the schema
        reason = f"The model's answer did not match the expected schema: {exc}"
    else:
        reason = str(exc)
    trace_id = current_trace_id()
    # One WARNING log per failure, with the exception attached: in Aspire's Structured logs it carries
    # exception.type, exception.message and the stack trace, and links to the failed request's trace.
    log.warning("upstream failure -> 502: %s", reason, exc_info=exc, extra={"error.type": type(exc).__name__})
    detail = ErrorDetail(error=reason, trace_id=trace_id, trace_url=trace_url_for(trace_id))
    return HTTPException(status_code=502, detail=detail.model_dump())


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Send the bare URL to the Swagger UI at /docs."""
    return RedirectResponse(url="/docs")


@app.get("/ping", tags=["ops"], summary="The cheapest way to generate a span")
async def ping() -> dict[str, Any]:
    """One request -> one SERVER span (plus the ASGI receive/send children). Compare with /healthz."""
    trace_id = current_trace_id()
    return {"pong": True, "trace_id": trace_id, "trace_url": trace_url_for(trace_id)}


@app.get("/healthz", tags=["ops"], summary="Liveness probe - NOT traced; the same JSON the heartbeat prints")
async def healthz() -> dict[str, Any]:
    """Excluded from instrumentation on purpose: probes run forever and are noise in a trace backend.
    Docker's HEALTHCHECK calls this every 60 s; watch `requests` grow while no span ever appears."""
    return stats.snapshot()


@app.get(
    "/tools",
    tags=["agent"],
    summary="List the Microsoft Learn MCP tools the agent can use",
    responses=UPSTREAM_ERROR_RESPONSES,
)
async def list_tools() -> list[ToolInfo]:
    """Talks to Microsoft Learn only - no OpenAI call, no tokens spent. Handy for exercising tracing for free."""
    mcp = McpClient(app.state.http, LEARN_MCP_URL)
    try:
        await mcp.initialize()
        tools = await mcp.list_tools()
    except (httpx.HTTPError, RuntimeError) as exc:
        raise to_http_error(exc) from exc
    return [
        ToolInfo(name=t["name"], description=t.get("description", ""), parameters=t.get("inputSchema", {}))
        for t in tools
    ]


@app.post(
    "/ask",
    tags=["agent"],
    summary="Ask the agent (tools run locally - every hop is a span)",
    responses=UPSTREAM_ERROR_RESPONSES,
)
async def ask(request: AskRequest) -> AskResponse:
    """Runs the full agent loop in this process: LLM -> Microsoft Learn tool calls -> structured answer.
    One trace, many spans: agent.run, one llm.turn per model call, one `mcp ...` span per Learn call."""
    mcp = McpClient(app.state.http, LEARN_MCP_URL)
    try:
        result = await run_agent(app.state.openai, mcp, build_user_prompt(**request.model_dump()))
    except (APIError, httpx.HTTPError, ValueError, RuntimeError) as exc:
        raise to_http_error(exc) from exc
    trace_id = current_trace_id()
    return AskResponse(
        **result.model_dump(),
        topic=request.topic,
        model=OPENAI_MODEL,
        trace_id=trace_id,
        trace_url=trace_url_for(trace_id),
    )


@app.post(
    "/ask-hosted",
    tags=["agent"],
    summary="Ask the agent (OpenAI runs the tools - one opaque span)",
    responses=UPSTREAM_ERROR_RESPONSES,
)
async def ask_hosted(request: AskRequest) -> AskResponse:
    """Same question, same answer shape, but OpenAI's hosted MCP feature executes the Microsoft Learn tools.
    One trace, FEW spans: agent.run -> llm.turn -> a single api.openai.com call. Compare it with /ask in Jaeger:
    the Learn hops are gone from the trace and only survive as `tool_calls` in the response body."""
    try:
        result = await run_agent_hosted(app.state.openai, build_user_prompt(**request.model_dump()))
    except (APIError, ValueError, RuntimeError) as exc:
        raise to_http_error(exc) from exc
    trace_id = current_trace_id()
    return AskResponse(
        **result.model_dump(),
        topic=request.topic,
        model=OPENAI_MODEL,
        trace_id=trace_id,
        trace_url=trace_url_for(trace_id),
    )
