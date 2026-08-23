"""
main.py — ChatBucket startup entrypoint (front-door architecture).

Under the front-door architecture, this file no longer runs arbitration
and execv's itself into either server.py or doorman.py. Instead:

  1. Detect / read machine name.
  2. Hand off to front_door.run(), which:
     - binds public port 5000 for its entire lifetime,
     - runs arbitration repeatedly as needed (not just once),
     - spawns/supervises server.py as a child on 127.0.0.1:5001 when hosting,
     - proxies 5000 -> 5001 for local traffic,
     - serves redirect responses (absorbed doorman.py logic) when client,
     - exposes a loopback status/control endpoint for the Rust Manager.

The name of this file is preserved deliberately: the Windows Task
Scheduler XML and the Arch launcher script both invoke `python3 main.py
[<name>]`. Keeping the entrypoint name identical means neither of those
files has to change to adopt the front-door design — only main.py's
behavior does.

Usage:
    python3 main.py [my-machine-name]

If my-machine-name is omitted, we auto-detect (fine for Arch where the
hostname naturally matches; explicitly required on Windows where the
system hostname almost never matches the tidy names used in
host-state.json / Tailscale DNS).
"""

import os
import platform
import socket
import sys

import front_door


def normalize(name: str) -> str:
    return name.split(".")[0].strip().lower()


def get_machine_name() -> str:
    name = (
        socket.gethostname()
        or os.getenv("COMPUTERNAME")
        or os.getenv("HOSTNAME")
        or platform.node()
    )
    return normalize(name)


def main():
    if len(sys.argv) > 2:
        print("Usage: python3 main.py [my-machine-name]")
        sys.exit(1)
    my_name = normalize(sys.argv[1]) if len(sys.argv) == 2 else get_machine_name()
    print(f"[main] Detected machine name: {my_name}")
    print(f"[main] Starting front door (persistent supervisor)")
    exit_code = front_door.run(my_name)
    sys.exit(exit_code or 0)


if __name__ == "__main__":
    main()
