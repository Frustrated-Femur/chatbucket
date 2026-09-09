"""
main.py — ChatBucket dedicated-server entrypoint.

This entrypoint replaces the old front-door / arbitration / doorman lifecycle.
The Oracle VM is now a permanent 24/7 dedicated ChatBucket server with a
public IP, so there is nothing to arbitrate: this machine is ALWAYS the host.

Two run modes are supported:

  1. Direct (dev / manual):
         python3 main.py
     → runs the Flask/flask-sock app in-process via Werkzeug's threaded
       server on 0.0.0.0:5000. Convenient, but the Werkzeug dev server is
       NOT suitable for the public Internet. Use only for local testing.

  2. Production (systemd):
         gunicorn -k gevent -w 1 --threads 16 -b 0.0.0.0:5000 \
                  --graceful-timeout 5 server:app
     → the recommended way; see systemd/chatbucket.service.

The old multi-host machinery (arbitration.py, host_state.py, doorman.py,
front_door.py, tcp_proxy.py, manager_config.py) has been preserved under
legacy/ but is NOT executed on the dedicated server. See docs/MIGRATION.md.

Firebase / FCM credentials continue to be loaded by push_notifications.py
from either an environment variable or a file outside source control —
never checked into the repo. See .env.example.
"""

import os
import sys

import server


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def main():
    host = os.environ.get("CHATBUCKET_HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("CHATBUCKET_PORT", "5000"))
    except ValueError:
        print("[main] CHATBUCKET_PORT must be an integer", file=sys.stderr)
        sys.exit(2)

    debug = _env_bool("CHATBUCKET_DEBUG", False)

    print(f"[main] ChatBucket dedicated server — binding {host}:{port}")
    print("[main] NOTE: this is Werkzeug's threaded dev server. For the "
          "public Internet, run under gunicorn+gevent. See "
          "systemd/chatbucket.service.")

    # threaded=True lets flask_sock hijack sockets for WebSockets while the
    # HTTP side stays concurrent. Matches the transport model gunicorn+gevent
    # uses in production.
    server.app.run(host=host, port=port, threaded=True, debug=debug,
                   use_reloader=False)


if __name__ == "__main__":
    main()
