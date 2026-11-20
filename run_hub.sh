#!/bin/sh
# Search Hub launcher (Linux/macOS, and for testing on the mini).
cd "$(dirname "$0")"
exec python3 hub.py "$@"
