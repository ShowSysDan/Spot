#!/usr/bin/env bash
# Build an offline "wheelhouse" of every Python dependency so Spot can be
# installed on a server with no internet access.
#
# Run this on a machine WITH internet that matches the target server's
# OS, CPU architecture and Python version (e.g. another Debian 12 amd64 box):
#
#   ./scripts/build-wheelhouse.sh            # writes ./wheelhouse/
#
# Copy the Spot directory (including wheelhouse/) to the server, then:
#
#   python3 -m venv .venv
#   .venv/bin/pip install --no-index --find-links wheelhouse -r requirements.txt
#
# Browser libraries need no extra step: they are vendored in app/static/vendor.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip download --dest wheelhouse -r requirements.txt pip setuptools wheel
echo "Wheelhouse ready: $(ls wheelhouse | wc -l) files in $(pwd)/wheelhouse"
