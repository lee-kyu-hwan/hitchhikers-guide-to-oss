"""This machine's settings the analysis bundle reads (#290).

Each one can be overridden by the environment. Unset or empty keeps the default below, which is the value
the bundle used before these settings were split out, so nothing changes when none is set.

  OSS_RESEARCH_VM_ROOT       host work root of the Lima VM: home/, lima/, tool/bin/limactl, logs/ (run evidence)
  OSS_RESEARCH_RECORDS_ROOT  the dotfiles docs/research directory holding the dated run records the tests read

harness/analysis_call.sh reads OSS_RESEARCH_VM_ROOT with the same default, and OSS_RESEARCH_VM_NAME (the Lima
instance, default oss-research-debian) too.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_VM_ROOT = Path("/Users/lee-kyu-hwan/.oss-research-vm")
DEFAULT_RECORDS_ROOT = Path(__file__).resolve().parents[4] / "docs/research"

VM_ROOT = Path(os.environ.get("OSS_RESEARCH_VM_ROOT") or DEFAULT_VM_ROOT)
VM_LOGS = VM_ROOT / "logs"
RECORDS_ROOT = Path(os.environ.get("OSS_RESEARCH_RECORDS_ROOT") or DEFAULT_RECORDS_ROOT)
