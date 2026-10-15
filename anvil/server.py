"""HTTP + MCP server. Same pattern as Ember: REST under /api/v1, MCP at /api/mcp,
API key in ANVIL_API_KEYS (sent as `Authorization: Bearer`, `X-Anvil-Key` or `?key=`).

Runs on Vercel (api/index.py exposes `app`) or locally: `python -m anvil serve`.
Pure stdlib WSGI, no framework.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import traceback
from urllib.parse import parse_qs, quote

from . import __version__, ember, service, store

MCP_PROTOCOL = "2025-06-18"

# ---------------------------------------------------------------- tool catalogue (MCP + OpenAPI)

TOOLS = [
    {"name": "leads", "method": "GET", "path": "/api/v1/leads",
     "description": "List job leads from Ember's open-leads.csv that Anvil can tailor for (verdict PASS, PARK or "
                    "HIGHPAY), with canonical_id and whether a CV was already made.",
     "schema": {"type": "object", "properties": {
         "include_all": {"type": "boolean", "description": "Include every verdict, not just actionable ones."}}}},
    {"name": "tailor", "method": "POST", "path": "/api/v1/tailor",
     "description": "Build Swastik's tailored one-page CV for one job from his real CV text only (no invented "
                    "facts). Give canonical_id from `leads`, or jd_text + company + role for a job not on the "
                    "board. Returns PDF/DOCX download links, the CV text, and an honest 3-bucket JD diff "
                    "(exact matches, implied safe swaps, true gaps).",
     "schema": {"type": "object", "properties": {
         "canonical_id": {"type": "string", "description": "<ats>:<company>:<job_id> from leads"},
         "jd_text": {"type": "string", "description": "Full job description text (for jobs not on the board)"},
         "company": {"type": "string"}, "role": {"type": "string"}, "url": {"type": "string"},
         "notes": {"type": "string", "description": "Extra recon, e.g. 2 lines from a hiring manager's post"},
         "company_url": {"type": "string", "description": "Company homepage if the automatic guess is wrong"},
         "cluster": {"type": "string", "enum": service.CLUSTERS},
         "polish": {"type": "boolean", "description": "Optional ~300-token LLM synonym pass, guarded in code"},
         "golden": {"type": "boolean", "description": "Reuse the last CV recipe that got a callback"},
         "dry_run": {"type": "boolean", "description": "Make the CV but do not log it as sent"}}}},
    {"name": "feedback", "method": "POST", "path": "/api/v1/feedback",
     "description": "Record a company's reply so Anvil learns which CV versions work. callback/interview/offer "
                    "boost the genes used; rejected/ghosted penalise them.",
     "schema": {"type": "object", "required": ["canonical_id", "outcome"], "properties": {
         "canonical_id": {"type": "string"}, "outcome": {"type": "string", "enum": service.OUTCOMES},
         "note": {"type": "string"}}}},
    {"name": "applications", "method": "GET", "path": "/api/v1/applications",
     "description": "List CVs Anvil has logged as sent, newest first, with their outcome.",
     "schema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "recon", "method": "GET", "path": "/api/v1/recon",
     "description": "Saved company/JD recon for a lead (product summary, product terms, pain points, matched "
                    "CV hooks, JD diff). Use it to draft a cold email without re-scraping.",
     "schema": {"type": "object", "required": ["canonical_id"], "properties": {"canonical_id": {"type": "string"}}}},
    {"name": "stats", "method": "GET", "path": "/api/v1/stats",
     "description": "What the bandit has learned: replies by role cluster, best/worst genes, deprecated hooks, "
                    "golden variants.", "schema": {"type": "object", "properties": {}}},
    {"name": "check", "method": "POST", "path": "/api/v1/check",
     "description": "Run Anvil's guardrails on any CV text (e.g. written by another AI): banned words, em dashes, "
                    "client scope, numbers or names not in his real CVs, locked title/location/dates.",
     "schema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}},
    {"name": "ghost_sweep", "method": "POST", "path": "/api/v1/ghost-sweep",
     "description": "Mark applications with no reply after N days (default 14) as ghosted.",
     "schema": {"type": "object", "properties": {"days": {"type": "integer"}}}},
]
TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


def call_tool(name: str, args: dict, base_url: str, link_key: str = "") -> dict:
    args = args or {}
    if name == "leads":
        return service.leads(bool(args.get("include_all")))
    if name == "tailor":
        allowed = set(TOOL_BY_NAME["tailor"]["schema"]["properties"])
        res = service.tailor(**{k: v for k, v in args.items() if k in allowed}, base_url=base_url)
        if link_key:
            res["downloads"] = {k: f"{v}?key={quote(link_key)}" for k, v in res["downloads"].items()}
        return res
    if name == "feedback":
        return service.feedback(args.get("canonical_id", ""), args.get("outcome", ""), args.get("note", ""))
    if name == "applications":
        return service.applications(int(args.get("limit") or 50))
    if name == "recon":
        return service.get_recon(args.get("canonical_id", ""))
    if name == "stats":
        return service.stats()
    if name == "check":
        return service.check(args.get("text", ""))
    if name == "ghost_sweep":
        return service.ghost_sweep(int(args.get("days") or 14))
    raise service.UserError(f"unknown tool {name}")


# ---------------------------------------------------------------- auth

def _keys() -> list[str]:
    return [k.strip() for k in os.environ.get("ANVIL_API_KEYS", "").split(",") if k.strip()]


def presented_key(environ: dict) -> tuple[str, str]:
    """Returns (key, how) where how is header|query|''."""
    auth = environ.get("HTTP_AUTHORIZATION", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip(), "header"
    if environ.get("HTTP_X_ANVIL_KEY"):
        return environ["HTTP_X_ANVIL_KEY"].strip(), "header"
    q = parse_qs(environ.get("QUERY_STRING", ""))
    if q.get("key"):
        return q["key"][0].strip(), "query"
    return "", ""


def authorized(environ: dict) -> tuple[bool, str]:
    keys = _keys()
    if not keys:
        return (os.environ.get("ANVIL_ALLOW_OPEN") == "1",
                "Server has no ANVIL_API_KEYS configured (set it, or ANVIL_ALLOW_OPEN=1 for local use)")
    key, _ = presented_key(environ)
    ok = any(hmac.compare_digest(key, k) for k in keys) if key else False
    return ok, "Missing or wrong API key"


# ---------------------------------------------------------------- WSGI

def _resp(start, status: str, body, ctype="application/json", extra=None):
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode()
    elif isinstance(body, str):
        body = body.encode()
    headers = [("Content-Type", ctype), ("Content-Length", str(len(body))),
               ("Access-Control-Allow-Origin", "*"),
               ("Access-Control-Allow-Headers", "authorization, content-type, x-anvil-key, mcp-session-id, mcp-protocol-version"),
               ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"), ("Cache-Control", "no-store")]
    start(status, headers + (extra or []))
    return [body]


def _body(environ) -> dict:
    try:
        n = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        n = 0
    raw = environ["wsgi.input"].read(n) if n else b""
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise service.UserError("body must be JSON") from e
    return data


def _base_url(environ) -> str:
    proto = environ.get("HTTP_X_FORWARDED_PROTO") or environ.get("wsgi.url_scheme", "http")
    host = environ.get("HTTP_X_FORWARDED_HOST") or environ.get("HTTP_HOST") or "localhost"
    return f"{proto}://{host}"


def app(environ, start):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/") or "/"
    # On Vercel every route is rewritten to /api/index?__path=/<original path> (see vercel.json).
    qs = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
    if "__path" in qs:
        path = "/" + qs.pop("__path")[0].lstrip("/")
        environ["QUERY_STRING"] = "&".join(f"{k}={quote(v, safe='')}" for k, vs in qs.items() for v in vs)
        environ["PATH_INFO"] = path
    if method == "OPTIONS":
        return _resp(start, "204 No Content", b"")
    if path in ("/", "/index.html"):
        from .webpage import PAGE
        return _resp(start, "200 OK", PAGE, "text/html; charset=utf-8")
    if path == "/api/v1/openapi.json":
        return _resp(start, "200 OK", openapi(_base_url(environ)))
    if path == "/api/v1/status" and method == "GET":
        ok, why = authorized(environ)
        if not ok:
            return _resp(start, "200 OK", {"ok": True, "service": "anvil", "authorized": False})
    ok, why = authorized(environ)
    if not ok:
        return _resp(start, "401 Unauthorized", {"success": False, "error": why})
    try:
        if path == "/api/mcp":
            return mcp(environ, start, method)
        if path == "/api/v1/status":
            return _resp(start, "200 OK", {"success": True, "data": service.status() | {"authorized": True}})
        m = re.fullmatch(r"/api/v1/cv/([A-Za-z0-9._-]+)\.(pdf|docx|html|txt)", path)
        if m and method == "GET":
            data, ctype, name = service.cv_file(m.group(1), m.group(2))
            disp = "inline" if m.group(2) in ("pdf", "html", "txt") else "attachment"
            return _resp(start, "200 OK", data, ctype, [("Content-Disposition", f'{disp}; filename="{name}"')])
        for t in TOOLS:
            if t["path"] == path and t["method"] == method:
                if method == "GET":
                    args = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items() if k != "key"}
                    if "include_all" in args:
                        args["include_all"] = args["include_all"] in ("1", "true", "yes")
                else:
                    args = _body(environ)
                key, how = presented_key(environ)
                data = call_tool(t["name"], args, _base_url(environ), key if how == "query" else "")
                return _resp(start, "200 OK", {"success": True, "data": data})
        return _resp(start, "404 Not Found", {"success": False, "error": f"no route {method} {path}"})
    except (service.UserError, ember.EmberError, store.StateError) as e:
        return _resp(start, "400 Bad Request", {"success": False, "error": str(e)})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return _resp(start, "500 Internal Server Error", {"success": False, "error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------- MCP (Streamable HTTP, JSON responses)

def mcp_handle(msg: dict, base_url: str, link_key: str = "") -> dict | None:
    mid = msg.get("id")
    meth = msg.get("method", "")
    if mid is None:  # notification
        return None
    try:
        if meth == "initialize":
            ver = (msg.get("params") or {}).get("protocolVersion") or MCP_PROTOCOL
            result = {"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "anvil", "version": __version__},
                      "instructions": "Anvil tailors Swastik's CV from his real CV text only. Use `leads` to "
                                      "find a canonical_id, then `tailor`. Never add skills from the JD's "
                                      "true-gaps bucket to a CV. Record replies with `feedback`."}
        elif meth == "ping":
            result = {}
        elif meth == "tools/list":
            result = {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["schema"]}
                                for t in TOOLS]}
        elif meth == "tools/call":
            p = msg.get("params") or {}
            try:
                data = call_tool(p.get("name", ""), p.get("arguments") or {}, base_url, link_key)
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}],
                          "isError": False}
            except (service.UserError, ember.EmberError, store.StateError) as e:
                result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {meth}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def mcp(environ, start, method):
    if method != "POST":
        return _resp(start, "405 Method Not Allowed", {"error": "POST JSON-RPC to this endpoint"},
                     extra=[("Allow", "POST, OPTIONS")])
    try:
        msg = _body(environ)
    except service.UserError:
        return _resp(start, "400 Bad Request", {"jsonrpc": "2.0", "id": None,
                                                "error": {"code": -32700, "message": "parse error"}})
    key, how = presented_key(environ)
    link_key = key if how == "query" else ""
    base = _base_url(environ)
    if isinstance(msg, list):
        out = [r for r in (mcp_handle(m, base, link_key) for m in msg) if r]
        return _resp(start, "200 OK", out) if out else _resp(start, "202 Accepted", b"")
    r = mcp_handle(msg, base, link_key)
    return _resp(start, "200 OK", r) if r else _resp(start, "202 Accepted", b"")


# ---------------------------------------------------------------- OpenAPI (ChatGPT actions, other AIs)

def openapi(base_url: str) -> dict:
    paths = {}
    for t in TOOLS:
        op = {"operationId": t["name"], "summary": t["description"][:120], "description": t["description"],
              "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}}}}
        if t["method"] == "GET":
            op["parameters"] = [{"name": k, "in": "query", "required": k in t["schema"].get("required", []),
                                 "schema": v} for k, v in t["schema"]["properties"].items()]
        else:
            op["requestBody"] = {"required": True, "content": {"application/json": {"schema": t["schema"]}}}
        paths.setdefault(t["path"], {})[t["method"].lower()] = op
    paths["/api/v1/cv/{id}.{format}"] = {"get": {
        "operationId": "download_cv", "summary": "Download a tailored CV (pdf, docx, html, txt)",
        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                       {"name": "format", "in": "path", "required": True,
                        "schema": {"type": "string", "enum": ["pdf", "docx", "html", "txt"]}}],
        "responses": {"200": {"description": "file"}}}}
    return {"openapi": "3.1.0",
            "info": {"title": "Anvil", "version": __version__,
                     "description": "Evolutionary resume tailoring for Swastik from his real CVs only."},
            "servers": [{"url": base_url}],
            "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
            "security": [{"bearer": []}], "paths": paths}


# ---------------------------------------------------------------- local runners

def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    from wsgiref.simple_server import make_server
    print(f"Anvil on http://{host}:{port}  (phone on same Wi-Fi: use --host 0.0.0.0)")
    if not _keys() and os.environ.get("ANVIL_ALLOW_OPEN") != "1":
        print("Note: set ANVIL_API_KEYS=<secret> (or ANVIL_ALLOW_OPEN=1 for local only)")
    make_server(host, port, app).serve_forever()


def stdio() -> None:
    """MCP over stdio, for Claude Code / Claude Desktop / any local MCP client."""
    import sys
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            r = mcp_handle(m, os.environ.get("ANVIL_PUBLIC_URL", "http://127.0.0.1:8787"))
            if r:
                sys.stdout.write(json.dumps(r, ensure_ascii=False) + "\n")
                sys.stdout.flush()
