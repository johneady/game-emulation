#!/usr/bin/env bash

# Local preview of the retro games launcher.
#
# Serves web/ with the data snapshot merged in (manifests, js-dos bundles,
# box art) on http://127.0.0.1:8090/ and adds the cross-origin isolation
# headers the emulators need for SharedArrayBuffer.

set -euo pipefail

PORT="${1:-8090}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVE=$(mktemp -d /tmp/dosgames-dev.XXXXXX)
trap 'rm -rf "$SERVE"' EXIT

cp "$SCRIPT_DIR/web/index.html" "$SERVE/"
cp -r "$SCRIPT_DIR/web/img" "$SCRIPT_DIR/web/ejs" "$SCRIPT_DIR/web/jsdos" "$SERVE/"
mkdir -p "$SERVE/games" "$SERVE/art"
cp "$SCRIPT_DIR"/data/manifests/*.json "$SERVE/" 2>/dev/null || true
cp "$SCRIPT_DIR"/data/games/*.jsdos "$SERVE/games/" 2>/dev/null || true
cp "$SCRIPT_DIR"/data/art/*.jpg "$SERVE/art/" 2>/dev/null || true

echo "Serving on http://127.0.0.1:${PORT}/  (Ctrl-C to stop)"

python3 - "$SERVE" "$PORT" <<'PY'
import os, sys
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

root, port = sys.argv[1], int(sys.argv[2])

class Isolated(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

os.chdir(root)
ThreadingHTTPServer(("127.0.0.1", port), Isolated).serve_forever()
PY
