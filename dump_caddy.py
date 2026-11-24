#!/usr/bin/env python3
"""
Dump human-readable info from a Caddy admin /config snapshot (JSON).

Usage:
    dump_caddy.py <caddy-config.json>

It prints:
- apps.http.servers  → listen addresses, TLS routes, reverse_proxies
- apps.http.handle   → the routed handlers (file_server, reverse_proxy, ...)
- admin.listen
- any 'route' arrays

The goal is to see exactly which listener/host/path each handler is bound to,
so a redirect from /maya → /collection (or any 308 in general) can be traced.
"""
from __future__ import annotations
import json, sys, os
from pathlib import Path

def _ip(host: str | None) -> str:
    if not host:
        return "(none)"
    return host.split(":")[0] if host.count(":") == 1 else host

def dump(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    cfg = json.loads(raw)
    print(f"=== file: {path} ({path.stat().st_size} bytes) ===\n")

    apps = cfg.get("apps") or {}
    http = apps.get("http") or {}
    servers = http.get("servers") or {}

    if not servers:
        print("(no http servers)")
        return

    for sname, sdef in servers.items():
        listen = sdef.get("listen", [])
        print(f"server[{sname}] listen={listen}  "
              f"tls={bool(sdef.get('tls_app_policies')) or bool(sdef.get('tls_connection_policies'))}")

        routes = sdef.get("routes") or []
        for ridx, r in enumerate(routes):
            _print_route(r, depth=1, label=f"route[{ridx}]")
            print()

def _print_route(node: dict, depth: int, label: str) -> None:
    pad = "  " * depth
    host = node.get("match") or []
    matcher = node.get("matcher", {})

    # host matcher summary
    h = []
    if host:
        for m in host:
            if isinstance(m, dict) and "host" in m:
                h.extend(m["host"])
    path_summary = ""
    if isinstance(matcher, dict):
        if "path" in matcher:
            path_summary = f" path={matcher['path']}"
        if "path_regexp" in matcher:
            path_summary += f" path_regexp={matcher['path_regexp']}"
        if "method" in matcher:
            path_summary += f" method={matcher['method']}"

    print(f"{pad}{label}  match.host={h}{path_summary}")

    # handlers list
    for hidx, h in enumerate(node.get("handle") or []):
        _print_handler(h, depth + 1, f"handle[{hidx}]")

    # nested routes (groups / sub-routes)
    for ridx, sub in enumerate(node.get("routes") or []):
        _print_route(sub, depth + 1, f"sub-route[{ridx}]")

def _print_handler(h: dict, depth: int, label: str) -> None:
    pad = "  " * depth
    htype = h.get("handler") or "?"
    if htype == "subroute":
        print(f"{pad}{label}  handler=subroute")
        for ridx, sub in (h.get("routes") or []):
            _print_route(sub, depth + 1, f"sub-route[{ridx}]")
        return

    if htype == "rewrite":
        # This is what we suspect is causing 308 redirect
        uri = h.get("uri") or ""
        strip = h.get("strip_path_prefix") or h.get("strip_path_suffix") or ""
        print(f"{pad}{label}  handler=rewrite  uri={uri!r}  strip={strip!r}")
        return

    if htype == "reverse_proxy":
        upstreams = h.get("upstreams") or []
        ups = ", ".join(str(u) for u in upstreams)
        print(f"{pad}{label}  handler=reverse_proxy  upstreams=[{ups}]")
        return

    if htype == "file_server":
        root = h.get("root") or ""
        hide = h.get("hide", [])
        srv = h.get("fs", "")
        print(f"{pad}{label}  handler=file_server  root={root!r}  hide={hide}")
        return

    if htype == "static_response":
        print(f"{pad}{label}  handler=static_response  status={h.get('status_code')}  body[:60]={(h.get('body') or '')[:60]!r}")
        return

    if htype == "vars":
        print(f"{pad}{label}  handler=vars")
        return

    # fallback
    keys = sorted(k for k in h.keys() if k != "handler")
    print(f"{pad}{label}  handler={htype}  keys={keys}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    p = Path(sys.argv[1])
    if not p.exists():
        print(f"no such file: {p}", file=sys.stderr)
        sys.exit(1)
    dump(p)
