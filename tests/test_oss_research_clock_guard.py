"""The unit wrapper's clock guard, offline.

    python3 -B -m unittest tests.test_oss_research_clock_guard

Before a run, the root wrapper compares the guest's system clock with its RTC (the
RTC is the host's time and keeps running while the host sleeps; the VZ guest's
system clock stops). Inside the tolerance it goes on; outside it sets the system
clock from the RTC, without any network, and reads both again; if the RTC cannot be
read, the reset fails or the clocks still differ, nothing is started. The clocks,
the reset and the reset program are injected here; the guest cases are
tests/fixtures/oss_research_worker/linux/clock_guard_cases.sh. No network, systemd
or VM is used.
"""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tests.test_oss_research_worker_linux as linux_tests

wrapper = linux_tests.wrapper
RTC = 1_790_930_000  # an arbitrary RTC reading (2026-10-02T08:33:20Z)


class FakeClock:
    """The RTC and the system clock of a fake guest, and the reset between them."""

    def __init__(self, offset=0.0, rtc=RTC):
        self.rtc = rtc
        self.system = rtc + offset
        self.reads = 0
        self.sets = []
        self.stuck = False        # the reset "succeeds" but changes nothing
        self.fail_set = None      # the reset raises this
        self.fail_read = None     # the RTC cannot be read (this exception)
        self.fail_read_after = None  # ... from this read on (1-based)

    def read_rtc(self):
        self.reads += 1
        if self.fail_read is not None or (self.fail_read_after is not None
                                          and self.reads >= self.fail_read_after):
            raise self.fail_read or OSError(5, "Input/output error")
        return self.rtc

    def now(self):
        return self.system

    def set_clock(self, epoch):
        self.sets.append(epoch)
        if self.fail_set is not None:
            raise self.fail_set
        if not self.stuck:
            self.system = float(epoch)

    def guard(self, **overrides):
        return wrapper.clock_guard(read_rtc=overrides.get("read_rtc", self.read_rtc),
                                   now=overrides.get("now", self.now),
                                   set_clock=overrides.get("set_clock", self.set_clock))


class ClockGuardTests(unittest.TestCase):
    def test_the_fixed_values(self):
        self.assertEqual(wrapper.CLOCK_TOLERANCE_SECONDS, 5.0)
        self.assertEqual(wrapper.CLOCK_MAX_RESYNC_SECONDS, 7 * 24 * 3600)
        self.assertTrue(wrapper.CLOCK_SET_PROGRAM.is_absolute())
        self.assertEqual(wrapper.CLOCK_SET_PROGRAM, Path("/usr/bin/date"))
        self.assertEqual(wrapper.RTC_SINCE_EPOCH, Path("/sys/class/rtc/rtc0/since_epoch"))

    def test_inside_the_tolerance_nothing_is_done(self):
        for offset in (0.0, 0.4, -1.5, 4.9, -5.0, 5.0):
            with self.subTest(offset):
                clock = FakeClock(offset)
                record = clock.guard()
                self.assertEqual(record["action"], "none")
                self.assertEqual(clock.sets, [])
                self.assertEqual(record["offset_before"], offset)
                self.assertEqual(record["offset_after"], offset)
                self.assertIsNone(record["reason"])

    def test_outside_the_tolerance_the_system_clock_is_set_from_the_rtc_and_read_again(self):
        for offset in (-7 * 3600 - 71.0, -1186.0, -5.1, 5.1, 600.0):
            with self.subTest(offset):
                clock = FakeClock(offset)
                record = clock.guard()
                self.assertEqual(record["action"], "resynced")
                self.assertEqual(clock.sets, [RTC])  # one reset, to the RTC's whole second
                self.assertEqual(record["offset_before"], offset)
                self.assertEqual(record["offset_after"], 0.0)
                self.assertIsNone(record["reason"])
                self.assertEqual(clock.reads, 2)  # the RTC is read once more after the reset

    def test_the_record_says_when_how_far_and_what_was_done(self):
        record = FakeClock(-1186.0).guard()
        self.assertEqual(set(record), {"checked_at", "tolerance_seconds", "offset_before", "action",
                                       "offset_after", "reason"})
        self.assertEqual(record["checked_at"], "2026-10-02T08:33:20Z")  # the RTC's time, not the wrong clock's
        self.assertEqual(record["tolerance_seconds"], 5.0)
        json.dumps(record)

    def test_a_clock_that_stays_off_after_the_reset_blocks(self):
        clock = FakeClock(-300.0)
        clock.stuck = True
        record = clock.guard()
        self.assertEqual((record["action"], record["reason"]), ("blocked", "still-off-after-resync"))
        self.assertEqual(record["offset_before"], -300.0)
        self.assertEqual(record["offset_after"], -300.0)
        self.assertEqual(clock.sets, [RTC])  # no second try

    def test_an_unreadable_rtc_blocks_and_nothing_is_set(self):
        for label, error in (("io error", OSError(5, "Input/output error")), ("missing", FileNotFoundError()),
                             ("garbage", ValueError("not a number"))):
            with self.subTest(label):
                clock = FakeClock(-300.0)
                clock.fail_read = error
                record = clock.guard()
                self.assertEqual((record["action"], record["reason"]), ("blocked", "rtc-unreadable"))
                self.assertIsNone(record["offset_before"])
                self.assertIsNone(record["offset_after"])
                self.assertEqual(clock.sets, [])
                self.assertIsNone(record["checked_at"])

    def test_an_rtc_that_cannot_be_read_again_after_the_reset_blocks(self):
        clock = FakeClock(-300.0)
        clock.fail_read_after = 2
        record = clock.guard()
        self.assertEqual((record["action"], record["reason"]), ("blocked", "rtc-unreadable"))
        self.assertEqual(record["offset_before"], -300.0)
        self.assertIsNone(record["offset_after"])
        self.assertEqual(clock.sets, [RTC])

    def test_a_failed_reset_blocks(self):
        for label, error in (("os error", OSError(1, "Operation not permitted")),
                             ("timeout", subprocess.TimeoutExpired("date", 10)),
                             ("subprocess error", subprocess.SubprocessError("x"))):
            with self.subTest(label):
                clock = FakeClock(-300.0)
                clock.fail_set = error
                record = clock.guard()
                self.assertEqual((record["action"], record["reason"]), ("blocked", "resync-failed"))
                self.assertEqual(record["offset_before"], -300.0)
                self.assertIsNone(record["offset_after"])
        # The reason never carries the error text (a program may print anything).
        clock = FakeClock(-300.0)
        clock.fail_set = OSError(1, "SECRET-TEXT")
        self.assertNotIn("SECRET-TEXT", json.dumps(clock.guard()))

    def test_an_offset_too_large_to_trust_blocks_and_nothing_is_set(self):
        for offset in (-(7 * 24 * 3600 + 1.0), 7 * 24 * 3600 + 1.0, -56 * 365 * 86400.0):
            with self.subTest(offset):
                clock = FakeClock(offset)
                record = clock.guard()
                self.assertEqual((record["action"], record["reason"]), ("blocked", "offset-too-large"))
                self.assertEqual(clock.sets, [])
        # The edge itself is still reset.
        clock = FakeClock(-7 * 24 * 3600.0)
        self.assertEqual(clock.guard()["action"], "resynced")

    def test_the_rtc_is_read_from_the_fixed_file_as_whole_seconds(self):
        root = Path(tempfile.mkdtemp(prefix="osr-rtc-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = root / "since_epoch"
        with mock.patch.object(wrapper, "RTC_SINCE_EPOCH", path):
            path.write_text("1790930000\n")
            self.assertEqual(wrapper.read_rtc(), 1790930000)
            for bad in ("", "\n", "abc", "1.5", "-1", "0", "1790930000 x", "١٧٩٠٩٣٠٠٠٠", "9" * 40, "0x10"):
                with self.subTest(bad):
                    path.write_text(bad)
                    with self.assertRaises((OSError, ValueError)):
                        wrapper.read_rtc()
            path.unlink()
            with self.assertRaises(OSError):
                wrapper.read_rtc()
            path.symlink_to(root / "elsewhere")
            with self.assertRaises(OSError):
                wrapper.read_rtc()

    def test_the_reset_runs_the_fixed_program_with_a_fixed_argv_and_a_bare_environment(self):
        seen = {}

        def fake_run(argv, **options):
            seen.update(argv=argv, options=options)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(wrapper.subprocess, "run", fake_run):
            wrapper.set_system_clock(1790930000)
        self.assertEqual(seen["argv"], [str(wrapper.CLOCK_SET_PROGRAM), "-u", "-s", "@1790930000"])
        options = seen["options"]
        self.assertNotIn("shell", options)
        self.assertEqual(options["env"], {"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
        self.assertEqual(options["timeout"], wrapper.CLOCK_SET_SECONDS)
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        with mock.patch.object(wrapper.subprocess, "run", lambda argv, **o: subprocess.CompletedProcess(
                argv, 1, "", "date: cannot set date")):
            with self.assertRaises(OSError):
                wrapper.set_system_clock(1790930000)
        for bad in (-1, 0, "1", 1.5, True):
            with self.subTest(bad), self.assertRaises(ValueError):
                wrapper.set_system_clock(bad)

    def test_no_option_or_variable_changes_the_guard(self):
        code = Path(linux_tests.WRAPPER).read_text().split('"""', 2)[2]
        self.assertEqual(set(__import__("re").findall(r'add_argument\("(--?[a-z-]+|arguments)"', code)),
                         {"--entry", "--timeout", "--egress", "arguments"})
        for name in ("CLOCK", "RTC"):
            self.assertNotIn(f'environ.get("{name}', code)
            self.assertNotIn(f"environ[\"{name}", code)
        self.assertNotIn("OSS_RESEARCH_CLOCK", code)
        # The environment handed to the unit has no clock variable either.
        self.assertFalse([name for name in wrapper.EXTRA_VARIABLES if "CLOCK" in name or "RTC" in name])


class ClockGuardMainTests(linux_tests.BootstrapFixture):
    """The guard inside main(): first among the things main does, with a scratch tree."""

    def setUp(self):
        super().setUp()
        for relative in wrapper.ENTRIES.values():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("print('entry')\n")
        pin = self.source / wrapper.EGRESS_PIN
        pin.write_text("print('pin')\n")
        for current, directories, files in os.walk(self.root):
            os.chmod(current, 0o755)
            for name in files:
                os.chmod(Path(current) / name, 0o755)
        self.lock = self.root / "run/lock"
        self.lock.parent.mkdir(parents=True)
        self.lock.parent.chmod(0o700)

    def run_main(self, clock, *, entry="verify", egress="none", launched=None, record=None,
                 programs=None):
        """main() with the clocks and programs faked; returns (exit code, stdout record or None, mocks)."""
        me = linux_tests.pwd.getpwuid(os.getuid()).pw_name
        argv = ["linux-run", "--entry", entry, "--timeout", "10", "--egress", egress, "--", "run", "/o"]
        record = dict(record or {"unit": "oss-research-0123456789abcdef", "entry": entry, "egress": egress,
                                 "exit_code": 0, "failures": []})
        started = {name: mock.Mock(name=name, side_effect=AssertionError(f"{name} must not run"))
                   for name in ("run_unit", "open_egress", "egress_idle", "unit_argv")}

        def fake_launch(*args, **kwargs):
            if launched is not None:
                launched(clock)
            return dict(record)
        launch = mock.Mock(side_effect=fake_launch)
        stdout = io.StringIO()
        patches = mock.patch.multiple(
            self.copy, TRUSTED_OWNER=os.getuid(), FILESYSTEM_ROOT=self.root, TRUSTED_PARENT=self.trusted,
            USER=me, SYSTEMD_RUN=self.tools / "systemd-run", SYSTEMCTL=self.tools / "systemctl",
            NFT=self.tools / "nft", CLOCK_SET_PROGRAM=self.tools / "date", RUN_LOCK=self.lock,
            # The lock is flock on a descriptor main never closes; its own tests are IdleEgressAndLockTests.
            acquire_run_lock=mock.Mock(return_value=0),
            unit_environment=mock.Mock(return_value={}), launch=launch, **started)
        reads = mock.Mock(side_effect=clock.read_rtc)
        sets = mock.Mock(side_effect=clock.set_clock)
        with patches, mock.patch.object(self.copy, "read_rtc", reads), \
                mock.patch.object(self.copy, "system_seconds", clock.now), \
                mock.patch.object(self.copy, "set_system_clock", sets), \
                mock.patch.object(self.copy.sys, "platform", "linux"), \
                mock.patch.object(self.copy.sys, "executable", str(self.tools / "python3")), \
                mock.patch.object(self.copy.sys, "argv", argv), \
                mock.patch.dict(self.copy.os.environ, {self.copy.EGRESS_RESOLVER_VARIABLE: "192.168.5.2"}), \
                mock.patch("sys.stderr"), mock.patch("sys.stdout", stdout):
            code = self.copy.main()
        lines = stdout.getvalue().strip().splitlines()
        return code, (json.loads(lines[-1]) if lines else None), {"launch": launch, "reads": reads,
                                                                  "sets": sets, **started}

    def assert_nothing_started(self, mocks):
        mocks["launch"].assert_not_called()
        for name in ("run_unit", "open_egress", "egress_idle", "unit_argv"):
            mocks[name].assert_not_called()

    def test_inside_the_tolerance_the_run_goes_on_and_the_record_says_so(self):
        code, record, mocks = self.run_main(FakeClock(1.2))
        self.assertEqual(code, 0)
        self.assertEqual(record["clock"]["action"], "none")
        self.assertEqual(record["clock"]["offset_before"], 1.2)
        mocks["launch"].assert_called_once()
        mocks["sets"].assert_not_called()

    def test_an_offset_is_reset_and_the_run_goes_on_with_the_action_in_the_record(self):
        clock = FakeClock(-1186.0)
        code, record, mocks = self.run_main(clock)
        self.assertEqual(code, 0)
        self.assertEqual(record["clock"]["action"], "resynced")
        self.assertEqual(record["clock"]["offset_before"], -1186.0)
        self.assertEqual(record["clock"]["offset_after"], 0.0)
        mocks["sets"].assert_called_once_with(RTC)
        mocks["launch"].assert_called_once()
        self.assertEqual(record["failures"], [])

    def test_every_blocked_case_starts_nothing_for_every_entry_and_egress_mode(self):
        def stuck(clock):
            clock.stuck = True

        def unreadable(clock):
            clock.fail_read = OSError(5, "Input/output error")

        def reset_fails(clock):
            clock.fail_set = OSError(1, "Operation not permitted")

        def too_large(clock):
            clock.system = clock.rtc - 30 * 86400.0
        cases = {"offset stays": (-300.0, stuck, "still-off-after-resync"),
                 "rtc unreadable": (-300.0, unreadable, "rtc-unreadable"),
                 "reset fails": (-300.0, reset_fails, "resync-failed"),
                 "offset too large": (0.0, too_large, "offset-too-large")}
        for entry in sorted(wrapper.ENTRIES):
            for egress in wrapper.EGRESS_MODES:
                for label, (offset, make, reason) in cases.items():
                    with self.subTest(entry=entry, egress=egress, case=label):
                        clock = FakeClock(offset)
                        make(clock)
                        code, record, mocks = self.run_main(clock, entry=entry, egress=egress)
                        self.assertEqual(code, self.copy.EXIT_BLOCKED)
                        self.assert_nothing_started(mocks)
                        self.assertEqual(record["clock"]["action"], "blocked")
                        self.assertEqual(record["clock"]["reason"], reason)
                        self.assertEqual(record["failures"], ["clock-guard: " + reason])
                        self.assertIsNone(record["exit_code"])
                        self.assertIsNone(record["unit"])
                        self.assertEqual((record["entry"], record["egress"]), (entry, egress))

    def test_the_guard_applies_to_every_entry_and_egress_mode_when_it_passes(self):
        for entry in sorted(wrapper.ENTRIES):
            for egress in wrapper.EGRESS_MODES:
                with self.subTest(entry=entry, egress=egress):
                    code, record, mocks = self.run_main(FakeClock(-90.0), entry=entry, egress=egress)
                    self.assertEqual(code, 0)
                    self.assertEqual(record["clock"]["action"], "resynced")
                    mocks["launch"].assert_called_once()

    def test_a_reset_program_that_is_not_trusted_blocks_before_anything_is_read_or_set(self):
        program = self.tools / "date"
        for label, tamper in (("group-writable", lambda: program.chmod(0o775)),
                              ("other-writable", lambda: program.chmod(0o757)),
                              ("set-id", lambda: program.chmod(0o4755)),
                              ("missing", lambda: program.unlink()),
                              ("a link", lambda: (program.unlink(), program.symlink_to(self.tools / "python3")))):
            with self.subTest(label):
                tamper()
                try:
                    clock = FakeClock(-300.0)
                    code, record, mocks = self.run_main(clock)
                    self.assertEqual(code, self.copy.EXIT_BLOCKED)
                    self.assert_nothing_started(mocks)
                    mocks["reads"].assert_not_called()
                    mocks["sets"].assert_not_called()
                    self.assertFalse(self.sentinel.exists(), "the worker was imported")
                finally:
                    if program.is_symlink() or program.exists():
                        program.unlink()
                    linux_tests.FakeHost.executable(program)
                    program.chmod(0o755)
        # The restored program is accepted again.
        code, _, mocks = self.run_main(FakeClock(0.0))
        self.assertEqual(code, 0)

    def test_the_directory_of_the_reset_program_must_be_closed_too(self):
        self.tools.chmod(0o777)
        code, _, mocks = self.run_main(FakeClock(-300.0))
        self.assertEqual(code, self.copy.EXIT_BLOCKED)
        self.assert_nothing_started(mocks)
        mocks["sets"].assert_not_called()

    def test_the_clock_is_read_again_after_the_run_and_a_drift_is_marked_not_judged(self):
        def host_slept(clock):
            clock.system -= 900.0  # the host slept during the run: the system clock stopped
        code, record, mocks = self.run_main(FakeClock(2.0), launched=host_slept)
        after = record["clock"]["after_run"]
        self.assertFalse(after["within_tolerance"])
        self.assertEqual(after["offset"], -898.0)
        self.assertIsNone(after["error"])
        # The run's own result is not rewritten: same exit code, no new failure.
        self.assertEqual((code, record["exit_code"], record["failures"]), (0, 0, []))
        code, record, _ = self.run_main(FakeClock(2.0))
        self.assertTrue(record["clock"]["after_run"]["within_tolerance"])
        self.assertEqual(record["clock"]["after_run"]["offset"], 2.0)

    def test_an_rtc_that_cannot_be_read_after_the_run_is_marked(self):
        def broken(clock):
            clock.fail_read = OSError(5, "Input/output error")
        code, record, _ = self.run_main(FakeClock(0.0), launched=broken)
        after = record["clock"]["after_run"]
        self.assertEqual((after["within_tolerance"], after["offset"], after["error"]),
                         (None, None, "rtc-unreadable"))
        self.assertEqual((code, record["failures"]), (0, []))

    def test_the_post_run_read_never_sets_the_clock(self):
        def host_slept(clock):
            clock.system -= 900.0
        clock = FakeClock(0.0)
        code, record, mocks = self.run_main(clock, launched=host_slept)
        mocks["sets"].assert_not_called()
        self.assertEqual(clock.sets, [])

    def test_a_failed_run_keeps_its_failures_and_still_carries_the_clock(self):
        failed = {"unit": "oss-research-0123456789abcdef", "entry": "verify", "egress": "none",
                  "exit_code": None, "failures": ["timeout"]}
        code, record, _ = self.run_main(FakeClock(0.0), record=failed)
        self.assertEqual(code, self.copy.EXIT_FAILED)
        self.assertEqual(record["failures"], ["timeout"])
        self.assertEqual(record["clock"]["action"], "none")


if __name__ == "__main__":
    unittest.main()
