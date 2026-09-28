"""
Foundry Learn Agent
===================
Description : A small FastAPI service for learning OpenTelemetry (OTEL) end to end. An LLM agent answers
              questions about Microsoft products using the Microsoft Learn MCP server, and every hop -
              inbound HTTP request, agent loop, Learn tool calls, OpenAI calls - becomes a span in ONE trace.
              The same agent is exposed twice so the two trace shapes can be compared side by side:

                POST /ask         local tools  - the tool loop runs HERE; every Learn call is a span you can see
                POST /ask-hosted  hosted MCP   - OpenAI runs the tool loop; one opaque OpenAI span, far less code

Author      : dcodev1702 & M365 Copilot / Cowork
Created     : 2026-09-28
Version     : 0.2.4
Python      : 3.11+
Run (local) : uvicorn app:app --reload                                  -> http://127.0.0.1:8000/docs
Run (Docker): docker compose up --build                                 -> http://localhost:8000/docs
              docker compose -f compose.yaml -f compose.jaeger.yaml up --build   (+ Jaeger UI on :16686)

Endpoints
    GET  /ping         the cheapest possible span: one request, one trace, returns its trace_id
    GET  /healthz      liveness for Docker / Kubernetes. Deliberately NOT traced. The same status JSON is also
                       printed to stdout every HEALTHZ_INTERVAL_SECONDS (default 60) by a background heartbeat
    GET  /tools        lists the Microsoft Learn MCP tools (Learn calls only: no OpenAI, no tokens, free tracing)
    POST /ask          the agent, tools executed locally
    POST /ask-hosted   the agent, tools executed by OpenAI's hosted MCP feature

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
    1. OpenTelemetry setup       API vs SDK vs instrumentation. Step 4 (Azure Monitor) is built in, commented out.
    2. Microsoft Learn MCP client
    3. The agent                 prompts, output contract, the local tool loop, the hosted variant
    4. Health + heartbeat        the `stats` counters - deliberately span-free
    5. The API
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from openai import APIError, AsyncOpenAI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor, HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from pydantic import BaseModel, ConfigDict, Field

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "foundry-learn-agent")
SERVICE_VERSION = "0.2.4"

# =============================================================================
# 1. OPENTELEMETRY SETUP
#
#    API              = the interface everyone codes against (get_tracer, spans, attributes, events)
#    SDK              = the implementation we configure here: Resource -> TracerProvider -> Processor -> Exporter
#    Instrumentation  = plug-ins that create spans for frameworks/libraries we do not own (FastAPI, httpx)
#
#    A provider fans out to EVERY processor added to it, which is why console, OTLP and Azure Monitor can all be
#    switched on at the same time without touching a single line of application code.
# =============================================================================
EXCLUDED_URLS = os.getenv(
    "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS",
    "docs,openapi.json,redoc,healthz",  # comma-separated regexes; Swagger traffic and liveness probes are noise
)


def configure_opentelemetry() -> str:
    """Wire the SDK and switch on outbound instrumentation. Returns the exporter mode for the health snapshot."""
    resource = Resource.create({"service.name": SERVICE_NAME, "service.version": SERVICE_VERSION})
    provider = TracerProvider(resource=resource)

    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        # Ship spans to Jaeger / an OTel Collector / the Aspire Dashboard over OTLP-HTTP, e.g.
        #   OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318          (uvicorn on your machine)
        #   OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318             (inside docker compose)
        # pylint: disable-next=import-outside-toplevel  # only OTLP mode needs it, like the Step 4 exporter below
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

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
    #   1. pip install azure-monitor-opentelemetry-exporter        (also uncomment it in requirements.txt)
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

    # OUTBOUND: wrap the HTTP clients so every request becomes a CLIENT span and carries a W3C `traceparent`
    # header. It takes two instrumentors: our McpClient uses httpx, but the OpenAI SDK (openai 3.x) is built on
    # httpx2, a separate package - with the httpx one alone, every llm.turn span is missing its api.openai.com
    # child. Do this BEFORE any httpx / OpenAI client is created - that is the safest ordering across versions.
    HTTPXClientInstrumentor().instrument()
    HTTPX2ClientInstrumentor().instrument()
    return mode


EXPORTER_MODE = configure_opentelemetry()
tracer = trace.get_tracer(SERVICE_NAME, SERVICE_VERSION)  # our handle for manual spans (this is the API side)


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

# pylint: disable=line-too-long  # prompt text is what the model reads; re-wrapping it for a linter would change it
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
# pylint: enable=line-too-long

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


def record_usage(span: trace.Span, response: Any, totals: TokenUsage) -> None:
    """Tokens are money: stamp them on the llm.turn span and accumulate them per request and per process."""
    stats.llm_turns += 1
    if response.usage is None:
        return
    span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)
    totals.input_tokens += response.usage.input_tokens
    totals.output_tokens += response.usage.output_tokens
    stats.tokens.input_tokens += response.usage.input_tokens
    stats.tokens.output_tokens += response.usage.output_tokens


def finish_run(  # pylint: disable=too-many-arguments,too-many-positional-arguments  # six facts per run
    span: trace.Span, mode: AgentMode, brief: Brief, turns: int, usage: TokenUsage, records: list[ToolCallRecord]
) -> AgentResult:
    """Summarise the run on the agent.run span (what a dashboard would chart) and return the API-facing result."""
    span.set_attribute("agent.turns", turns)
    span.set_attribute("agent.tool_calls", len(records))
    span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
    stats.tool_calls += len(records)
    return AgentResult(mode=mode, brief=brief, turns=turns, usage=usage, tool_calls=records)


# pylint: disable-next=too-many-locals  # the whole tool loop reads top to bottom on purpose
async def run_agent(openai_client: AsyncOpenAI, mcp: McpClient, user_prompt: str) -> AgentResult:
    """LOCAL TOOLS. The whole agent is a loop: ask the model -> run the tools it asks for -> ask again -> answer.

    Every step is visible in the trace because every step happens in this process: the Learn round-trips are
    real httpx calls (CLIENT spans) wrapped in our manual `mcp ...` spans, interleaved with the `llm.turn` spans.
    If anything raises inside the `with` block, start_as_current_span records the exception on the span and marks
    it ERROR before re-raising - no extra code needed.
    """
    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("agent.mode", "local-tools")
        span.set_attribute("gen_ai.request.model", OPENAI_MODEL)

        await mcp.initialize()
        tools = to_openai_tools(await mcp.list_tools())
        span.set_attribute("agent.tools", [tool["name"] for tool in tools])

        conversation: list[Any] = [{"role": "user", "content": user_prompt}]
        records: list[ToolCallRecord] = []
        usage = TokenUsage()

        for turn in range(1, MAX_TURNS + 1):
            with tracer.start_as_current_span("llm.turn") as llm_span:
                llm_span.set_attribute("agent.turn", turn)
                response = await openai_client.responses.create(
                    model=OPENAI_MODEL,
                    instructions=SYSTEM_PROMPT,
                    input=conversation,
                    tools=tools,
                    text=BRIEF_TEXT_FORMAT,
                )
                record_usage(llm_span, response, usage)

            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:  # no tool calls requested -> the model produced its final (JSON) answer
                brief = Brief.model_validate_json(response.output_text)
                return finish_run(span, "local-tools", brief, turn, usage, records)

            conversation.extend(response.output)  # keep the model's turn (incl. its tool calls) in the history
            for call in calls:
                arguments = parse_arguments(call.arguments)
                started = time.perf_counter()
                error: str | None = None
                try:
                    output = await mcp.call_tool(call.name, arguments)
                except (httpx.HTTPError, RuntimeError) as exc:
                    error = str(exc)
                    output = f"TOOL ERROR: {exc}"  # let the model see the failure and decide what to do next
                    span.record_exception(exc)  # ...but leave a record of it on the agent span
                records.append(
                    ToolCallRecord(
                        tool=call.name,
                        arguments=arguments,
                        result_chars=len(output),
                        duration_ms=round((time.perf_counter() - started) * 1000, 1),
                        error=error,
                    )
                )
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
        span.set_attribute("agent.mode", "hosted-mcp")
        span.set_attribute("gen_ai.request.model", OPENAI_MODEL)
        usage = TokenUsage()

        with tracer.start_as_current_span("llm.turn") as llm_span:
            llm_span.set_attribute("agent.turn", 1)
            response = await openai_client.responses.create(
                model=OPENAI_MODEL,
                instructions=SYSTEM_PROMPT,
                input=user_prompt,
                tools=[HOSTED_MCP_TOOL],
                text=BRIEF_TEXT_FORMAT,
            )
            record_usage(llm_span, response, usage)

        # Reconstruct what happened from the output items OpenAI returns alongside the final message.
        records: list[ToolCallRecord] = []
        for item in response.output:
            if item.type == "mcp_list_tools":  # OpenAI did the tools/list for us
                span.set_attribute("agent.tools", [tool.name for tool in item.tools])
            elif item.type == "mcp_call":  # one per tool call OpenAI executed against Learn
                error = str(item.error) if item.error else None
                if error:
                    span.add_event("mcp_call.error", {"mcp.tool.name": item.name, "error": error})
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
#    it as the control group: plain stdout next to the OTEL output, so you can see what each one is good for.
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
            "exporter": EXPORTER_MODE,
            "model": OPENAI_MODEL,
            "rss_mb": current_rss_mb(),
            "requests": dict(self.requests),
            "errors": dict(self.errors),
            "llm_turns": self.llm_turns,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens.model_dump(),
        }


stats = Stats()


def current_rss_mb() -> float | None:
    """Resident memory of this process - worth watching against the 3 GB cap in compose.yaml. Linux only."""
    try:
        with open("/proc/self/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)  # kB -> MB
    except OSError:
        pass
    return None


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


class ToolInfo(BaseModel):
    """One Microsoft Learn MCP tool, as GET /tools lists it."""

    name: str
    description: str
    parameters: dict[str, Any]


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
    yield
    if heartbeat_task:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
    await app.state.http.aclose()
    await app.state.openai.close()
    trace.get_tracer_provider().shutdown()  # flush the batch processor so the last spans are not lost


app = FastAPI(
    title="Foundry Learn Agent",
    version=SERVICE_VERSION,
    description=(
        "A small FastAPI service that shows the OpenTelemetry pattern end to end: an inbound request becomes a "
        "server span, and every outbound call the agent makes (Microsoft Learn MCP, OpenAI) becomes a nested "
        "client span. Every traced response carries its trace_id so you can find it in your trace viewer. "
        "`/ask` runs the tools locally (every hop visible); `/ask-hosted` lets OpenAI run them (one opaque hop)."
    ),
    lifespan=lifespan,
)

# INBOUND: every request becomes a SERVER span - the root of the trace - except the excluded URLs.
FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS)


@app.middleware("http")
async def count_requests(request: Request, call_next):
    """Feeds the `stats` counters. Pure Python, no OTEL: this is what plain stdout status looks like."""
    path = request.url.path
    stats.requests[path] += 1
    try:
        response = await call_next(request)
    except Exception:
        stats.errors[path] += 1
        raise
    if response.status_code >= 500:
        stats.errors[path] += 1
    return response


def current_trace_id() -> str:
    """The active span's trace id as the 32-hex string Jaeger shows. All zeros means 'no span here'."""
    return format(trace.get_current_span().get_span_context().trace_id, "032x")


def to_http_error(exc: Exception) -> HTTPException:
    """Translate agent failures into a 502 (bad upstream) with a readable reason. The span keeps the full stack."""
    if isinstance(exc, APIError):
        return HTTPException(status_code=502, detail=f"OpenAI error: {exc}")
    if isinstance(exc, httpx.HTTPError):
        return HTTPException(status_code=502, detail=f"Microsoft Learn MCP transport error: {exc}")
    if isinstance(exc, ValueError):  # pydantic ValidationError is a ValueError: the model broke the schema
        return HTTPException(status_code=502, detail=f"The model's answer did not match the expected schema: {exc}")
    return HTTPException(status_code=502, detail=str(exc))


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Send the bare URL to the Swagger UI at /docs."""
    return RedirectResponse(url="/docs")


@app.get("/ping", tags=["ops"], summary="The cheapest way to generate a span")
async def ping() -> dict[str, Any]:
    """One request -> one SERVER span (plus the ASGI receive/send children). Compare with /healthz."""
    return {"pong": True, "trace_id": current_trace_id()}


@app.get("/healthz", tags=["ops"], summary="Liveness probe - NOT traced; the same JSON the heartbeat prints")
async def healthz() -> dict[str, Any]:
    """Excluded from instrumentation on purpose: probes run forever and are noise in a trace backend.
    Docker's HEALTHCHECK calls this every 60 s; watch `requests` grow while no span ever appears."""
    return stats.snapshot()


@app.get("/tools", tags=["agent"], summary="List the Microsoft Learn MCP tools the agent can use")
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


@app.post("/ask", tags=["agent"], summary="Ask the agent (tools run locally - every hop is a span)")
async def ask(request: AskRequest) -> AskResponse:
    """Runs the full agent loop in this process: LLM -> Microsoft Learn tool calls -> structured answer.
    One trace, many spans: agent.run, one llm.turn per model call, one `mcp ...` span per Learn call."""
    mcp = McpClient(app.state.http, LEARN_MCP_URL)
    try:
        result = await run_agent(app.state.openai, mcp, build_user_prompt(**request.model_dump()))
    except (APIError, httpx.HTTPError, ValueError, RuntimeError) as exc:
        raise to_http_error(exc) from exc
    return AskResponse(**result.model_dump(), topic=request.topic, model=OPENAI_MODEL, trace_id=current_trace_id())


@app.post("/ask-hosted", tags=["agent"], summary="Ask the agent (OpenAI runs the tools - one opaque span)")
async def ask_hosted(request: AskRequest) -> AskResponse:
    """Same question, same answer shape, but OpenAI's hosted MCP feature executes the Microsoft Learn tools.
    One trace, FEW spans: agent.run -> llm.turn -> a single api.openai.com call. Compare it with /ask in Jaeger:
    the Learn hops are gone from the trace and only survive as `tool_calls` in the response body."""
    try:
        result = await run_agent_hosted(app.state.openai, build_user_prompt(**request.model_dump()))
    except (APIError, ValueError, RuntimeError) as exc:
        raise to_http_error(exc) from exc
    return AskResponse(**result.model_dump(), topic=request.topic, model=OPENAI_MODEL, trace_id=current_trace_id())
