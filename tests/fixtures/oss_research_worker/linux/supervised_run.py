#!/usr/bin/env python3
"""One supervised non-AI run on the Linux guest whose result stays for pickup.

    OSS_RESEARCH_SRT_PREFIX=/opt/... python3 -B supervised_run.py OUT_DIR

Runs from the source checkout root (it imports the tests package). It is the
relay read of RealSrtSupervisorTests: the product relay against the relay
tests' 127.0.0.1 fixture upstream, the non-AI relay probe inside the pinned
srt, and the product supervisor and collect. The run directory is OUT_DIR/run
instead of a temporary one, so the host can take run/result as a tar stream.
Prints the manifest status. No model, token, GitHub host or OSS code.
"""

import json
from pathlib import Path
import sys

sys.path.insert(0, ".")
import tests.test_oss_research_worker_supervisor as supervisor  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1]).resolve()
    case = supervisor.RealSrtSupervisorTests("test_relay_read_through_srt_and_the_rest_refused")
    supervisor.RealSrtSupervisorTests.setUpClass()
    try:
        case.setUp()
        case.run_dir = out / "run"
        mock_port = case.upstream.server_address[1]
        succeeded, manifest, _ = case.supervise(
            lambda ports: ["read", str(ports["relay"]), str(mock_port)], seconds=60)
        case.doCleanups()
    finally:
        supervisor.RealSrtSupervisorTests.tearDownClass()
    print(json.dumps({"succeeded": succeeded, "status": manifest["status"],
                      "failures": manifest["failures"], "problems": manifest["problems"]}))
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
