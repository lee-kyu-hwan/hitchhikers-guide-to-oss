"""This machine's settings the analysis bundle reads (#290).

Each one can be overridden by the environment. Unset or empty keeps the default below.

  OSS_RESEARCH_VM_ROOT       host work root of the Lima VM: home/, lima/, tool/bin/limactl, logs/ (run evidence);
                             default ~/.oss-research-vm (HOME)
  OSS_RESEARCH_RECORDS_ROOT  the dated run records the tests read. In dotfiles they are its docs/research; this
                             repository holds a byte-identical copy of the three files the tests need under
                             tests/fixtures/dotfiles_records (the default). Point it at a dotfiles checkout's
                             docs/research to read the originals.

harness/analysis_call.sh reads OSS_RESEARCH_VM_ROOT with the same default, and OSS_RESEARCH_VM_NAME (the Lima
instance, default oss-research-debian) too.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_VM_ROOT = Path.home() / ".oss-research-vm"
DEFAULT_RECORDS_ROOT = Path(__file__).resolve().parents[3] / "tests/fixtures/dotfiles_records"

VM_ROOT = Path(os.environ.get("OSS_RESEARCH_VM_ROOT") or DEFAULT_VM_ROOT)
VM_LOGS = VM_ROOT / "logs"
RECORDS_ROOT = Path(os.environ.get("OSS_RESEARCH_RECORDS_ROOT") or DEFAULT_RECORDS_ROOT)
