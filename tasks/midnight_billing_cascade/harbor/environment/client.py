#!/usr/bin/env python3
"""Public client; no credentials, provider state or operator endpoints."""
import json
import sys
import urllib.request

BASE = "http://runtime:8080"


def request(path, data=None):
    payload = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("catalog", "observe"):
        result = request("/" + sys.argv[1])
    elif len(sys.argv) >= 3 and sys.argv[1] == "call":
        args = json.loads(sys.argv[3]) if len(sys.argv) == 4 else {}
        result = request("/tool", {"tool": sys.argv[2], "args": args})
    elif len(sys.argv) == 1:
        result = request("/tool", json.load(sys.stdin))
    else:
        raise SystemExit("usage: orgenvctl catalog | observe | call NAME JSON_ARGS; or pipe tool/args JSON on stdin")
    print(json.dumps(result, sort_keys=True))
