"""Test-side guard and upstream redirect for the relay subprocess.

Loaded only through PYTHONPATH by test_oss_research_offline_integration.py. It
sends the relay's https://api.github.com requests to the loopback fixture
upstream as plain HTTP (the request URL, and so ``geturl()``, stays
api.github.com) and refuses every other socket connection, logging each
refusal to OSS_INTEGRATION_GUARD_LOG. The relay source is unchanged.
"""

import http.client
import os
import socket
import urllib.request

_UPSTREAM = ("127.0.0.1", int(os.environ["OSS_INTEGRATION_UPSTREAM_PORT"]))
_LOG = os.environ["OSS_INTEGRATION_GUARD_LOG"]
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo


def _refused(what):
    with open(_LOG, "a", encoding="utf-8") as log:
        log.write(what + "\n")
    raise PermissionError("integration test guard refused " + what)


def _checked(address):
    if not isinstance(address, tuple) or tuple(address[:2]) != _UPSTREAM:
        _refused(repr(address))


def connect(self, address):
    _checked(address)
    return _connect(self, address)


def connect_ex(self, address):
    _checked(address)
    return _connect_ex(self, address)


def getaddrinfo(host, *arguments, **options):
    if host != "127.0.0.1":
        _refused("getaddrinfo " + repr(host))
    return _getaddrinfo(host, *arguments, **options)


def https_open(self, request):
    if request.host != "api.github.com":
        _refused("https " + repr(request.host))
    return self.do_open(lambda _host, **options: http.client.HTTPConnection(*_UPSTREAM, **options),
                        request)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = getaddrinfo
urllib.request.HTTPSHandler.https_open = https_open
