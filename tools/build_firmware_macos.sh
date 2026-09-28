#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PLATFORMIO_CORE_DIR="${PLATFORMIO_CORE_DIR:-$PWD/.pio_core}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.tools/uv-cache}"
exec .venv/bin/pio run "$@"
