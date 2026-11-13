#!/usr/bin/env python3
"""Render an OAI runtime config by changing only the E2 management address."""

import argparse
import ipaddress
import os
import re
import tempfile
from pathlib import Path


class RenderError(RuntimeError):
    """Raised when the active config cannot be transformed unambiguously."""


E2_BLOCK = re.compile(
    r"(?ms)(?P<open>^[ \t]*e2_agent[ \t]*=[ \t]*\{)(?P<body>.*?)(?P<close>^[ \t]*\};)"
)
NEAR_RIC_FIELD = re.compile(
    r'(?m)^(?P<prefix>[ \t]*near_ric_ip_addr[ \t]*=[ \t]*)"[^"\n]*"(?P<suffix>[ \t]*;)'
)


def render(source: Path, output: Path, near_ric_ip_addr: str) -> None:
    """Write a runtime copy while preserving every field except the RIC IP."""

    address = ipaddress.ip_address(near_ric_ip_addr)
    if address.version != 4:
        raise RenderError("near-RT RIC address must be IPv4")
    original = source.read_text(encoding="utf-8")
    blocks = list(E2_BLOCK.finditer(original))
    if len(blocks) != 1:
        raise RenderError(f"expected one e2_agent block, found {len(blocks)}")
    block = blocks[0]
    body, replacements = NEAR_RIC_FIELD.subn(
        rf'\g<prefix>"{address}"\g<suffix>', block.group("body")
    )
    if replacements != 1:
        raise RenderError(f"expected one near_ric_ip_addr field, found {replacements}")
    rendered_block = block.group("open") + body + block.group("close")
    rendered = original[: block.start()] + rendered_block + original[block.end() :]
    _atomic_text(output, rendered, source.stat().st_mode & 0o777)


def _atomic_text(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--near-ric-ip", default="192.168.56.101")
    arguments = parser.parse_args()
    render(arguments.source.resolve(), arguments.output.resolve(), arguments.near_ric_ip)
    print(arguments.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
