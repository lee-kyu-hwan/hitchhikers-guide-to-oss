#!/usr/bin/env python3
"""Local TLS fixture server for the egress-pin guest check.

  mock_tls.py CERT KEY ADDR:PORT [ADDR:PORT ...]     (IPv6 as [addr]:port)

Answers every HTTPS request with 200 "mock-ok <addr:port>". Certificates come
from a fixture CA made for this check; clients must verify against that CA.
"""
import http.server
import socket
import ssl
import sys
import threading


class V6Server(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6


def serve(cert, key, spec):
    host, port = spec.rsplit(":", 1)
    v6 = host.startswith("[")
    host = host.strip("[]")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = ("mock-ok %s\n" % spec).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            print("request %s %s" % (spec, self.path), flush=True)

    server = (V6Server if v6 else http.server.ThreadingHTTPServer)((host, int(port)), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("ready %s" % spec, flush=True)


def main():
    cert, key, specs = sys.argv[1], sys.argv[2], sys.argv[3:]
    for spec in specs:
        serve(cert, key, spec)
    threading.Event().wait()


if __name__ == "__main__":
    main()
