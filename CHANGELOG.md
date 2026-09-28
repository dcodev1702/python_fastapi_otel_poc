# Changelog

All notable changes to Foundry Learn Agent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).
The version lives in `app.py` (`SERVICE_VERSION`), the `compose.yaml` image tag and `design.md`, and is bumped
only when the application's behaviour changes (`design.md` §0).

## [Unreleased]

Documentation and repository setup; the application's behaviour is unchanged, so the version stays 0.2.0.

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
- A README troubleshooting entry for `ERR_CONNECTION_REFUSED` on `localhost:8000` when the browser runs on a
  different machine from Docker (for example VS Code Remote-SSH).
- This changelog.

### Changed

- `design.md` §2: the diagram replaces the ASCII architecture drawing.
- The file tables in `design.md` §3 and the README list the new files.

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

[Unreleased]: https://github.com/dcodev1702/python_fastapi_otel_poc/commits/main
