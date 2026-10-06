"""Offline tests for bin/oss-research-egress-pin.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_oss_research_egress_pin -v

The DNS client talks to a loopback mock resolver (127.0.0.1, random UDP port);
nft, the hosts file, and the state directory are injected. No public resolver,
endpoint, or nft is touched. The CLI has no option that loosens a check; the
tests reach the checks through the module functions with an injected Env.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import unittest

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "bin" / "oss-research-egress-pin"


def load_tool():
    loader = importlib.machinery.SourceFileLoader("oss_research_egress_pin", str(TOOL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module  # dataclasses look the module up by name
    loader.exec_module(module)
    return module


pin = load_tool()

GITHUB = ["140.82.112.5", "140.82.112.6"]
ANTHROPIC = ["160.79.104.10"]
PUBLIC = {"api.github.com": GITHUB, "api.anthropic.com": ANTHROPIC}


# -- loopback mock resolver --------------------------------------------------


def _name(name):
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"


def response(query, records, *, flags=0x8180, qname=None, rid=None):
    """records: list of (owner, type, rdata-bytes)."""
    qid = struct.unpack("!H", query[:2])[0] if rid is None else rid
    question = query[12:] if qname is None else _name(qname) + struct.pack("!HH", 1, 1)
    out = struct.pack("!HHHHHH", qid, flags, 1, len(records), 0, 0) + question
    for owner, rtype, rdata in records:
        out += _name(owner) + struct.pack("!HHIH", rtype, 1, 30, len(rdata)) + rdata
    return out


def a(owner, address):
    return (owner, 1, socket.inet_aton(address))


def query_name(query):
    labels, offset = [], 12
    while query[offset]:
        length = query[offset]
        labels.append(query[offset + 1:offset + 1 + length].decode())
        offset += 1 + length
    return ".".join(labels)


class MockResolver:
    """Answers by a handler(name, attempt, query) -> bytes | None (None = no reply)."""

    def __init__(self, handler):
        self.handler = handler
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.address = self.sock.getsockname()
        self.counts = {}
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                query, peer = self.sock.recvfrom(4096)
            except OSError:
                return
            name = query_name(query)
            self.counts[name] = self.counts.get(name, 0) + 1
            reply = self.handler(name, self.counts[name], query)
            if reply is not None:
                self.sock.sendto(reply, peer)

    def close(self):
        self.sock.close()


def public_handler(name, attempt, query):
    return response(query, [a(name, ip) for ip in PUBLIC[name]])


# -- injected nft and filesystem -------------------------------------------------


class FakeNft:
    """Mimics `nft -j list set` for a concat set with flags timeout.

    Element "timeout"/"expires" are whole seconds, as measured on the Debian 13
    guest (nftables 1.1.3: "timeout 120s" lists "timeout": 120, "expires": 119).
    `clock` (a one-item list) drives "expires" like the kernel lease would.
    """

    def __init__(self, fail_on=None):
        self.elements = set()
        self.timeouts = {}
        self.added_at = {}
        self.clock = None
        self.unit = 1  # 1000 would mimic a millisecond-based JSON output
        self.flags = ["timeout"]
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, args):
        self.calls.append(list(args))
        if self.fail_on and args[0] == self.fail_on:
            raise pin.PinError("nft %s failed (injected)" % self.fail_on)
        if args[:2] == ["flush", "set"]:
            self.elements.clear()
            return ""
        if args[:2] == ["add", "element"]:
            body = args[-1].strip("{} ")
            for item in body.split(","):
                match = re.fullmatch(r"\s*(\S+) \. (\d+) timeout (\d+)s\s*", item)
                assert match, item
                self.elements.add((match.group(1), int(match.group(2))))
                self.timeouts[(match.group(1), int(match.group(2)))] = int(match.group(3))
                self.added_at[(match.group(1), int(match.group(2)))] = self.clock[0] if self.clock else None
            return ""
        if args[:3] == ["-j", "list", "set"]:
            elem = []
            for ip, port in sorted(self.elements):
                timeout = self.timeouts.get((ip, port))
                inner = {"val": {"concat": [ip, port]}}
                if timeout is not None:
                    added = self.added_at.get((ip, port))
                    elapsed = int((self.clock[0] - added).total_seconds()) if self.clock and added else 0
                    inner.update(timeout=timeout * self.unit, expires=max(timeout - elapsed, 0) * self.unit)
                elem.append({"elem": inner})
            return json.dumps({"nftables": [{"metainfo": {}}, {"set": {"name": "egress4", "flags": self.flags,
                                                                        "elem": elem}}]})
        raise AssertionError("unexpected nft call %r" % (args,))


class BreaksAlways(FakeNft):
    def __call__(self, args):
        raise pin.PinError("nft unavailable (injected)")


class Fixture:
    def __init__(self, test, *, handler=public_handler, nft=None, now=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="egress-pin-"))
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.hosts = self.tmp / "hosts"
        self.hosts.write_text("127.0.0.1 localhost\n127.0.1.1 lima-oss-research-debian\n", encoding="utf-8")
        self.resolver = MockResolver(handler)
        test.addCleanup(self.resolver.close)
        self.nft = nft or FakeNft()
        self.clock = [now or datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)]
        self.nft.clock = self.clock
        self.env = pin.Env(hosts_path=self.hosts, state_dir=self.tmp / "state", nft=self.nft,
                           owner_uid=os.getuid(), now=lambda: self.clock[0], require_root=False,
                           exchange=lambda resolver, payload: pin.udp_exchange(resolver, payload, timeout=2.0))

    def apply(self, run_id="run-1", ttl=600):
        return pin.apply(self.env, run_id, ttl, self.resolver.address)


# -- tests --------------------------------------------------------------------


class AddressPolicyTests(unittest.TestCase):
    def test_only_public_ipv4_unicast_is_accepted(self):
        self.assertIsNone(pin.address_problem("140.82.112.5"))
        refused = ["10.0.0.1", "172.16.0.1", "192.168.1.1", "192.168.5.2", "127.0.0.1", "0.0.0.0",
                   "169.254.169.254", "100.64.0.1", "100.100.100.200", "168.63.129.16", "192.0.0.192",
                   "224.0.0.1", "240.0.0.1", "255.255.255.255", "203.0.113.10", "192.0.0.1",
                   "192.0.0.8", "192.0.0.170", "192.0.2.1", "198.18.0.1", "198.51.100.1", "192.88.99.1",
                   "192.31.196.1", "192.52.193.1", "192.175.48.1", "::1",
                   "2606:4700::1", "::ffff:140.82.112.5", "64:ff9b::8c52:7005", "api.github.com"]
        for address in refused:
            self.assertIsNotNone(pin.address_problem(address), address)


    def test_denied_ranges_do_not_depend_on_the_runtime_ipaddress_tables(self):
        # Every address of each listed range is refused by the explicit table alone.
        for network in pin.DENIED_NETWORKS:
            for address in (network.network_address, network.broadcast_address):
                self.assertTrue(any(address in n for n in pin.DENIED_NETWORKS), address)
                self.assertIsNotNone(pin.address_problem(str(address)), address)
        for label in ("192.0.0.192", "100.100.100.200", "169.254.169.254", "168.63.129.16"):
            self.assertIn("special-purpose or platform range", pin.address_problem(label), label)


class ResolverTests(unittest.TestCase):
    def test_public_answers_are_pinned_from_every_query(self):
        fixture = Fixture(self)
        pins, log = pin.resolve_pins(fixture.resolver.address, fixture.env.exchange)
        self.assertEqual({"api.github.com": GITHUB, "api.anthropic.com": ANTHROPIC}, pins)
        self.assertEqual({"api.github.com": 2, "api.anthropic.com": 2}, fixture.resolver.counts)
        self.assertEqual(4, len(log))

    def test_cname_chain_to_public_addresses_is_accepted(self):
        def handler(name, attempt, query):
            return response(query, [(name, 5, _name("edge.example.net")), a("edge.example.net", PUBLIC[name][0])])
        fixture = Fixture(self, handler=handler)
        pins, _ = pin.resolve_pins(fixture.resolver.address, fixture.env.exchange)
        self.assertEqual(GITHUB[:1], pins["api.github.com"])

    def _refused(self, handler, text):
        fixture = Fixture(self, handler=handler)
        with self.assertRaises(pin.PinError) as caught:
            pin.resolve_pins(fixture.resolver.address, fixture.env.exchange)
        self.assertIn(text, str(caught.exception))

    def test_private_mixed_ipv6_rebinding_and_malformed_answers_are_refused(self):
        cases = {
            "private": (lambda n, i, q: response(q, [a(n, "10.1.2.3")]), "refused addresses"),
            "guest gateway": (lambda n, i, q: response(q, [a(n, "192.168.5.2")]), "refused addresses"),
            "metadata": (lambda n, i, q: response(q, [a(n, "169.254.169.254")]), "refused addresses"),
            "mixed": (lambda n, i, q: response(q, [a(n, "140.82.112.5"), a(n, "127.0.0.1")]), "refused addresses"),
            "ipv6 record": (lambda n, i, q: response(q, [(n, 28, socket.inet_pton(socket.AF_INET6, "2606:4700::1"))]),
                            "IPv6 answer refused"),
            "rebinding to private": (lambda n, i, q: response(q, [a(n, "140.82.112.5" if i == 1 else "192.168.1.9")]),
                                     "refused addresses"),
            "changing public": (lambda n, i, q: response(q, [a(n, "140.82.112.5" if i == 1 else "140.82.113.9")]),
                                "possible rebinding"),
            "unrelated A": (lambda n, i, q: response(q, [a("evil.example", "140.82.112.5")]), "unrelated name"),
            "unrelated CNAME": (lambda n, i, q: response(q, [("x.example", 5, _name("y.example"))]), "unrelated name"),
            "no answer": (lambda n, i, q: response(q, []), "no IPv4 answer"),
            "nxdomain": (lambda n, i, q: response(q, [], flags=0x8183), "rcode 3"),
            "truncated": (lambda n, i, q: response(q, [a(n, "140.82.112.5")], flags=0x8380), "truncated"),
            "wrong id": (lambda n, i, q: response(q, [a(n, "140.82.112.5")], rid=(struct.unpack("!H", q[:2])[0] + 1) % 65536),
                         "id mismatch"),
            "other question": (lambda n, i, q: response(q, [a(n, "140.82.112.5")], qname="evil.example"),
                               "does not match"),
            "other type": (lambda n, i, q: response(q, [(n, 16, b"\x03txt")]), "unexpected DNS record type"),
            "timeout": (lambda n, i, q: None, "failed"),
        }
        for label, (handler, text) in cases.items():
            with self.subTest(label):
                self._refused(handler, text)


class TransactionTests(unittest.TestCase):
    def test_apply_writes_hosts_then_nft_then_manifest_and_verifies(self):
        fixture = Fixture(self)
        result = fixture.apply()
        self.assertEqual("pinned", result["status"])
        self.assertEqual(["flush", "set", "inet", "oss_research", "egress4"], fixture.nft.calls[0])
        self.assertEqual({(ip, 443) for ip in GITHUB + ANTHROPIC}, fixture.nft.elements)
        hosts = fixture.hosts.read_text(encoding="utf-8")
        self.assertTrue(hosts.startswith("127.0.0.1 localhost\n127.0.1.1 lima-oss-research-debian\n"))
        for ip in GITHUB:
            self.assertIn("%s api.github.com\n" % ip, hosts)
        manifest = json.loads(fixture.env.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(("run-1", "2026-09-28T00:10:00Z", 600, 443),
                         (manifest["run_id"], manifest["expires_at"], manifest["ttl_seconds"], manifest["port"]))
        self.assertEqual(0o644, os.stat(fixture.env.manifest_path).st_mode & 0o777)
        check = pin.verify(fixture.env, "run-1")
        self.assertEqual((True, []), (check["ok"], check["problems"]))
        self.assertEqual(result["manifest_sha256"], check["manifest_sha256"])

    def test_active_pin_of_another_run_is_never_overwritten(self):
        fixture = Fixture(self)
        fixture.apply("run-1")
        calls = len(fixture.nft.calls)
        with self.assertRaises(pin.PinError) as caught:
            fixture.apply("run-2")
        self.assertIn("holds an active pin", str(caught.exception))
        self.assertTrue(pin.verify(fixture.env, "run-1")["ok"])  # run-1 untouched
        self.assertEqual(["-j", "list", "set"], fixture.nft.calls[calls][:3])  # only a read happened
        with self.assertRaises(pin.PinError):
            pin.clear(fixture.env, "run-2")
        self.assertTrue(pin.verify(fixture.env, "run-1")["ok"])
        # The same run may refresh its own pin.
        fixture.clock[0] += timedelta(seconds=60)
        fixture.apply("run-1")
        self.assertTrue(pin.verify(fixture.env, "run-1")["ok"])

    def test_second_run_after_expiry_replaces_the_block_and_elements(self):
        fixture = Fixture(self)
        fixture.apply("run-1", ttl=60)
        fixture.clock[0] += timedelta(seconds=61)
        self.assertFalse(pin.verify(fixture.env, "run-1")["ok"])
        fixture.apply("run-2")
        hosts = fixture.hosts.read_text(encoding="utf-8")
        self.assertEqual(1, hosts.count(pin.BEGIN_MARK))
        self.assertIn("run-2", hosts)
        self.assertTrue(pin.verify(fixture.env, "run-2")["ok"])
        self.assertFalse(pin.verify(fixture.env, "run-1")["ok"])

    def test_lock_held_by_another_operation_refuses(self):
        import fcntl
        fixture = Fixture(self)
        pin.ensure_state_dir(fixture.env)
        fd = os.open(fixture.env.state_dir / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with self.assertRaises(pin.PinError) as caught:
            fixture.apply()
        self.assertIn("lock", str(caught.exception))
        self.assertEqual([], fixture.nft.calls)

    def test_cleanup_failure_is_reported_not_swallowed(self):
        class FailsAfterOpening(FakeNft):
            flushes = 0

            def __call__(self, args):
                if args[:2] == ["add", "element"]:
                    raise pin.PinError("add failed (injected)")
                if args[:2] == ["flush", "set"]:
                    self.flushes += 1
                    if self.flushes >= 2:
                        raise pin.PinError("flush failed (injected)")
                return super().__call__(args)
        nft = FailsAfterOpening()
        fixture = Fixture(self, nft=nft)
        with self.assertRaises(pin.CleanupFailed) as caught:
            fixture.apply()
        self.assertIn("do not launch", str(caught.exception))
        self.assertFalse(fixture.env.manifest_path.exists())
        self.assertEqual(4, pin.main(["clear"], env=pin.Env(**{**fixture.env.__dict__, "nft": BreaksAlways()})))

    def _assert_closed(self, fixture):
        self.assertEqual(set(), fixture.nft.elements)
        self.assertFalse(fixture.env.manifest_path.exists())
        self.assertNotIn(pin.BEGIN_MARK, fixture.hosts.read_text(encoding="utf-8"))
        self.assertFalse(pin.verify(fixture.env)["ok"])

    def test_resolve_failure_after_a_previous_pin_ends_closed(self):
        state = {"bad": False}

        def handler(name, attempt, query):
            return response(query, [a(name, "10.0.0.1" if state["bad"] else PUBLIC[name][0])])
        fixture = Fixture(self, handler=handler)
        fixture.apply("run-1")
        state["bad"] = True
        with self.assertRaises(pin.PinError):
            fixture.apply("run-1")  # the same run refreshing its pin
        self._assert_closed(fixture)

    def test_nft_failure_ends_closed_and_restores_hosts(self):
        fixture = Fixture(self, nft=FakeNft(fail_on="add"))
        with self.assertRaises(pin.PinError):
            fixture.apply()
        self._assert_closed(fixture)
        self.assertEqual("127.0.0.1 localhost\n127.0.1.1 lima-oss-research-debian\n",
                         fixture.hosts.read_text(encoding="utf-8"))

    def test_interrupt_during_apply_ends_closed(self):
        fixture = Fixture(self)
        real = pin.write_atomic

        def interrupted(path, text, mode):
            if path.name == pin.MANIFEST_NAME:
                raise KeyboardInterrupt
            return real(path, text, mode)
        pin.write_atomic = interrupted
        self.addCleanup(setattr, pin, "write_atomic", real)
        with self.assertRaises(KeyboardInterrupt):
            fixture.apply()
        pin.write_atomic = real
        self._assert_closed(fixture)

    def test_invalid_run_id_and_ttl_are_refused_before_anything_changes(self):
        fixture = Fixture(self)
        for run_id, ttl in (("../x", 600), ("", 600), ("ok", 59), ("ok", 3601), ("ok", True)):
            with self.subTest(run_id=run_id, ttl=ttl), self.assertRaises(pin.PinError):
                fixture.apply(run_id, ttl)
        self.assertEqual([], fixture.nft.calls)

    def test_clear_closes_and_removes_everything(self):
        fixture = Fixture(self)
        fixture.apply()
        self.assertEqual({"status": "cleared"}, pin.clear(fixture.env))
        self._assert_closed(fixture)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture(self)
        self.fixture.apply()

    def _problems(self):
        result = pin.verify(self.fixture.env, "run-1")
        self.assertFalse(result["ok"])
        return " ".join(result["problems"])

    def test_expired_pin_is_refused(self):
        self.fixture.clock[0] += timedelta(seconds=600)
        self.assertIn("expired", self._problems())

    def test_changed_hosts_block_is_refused(self):
        text = self.fixture.hosts.read_text(encoding="utf-8").replace(GITHUB[0], "140.82.114.4")
        self.fixture.hosts.write_text(text, encoding="utf-8")
        self.assertIn("hosts pin block", self._problems())

    def test_live_set_drift_is_refused(self):
        self.fixture.nft.elements.add(("1.2.3.4", 443))
        self.assertIn("live nft allow set differs", self._problems())
        self.fixture.nft.elements.clear()
        self.assertIn("live nft allow set differs", self._problems())

    def test_writable_or_edited_manifest_is_refused(self):
        path = self.fixture.env.manifest_path
        os.chmod(path, 0o666)
        self.assertIn("not group/other writable", self._problems())
        os.chmod(path, 0o644)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["pins"]["api.github.com"] = ["10.0.0.1"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("refused", self._problems())
        data["pins"] = {"evil.example": ["140.82.112.5"], "api.anthropic.com": ANTHROPIC}
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("pins must name exactly", self._problems())
        data["extra"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("keys differ", self._problems())

    def test_set_without_kernel_timeout_is_refused(self):
        self.fixture.nft.flags = []
        self.assertIn("lacks the timeout flag", self._problems())
        self.fixture.nft.flags = ["timeout"]
        saved = dict(self.fixture.nft.timeouts)
        self.fixture.nft.timeouts = {}
        self.assertIn("lease is missing", self._problems())
        self.fixture.nft.timeouts = saved

    def test_millisecond_json_or_lease_outliving_the_manifest_is_refused(self):
        self.assertTrue(pin.verify(self.fixture.env, "run-1")["ok"])
        self.fixture.nft.unit = 1000
        self.assertIn("not seconds-based", self._problems())
        self.fixture.nft.unit = 1
        # A kernel lease re-armed later than the manifest window must fail.
        for key in self.fixture.nft.added_at:
            self.fixture.nft.added_at[key] = self.fixture.clock[0] + timedelta(seconds=30)
        self.assertIn("outlives the manifest", self._problems())

    def test_expires_counts_down_with_the_kernel_clock(self):
        self.fixture.clock[0] += timedelta(seconds=300)
        self.assertTrue(pin.verify(self.fixture.env, "run-1")["ok"])
        data = json.loads(self.fixture.nft(["-j", "list", "set", "inet", "oss_research", "egress4"]))
        leases = {e["elem"]["expires"] for e in data["nftables"][1]["set"]["elem"]}
        self.assertEqual({300}, leases)

    def test_fixed_fields_times_and_address_limits_are_checked(self):
        path = self.fixture.env.manifest_path
        original = json.loads(path.read_text(encoding="utf-8"))
        edits = {
            "tool_revision": ("sha256:" + "0" * 64, "tool_revision"),
            "tool": ("other", "manifest tool"),
            "nft_set": ("other", "manifest nft_set"),
            "ttl_seconds": (601, "times do not match"),
            "resolver": ("resolver.example:53", "resolver is invalid"),
            "run_id": ("../x", "run id is invalid"),
        }
        for key, (value, text) in edits.items():
            with self.subTest(key):
                data = dict(original)
                data[key] = value
                path.write_text(json.dumps(data), encoding="utf-8")
                self.assertIn(text, self._problems())
        data = dict(original)
        data["pins"] = dict(original["pins"], **{"api.github.com": ["140.82.112.%d" % i for i in range(1, 18)]})
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("distinct addresses", self._problems())

    def test_other_owner_is_refused(self):
        env = self.fixture.env
        env.owner_uid = os.getuid() + 1
        result = pin.verify(env, "run-1")
        self.assertFalse(result["ok"])


class CliTests(unittest.TestCase):
    def test_cli_has_no_check_loosening_option_and_refuses_off_guest(self):
        parser = pin.build_parser()
        text = parser.format_help() + "".join(
            action.format_help() for action in parser._subparsers._group_actions[0].choices.values())
        for word in ("--hosts", "--state", "--nft", "--port", "--insecure", "--allow", "--name", "--test"):
            self.assertNotIn(word, text)
        # Not root / not Linux on this host: refused before touching nft.
        env = pin.Env(nft=lambda args: self.fail("nft must not run"))
        if os.geteuid() != 0:
            self.assertEqual(2, pin.main(["apply", "--run-id", "r", "--ttl", "600", "--resolver", "127.0.0.1"], env=env))
        self.assertEqual(2, pin.main(["apply", "--run-id", "r", "--ttl", "600", "--resolver", "resolver.example"],
                                     env=pin.Env(require_root=False, nft=lambda args: self.fail("nft must not run"))))

    def test_fixed_names_and_port(self):
        self.assertEqual(("api.github.com", "api.anthropic.com"), pin.PINNED_NAMES)
        self.assertEqual(443, pin.PINNED_PORT)
        self.assertEqual("/usr/sbin/nft", pin.NFT)
        self.assertEqual(Path("/run/oss-research-egress"), pin.STATE_DIR)


if __name__ == "__main__":
    unittest.main()
