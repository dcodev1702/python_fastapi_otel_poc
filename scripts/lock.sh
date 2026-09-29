#!/usr/bin/env bash
# =============================================================================
# Foundry Learn Agent - resolve requirements.txt into requirements.lock.txt
# Author : dcodev1702 & M365 Copilot / Cowork
# Created: 2026-09-29
#
# Produces the fully pinned set the Dockerfile installs. By default it resolves INSIDE the same base image the
# Dockerfile uses (read from its FROM line), so the lock matches the container's Python and platform exactly.
#
#   scripts/lock.sh              first lock, or re-lock after editing requirements.txt (keeps versions if allowed)
#   scripts/lock.sh --upgrade    move every package to the newest release the floors allow, then `git diff` it
#   scripts/lock.sh --local      resolve with a `uv` already installed on this machine instead of in Docker
#
# After resolving, the script installs the lock into a throwaway venv and checks the two facts the code and docs
# rely on: `HTTPX2ClientInstrumentor` importable (OpenAI spans) and openai >= 3 (the httpx2 transport the README
# describes). A lock that fails either check is deleted, so a bad set can never reach the image.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

UPGRADE=""
LOCAL=0
for arg in "$@"; do
    case "$arg" in
        --upgrade) UPGRADE="--upgrade" ;;
        --local)   LOCAL=1 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

BASE_IMAGE=$(sed -n 's/^FROM[[:space:]]\+\([^[:space:]]\+\).*/\1/p' Dockerfile | head -n 1)
[ -n "$BASE_IMAGE" ] || { echo "could not read the FROM line of Dockerfile" >&2; exit 1; }

# The resolve-and-verify steps. One script for both paths (local uv, or uv inside the base image) so they can
# never drift apart. A failed verification removes the lock file before the script exits.
STEPS=$(cat <<EOS
set -euo pipefail
trap 'rm -f requirements.lock.txt; echo "lock FAILED - requirements.lock.txt removed" >&2' ERR
uv pip compile requirements.txt --output-file requirements.lock.txt ${UPGRADE}
python3 -m venv /tmp/lockcheck
uv pip install --quiet --python /tmp/lockcheck/bin/python -r requirements.lock.txt
/tmp/lockcheck/bin/python - <<'PY'
import importlib.metadata as metadata
from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor  # noqa: F401  (the OpenAI spans need it)

openai_version = metadata.version("openai")
assert int(openai_version.split(".")[0]) >= 3, f"openai {openai_version} resolved; the docs describe the 3.x/httpx2 transport"
otel = {p: metadata.version(p) for p in ("opentelemetry-sdk", "opentelemetry-instrumentation-httpx", "opentelemetry-instrumentation-fastapi")}
print("lock OK: openai " + openai_version + "; " + "; ".join(f"{k} {v}" for k, v in otel.items()))
PY
EOS
)

if [ "$LOCAL" = 1 ]; then
    command -v uv >/dev/null || { echo "uv not found - install it (https://docs.astral.sh/uv/) or drop --local" >&2; exit 1; }
    echo ">> resolving with local uv ($(uv --version)) against $(python3 --version 2>&1)"
    bash -c "$STEPS"
else
    command -v docker >/dev/null || { echo "docker not found - install it or use --local" >&2; exit 1; }
    echo ">> resolving inside $BASE_IMAGE (the Dockerfile's base image)"
    INNER="set -euo pipefail
pip install --quiet --disable-pip-version-check uv
$STEPS
chown \$HOST_UID:\$HOST_GID requirements.lock.txt"
    docker run --rm \
        -v "$PWD":/work -w /work \
        -e "HOST_UID=$(id -u)" -e "HOST_GID=$(id -g)" \
        "$BASE_IMAGE" bash -c "$INNER"
fi

echo ">> wrote requirements.lock.txt ($(grep -c '==' requirements.lock.txt) pinned packages). Review it, then commit it."
