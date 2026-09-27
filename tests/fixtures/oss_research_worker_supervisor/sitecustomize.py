"""Test-only: send the relay's https://api.github.com calls to a loopback fixture.

Loaded into the relay subprocess through PYTHONPATH by
tests/test_oss_research_worker_supervisor.py. The request URL stays
https://api.github.com/..., so the relay's own URL checks run unchanged; only
the connection goes to 127.0.0.1:$OSS_SUPERVISOR_UPSTREAM_PORT over plain HTTP.
Every other non-loopback connection is refused.
"""

import functools
import http.client
import os
import socket
import urllib.request

_PORT = int(os.environ["OSS_SUPERVISOR_UPSTREAM_PORT"])
_connect = socket.socket.connect


def _checked_connect(self, address):
    if not (isinstance(address, tuple) and address[0] == "127.0.0.1"):
        raise PermissionError("test network guard refused " + repr(address))
    return _connect(self, address)


def _fixture_connection(host, timeout=None, **_options):
    if host != "api.github.com":
        raise PermissionError("test network guard refused host " + repr(host))
    return http.client.HTTPConnection("127.0.0.1", _PORT, timeout=timeout)


def _https_open(self, request):
    return self.do_open(functools.partial(_fixture_connection), request)


socket.socket.connect = _checked_connect
urllib.request.HTTPSHandler.https_open = _https_open
