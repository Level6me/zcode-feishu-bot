#!/usr/bin/env python3
"""Local streaming reverse proxy for the z.ai Anthropic-compatible API.

Why: the zcode engine signs requests whose target origin is api.z.ai
(ClientRequestSigningV4). Signed requests are routed by the server to the
coding-plan gateway, which requires a resource package. Requests to any other
origin are sent unsigned with plain Bearer auth and hit the standard API route
(where the free flash tier lives). Pointing the provider baseUrl at this local
proxy therefore restores plain-Bearer behaviour.

Run standalone:  python3 api_proxy.py [listen_port] [upstream_origin]
Default:         127.0.0.1:8787 -> https://api.z.ai
"""
from __future__ import annotations

import http.client
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("ZCODE_API_PROXY_PORT", "8787"))
UPSTREAM_ORIGIN = (sys.argv[2] if len(sys.argv) > 2 else os.environ.get("ZCODE_API_PROXY_UPSTREAM", "https://api.z.ai")).replace("https://", "").replace("http://", "")

HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep stdout quiet
        pass

    def log_request(self, code="-", size="-"):
        try:
            import sys as _sys
            print(f"[api_proxy] {self.command} {self.path} -> {code}", file=_sys.stderr, flush=True)
        except Exception:
            pass

    def _proxy(self):
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)

        out_headers = {}
        for k, v in self.headers.items():
            if k.lower() not in HOP_HEADERS:
                out_headers[k] = v
        if body is not None:
            out_headers["Content-Length"] = str(len(body))

        try:
            conn = http.client.HTTPSConnection(UPSTREAM_ORIGIN, timeout=600)
            conn.request(self.command, self.path, body=body, headers=out_headers)
            resp = conn.getresponse()
        except Exception as e:
            payload = f'{"type":"error","error":{"type":"proxy_error","message":"upstream failed: {e}"}}'.encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(resp.status)
        chunked = False
        for k, v in resp.getheaders():
            lk = k.lower()
            if lk in HOP_HEADERS:
                if lk == "transfer-encoding" and "chunked" in v.lower():
                    chunked = True
                continue
            self.send_header(k, v)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _proxy


def start_proxy_thread(port: int = LISTEN_PORT, upstream: str = UPSTREAM_ORIGIN, retries: int = 5) -> int:
    """Start the proxy in a daemon thread (idempotent, port-conflict tolerant)."""
    global LISTEN_PORT, UPSTREAM_ORIGIN
    key = f"_zcode_api_proxy_{port}"
    if getattr(threading, key, None):
        return port
    import time as _time
    last_err = None
    for _ in range(retries):
        try:
            server, _tls = _build_server(port)
        except OSError as e:
            last_err = e
            _time.sleep(1.0)
            continue
        t = threading.Thread(target=server.serve_forever, daemon=True, name=f"api-proxy-{port}")
        t.start()
        setattr(threading, key, server)
        return server.server_address[1]
    raise last_err if last_err else RuntimeError("proxy failed to start")


HANDSHAKE_PREFIX = "/api/paas/"  # V4 client-signing handshake: force network-level failure -> fail-open unsigned


class SafeProxyHandler(ProxyHandler):
    def _proxy(self):
        if self.path.startswith(HANDSHAKE_PREFIX):
            # Abruptly close: the engine treats handshake-network errors as
            # fail-open and resends the model request unsigned (plain Bearer).
            try:
                self.connection.close()
            except Exception:
                pass
            return
        super()._proxy()

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _proxy


def _build_server(port):
    import ssl
    cert_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
    cert_file = os.path.join(cert_dir, "proxy.crt")
    key_file = os.path.join(cert_dir, "proxy.key")
    if os.path.exists(cert_file) and os.path.exists(key_file):
        server = ThreadingHTTPServer((LISTEN_HOST, port), SafeProxyHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        return server, True
    return ThreadingHTTPServer((LISTEN_HOST, port), SafeProxyHandler), False


if __name__ == "__main__":
    server, is_tls = _build_server(LISTEN_PORT)
    scheme = "https" if is_tls else "http"
    print(f"[api_proxy] listening on {scheme}://{LISTEN_HOST}:{LISTEN_PORT} -> https://{UPSTREAM_ORIGIN}")
    server.serve_forever()
