#!/usr/bin/env python3
"""
Box-art upload service for the retro games launcher.

Accepts an image for a game id, verifies it really is an image, re-encodes it
to a bounded JPEG and writes /opt/art/<id>.jpg. Nothing is stored as uploaded,
so a hostile file cannot be served back out.

LAN-only, no auth, by deployment choice.
"""

import io
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

ART_DIR = "/opt/art"
MAX_BYTES = 12 * 1024 * 1024        # reject anything larger before decoding
THUMB = (640, 480)                  # box art is display-only; cap the stored size
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
LISTEN = ("127.0.0.1", 8089)


def safe_id(raw: str) -> str | None:
    gid = raw.strip().lower()
    return gid if ID_RE.match(gid) else None


class Handler(BaseHTTPRequestHandler):
    server_version = "artsvc/1.0"

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        m = re.match(r"^/api/art/([^/]+)$", self.path)
        if not m:
            return self._json(404, {"error": "not found"})

        gid = safe_id(m.group(1))
        if not gid:
            return self._json(400, {"error": "bad game id"})

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json(400, {"error": "bad length"})

        if length <= 0:
            return self._json(400, {"error": "empty body"})
        if length > MAX_BYTES:
            return self._json(413, {"error": "image too large (max 12 MB)"})

        raw = self.rfile.read(length)

        # Pillow both validates and normalises: anything that is not a real
        # image raises here, and the re-encode strips any embedded payload.
        try:
            img = Image.open(io.BytesIO(raw))
            img.verify()
            img = Image.open(io.BytesIO(raw))
            img = img.convert("RGB")
            img.thumbnail(THUMB, Image.LANCZOS)
        except Exception:
            return self._json(415, {"error": "not a valid image"})

        os.makedirs(ART_DIR, exist_ok=True)
        tmp = os.path.join(ART_DIR, f".{gid}.tmp")
        dst = os.path.join(ART_DIR, f"{gid}.jpg")
        try:
            img.save(tmp, "JPEG", quality=82, optimize=True)
            os.replace(tmp, dst)
            os.chmod(dst, 0o644)
        except Exception as exc:
            if os.path.exists(tmp):
                os.unlink(tmp)
            return self._json(500, {"error": f"could not save: {exc}"})

        return self._json(200, {"ok": True, "id": gid, "url": f"/art/{gid}.jpg"})

    def do_DELETE(self) -> None:
        m = re.match(r"^/api/art/([^/]+)$", self.path)
        if not m:
            return self._json(404, {"error": "not found"})
        gid = safe_id(m.group(1))
        if not gid:
            return self._json(400, {"error": "bad game id"})
        dst = os.path.join(ART_DIR, f"{gid}.jpg")
        if os.path.exists(dst):
            os.unlink(dst)
        return self._json(200, {"ok": True, "id": gid})

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    os.makedirs(ART_DIR, exist_ok=True)
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
