"""Refuse every non-loopback socket connection in a relay test subprocess."""

import socket

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _checked(address):
    host = address[0] if isinstance(address, tuple) else None
    if host != "127.0.0.1":
        raise PermissionError("test network guard refused " + repr(address))


def connect(self, address):
    _checked(address)
    return _connect(self, address)


def connect_ex(self, address):
    _checked(address)
    return _connect_ex(self, address)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
