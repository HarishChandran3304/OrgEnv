"""The chat gateway: the one container that can reach a language model.

The runtime, the agent and submitted code have no route off the task's internal networks. When
colleagues' words come from a model (ORGENV_CHAT_BACKEND=gateway), the runtime sends each
prompt here over a network that only it and this gateway share, with a bearer token that only
the two of them hold. The gateway holds the provider key, forwards the prompt and returns the
text. It holds none of the task's source, keeps no state about the task and never sees the
verifier's key or the evidence.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import json
import os



class ChatUnavailable(Exception):
    pass


def anthropic_complete(model, *, api_key, base_url=None, effort="low"):
    """The same call as chat.anthropic_complete; repeated here so that this container holds no task source."""
    import anthropic
    base_url = base_url.rstrip("/").removesuffix("/v1") if base_url else None  # a relay address may end in /v1
    client = anthropic.Anthropic(api_key=api_key, base_url=base_url, timeout=60.0, max_retries=2)

    def complete(system, user, max_tokens):
        try:
            response = client.messages.create(model=model, max_tokens=max_tokens + 2000, system=system,
                                              output_config={"effort": effort},
                                              messages=[{"role": "user", "content": user}])
        except anthropic.APIError as exc:
            raise ChatUnavailable(type(exc).__name__) from None  # provider text can carry request data
        if response.stop_reason == "refusal":
            raise ChatUnavailable("refusal")
        return "".join(block.text for block in response.content if block.type == "text")
    return complete

MAX_REQUEST_BYTES = 400_000
MAX_CALLS = int(os.environ.get("ORGENV_CHAT_MAX_CALLS", "6000"))   # per run: a day needs a few hundred
MAX_TOKENS = 4000


def serve(port=8090):
    token = os.environ.get("ORGENV_CHAT_TOKEN", "")
    state = {"complete": None, "calls": 0}

    def complete(system, user, max_tokens):
        if state["complete"] is None:  # built on first use, so the gateway also starts when no model is configured
            state["complete"] = anthropic_complete(os.environ.get("ORGENV_CHAT_MODEL", "claude-haiku-5-5"),
                                                   api_key=os.environ["ORGENV_CHAT_API_KEY"],
                                                   base_url=os.environ.get("ORGENV_CHAT_BASE_URL") or None)
        return state["complete"](system, user, max_tokens)

    class API(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass

        def reply(self, status, data):
            payload = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.reply(200 if self.path == "/health" else 404, {"ok": self.path == "/health"})

        def do_POST(self):
            given = self.headers.get("Authorization", "")
            if self.path != "/complete" or not token or not hmac.compare_digest(given, "Bearer " + token):
                self.reply(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise ValueError("invalid request size")
                request = json.loads(self.rfile.read(length))
                if state["calls"] >= MAX_CALLS:
                    raise ChatUnavailable("call budget exhausted")
                state["calls"] += 1
                self.connection.settimeout(None)
                text = complete(str(request["system"]), str(request["user"]), min(int(request["max_tokens"]), MAX_TOKENS))
                self.reply(200, {"text": text})
            except (ChatUnavailable, KeyError):
                self.reply(503, {"error": "model unavailable"})  # never the provider's own error text
            except (ValueError, TypeError):
                self.reply(400, {"error": "invalid request"})

    HTTPServer(("0.0.0.0", port), API).serve_forever()


if __name__ == "__main__":
    serve()
