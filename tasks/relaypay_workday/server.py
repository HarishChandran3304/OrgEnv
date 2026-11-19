"""Minimal HTTP boundary: only Alex's public tools; never operator evidence.

Requests that originate inside the runtime container (e.g. from sandboxed
application code) are refused, so submitted code cannot act as Alex.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import socket

from .chat import backend_from_environment
from .task import EpisodeV3
from .verifier import seal

MAX_REQUEST_BYTES = 200_000


def _local_addresses():
    """Every address that belongs to this container (requests from them are refused)."""
    local = set()
    try:
        local.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    try:  # all IPv4 addresses assigned to local interfaces
        lines = Path("/proc/net/fib_trie").read_text().splitlines()
        for i, line in enumerate(lines):
            if "/32 host LOCAL" in line and i > 0:
                local.add(lines[i - 1].split()[-1])
    except OSError:
        pass
    return local


def _is_local(address, local):
    return address.startswith("127.") or address in ("::1", "localhost") or address in local


def serve(state_dir=Path("/state"), port=8080):
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    key = secrets.token_bytes(32)
    key_path = state_dir / "verifier-key"
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    episode = EpisodeV3(int(os.environ.get("ORGENV_SEED", "11")), variant=os.environ.get("ORGENV_VARIANT", "shift"),
                        chat_backend=backend_from_environment())
    local = _local_addresses()

    def persist():
        tmp = state_dir / "evidence.tmp"
        tmp.write_text(json.dumps(seal(episode.evidence(), key), sort_keys=True, allow_nan=False))
        tmp.chmod(0o600)
        tmp.replace(state_dir / "evidence.json")
    persist()

    class API(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass

        def reply(self, status, data):
            payload = json.dumps(data, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/catalog":
                self.reply(200, episode.catalog())
            elif self.path == "/observe" and not _is_local(self.client_address[0], local):
                self.reply(200, episode.observe())
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/tool" or _is_local(self.client_address[0], local):
                self.reply(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise ValueError("invalid request size")
                request = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
                if not isinstance(request, dict) or set(request) != {"tool", "args"} or not isinstance(request["args"], dict):
                    raise ValueError("request requires only tool and args; caller is always Alex")
                result = episode.invoke(request["tool"], request["args"])
                persist()
                self.reply(200, result)
            except (ValueError, TypeError):
                self.reply(400, {"error": "invalid request"})

    HTTPServer(("0.0.0.0", port), API).serve_forever()


if __name__ == "__main__":
    serve()
