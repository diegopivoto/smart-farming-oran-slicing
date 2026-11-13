#!/usr/bin/env python3
"""Freeze read-only evidence of the UN3GPP BS MAC repository before patching it."""

import argparse
import json
import time
from pathlib import Path, PurePosixPath

from smartfarm.inventory.runner import LocalSshCommandRunner
from smartfarm.un3gpp.mac_audit import MacAuditCollector


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="smartfarm-pc2", help="SSH alias of the BS host")
    parser.add_argument("--repo", type=PurePosixPath, default=PurePosixPath("/home/inatel-crr/src/inatel5g_range_mac"))
    parser.add_argument("--output", type=Path, required=True, help="maintenance directory for the JSON evidence")
    arguments = parser.parse_args()
    audit = MacAuditCollector(LocalSshCommandRunner()).collect(arguments.host, arguments.repo)
    arguments.output.mkdir(parents=True, exist_ok=True)
    path = arguments.output / f"{arguments.host}-mac-audit.json"
    document = {"collected_at_ns": time.time_ns(), **audit.model_dump(mode="json")}
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{path}: branch={audit.branch} head={audit.head} dirty={len(audit.status_short)} "
          f"control_hits={len(audit.control_hits)} ipc_hits={len(audit.ipc_hits)} errors={len(audit.errors)}")
    return 0 if not audit.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
