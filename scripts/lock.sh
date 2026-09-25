#!/usr/bin/env bash
# Regenerate the hash-locked requirement files the container image installs from.
#
# constraints.txt holds the versions (and is what `pip install -c` uses outside the
# image); these files add the hashes, for the same versions, so the image build can run
# `pip install --require-hashes`. What hashes add on top of `==` pins is completeness:
# pip then FAILS if any requirement, indirect ones included, is left unpinned.
#
#   scripts/lock.sh          rewrite requirements.lock and requirements-dev.lock
#   scripts/lock.sh --check  exit 1 if either file differs from what would be written
#
# Needs uv (pinned: the version below). The image targets Ubuntu 24.04's Python 3.12
# on x86_64 Linux, so the lock is resolved for exactly that.
set -euo pipefail

UV_VERSION="0.12.19"
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null || [[ "$(uv --version | awk '{print $2}')" != "$UV_VERSION" ]]; then
    echo "scripts/lock.sh needs uv $UV_VERSION on PATH (pip install uv==$UV_VERSION)" >&2
    exit 2
fi

compile() {
    local out="$1"
    shift
    uv pip compile pyproject.toml "$@" \
        --constraint constraints.txt \
        --generate-hashes \
        --python-version 3.12 \
        --python-platform x86_64-unknown-linux-gnu \
        --no-header --quiet --output-file "$out"
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
compile "$tmp/requirements.lock"
compile "$tmp/requirements-dev.lock" --group dev

if [[ "${1:-}" == "--check" ]]; then
    status=0
    for f in requirements.lock requirements-dev.lock; do
        if ! diff -u "$f" "$tmp/$f"; then
            echo "$f is out of date with pyproject.toml/constraints.txt: run scripts/lock.sh" >&2
            status=1
        fi
    done
    exit "$status"
fi
cp "$tmp/requirements.lock" "$tmp/requirements-dev.lock" .
