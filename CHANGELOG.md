# Changelog

All notable changes to Foundry Learn Agent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).
The version lives in `app.py` (`SERVICE_VERSION`), the `compose.yaml` image tag and `design.md`, and is bumped
only when the application's behaviour changes (`design.md` §0).

## [Unreleased]

### Changed

- The dependency files have their own directory, `requirements/`:
  - `requirements.txt` becomes `requirements/base.txt`, the floors you edit;
  - `requirements.lock.txt` becomes `requirements/lock.txt`, the pinned set the image installs;
  - `requirements-dev.txt` becomes `requirements/dev.txt`, the test and lint tools.

  The Dockerfile copies the directory; `scripts/lock.sh`, the Makefile, CI, `.dockerignore` and the docs use the
  new paths. The lock was regenerated in place, and its 47 pins are unchanged.
- Step 4 (Azure Monitor): for Docker, uncomment the exporter in `requirements/base.txt` and run `make lock`
  before rebuilding. Since 0.3.0 the image installs the lock, so uncommenting the floor alone no longer reached
  the image. The README, `app.py`'s Step 4 comment and `requirements/base.txt` now say so. Also removed a stale
  `.gitignore` comment about generating the lock with `pip freeze`.

### Fixed

- CI's container smoke test raced the 5 s heartbeat: it could read the startup `[healthz]` line, logged before any
  request, and fail. It happened on the first 0.3.0 run. The step now polls for a heartbeat that counted `/healthz`,
  and the `/ping` span check polls too.
- CI's "no `X-Trace-Id` on `/healthz`" and "no `/healthz` span" checks could never fail: GitHub runs steps with
  `bash -e`, which ignores a failing command negated with `!`. They are now explicit `if … exit 1` checks.

## [0.3.0] - 2026-09-29

Trace ids on every response and failure, a committed dependency lock, trace-shape tests with CI, and the Aspire
Dashboard as a second trace UI. These changes came from a repository review by Fable 5.1, and were then
integrated, corrected where running them showed a problem, and verified live. The minor version bump is because
the 502 body changes shape.

### Added

- Every traced response carries its trace id in an `X-Trace-Id` header as well as in the body. With a trace UI
  running, it also carries a `trace_url` that opens that exact trace. The template is `TRACE_UI_URL`; the Jaeger
  and Aspire overrides set it, using `LAN_IP` when set and `localhost` otherwise. `/healthz` gets neither: it has
  no span.
- 502 bodies carry `detail.trace_id` and `detail.trace_url`, so a failed request is as easy to find as a good one.
  The 502 shape (`ErrorResponse`) is documented in Swagger on `/tools`, `/ask` and `/ask-hosted`.
- `requirements.lock.txt`, the exact set the image installs. `scripts/lock.sh` (`make lock`, `make lock-upgrade`)
  resolves it from `requirements.txt` inside the Dockerfile's base image. It keeps the lock only if
  `HTTPX2ClientInstrumentor` imports and openai is 3 or later. The first lock pins exactly what the 0.2.4 image
  had installed.
- Trace-shape tests (`tests/`, `pytest.ini`, `requirements-dev.txt`): 10 tests that assert which spans each
  endpoint produces, how they nest and what they carry. They run against a fake Microsoft Learn MCP server under
  the real httpx instrumentation and a scripted fake OpenAI client, so they need no network, key or tokens.
- GitHub Actions CI (`.github/workflows/ci.yml`), on every push to `main` and every pull request:
  - pylint and the tests on Python 3.14.7;
  - compose validation for all three stacks;
  - an image build, and a container smoke test proving that `/ping` is traced and `/healthz` is not.
- `compose.aspire.yaml`, an alternative to `compose.jaeger.yaml`: the same spans in the .NET Aspire Dashboard,
  which shows traces, metrics and structured logs side by side. Its UI (18888) follows the IPv4-only rules;
  OTLP (18890) stays on `127.0.0.1`.
- `Makefile` shortcuts: `venv` (uv, the image's Python), `check`, `test`, `lint`, `lock`, `lock-upgrade`, `up`,
  `up-jaeger`, `up-aspire`, `down`, `logs`.
- The README gains "Run the tests" and "Dependencies: floors and a lock" sections. `design.md` gains decisions 11
  and 12, checklist items T8, C6 and C7, and acceptance steps for `make check` and Aspire mode.

### Changed

- **The 502 `detail` is an object** (`error`, `trace_id`, `trace_url`) instead of a string. If you scripted
  against it, read `detail.error`.
- The Dockerfile installs `requirements.lock.txt`, falling back to the floors with a warning, then runs `pip check`.
- `requirements.txt` sets `openai>=3`, matching the httpx2 transport the code and docs describe.
- `configure_opentelemetry()` honours the standard `OTEL_TRACES_EXPORTER=none`: no exporter attached, and
  `exporter: "none"` in the status snapshot. The tests use it.
- The `count_requests` middleware sits before `instrument_app`, so it runs inside the SERVER span and can stamp
  `X-Trace-Id`.
- `.pylintrc` adds `source-roots=tests,.`, so Pylint resolves the tests' imports; `app.py` and `tests/` both
  rate 10.00/10. `.vscode/settings.json` turns on the Testing view, and `.dockerignore` keeps the new
  development-only files out of the build context.
- Local runs are documented for Python 3.14.7 or newer, the version the container and the `.venv` use: the
  README's "Runs on" row and Prerequisites, and the `app.py` header.

### Fixed (in the reviewed proposal, before release)

- The test fixture's fake Learn client produced no CLIENT spans. `HTTPXClientInstrumentor().instrument()` patches
  the real transport class, not `httpx.MockTransport`, so the fixture now uses `instrument_client()` (2 of 10
  tests failed before this).
- `compose.aspire.yaml` used the older `DOTNET_DASHBOARD_UNSECURED_ALLOW_ANONYMOUS`. It now uses the documented
  `ASPIRE_DASHBOARD_UNSECURED_ALLOW_ANONYMOUS`, and relies on the image's documented OTLP/HTTP port.
- `trace_url` said `localhost`, which opens nothing from another machine on the LAN; it now follows `LAN_IP`.
- `scripts/lock.sh --local`, the Makefile and the `make venv` target called `python`, which Ubuntu doesn't ship.
  They now use `python3`, uv and `.venv`.
- CI used `actions/checkout@v4` and `actions/setup-python@v5`; it now uses v7 of both, pins Python 3.14.7 like
  the image, caches pip, and lints the tests too.

## [0.2.4] - 2026-09-28

### Added

- `.vscode/settings.json`, which points Pylint and the Python extension at `${workspaceFolder}/.venv/bin/python`.
  Once you create the README's `.venv`, VS Code resolves the project's imports and stops reporting them as
  unresolved.
- `.pylintrc` with `max-line-length=120`, the width the code is written to; Pylint's default of 100 flagged 118
  lines. `SYSTEM_PROMPT` keeps its one 122-character line under a scoped `line-too-long` disable, because
  re-wrapping it would change the text the model reads.

### Changed

- The default model is `gpt-5.6-luna` again, in `app.py`, `.env.example` and the docs. The maintainer's OpenAI
  project now has access to it, and `/ask` and `/ask-hosted` both return 200 with it.
- Jaeger's memory limit in `compose.jaeger.yaml` is 2 GB, up from 1 GB, so it can hold more traces in memory.
- The docs and the diagram list the Microsoft Learn MCP server's third tool, `microsoft_code_sample_search`
  (`query`, optional `language`). `GET /tools` now returns it alongside `microsoft_docs_search` and
  `microsoft_docs_fetch` (see the README's `/tools` step, `design.md` L2 and acceptance test 3). The agent already
  offers the model every listed tool, so its code and prompts are unchanged.
- `lifespan(app)` carries a targeted `# pylint: disable=redefined-outer-name`: it is FastAPI's documented signature,
  and the parameter is the module's own `app`.
- `app.py` now scores 10.00/10 in Pylint. It gains docstrings for the MCP client methods, the helper functions and
  five API models, which now describe those models in Swagger. Where a warning conflicts with a deliberate choice,
  a targeted, commented disable remains:
  - the OTLP exporter, imported only in OTLP mode;
  - `Link`, whose docstring would join the JSON schema sent to OpenAI;
  - `Stats`, `finish_run` and `run_agent`.

  The prompts and the structured-output schema sent to OpenAI are unchanged.

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

[Unreleased]: https://github.com/dcodev1702/python_fastapi_otel_poc/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/dcodev1702/python_fastapi_otel_poc/compare/v0.2.4...v0.3.0
[0.2.4]: https://github.com/dcodev1702/python_fastapi_otel_poc/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/dcodev1702/python_fastapi_otel_poc/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/dcodev1702/python_fastapi_otel_poc/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/dcodev1702/python_fastapi_otel_poc/releases/tag/v0.2.1
