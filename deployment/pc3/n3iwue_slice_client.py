#!/usr/bin/env python3
"""PC3 helper: one command to the N3IWUE slice listener (127.0.0.1:9600), reply printed as one line.

    n3iwue_slice_client.py status        -> STATUS slice=2 sst=1 sd=000002 pdu_session_id=2 ... status=accepted
    n3iwue_slice_client.py slice 2       -> OK requested

Invoked by the PC1 orchestrator over SSH (key auth); the listener itself stays on loopback.
"""

import socket
import sys

LISTENER = ("127.0.0.1", 9600)


def main() -> int:
    if sys.argv[1:] == ["status"]:
        command = "STATUS"
    elif len(sys.argv) == 3 and sys.argv[1] == "slice" and sys.argv[2].isdigit() and 1 <= int(sys.argv[2]) <= 9:
        command = f"SLICE {int(sys.argv[2])}"
    else:
        print("ERROR usage: status | slice <1-9>")
        return 2
    try:
        with socket.create_connection(LISTENER, timeout=5) as conn:
            conn.sendall(command.encode() + b"\n")
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                reply += chunk
    except OSError as error:
        print(f"ERROR listener unreachable: {error}")
        return 1
    print(reply.decode(errors="replace").strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
