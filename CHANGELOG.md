# Changelog

All notable changes to Foundry Learn Agent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).
The version lives in `app.py` (`SERVICE_VERSION`), the `compose.yaml` image tag and `design.md`, and is bumped
only when the application's behaviour changes (`design.md` §0).

## [Unreleased]

### Changed

- The docs and the diagram list the Microsoft Learn MCP server's third tool, `microsoft_code_sample_search`
  (`query`, optional `language`). `GET /tools` now returns it alongside `microsoft_docs_search` and
  `microsoft_docs_fetch` (see the README's `/tools` step, `design.md` L2 and acceptance test 3). The agent already
  offers the model every listed tool, so its code and prompts are unchanged.

## [0.2.3] - 2026-09-28

### Changed

- The image is built on `python:3.14.7-slim` instead of `python:3.12-slim`, so the container runs Python 3.14.7.
  Every dependency installs from a prebuilt wheel, pip resolves the same package versions as before, and the
  image is 12 MB smaller. Local runs still need Python 3.11 or newer.

## [0.2.2] - 2026-09-28

### Fixed

- Calls to OpenAI are traced again. openai 3.x is built on `httpx2`, a separate package that
  `HTTPXClientInstrumentor` does not patch, so every `llm.turn` span was missing its `POST api.openai.com` CLIENT
  child and no `traceparent` went to OpenAI. `configure_opentelemetry()` now also calls
  `HTTPX2ClientInstrumentor().instrument()`, from the same `opentelemetry-instrumentation-httpx` package, so the
  requirements are unchanged.

## [0.2.1] - 2026-09-28

Fixes the default model and stops publishing ports on every interface. Also adds the documentation and repository
setup done since 0.2.0.

### Added

- `images/foundry-learn-agent-architecture-dark.svg`: a dark-theme architecture diagram. It shows where each
  component runs:
  - the API client on the Linux host;
  - the `foundry-learn-agent` container: FastAPI, the local-tools and hosted-MCP agents, health and heartbeat,
    and the OpenTelemetry SDK;
  - the container log and Jaeger;
  - the Microsoft Learn MCP server, the OpenAI Responses API and Application Insights.

  It has nine numbered flows with a key and a legend. Dashed lines mark the opt-in exports and the
  OpenAI → Learn calls that never appear in your trace.
- An **Architecture** section in the README that embeds the diagram.
- `.env.example`, the template that the README and `design.md` tell you to copy to `.env`; it was missing. It
  holds an empty `OPENAI_API_KEY` and the optional settings, commented out with their defaults.
- README troubleshooting entries for `ERR_CONNECTION_REFUSED` from another machine and for a `LAN_IP` that is no
  longer this host's address.
- `LICENSE`: the MIT License, copyright 2026 DCODEV1702.
- `LAN_IP` in `.env`: also publishes the API and the Jaeger UI on that IPv4 LAN address.
- This changelog.

### Changed

- `design.md` §2: the diagram replaces the ASCII architecture drawing.
- The file tables in `design.md` §3 and the README list the new files.

### Fixed

- The default model `gpt-5.6-luna` isn't available to the maintainer's OpenAI project, so `/ask` and
  `/ask-hosted` failed with `502` (`403 model_not_found`). The default in `app.py` and `.env.example` is now
  `gpt-5.6-sol`, and both endpoints return 200 with it. To use another model, set `OPENAI_MODEL` in `.env`; the
  README's troubleshooting table shows how to list the models your key can use.

### Security

- Ports are published on IPv4 `127.0.0.1` only, plus `LAN_IP` when `.env` sets it; never on `0.0.0.0` or IPv6.
  They used to be published on every interface, IPv6 included, so anything that could reach the host could call
  `/ask` and spend your OpenAI tokens, read traces in the Jaeger UI or send spans to OTLP. OTLP/HTTP (4318) stays
  on `127.0.0.1` even when `LAN_IP` is set.

## [0.2.0] - 2026-09-28

Initial version, written offline; `design.md` §6 lists the external contracts that still need verifying.

### Added

- `app.py`: a FastAPI service with `GET /ping`, `GET /healthz` (not traced), `GET /tools`, `POST /ask` (a local
  tool loop over the Microsoft Learn MCP server) and `POST /ask-hosted` (the OpenAI hosted MCP tool).
- OpenTelemetry tracing:
  - FastAPI and httpx auto-instrumentation, plus manual `agent.run`, `llm.turn` and `mcp …` spans;
  - the console exporter by default, and OTLP/HTTP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set;
  - an Azure Monitor exporter (Step 4), commented out.
- A `[healthz]` heartbeat and `stats` counters that create no spans.
- Container setup:
  - `Dockerfile`: `python:3.12-slim`, non-root user, `HEALTHCHECK`;
  - `compose.yaml` with a 3 GB memory cap;
  - the `compose.jaeger.yaml` override that adds Jaeger.
- `README.md`, a walkthrough, and `design.md`, the design notes and verification checklist.

[Unreleased]: https://github.com/dcodev1702/python_fastapi_otel_poc/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/dcodev1702/python_fastapi_otel_poc/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/dcodev1702/python_fastapi_otel_poc/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/dcodev1702/python_fastapi_otel_poc/releases/tag/v0.2.1
