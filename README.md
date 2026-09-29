# Foundry Learn Agent — a FastAPI + OpenTelemetry starter

| | |
|---|---|
| **Author** | dcodev1702 & M365 Copilot / Cowork |
| **Created** | 2026-09-28 |
| **Version** | 0.4.0 |
| **Runs on** | Python 3.14.7+ locally, or Docker Compose on Linux (container capped at 3 GB RAM) |

A small, runnable service for learning how OpenTelemetry (OTEL) tracing works in a Python API that drives an
LLM agent. One `POST /ask` produces **one trace** with a dozen-plus nested spans: the inbound HTTP request, the
agent loop, every Microsoft Learn MCP tool call and every OpenAI call. The same agent is exposed a second time
as `POST /ask-hosted`, where OpenAI runs the tools for you — so you can see, in a trace viewer, exactly what
visibility you give up for less code.

Traces are one of OpenTelemetry's three *signals*. The same app also records **metrics** (token usage, LLM and tool
latency, request rates, memory) and **structured logs**, each log stamped with the trace it happened in. Switch
those two on with the Aspire Dashboard override, and all three signals appear side by side.

```
POST /ask  (tools run here)                                 POST /ask-hosted  (OpenAI runs the tools)

SERVER  POST /ask                                           SERVER  POST /ask-hosted
 └─ agent.run  mode=local-tools                              └─ agent.run  mode=hosted-mcp
     ├─ mcp initialize                                           └─ llm.turn
     │   └─ CLIENT POST learn.microsoft.com                          └─ CLIENT POST api.openai.com
     ├─ mcp notifications/initialized                                    (OpenAI → Learn happens over there;
     │   └─ CLIENT POST learn.microsoft.com                               not in your trace)
     ├─ mcp tools/list
     │   └─ CLIENT POST learn.microsoft.com
     ├─ llm.turn  turn=1
     │   └─ CLIENT POST api.openai.com
     ├─ mcp tools/call microsoft_docs_search      ← one per tool call the model makes
     │   └─ CLIENT POST learn.microsoft.com
     └─ llm.turn  turn=n  → final JSON answer
         └─ CLIENT POST api.openai.com
```

## Architecture

![Foundry Learn Agent architecture. An API client on the Linux host calls the FastAPI app in the foundry-learn-agent container. The local-tools agent (POST /ask) calls the Microsoft Learn MCP server and the OpenAI Responses API itself, so every hop is a span; the hosted-MCP agent (POST /ask-hosted) makes one OpenAI call, and OpenAI calls Learn outside your trace. The OpenTelemetry SDK exports spans to the container log by default, to Jaeger when compose.jaeger.yaml is used, and to Application Insights once the commented Step 4 is enabled; with compose.aspire.yaml it sends traces, metrics and logs to the Aspire Dashboard instead. The health heartbeat prints to the same log without creating spans.](images/foundry-learn-agent-architecture-dark.svg)

Where each piece runs and what talks to what; the numbered flows are explained in the key under the drawing.
[`design.md` §2](design.md#2-architecture) has the design notes behind it.

## Files

| File | Purpose |
|---|---|
| `app.py` | The whole program in five numbered sections: OTEL setup (with **Step 4 / Azure Monitor built in, commented out**), Microsoft Learn MCP client, the agent (prompts + local loop + hosted variant), health + `stats` heartbeat, the API |
| `requirements/` | Dependencies, in one place (see *Dependencies*). `base.txt` holds the *floors*: the intent you edit, with the OTEL family in lockstep. `lock.txt` is the exact, pinned set the image installs, made from `base.txt` by `scripts/lock.sh`. `dev.txt` has the test and lint tools (`pytest`, `pylint`), never installed in the image |
| `scripts/lock.sh` | Resolves the lock inside the Dockerfile's base image and checks it before keeping it |
| `tests/`, `pytest.ini` | Trace-shape tests, plus metrics and logs tests, against a fake Microsoft Learn server and a fake OpenAI client: no network, no key, no tokens |
| `Makefile` | Shortcuts: `make venv`, `make check`, `make lock`, `make up-jaeger`, `make up-aspire`; `make` lists them all |
| `.github/workflows/ci.yml` | GitHub Actions: pylint and the tests, then an image build and a container smoke test, on every push and pull request |
| `Dockerfile` | `python:3.14.7-slim`, non-root, installs the lock and runs `pip check`, health check on `/healthz` |
| `compose.yaml` | Console mode; the container is hard-capped at **3 GB** RAM |
| `compose.jaeger.yaml` | Override that adds a Jaeger UI and ships spans to it |
| `compose.aspire.yaml` | Alternative override: the .NET Aspire Dashboard instead of Jaeger, with metrics and logs switched on |
| `.env.example` | Copy to `.env`; holds `OPENAI_API_KEY` and optional knobs |
| `design.md` | Design notes plus a verification checklist for an AI assistant with internet access (this project was written offline) |
| `CHANGELOG.md` | Notable changes, version by version |
| `LICENSE` | MIT License |
| `images/foundry-learn-agent-architecture-dark.svg` | The architecture diagram above (also in `design.md` §2) |
| `.dockerignore`, `.gitignore` | Keep `.env` out of the image and the repo |
| `.vscode/settings.json` | Points Pylint and the Python extension at `.venv`, and turns on the **Testing** view (see *Run it locally instead*) |
| `.pylintrc` | Pylint settings: 120-column lines, the width the code is written to, and the import roots the tests need |

## Prerequisites

- An OpenAI API key.
- Outbound internet access to `learn.microsoft.com` and `api.openai.com`.
- **Docker route:** Docker Engine + Compose v2 on Linux (`docker compose version`).
- **Local route:** Python 3.14.7 or newer, the version the container runs.

## Run it with Docker Compose (Linux)

```bash
cp .env.example .env            # put your OPENAI_API_KEY in .env
docker compose up --build       # builds the image from requirements/lock.txt, starts the API on :8000
```

Open <http://localhost:8000/docs>. In a second terminal, watch the spans and the heartbeat:

```bash
docker compose logs -f api
```

Useful commands:

| Command | What it does |
|---|---|
| `docker compose up --build -d` | run in the background |
| `docker compose logs -f api` | follow spans (JSON) and `[healthz]` lines |
| `docker compose logs api \| grep '\[healthz\]'` | just the heartbeat |
| `docker stats foundry-learn-agent` | live memory use vs the 3 GB limit |
| `docker inspect --format '{{json .State.Health}}' foundry-learn-agent` | Docker's own view of `/healthz` |
| `docker compose down` | stop and remove the container |

**About the 3 GB cap.** `compose.yaml` sets `deploy.resources.limits.memory: 3g`. If the process ever exceeds it,
the kernel kills it and Compose restarts it (`restart: unless-stopped`). The heartbeat's `rss_mb` field lets you
watch actual usage — this app idles far below 200 MB, so the cap is a safety net, not a constraint. Swap is not
counted; uncomment `memswap_limit` in `compose.yaml` if you want memory + swap capped at 3 GB together.

**Who can connect.** Docker publishes the ports on IPv4 `127.0.0.1` only (never on `0.0.0.0`, never on IPv6), so
by default only this host can call the API, and `/ask` spends your OpenAI tokens. To use it from the other machines
on your LAN, set `LAN_IP` in `.env` to this host's IPv4 LAN address (`hostname -I` lists it) and run
`docker compose up -d` again. The API (8000) and the trace UI (Jaeger on 16686, or Aspire on 18888) are then
published on that address too, and the `trace_url` links point at it. OTLP (4318, 18890) stays local. Keep `LAN_IP`
a private address and don't forward these ports on your router. Without `LAN_IP`, reach the API from another
machine through an SSH tunnel (the VS Code **Ports** view, or `ssh -L 8000:localhost:8000 -L 16686:localhost:16686 <host>`).

### With a Jaeger UI (recommended once the console makes sense)

```bash
docker compose -f compose.yaml -f compose.jaeger.yaml up --build
```

Open <http://localhost:16686>, choose the service `foundry-learn-agent`, and either browse traces or paste a
`trace_id` from an API response into the search box. Quicker still: every traced response carries a `trace_url`
that opens that exact trace. The waterfall makes parent/child nesting and where the time goes (LLM turns dominate)
obvious in a way the console never will. The override file does three things, and `app.py` is untouched:
- adds the Jaeger container, with its own 2 GB cap;
- sets `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318` on the API;
- sets `TRACE_UI_URL`, the template for those links. It uses `LAN_IP` when that's set, otherwise `localhost`.

### With the Aspire Dashboard instead

```bash
docker compose -f compose.yaml -f compose.aspire.yaml up --build --remove-orphans
```

All three signals in one UI: the standalone .NET Aspire Dashboard at <http://localhost:18888> is an OTLP receiver for
any language, and this override also switches the app's metrics and logs on (Jaeger stores traces only). Metrics are
exported every 10 s, so the charts move while you watch.
- **Traces:** the same spans as in Jaeger. `trace_url` links open Aspire's trace-detail page.
- **Metrics:** our own instruments, listed under *Metrics in this app* below. The FastAPI and httpx
  instrumentations add request durations and in-flight requests for free. Histograms show *exemplars*: individual
  measurements that link to the trace they were recorded in.
- **Structured logs:** one record per agent run, tool call and upstream failure. Each record is linked to its
  trace, and its attributes are searchable (`mcp.tool.name`, `agent.mode`, `error.type`).

Use it *instead of* Jaeger, not with it: `--remove-orphans` removes the other viewer's container, and its in-memory
data goes with it. The dashboard runs without a login token, which is fine for local learning; its ports follow the
same IPv4-only rules as Jaeger's.

#### Metrics in this app

| Metric | Type | What it measures | Attributes |
|---|---|---|---|
| `gen_ai.client.token.usage` | histogram, `{token}` | tokens per LLM call (GenAI semantic convention) | `gen_ai.token.type` (input/output), `gen_ai.request.model`, `agent.mode` |
| `gen_ai.client.operation.duration` | histogram, `s` | each LLM call, one per `llm.turn` span; failures too | `gen_ai.request.model`, `agent.mode`, `error.type` on failure |
| `agent.tool.calls` | counter | Learn tool calls, made here (`/ask`) or by OpenAI (`/ask-hosted`) | `mcp.tool.name`, `agent.mode`, `error.type` on failure |
| `agent.tool.duration` | histogram, `s` | tool calls made from this process (`/ask` only) | `mcp.tool.name`, `agent.mode`, `error.type` |
| `process.memory.usage` | observable, `By` | resident memory, read at each collection | — |
| `http.server.*`, `http.client.*` | from the instrumentations | request duration, in-flight requests, sizes; not recorded for the excluded `/healthz` | HTTP semantic conventions |

## Run it locally instead (venv)

```bash
make venv                       # .venv with the image's Python (via uv), the pinned packages and the test tools
source .venv/bin/activate
export OPENAI_API_KEY=sk-...
uvicorn app:app --reload
```

Without `make` or `uv`, the equivalent is `python3 -m venv .venv` followed by
`.venv/bin/pip install -r requirements/lock.txt -r requirements/dev.txt`.

Open <http://127.0.0.1:8000/docs>. uvicorn listens on `127.0.0.1` only unless you pass `--host`; to allow your LAN,
pass this machine's LAN address, never `--host 0.0.0.0`. To use Jaeger from a local run, start only Jaeger from the override file
(`docker compose -f compose.yaml -f compose.jaeger.yaml up jaeger`) and `export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`
before starting uvicorn.

**Editing in VS Code?** Create the `.venv` above even if you only run the app in Docker: `.vscode/settings.json`
points Pylint and the Python extension at `.venv/bin/python`. Without it they check `app.py` against a Python that
lacks these packages and flag every third-party import as unresolved.

## Run the tests

```bash
make check                      # pylint app.py tests (10.00/10), then pytest: 20 tests in about a second
```

The trace is this project's deliverable, so the tests assert the trace itself: which spans each endpoint produces,
how they nest and what they carry. They also check the promises around it:
- every traced response carries its `trace_id` in the body and the `X-Trace-Id` header;
- 502 bodies carry the trace id too;
- `/healthz` creates no span at all.

The other two signals are tested as well:
- the metrics each call records, with their values and attributes;
- the free HTTP metrics, which skip `/healthz`;
- log records carrying their request's trace id, and a failure's exception details.

They need no network, no API key and no tokens:
- A fake Microsoft Learn MCP server sits under the app's real httpx client, so the real instrumentation makes real
  CLIENT spans and injects `traceparent`.
- A scripted fake OpenAI client drives both agent loops.
- In-memory exporters collect the spans, metrics and log records.

VS Code's **Testing** view runs the same tests. On every push and pull request, the GitHub Actions workflow in
`.github/workflows/ci.yml` runs pylint and the tests, then builds the image and proves in a real container that
`/ping` is traced and `/healthz` is not.

## Dependencies: floors and a lock

All three dependency files live in `requirements/`. `base.txt` holds *floors*: the oldest release of each package
known to work. That's the intent, and it's the file you edit. The image installs `lock.txt` instead: the exact
versions `scripts/lock.sh` resolved from those floors inside the Dockerfile's own base image. So a rebuild installs
the same packages tomorrow as today, and newer releases arrive only when you ask for them. `dev.txt` adds the test
and lint tools for `.venv` and CI.

| Command | When |
|---|---|
| `make lock-upgrade` | you want the newest releases the floors allow. Review `git diff requirements/lock.txt`, run `make check`, rebuild |
| `make lock` | after editing `requirements/base.txt`; versions stay put where the floors still allow them |

Before keeping a new lock, the script installs it in a throwaway environment and checks the facts the traces depend
on. For example, `HTTPX2ClientInstrumentor` must still exist, because openai 3.x moved to httpx2, and without that
instrumentor every OpenAI span silently disappeared (CHANGELOG 0.2.2). The Dockerfile then runs `pip check`, so a
set that isn't in lockstep fails the build.

## Configuration

All optional; set in `.env` (Docker) or export in your shell (local).

| Variable | Default | Effect |
|---|---|---|
| `OPENAI_API_KEY` | **required** | the app refuses to start without it |
| `OPENAI_MODEL` | `gpt-5.6-luna` | any model with function calling, structured outputs and the hosted MCP tool |
| `OTEL_SERVICE_NAME` | `foundry-learn-agent` | `service.name` stamped on every span |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset → console | set to ship spans over OTLP/HTTP (Jaeger, a Collector, Aspire Dashboard) |
| `HEALTHZ_INTERVAL_SECONDS` | `60` | how often the `[healthz]` line is printed; `0` disables it |
| `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` | `docs,openapi.json,redoc,healthz` | which URLs never become spans |
| `OTEL_BSP_SCHEDULE_DELAY` | `5000` | ms between span flushes; `1000` makes the console feel snappier |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | unset | `http` switches auto spans to the newer attribute names |
| `LEARN_MCP_URL` | `https://learn.microsoft.com/api/mcp` | another MCP server to experiment with (must be public for `/ask-hosted`) |
| `LAN_IP` | unset → `127.0.0.1` only | Docker Compose only: also publish the API and the trace UI on this IPv4 LAN address, and use it in `trace_url` links |
| `TRACE_UI_URL` | set by the Jaeger and Aspire overrides | the template for `trace_url`; the app replaces `{trace_id}`. Unset (console mode) means no link |
| `OTEL_TRACES_EXPORTER` | unset | `none` attaches no console or OTLP exporter; the tests use it and collect spans in memory instead |
| `OTEL_METRICS_EXPORTER` | unset → off | `otlp` (set by the Aspire override) or `console`; off otherwise, because Jaeger stores traces only |
| `OTEL_LOGS_EXPORTER` | unset → off | `otlp` (set by the Aspire override) or `console`. The `[log]` lines on stdout print either way |
| `OTEL_METRIC_EXPORT_INTERVAL` | `60000` | ms between metric exports; the Aspire override uses `10000` |

## Try it — in this order

Each response from a traced endpoint carries its `trace_id` in the body and in the `X-Trace-Id` header; the same
value appears as `context.trace_id` on every span of that request. With Jaeger or Aspire running, the body also has
a `trace_url` that opens that exact trace.

**1. `GET /healthz` — the control group.** Returns the status snapshot and creates **no span**: it is in the
excluded-URL list, because liveness probes run forever and would drown a trace backend. Docker's HEALTHCHECK
hits it every 60 s; watch `requests["/healthz"]` climb in the heartbeat while nothing OTEL-shaped appears. It has
no `X-Trace-Id` header either: there is no span to report.

**2. `GET /ping` — the cheapest span.** One request, one trace: the SERVER span `GET /ping` plus two small ASGI
children (`http receive`, `http send`). `curl -si localhost:8000/ping` shows the id twice, in the `X-Trace-Id`
header and the body; compare it with the span JSON in the log, or click the `trace_url`.

**3. `GET /tools` — outbound tracing for free.** Talks to Microsoft Learn only: **no OpenAI call, no tokens**.
It returns the server's three tools with their JSON schemas: `microsoft_docs_search` (`query`),
`microsoft_code_sample_search` (`query`, optional `language`) and `microsoft_docs_fetch` (`url`).
You get the manual spans `mcp initialize`, `mcp notifications/initialized`, `mcp tools/list`, each with an
automatic `POST` CLIENT child pointing at `learn.microsoft.com`. Hammer this one while you learn tracing.

**4. `POST /ask` — the full agent, tools local.**

```bash
curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"topic": "Microsoft Foundry", "paragraphs": 2, "links": 3}' | python3 -m json.tool
```

The response has `mode: "local-tools"`, the brief (`paragraphs`, `links`), how many LLM `turns` it took, token
`usage`, every `tool_calls` entry with its measured `duration_ms`, the `trace_id` and, with a trace UI running, the
`trace_url`. Expect 10–40 seconds. If an upstream call fails you get a 502 instead. Its body carries the same
`trace_id` and `trace_url` under `detail`, so a failed request is as easy to find as a good one:
`{"detail": {"error": "...", "trace_id": "...", "trace_url": "..."}}`.

**5. `POST /ask-hosted` — same question, OpenAI runs the tools.** Same body, same answer shape, but
`mode: "hosted-mcp"`, `turns: 1`, and every `tool_calls[].duration_ms` is `null` — you did not make those
calls, OpenAI did. In Jaeger the trace collapses to `agent.run → llm.turn → one POST api.openai.com`.

**6. Watch the heartbeat.** Every 60 s the log shows one line like

```
[healthz] {"status": "ok", "service": "foundry-learn-agent", "version": "0.4.0", "time": "...", "uptime_s": 420,
           "exporter": {"traces": "console", "metrics": "none", "logs": "none"}, "model": "gpt-5.6-luna",
           "rss_mb": 96.4, "requests": {"/healthz": 7, "/ping": 1, "/tools": 2, "/ask": 1, "/ask-hosted": 1},
           "errors": {}, "llm_turns": 4, "tool_calls": 5, "tokens": {"input_tokens": 18342, "output_tokens": 1210}}
```

`GET /healthz` returns exactly this object. It is plain `print()` fed by plain counters — no OTEL anywhere in
that path — which is the point: it shows what a process can tell you about itself *without* tracing, so the
value of the spans stands out by contrast. `exporter` shows where each of the three signals goes.

**7. Metrics and logs, in Aspire.** Start the Aspire override (see *With the Aspire Dashboard instead*) and repeat
steps 3–5. Then open **Metrics** → `foundry-learn-agent` → `gen_ai.client.token.usage`: the P50/P90/P99 tokens per
call, split by `gen_ai.token.type` and `agent.mode`, with exemplars that open the call's trace. Next, open
**Structured logs**: one line per agent start, tool call and finish, each linked to its trace. Filter on
`mcp.tool.name`, or search for "failed". The `[log]` lines in `docker compose logs api` are the same records in plain
text, each ending in its `trace_id`.

## Reading the console output

- A span is printed **when it ends**, so children appear *before* their parents; the SERVER span is last.
- Key fields: `name`, `kind` (`SERVER`, `CLIENT`, `INTERNAL`), `context.trace_id` (identical across the whole
  request), `context.span_id`, `parent_id` (how the tree is built), `attributes`, `status`,
  `resource.attributes.service.name`.
- The `BatchSpanProcessor` flushes roughly every 5 s — if nothing prints, wait a moment (or set
  `OTEL_BSP_SCHEDULE_DELAY=1000`).
- Auto-span attributes follow the OTEL HTTP semantic conventions: either `http.method` / `http.url` /
  `http.status_code` or the newer `http.request.method` / `url.full` / `http.response.status_code`.
- Our own attributes: `agent.mode`, `agent.turn`, `agent.turns`, `agent.tools`, `agent.tool_calls`,
  `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` (per `llm.turn` and totalled
  on `agent.run`), `mcp.tool.name`, `mcp.tool.arguments`, `rpc.method`.
- A failed tool call shows up as an `exception` event on `agent.run` (local) or an `mcp_call.error` event
  (hosted); a request that fails outright marks its spans `status: ERROR`.
- Our own log records print as `[log] LEVEL message trace_id=<32 hex>`, for example
  `[log] INFO tool microsoft_docs_search returned 22132 chars in 1874 ms trace_id=710f…`. Search a trace viewer for
  that id to see the same moment as a span. Lines logged outside a request, such as `service started`, end in
  `trace_id=-`.

## How the pieces fit

| Package | Layer | Where it shows up in `app.py` |
|---|---|---|
| `opentelemetry-api` | Interface | `trace.get_tracer(...)`, `start_as_current_span`, `set_attribute`, `add_event`, `record_exception`; `metrics.get_meter(...)`, `create_histogram`, `create_counter`, `record`, `add` |
| `opentelemetry-sdk` | Implementation | `TracerProvider`, `MeterProvider` + `PeriodicExportingMetricReader`, `LoggerProvider` + `BatchLogRecordProcessor`, `Resource`, the console exporters — in `configure_traces()`, `configure_metrics()`, `configure_logs()` |
| `opentelemetry-exporter-otlp-proto-http` | Exporter | `OTLPSpanExporter`, `OTLPMetricExporter`, `OTLPLogExporter`: one endpoint (`OTEL_EXPORTER_OTLP_ENDPOINT`), a path per signal (`/v1/traces`, `/v1/metrics`, `/v1/logs`) |
| `opentelemetry-instrumentation-fastapi` | Inbound auto-instrumentation | `FastAPIInstrumentor.instrument_app(app, excluded_urls=...)` — root SERVER span per request, plus `http.server.*` metrics |
| `opentelemetry-instrumentation-httpx` | Outbound auto-instrumentation | `HTTPXClientInstrumentor().instrument()` for our httpx client and `HTTPX2ClientInstrumentor().instrument()` for the OpenAI SDK, which is built on httpx2 — every request becomes a CLIENT span carrying a `traceparent` header, plus `http.client.*` metrics |
| `opentelemetry-instrumentation-logging` | Logs bridge | its `LoggingHandler`, attached to our `foundry_learn_agent` logger: each `logging` record becomes an OTel log record stamped with the current trace and span ids. The SDK's own `LoggingHandler` is deprecated |
| `azure-monitor-opentelemetry-exporter` | Exporter (Step 4, commented) | `AzureMonitorTraceExporter` added as a *second* processor — same spans, second destination |

Three rules of thumb the code demonstrates:

1. **Configure the SDK before creating clients.** `configure_opentelemetry()` runs at import time; the
   `httpx.AsyncClient` and `AsyncOpenAI` clients are created later, inside the FastAPI lifespan.
2. **Manual spans tell the story, auto spans give the plumbing.** All Learn MCP calls hit the *same URL*, so the
   automatic `POST` spans are indistinguishable. The manual `mcp tools/call microsoft_docs_search` span wrapping
   each one is what makes the trace readable.
3. **Exclude what you will never look at.** `/healthz` and the Swagger UI are excluded up front; a backend full
   of probe spans costs money and hides the traces you care about.

## Step 4 — Azure Monitor / Application Insights (built in, commented out)

In `configure_opentelemetry()` there is a clearly marked block that adds a second exporter. Nothing else in the
file changes: the provider fans out every span to the console (or OTLP) **and** to Application Insights.

To enable it:

1. `pip install azure-monitor-opentelemetry-exporter` for a local run. For Docker, uncomment its line in
   `requirements/base.txt`, run `make lock` (the image installs the lock, not the floors), and rebuild.
2. Put `APPLICATIONINSIGHTS_CONNECTION_STRING=...` in `.env` (Application Insights resource → Overview →
   Connection String). The string includes the ingestion endpoint, so an Azure Government resource works with
   no code change.
3. Uncomment the block and restart. The heartbeat's `exporter.traces` becomes `console+azure-monitor` (or
   `otlp+azure-monitor`).

What you will see in the portal: SERVER spans as **requests**, CLIENT spans as **dependencies** (HTTP) drawn as
edges on the **Application Map**, the manual `agent.run` / `llm.turn` / `mcp …` spans as in-process
dependencies in the **end-to-end transaction** view, span attributes under `customDimensions` (queryable with
KQL — e.g. `dependencies | where customDimensions["agent.mode"] == "hosted-mcp"`), and recorded exceptions in
the **exceptions** table.

## The prompts

Both live at the top of section 3 in `app.py`.

- **System prompt** — makes the agent *use the tools* (Microsoft products get renamed, so the model's memory is
  treated as untrustworthy), forbids invented URLs, caps page fetches, and fixes the answer format.
- **User prompt** — a template filled from the request body (`topic`, `paragraphs`, `links`), so the same agent
  answers for any Microsoft topic.
- **Output contract** — the `Brief` Pydantic model is sent to OpenAI as a strict JSON schema, so the answer is
  guaranteed to parse into the API's response model. Same class, two jobs.
- Both `/ask` and `/ask-hosted` use the identical prompts and contract — only *who runs the tools* differs.

## Exercises

1. **Auto vs manual.** Comment out `HTTPXClientInstrumentor().instrument()`, restart, call `/tools`. Every
   `POST` CLIENT span disappears; the manual `mcp …` spans remain. (The OpenAI calls in `/ask` have their own
   switch, `HTTPX2ClientInstrumentor`: the SDK is built on httpx2, not httpx.)
2. **Where the root comes from.** Remove `FastAPIInstrumentor.instrument_app(...)`. `agent.run` becomes the root
   and `/ping` reports a `trace_id` of all zeros — there is no span at all.
3. **Exclusion is a regex list.** Add `ping` to `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` and watch `/ping` go silent
   too — then remove `healthz` from it and watch the probe spam begin.
4. **Add your own attribute.** In `ask()`, add
   `trace.get_current_span().set_attribute("app.topic", request.topic)` and find it on the SERVER span.
5. **Cost per model.** Run the same question with two values of `OPENAI_MODEL` and compare `gen_ai.usage.*` and
   the `llm.turn` durations.
6. **Count the hops.** Ask about something broader (for example "Microsoft Sentinel data lake") and match the
   `mcp tools/call` spans against `tool_calls` in the response.
7. **Break it on purpose.** Set `LEARN_MCP_URL=https://learn.microsoft.com/api/does-not-exist` and call `/tools`.
   The `502` carries `detail.trace_url`: open it and look at `status` and the `exception` event on the failing
   span, then at `errors["/tools"]` in the next heartbeat.
8. **Local vs hosted, side by side.** With Jaeger running, call `/ask` and `/ask-hosted` with the same body and
   open both traces. Count spans, compare total duration, and note that the hosted response *still tells you*
   which tools ran — just not how long each took.
9. **Quieter traces.** `FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])` drops the
   ASGI plumbing spans.
10. **Print vs telemetry.** The heartbeat counts `llm_turns` and `tool_calls`; the metrics count the same things
    properly — per model, per tool, as distributions — and the spans carry them per request. In Aspire, compare
    the heartbeat with `agent.tool.calls` and `gen_ai.client.token.usage`, then open an exemplar. Which questions
    does each one answer ("how many today?", "how slow is p90?", "why was *this one* slow?")? That's the three
    signals in miniature.
11. **Logs vs events.** A failed tool call leaves three traces of itself: an `exception` event on `agent.run`, a
    WARNING log record and `agent.tool.calls{error.type=…}`. Break a call (exercise 7), then find all three in
    Aspire and decide which one you'd alert on.

## Where to go next

- **More metrics backends.** Metrics already flow over OTLP. Send them to Prometheus (the OTel Collector's Prometheus
  exporter) and chart them in Grafana, or add Step 4's `AzureMonitorMetricExporter` next to the trace exporter.
- **More logs.** Attach the same `LoggingHandler` to `uvicorn.access` or `httpx` to ship their records too. Each is
  one line, and each adds noise.
- **Distributed tracing.** The `traceparent` header is already injected into every outbound call. Learn and
  OpenAI ignore it, but a second service of yours running OTEL would continue the trace under the same `trace_id`.
- **Azure Monitor.** Step 4 above.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| App refuses to start: "Set OPENAI_API_KEY" | `.env` missing or the key line is empty (Docker); key not exported in the same shell (local) |
| `docker compose` says `.env` not found | `cp .env.example .env` first |
| `502 OpenAI error … 401` | wrong or expired API key |
| `502 OpenAI error … 403 … does not have access to model` | the OpenAI project behind your key can't use that model. List the ones it can with `docker compose exec api python -c "from openai import OpenAI; print(sorted(m.id for m in OpenAI().models.list()))"`, set one as `OPENAI_MODEL` in `.env`, and run `docker compose up -d` |
| `502 OpenAI error … model` | set `OPENAI_MODEL` to a model your account can use |
| `502 Microsoft Learn MCP transport error` | no route/proxy to `learn.microsoft.com` from the container; try `docker compose exec api python -c "import urllib.request;print(urllib.request.urlopen('https://learn.microsoft.com').status)"` |
| `502 The model's answer did not match the expected schema` | the model returned non-JSON; try a different `OPENAI_MODEL` |
| Any 502: which step failed? | open `detail.trace_url`, or search Jaeger for `detail.trace_id`. The failing span is marked `ERROR` and carries the exception |
| A `trace_url` link doesn't open from another machine | it says `localhost` because `LAN_IP` isn't set: set `LAN_IP` (or `TRACE_UI_URL`) in `.env` and run `docker compose … up -d` again |
| Build log: `WARNING: requirements/lock.txt not found` | a clone without the lock builds from the floors, with whatever is newest today. Run `make lock` and commit the lock |
| `make test` or `make lint`: `No module named pytest` / `pylint` | run `make venv` first; the targets use `.venv` |
| Nothing prints to the console | wait ~5 s for the batch flush; Swagger and `/healthz` traffic is excluded on purpose |
| Spans print but no `[healthz]` lines | `HEALTHZ_INTERVAL_SECONDS=0`, or you are not looking at the `api` service log |
| Jaeger shows no service | you started with `compose.yaml` only — add `-f compose.jaeger.yaml`; or check `docker compose logs api` for exporter connection errors |
| Aspire's **Metrics** or **Structured logs** is empty | metrics and logs are only on with `compose.aspire.yaml`: check `exporter` in `/healthz`. Metrics arrive every 10 s, so wait a moment |
| Exporter errors (404s) for `/v1/metrics` or `/v1/logs` in Jaeger mode | `OTEL_METRICS_EXPORTER` or `OTEL_LOGS_EXPORTER` is set in `.env`: Jaeger stores traces only, so leave them unset there |
| `WARNING: Your kernel does not support memory limit capabilities` | the host kernel has the memory cgroup disabled; the app runs but the 3 GB cap is not enforced |
| `pip` dependency conflict | the OTEL packages must stay in lockstep (core `1.N` ↔ contrib `0.(N+21)b0`); do not pin one without the others. `make lock` resolves a consistent set |
| Port 8000 already in use | change the host port: the middle number in both `…:8000:8000` lines of `compose.yaml` |
| Another machine gets `ERR_CONNECTION_REFUSED` on port 8000 although `docker compose ps` says healthy | the ports are published on `127.0.0.1` only: set `LAN_IP` in `.env` and run `docker compose up -d` again, or tunnel over SSH (VS Code **Ports** view, or `ssh -L 8000:localhost:8000 <host>`). `localhost` in a browser on another machine means that machine |
| `docker compose up` fails with `cannot assign requested address` | `LAN_IP` is no longer an address of this host (did DHCP hand out a new one?): update `LAN_IP`, or reserve the address on your router |
