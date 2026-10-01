#!/usr/bin/env bash
# Portable SCAD2STEP launcher for macOS and Linux.
#
# Everything lives inside this folder: a private copy of Python, the libraries, work
# files and your results. Nothing is installed on your system, and your own Python
# (Apple's, Homebrew's, conda's — any) is never used or touched.
# To remove SCAD2STEP completely, delete the folder.
#
# First run downloads ~20 MB (uv, the installer) + ~30 MB (Python) + ~400 MB (libraries).
set -u
APP="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$APP")"

# ---- which build of Python do we need?
OS="$(uname -s)"; M="$(uname -m)"
case "$OS" in
  Darwin) PLAT="apple-darwin"
          # Terminal running under Rosetta reports x86_64 on Apple Silicon — use native arm64
          [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ] && M="arm64" ;;
  Linux)  PLAT="unknown-linux-gnu" ;;
  *) echo "Unsupported system: $OS"; exit 1 ;;
esac
case "$M" in
  arm64|aarch64) ARCH="aarch64" ;;
  x86_64|amd64)  ARCH="x86_64" ;;
  *) echo "Unsupported processor: $M"; exit 1 ;;
esac
TARGET="$ARCH-$PLAT"
RT="$ROOT/runtime/$TARGET"          # per-CPU, so one folder works on Intel and Apple Silicon Macs
mkdir -p "$RT" "$ROOT/runtime/tmp" "$ROOT/Output"

# ---- insulate from whatever Python setup the system or user has
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV CONDA_PREFIX \
      CONDA_DEFAULT_ENV PIP_REQUIRE_VIRTUALENV PIP_TARGET PIP_PREFIX PIP_USER
export PYTHONNOUSERSITE=1
export UV_PYTHON_INSTALL_DIR="$RT/python" UV_PYTHON_BIN_DIR="$RT/bin" UV_CACHE_DIR="$RT/cache"
export UV_TOOL_DIR="$RT/tools" UV_TOOL_BIN_DIR="$RT/bin"
export UV_NO_CONFIG=1 UV_PYTHON_PREFERENCE=only-managed UV_NO_MODIFY_PATH=1
export TMPDIR="$ROOT/runtime/tmp" MPLCONFIGDIR="$ROOT/runtime/tmp/matplotlib"
export SCAD2STEP_WORK="$ROOT/runtime/work" SCAD2STEP_OUTPUT="$ROOT/Output"

say() { printf '%s\n' "$*"; }

find_py() {
  local p
  for p in "$RT"/python/cpython-3.12.*/bin/python3.12; do
    [ -x "$p" ] && "$p" -E -s -c "import sys" >/dev/null 2>&1 && { printf '%s\n' "$p"; return 0; }
  done
  return 1
}

get_uv() {
  [ -x "$RT/uv" ] && "$RT/uv" --version >/dev/null 2>&1 && return 0
  local t="$RT/uv-download"; rm -rf "$t"; mkdir -p "$t"
  say "Downloading uv (the Python installer, ~20 MB)…"
  if curl -fL --retry 3 --progress-bar -o "$t/uv.tar.gz" \
        "https://github.com/astral-sh/uv/releases/latest/download/uv-$TARGET.tar.gz" 2>/dev/null \
     && tar -xzf "$t/uv.tar.gz" -C "$t" 2>/dev/null; then
    :
  else
    say "GitHub didn't answer — getting uv from PyPI instead…"
    local tag url
    case "$TARGET" in
      aarch64-apple-darwin)      tag='macosx_[0-9_]*_arm64' ;;
      x86_64-apple-darwin)       tag='macosx_[0-9_]*_x86_64' ;;
      x86_64-unknown-linux-gnu)  tag='manylinux[0-9a-z_.]*x86_64' ;;
      aarch64-unknown-linux-gnu) tag='manylinux[0-9a-z_.]*aarch64' ;;
    esac
    url="$(curl -fsSL --retry 3 https://pypi.org/pypi/uv/json \
           | grep -o "https://files.pythonhosted.org/[^\"]*-py3-none-${tag}[^\"]*\.whl" | head -n 1)"
    if [ -z "$url" ] || ! curl -fL --retry 3 --progress-bar -o "$t/uv.whl" "$url" \
       || ! (cd "$t" && unzip -q -o uv.whl '*.data/scripts/uv'); then
      say "Couldn't download uv. Check your internet connection and run this again."
      rm -rf "$t"; return 1
    fi
  fi
  local bin; bin="$(find "$t" -type f -name uv | head -n 1)"
  [ -n "$bin" ] || { say "The uv download was incomplete — run this again."; rm -rf "$t"; return 1; }
  mv "$bin" "$RT/uv" && chmod +x "$RT/uv"; rm -rf "$t"
  "$RT/uv" --version >/dev/null 2>&1
}

PY="$(find_py)"
if [ -z "$PY" ]; then
  say "First run: setting up a private Python inside $ROOT"
  get_uv || exit 1
  say "Downloading Python 3.12 (~30 MB)…"
  if ! "$RT/uv" python install 3.12 --no-bin; then
    say "Couldn't download Python. Check your internet connection and run this again."
    exit 1
  fi
  PY="$(find_py)" || { say "Python was downloaded but won't start — delete the 'runtime' folder and try again."; exit 1; }
fi

if ! cmp -s "$APP/requirements.txt" "$RT/req.txt"; then
  get_uv || exit 1
  say "Installing libraries — about 400 MB the first time (1.5 GB on disk), please wait…"
  if ! "$RT/uv" pip install --python "$PY" --system --break-system-packages -r "$APP/requirements.txt"; then
    say "Installing the libraries failed. Check your internet connection and run this again."
    exit 1
  fi
  cp "$APP/requirements.txt" "$RT/req.txt"
  rm -rf "$UV_CACHE_DIR"          # downloads no longer needed
fi

exec "$PY" -E -s "$APP/scad2step.py" "$@"
