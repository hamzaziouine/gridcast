#!/usr/bin/env bash
# Fresh-clone check (BUILD_PLAN C15: "a fresh clone follows the instructions to a working local
# run in < 15 min"). Follows README "Run it locally" from nothing: clone, install Python 3.11 and
# the locked dependencies into EMPTY uv caches (nothing reused from this machine), run the CLI and
# the whole test suite (integration tests on the embedded Postgres). Needs only git and uv.
# No secrets, no .env: tests never touch Neon, R2, DagsHub or GitHub.
#
#   scripts/fresh_clone_check.sh [repo-url-or-path] [ref]
#   FRESH_CLONE_LIMIT_S=900 (default) fails the check when the total is slower.
set -euo pipefail

SRC=${1:-https://github.com/hamzaziouine/gridcast.git}
REF=${2:-main}
LIMIT_S=${FRESH_CLONE_LIMIT_S:-900}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
export UV_CACHE_DIR="$WORK/uv-cache"
export UV_PYTHON_INSTALL_DIR="$WORK/uv-python"
unset DATABASE_URL GRIDCAST_TEST_DATABASE_URL || true

t0=$(date +%s)
lap() { echo "[$(( $(date +%s) - t0 ))s] $*"; }

git clone -q --depth 1 --branch "$REF" "$SRC" "$WORK/gridcast"
cd "$WORK/gridcast"
lap "cloned $(git rev-parse --short HEAD)"
uv python install 3.11 >/dev/null 2>&1
uv sync --locked -q
lap "uv sync --locked done"
uv run gridcast --help >/dev/null
uv run gridcast version
uv run pytest -q -p no:cacheprovider | tail -n 3
elapsed=$(( $(date +%s) - t0 ))
echo "fresh clone -> CLI + green test suite in ${elapsed}s (limit ${LIMIT_S}s)"
[ "$elapsed" -le "$LIMIT_S" ]
