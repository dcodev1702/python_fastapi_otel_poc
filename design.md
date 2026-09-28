# Foundry Learn Agent — Design & Hand-off Document

| | |
|---|---|
| **Project** | Foundry Learn Agent — FastAPI + OpenTelemetry learning service with an LLM agent that uses the Microsoft Learn MCP server |
| **Author** | dcodev1702 & M365 Copilot / Cowork |
| **Created** | 2026-09-28 |
| **Version** | 0.2.0 |
| **Audience** | (1) the human maintainer; (2) a GenAI assistant **with internet access** that will finish verification |

---

## 0. Instructions for the AI reading this

This project was written **offline** — no PyPI, no vendor docs, no live API calls were reachable while it was
built. The code compiles and its logic was exercised against stubbed dependencies (see §8), but every
**external contract** below (package versions, API request/response shapes, model names, container images) was
written from memory and must be confirmed against current documentation before the first real run.

Your job:

1. Work through the **§6 verification checklist** top to bottom. Each item names the assumption, the exact
   file + symbol where it lives, how to verify it, and what to change if the assumption is wrong.
2. Change **only** what verification proves wrong. Do not restyle, rename or "improve" working code — the
   file layout, span names, attribute names and endpoint contracts in §4–§5 are load-bearing for `README.md`.
3. If you can execute code: run the **§7 acceptance tests** and report exact output, not a summary.
4. Keep the header block (Author / Created / Version) on every file and bump `Version` in `app.py`
   (`SERVICE_VERSION`), `compose.yaml` (image tag) and this file together if you change behaviour.
5. Do **not** add security hardening, auth, rate limiting or secret scrubbing — this is an intro learning
   project by explicit request. Do not remove the commented-out Step 4 block; it is meant to stay commented.
   The one exception, requested by the maintainer: the compose files publish ports on IPv4 `127.0.0.1` and,
   when `.env` sets `LAN_IP`, on that LAN address — never on `0.0.0.0` or IPv6 (§5, decision 10). Keep it.
6. Report back in three lists: *verified as-is*, *changed (file:symbol, before → after, source URL)*,
   *could not verify*.

---

## 1. Goal

Teach the OpenTelemetry (OTEL) pattern in Python by making one REST call produce one readable trace that spans
an inbound request, an LLM agent loop, MCP tool calls and LLM calls — and show the same agent two ways so the
trade-off between *local tools* (visible hops) and *hosted tools* (opaque hop) is visible in a trace viewer.

Non-goals: production hardening, auth, multi-tenancy, persistence, metrics/logs pipelines (listed as next steps
only).

## 2. Architecture

![Foundry Learn Agent architecture. An API client on the Linux host calls the FastAPI app in the foundry-learn-agent container. The local-tools agent (POST /ask) calls the Microsoft Learn MCP server and the OpenAI Responses API itself, so every hop is a span; the hosted-MCP agent (POST /ask-hosted) makes one OpenAI call, and OpenAI calls Learn outside our trace. The OpenTelemetry SDK exports spans to the container log by default, to Jaeger when compose.jaeger.yaml is used, and to Application Insights once the commented Step 4 is enabled; the health heartbeat prints to the same log without creating spans.](images/foundry-learn-agent-architecture-dark.svg)

### Trace shapes (the teaching payload)

`POST /ask` (local tools): `SERVER POST /ask` → `agent.run` → `mcp initialize` → `mcp notifications/initialized`
→ `mcp tools/list` → `llm.turn` (×N) interleaved with `mcp tools/call <tool>` (×M); each `mcp …` and `llm.turn`
span has one automatic httpx `POST` CLIENT child.

`POST /ask-hosted` (hosted MCP): `SERVER POST /ask-hosted` → `agent.run` → `llm.turn` → one httpx `POST`
CLIENT child. The Learn calls exist only as `mcp_call` items in the OpenAI response body, surfaced as
`tool_calls` (with `duration_ms: null`).

## 3. Files

| File | Role | Notes |
|---|---|---|
| `app.py` | Entire application, 5 numbered sections | §1 OTEL setup (+ Step 4 commented), §2 MCP client, §3 agent, §4 health/`stats`, §5 API |
| `requirements.txt` | Dependencies as **floors** | pip resolves newest compatible set at build; OTEL family lockstep enforced by pip |
| `Dockerfile` | `python:3.12-slim`, non-root, pip upgraded before install, HEALTHCHECK on `/healthz` | `PYTHONUNBUFFERED=1` so spans + heartbeat reach `docker compose logs` |
| `compose.yaml` | Console mode; API on `127.0.0.1:8000` (+ `LAN_IP`); `deploy.resources.limits.memory: 3g` | Linux host, Compose v2 |
| `compose.jaeger.yaml` | Override: adds Jaeger all-in-one (UI on `127.0.0.1` + `LAN_IP`, OTLP/HTTP on `127.0.0.1` only), sets `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318` | `docker compose -f compose.yaml -f compose.jaeger.yaml up --build` |
| `.env.example` | Template for `.env` (`OPENAI_API_KEY`, optional knobs) | `.env` is git- and docker-ignored |
| `.dockerignore`, `.gitignore` | Keep `.env` out of the image and the repo | |
| `README.md` | User-facing guide: setup, walkthrough, exercises, troubleshooting | Depends on the contracts in §4–§5 |
| `design.md` | This document | |
| `CHANGELOG.md` | Notable changes, version by version | Keep a Changelog format; the version bumps only with behaviour (§0) |
| `LICENSE` | MIT License, copyright 2026 DCODEV1702 | Keep the text unmodified and without a header block, or GitHub stops detecting the license |
| `images/foundry-learn-agent-architecture-dark.svg` | §2 architecture diagram | Dark-theme SVG; update it when components, endpoints or flows change |

## 4. Contracts that README.md depends on (do not change without updating README)

### Endpoints

| Method/path | Traced? | Purpose | Response |
|---|---|---|---|
| `GET /` | no (redirect) | → `/docs` | 307 |
| `GET /ping` | **yes** | cheapest span | `{"pong": true, "trace_id": "<32 hex>"}` |
| `GET /healthz` | **no** (in `EXCLUDED_URLS`) | liveness; same JSON the heartbeat prints | `stats.snapshot()` |
| `GET /tools` | yes | Learn MCP `initialize` + `tools/list`; no OpenAI call | `[{name, description, parameters}]` |
| `POST /ask` | yes | agent, local tool loop | `AskResponse` |
| `POST /ask-hosted` | yes | agent, OpenAI hosted MCP | `AskResponse` |

`AskRequest`: `{topic: str = "Microsoft Foundry", paragraphs: int 1..5 = 2, links: int 1..10 = 3}`
`AskResponse`: `{mode: "local-tools"|"hosted-mcp", brief: {paragraphs: [str], links: [{title, url}]}, turns: int,
usage: {input_tokens, output_tokens}, tool_calls: [{tool, arguments, result_chars, duration_ms|null, error|null}],
topic, model, trace_id}`
Errors from upstream (OpenAI, Learn, schema violation, turn limit) → HTTP **502** with a readable `detail`.

### Span names and attributes

| Span | Kind | Attributes |
|---|---|---|
| `agent.run` | INTERNAL (manual) | `agent.mode`, `gen_ai.request.model`, `agent.tools[]`, `agent.turns`, `agent.tool_calls`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`; `record_exception` on tool failures; event `mcp_call.error` (hosted) |
| `llm.turn` | INTERNAL (manual) | `agent.turn`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| `mcp <method>[ <tool>]` | INTERNAL (manual) | `rpc.system=jsonrpc`, `rpc.method`, `mcp.tool.name`, `mcp.tool.arguments` |
| `POST` (httpx auto) | CLIENT | HTTP semconv attrs (old or new names depending on `OTEL_SEMCONV_STABILITY_OPT_IN`) |
| `<METHOD> <route>` (FastAPI auto) | SERVER | HTTP semconv attrs; children `… http receive` / `… http send` |

Resource: `service.name` (from `OTEL_SERVICE_NAME`, default `foundry-learn-agent`), `service.version`.

### `stats` snapshot (heartbeat line and `/healthz` body)

```json
{"status":"ok","service":"foundry-learn-agent","version":"0.2.0","time":"<UTC ISO>","uptime_s":0,
 "exporter":"console|otlp[+azure-monitor]","model":"gpt-5.6-luna","rss_mb":25.6,
 "requests":{"/path":n},"errors":{"/path":n},"llm_turns":0,"tool_calls":0,
 "tokens":{"input_tokens":0,"output_tokens":0}}
```
Printed as `[healthz] {json}` at startup and then every `HEALTHZ_INTERVAL_SECONDS` (default 60; `0` disables).
Nothing in this path may perform I/O that would create a span.

### Environment variables

| Variable | Default | Read in |
|---|---|---|
| `OPENAI_API_KEY` | required | `lifespan` (fail fast) + OpenAI SDK |
| `OPENAI_MODEL` | `gpt-5.6-luna` | `app.py` §3 `OPENAI_MODEL` |
| `OTEL_SERVICE_NAME` | `foundry-learn-agent` | `SERVICE_NAME` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset → console | `configure_opentelemetry()` |
| `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` | `docs,openapi.json,redoc,healthz` | `EXCLUDED_URLS` |
| `HEALTHZ_INTERVAL_SECONDS` | `60` | `HEALTHZ_INTERVAL_SECONDS` |
| `LEARN_MCP_URL` | `https://learn.microsoft.com/api/mcp` | `LEARN_MCP_URL` (also used by `HOSTED_MCP_TOOL`) |
| `OTEL_BSP_SCHEDULE_DELAY`, `OTEL_SEMCONV_STABILITY_OPT_IN` | SDK defaults | read by the SDK/instrumentation, not by app code |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | unset | Step 4 block only (commented) |
| `LAN_IP` | unset → `127.0.0.1` only | Docker Compose port publishing in `compose*.yaml`, not app code |

## 5. Design decisions (and why)

1. **Hand-rolled tool loop for `/ask`** instead of OpenAI's hosted MCP: every hop becomes a span. Hosted MCP is
   offered as `/ask-hosted` precisely so the loss of visibility can be *seen*.
2. **Manual spans wrap automatic spans.** All Learn calls hit one URL, so httpx's automatic `POST` spans are
   indistinguishable; the manual `mcp tools/call <tool>` span carries the meaning.
3. **One Pydantic model, two jobs.** `Brief` is the FastAPI response schema *and* the OpenAI strict JSON
   schema (`extra="forbid"` → `additionalProperties: false`, all fields required — both strict-mode rules).
4. **SDK configured at import, clients created in `lifespan`.** `HTTPXClientInstrumentor().instrument()` must
   run before any `httpx.AsyncClient`/`AsyncOpenAI` exists so both are patched.
5. **`/healthz` is excluded from tracing, `/ping` is not.** Probes run forever and are noise; the contrast is
   itself a lesson about `excluded_urls`. The heartbeat is plain `print` — a deliberate "control group".
6. **Console exporter by default, OTLP by env var, Azure Monitor commented.** Zero infrastructure first; the
   provider fans out to every processor, so adding a backend never touches application code.
7. **Floors, not pins, in `requirements.txt`.** Maintainer wants newest packages; pip enforces OTEL lockstep
   because contrib packages pin `opentelemetry-semantic-conventions` exactly.
8. **Compose override file for Jaeger** rather than profiles, so one command both starts Jaeger and points the
   exporter at it (no half-configured state where the exporter targets a Jaeger that is not running).
9. **`MAX_TURNS = 8`** bounds cost if the model loops.
10. **Ports are published on IPv4 `127.0.0.1`, plus `LAN_IP` when set — never on `0.0.0.0` or IPv6.** `/ask` and
    `/ask-hosted` spend OpenAI tokens, the Jaeger UI shows every span attribute (tool arguments included) and
    OTLP accepts spans from anyone, so none of them may be reachable from the Internet. An explicit IPv4 host
    address also stops Docker from publishing on `[::]`, and Compose merges the two identical mappings when
    `LAN_IP` is unset. Inside the container uvicorn still listens on `0.0.0.0`: that is the container's own
    network namespace, which Docker needs in order to publish the port.

## 6. VERIFICATION CHECKLIST — assumptions made without internet access

Legend: **Assumed** = what the code believes · **Where** = file:symbol · **Verify** = what to check · **If wrong** = the fix.

### 6.1 OpenAI

| # | Assumed | Where | Verify | If wrong |
|---|---|---|---|---|
| O1 | Model id **`gpt-5.6-luna`** exists, is available to the maintainer's account, and supports function calling, structured outputs (`json_schema`, `strict: true`) and the hosted MCP tool. | `app.py` §3 `OPENAI_MODEL`; `.env.example`; `design.md` §4 | OpenAI model docs / `GET /v1/models` | Change the default in `OPENAI_MODEL` and `.env.example` to a model that supports all three. |
| O2 | Responses API **function tool** shape `{"type":"function","name","description","parameters","strict"}` at the top level (not nested under `"function"`). | `to_openai_tools()` | Responses API reference → tools | Adjust the dict shape. |
| O3 | Tool-call output items have `type == "function_call"` with `.name`, `.arguments` (JSON string), `.call_id`; results are fed back as `{"type":"function_call_output","call_id","output"}`; prior output items can be appended verbatim to `input`. | `run_agent()` | Responses API function-calling guide | Adjust item access / input construction. |
| O4 | Structured outputs are passed as `text={"format":{"type":"json_schema","name","strict":true,"schema"}}`; final text is `response.output_text`; usage is `response.usage.input_tokens/.output_tokens`. | `BRIEF_TEXT_FORMAT`, `record_usage()` | Responses API structured-outputs guide | Adjust parameter path / attribute names. |
| O5 | **Hosted MCP tool** shape `{"type":"mcp","server_label","server_url","require_approval":"never"}` (optional `allowed_tools`); output items `mcp_list_tools` (`.tools[].name`) and `mcp_call` (`.name`, `.arguments`, `.output`, `.error`); with `require_approval: "never"` no `mcp_approval_request` items appear; structured outputs may be combined with the `mcp` tool. | `HOSTED_MCP_TOOL`, `run_agent_hosted()` | Responses API → MCP tool guide | Adjust shape / item parsing. If structured outputs cannot be combined with `mcp`, drop `text=` in the hosted call and parse `output_text` leniently. |
| O6 | `openai>=1.80` is a sufficient floor for the hosted MCP tool and `responses.create`. | `requirements.txt` | SDK changelog | Raise the floor. |
| O7 | `openai.APIError` is the common base of status/connection/timeout errors. | `to_http_error()` | SDK docs | Widen the `except` tuple. |

### 6.2 Microsoft Learn MCP server

| # | Assumed | Where | Verify | If wrong |
|---|---|---|---|---|
| L1 | Endpoint **`https://learn.microsoft.com/api/mcp`**, Streamable HTTP transport, no auth. | `LEARN_MCP_URL` | Microsoft Learn MCP server docs / GitHub `MicrosoftDocs/mcp` | Update the default. |
| L2 | Tool names **`microsoft_docs_search`** and **`microsoft_docs_fetch`**; search takes `query`, fetch takes `url`. | `SYSTEM_PROMPT`, `HOSTED_MCP_TOOL` comment, README | `GET /tools` output once running | Update names in the system prompt (rules 2–3) and the comment. |
| L3 | JSON-RPC `initialize` with `protocolVersion: "2025-03-26"` is accepted; the server may answer as `application/json` **or** a one-shot `text/event-stream`; it may return `Mcp-Session-Id`, which must be echoed. | `McpClient.initialize()`, `_rpc()`, `_parse()` | MCP spec (Streamable HTTP) and the Learn server behaviour | Bump `protocolVersion`. If the server requires the `MCP-Protocol-Version` header on follow-up requests (added in the 2025-06-18 spec), add it to `headers` in `_rpc()`. |
| L4 | `notifications/initialized` returns 202 with an empty body and needs no parsing. | `_rpc(notification=True)` | live call | If the server returns a body/other code, keep ignoring it — only raise on ≥400. |
| L5 | Tool results arrive as `result.content[]` with `{"type":"text","text":...}` blocks. | `McpClient.call_tool()` | live call | Adjust extraction. |

### 6.3 OpenTelemetry (Python)

| # | Assumed | Where | Verify | If wrong |
|---|---|---|---|---|
| T1 | Latest core is **≥ 1.44.0** and the matching contrib is **`0.(N+21)b0`** (1.44.0 ↔ 0.65b0); floors `>=1.44.0,<2` / `>=0.65b0,<1` resolve to a consistent set. | `requirements.txt` | PyPI: `opentelemetry-sdk`, `opentelemetry-instrumentation-httpx` | Adjust floors; keep the lockstep note accurate. |
| T2 | `FastAPIInstrumentor.instrument_app(app, excluded_urls="<comma-separated regexes>")` exists; passing `excluded_urls` overrides `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS`; `exclude_spans=["receive","send"]` is a valid kwarg (README exercise). | `app.py` §5, README exercises | opentelemetry-instrumentation-fastapi docs | Adjust kwarg names. |
| T3 | `HTTPXClientInstrumentor().instrument()` patches clients created afterwards, including the one inside the OpenAI SDK; a `traceparent` header is injected. | `configure_opentelemetry()` | opentelemetry-instrumentation-httpx docs | If newer versions require `instrument_client(client)`, instrument `app.state.http` and `app.state.openai._client` in `lifespan`. |
| T4 | OTLP/HTTP exporter import path `opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter`; with no args it reads `OTEL_EXPORTER_OTLP_ENDPOINT` and appends `/v1/traces`. | `configure_opentelemetry()` | opentelemetry-exporter-otlp-proto-http docs | Adjust import / pass `endpoint=` explicitly. |
| T5 | `OTEL_BSP_SCHEDULE_DELAY` (ms) and `OTEL_SEMCONV_STABILITY_OPT_IN=http` are honoured. | `.env.example`, README | OTEL Python SDK env var docs | Fix names in `.env.example` and README. |
| T6 | Semantic-convention attribute names on auto spans are either `http.method/http.url/http.status_code` or `http.request.method/url.full/http.response.status_code`. | README "Reading the console output" | Run `/ping` once | Update README wording. |
| T7 | `trace.get_tracer_provider().shutdown()` flushes `BatchSpanProcessor`. | `lifespan` | SDK docs | Use `provider.force_flush()` then `shutdown()`. |

### 6.4 Step 4 — Azure Monitor (commented code; verify so it works when uncommented)

| # | Assumed | Where | Verify | If wrong |
|---|---|---|---|---|
| A1 | Package **`azure-monitor-opentelemetry-exporter`**; class `azure.monitor.opentelemetry.exporter.AzureMonitorTraceExporter`; no-arg constructor reads `APPLICATIONINSIGHTS_CONNECTION_STRING`. | `configure_opentelemetry()` Step 4 block; `requirements.txt` | Azure SDK for Python docs | Fix import/constructor in the commented block. |
| A2 | The exporter is compatible with the resolved `opentelemetry-sdk` version. | `requirements.txt` | package metadata | Add a version constraint on the commented line. |
| A3 | Portal mapping: SERVER→requests, CLIENT→dependencies (HTTP), INTERNAL→dependencies (InProc), attributes→customDimensions, exceptions→exceptions, `service.name`→`cloud_RoleName`. | Step 4 comments; README | Azure Monitor OpenTelemetry docs | Correct the comment text. |
| A4 | Connection string carries `IngestionEndpoint`, so Azure Government works without code changes. | Step 4 comments; README | Azure Monitor docs | Correct the comment text. |

### 6.5 Containers

| # | Assumed | Where | Verify | If wrong |
|---|---|---|---|---|
| C1 | `python:3.12-slim` is current and appropriate (3.13-slim acceptable if all wheels exist). | `Dockerfile` | Docker Hub | Bump tag; keep `slim`. |
| C2 | Compose v2 honours `deploy.resources.limits.memory` outside Swarm; `3g` is a valid byte value; `init: true`, top-level `name:` and `depends_on.condition: service_started` are valid. | `compose.yaml`, `compose.jaeger.yaml` | Compose spec | Fall back to `mem_limit: 3g` if limits are ignored (`docker stats` shows LIMIT). |
| C3 | `jaegertracing/jaeger:latest` (Jaeger v2) runs all-in-one by default with OTLP receivers on 4317/4318 and UI on 16686. | `compose.jaeger.yaml`, README | Jaeger docs | Pin a version tag; if v1 semantics are needed use `jaegertracing/all-in-one` with `COLLECTOR_OTLP_ENABLED=true`. |
| C4 | `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` is harmless with the HTTP exporter. | `compose.jaeger.yaml` | SDK docs | Remove if it causes a warning. |
| C5 | `python -c "urllib.request.urlopen(...)"` works as HEALTHCHECK in slim (no curl). | `Dockerfile`, `compose.yaml` | `docker inspect --format '{{json .State.Health}}'` | Adjust. |

### 6.6 FastAPI / Pydantic

| # | Assumed | Where | Verify | If wrong |
|---|---|---|---|---|
| F1 | `@app.middleware("http")` (BaseHTTPMiddleware) coexists with `FastAPIInstrumentor.instrument_app`; `request.url.path` is available; exceptions propagate through `call_next`. | `count_requests` | run `/ping`, `/healthz` | If ordering issues arise, register the middleware before `instrument_app`. |
| F2 | Pydantic v2 API: `ConfigDict(extra="forbid")`, `model_json_schema()`, `model_validate_json()`, `model_dump()`; nested `Link` lands in `$defs` with `additionalProperties:false` and all fields `required` (OpenAI strict rules). | `Brief`, `Link`, `BRIEF_TEXT_FORMAT` | print `Brief.model_json_schema()` | If strict mode rejects the schema, post-process: ensure every object has `additionalProperties:false` and `required` = all property names. |
| F3 | Lifespan context manager pattern (`FastAPI(lifespan=...)`) and `RedirectResponse` import path. | `lifespan`, `root()` | FastAPI docs | Adjust. |

## 7. Acceptance tests (run in this order; report raw output)

Prereqs: Docker on Linux, `.env` with a valid `OPENAI_API_KEY`.

```bash
# 0. Build + start (console mode)
cp .env.example .env && $EDITOR .env
docker compose up --build -d && sleep 5 && docker compose logs api | head -40
#    EXPECT: a "[healthz] {...}" line within the first lines; no tracebacks; uvicorn "Application startup complete".

# 1. Untraced probe
curl -s localhost:8000/healthz | python -m json.tool
docker compose logs api | grep -c '"name": "GET /healthz"'
#    EXPECT: JSON snapshot; grep count 0 (no span for /healthz)

# 2. Traced probe
curl -s localhost:8000/ping; sleep 6; docker compose logs api | grep -A3 '"name": "GET /ping"' | head
#    EXPECT: {"pong":true,"trace_id":"<32 hex>"}; a SERVER span JSON whose context.trace_id == trace_id

# 3. Learn only (no tokens)
curl -s localhost:8000/tools | python -m json.tool | head -30; sleep 6
docker compose logs api | grep -o '"name": "mcp [^"]*"' | sort | uniq -c
#    EXPECT: tools microsoft_docs_search + microsoft_docs_fetch; spans mcp initialize, mcp notifications/initialized, mcp tools/list

# 4. Local-tools agent
curl -s -X POST localhost:8000/ask -H 'Content-Type: application/json' \
     -d '{"topic":"Microsoft Foundry","paragraphs":2,"links":3}' | python -m json.tool
#    EXPECT: mode local-tools, 2 paragraphs, 3 learn.microsoft.com links, turns >= 2, tool_calls >= 1 with duration_ms numbers

# 5. Hosted-MCP agent
curl -s -X POST localhost:8000/ask-hosted -H 'Content-Type: application/json' \
     -d '{"topic":"Microsoft Foundry","paragraphs":2,"links":3}' | python -m json.tool
#    EXPECT: mode hosted-mcp, turns == 1, tool_calls with duration_ms null

# 6. Heartbeat + memory cap
sleep 65; docker compose logs api | grep '\[healthz\]' | tail -2
docker stats --no-stream foundry-learn-agent
#    EXPECT: requests counters include /healthz,/ping,/tools,/ask,/ask-hosted; MEM LIMIT column shows 3GiB

# 7. Jaeger mode
docker compose down && docker compose -f compose.yaml -f compose.jaeger.yaml up --build -d
#    repeat steps 4 and 5, then open http://localhost:16686 -> service foundry-learn-agent -> compare the two traces
#    EXPECT: /ask trace has many child spans (mcp ..., llm.turn, POST); /ask-hosted trace has agent.run -> llm.turn -> one POST

# 8. Who can connect (Jaeger mode still running)
ss -ltn | grep -E ':(8000|16686|4318) '
#    EXPECT: 127.0.0.1 for all three, plus the LAN_IP address for 8000 and 16686 when .env sets it;
#            never 0.0.0.0, [::] or any other address
```

## 8. What was verified offline (already done — do not repeat)

- `python -m py_compile app.py` passes.
- A stub harness (third-party packages replaced by fakes) executed: the local tool loop over a fake Learn MCP
  server (JSON **and** SSE responses, `Mcp-Session-Id` propagation, notification without `id`), the hosted-MCP
  reconstruction (`mcp_list_tools`, `mcp_call` with and without `error`, malformed arguments), the tool-failure
  path (`TOOL ERROR` fed back to the model, exception recorded on `agent.run`), span naming order, `stats`
  accumulation, `to_http_error` mapping, route registration.
- Not verified: anything involving a real network call, real package versions, real container runtime.

## 9. Extension points (after verification)

1. **Metrics** — add a `MeterProvider` in `configure_opentelemetry()`; FastAPI instrumentation then emits
   request duration / active requests with no further code. Consider exporting the `stats` counters as OTEL
   metrics to show the "print vs telemetry" convergence.
2. **Logs** — inject `trace_id`/`span_id` into `logging` records (`opentelemetry-instrumentation-logging`) and
   replace `print_health()` with a logger while keeping stdout as the sink.
3. **Distributed tracing demo** — a second tiny FastAPI service that the agent calls; the injected `traceparent`
   makes both services appear in one Jaeger trace.
4. **Azure Monitor (Step 4)** — uncomment; the same spans appear in Application Insights.
5. **GenAI semantic conventions** — replace the ad-hoc `gen_ai.*`/`agent.*` attributes with the official
   `gen_ai.*` conventions once stable, or adopt an OpenAI instrumentation package.
