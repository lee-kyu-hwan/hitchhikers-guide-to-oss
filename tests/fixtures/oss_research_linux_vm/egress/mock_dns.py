#!/usr/bin/env python3
"""Loopback mock DNS for the egress-pin guest check (answers A queries only).

  mock_dns.py BIND_IP NAME=IP[,IP] [NAME=IP ...] [--delay NAME=SECONDS]

Serves only the fixed names given; anything else gets NXDOMAIN. Never forwards.
"""
import socket
import struct
import sys
import time


def parse(argv):
    records, delays, bind = {}, {}, argv[0]
    it = iter(argv[1:])
    for item in it:
        if item == "--delay":
            name, seconds = next(it).split("=")
            delays[name] = float(seconds)
        else:
            name, ips = item.split("=")
            records[name] = ips.split(",")
    return bind, records, delays


def qname(query):
    labels, offset = [], 12
    while query[offset]:
        n = query[offset]
        labels.append(query[offset + 1:offset + 1 + n].decode())
        offset += 1 + n
    return ".".join(labels).lower(), offset + 5


def main():
    bind, records, delays = parse(sys.argv[1:])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind, 53))
    print("ready %s:53" % bind, flush=True)
    while True:
        query, peer = sock.recvfrom(4096)
        name, end = qname(query)
        qtype = struct.unpack("!H", query[end - 4:end - 2])[0]
        if name in delays:
            time.sleep(delays[name])
        answers = records.get(name, []) if qtype == 1 else []
        rcode = 0 if name in records else 3
        head = struct.pack("!HHHHHH", struct.unpack("!H", query[:2])[0], 0x8180 | rcode, 1, len(answers), 0, 0)
        body = query[12:end]
        for ip in answers:
            body += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, 4) + socket.inet_aton(ip)
        sock.sendto(head + body, peer)
        print("answered %s type %d -> %s" % (name, qtype, answers), flush=True)


if __name__ == "__main__":
    main()
