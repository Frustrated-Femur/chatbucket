"""ChatBucket Manager

A lightweight cross-platform launcher/supervisor for ChatBucket.

Goals:
- Start / stop / restart ChatBucket without terminal babysitting
- Show current local runtime state
- Show host claim from state/host-state.json if present
- Optionally poll the running ChatBucket admin endpoint
- Stay tiny: standard library only, with optional psutil for better process-tree kills

Default behavior:
- Linux: use ./scripts/chatbucket-start.sh if present, otherwise python3 main.py
- Windows: use python main.py

Optional dependency:
- psutil (recommended, not required) for stopping process trees more reliably

No icons, no fonts, no Electron circus.
"""

from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence
from urllib.error import URLError, HTTPError
from urllib.request import urlopen, Request

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    from tkinter.scrolledtext import ScrolledText
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Tkinter is missing. Install the OS package that provides Tk first."
    ) from exc


APP_NAME = "ChatBucket Manager"
REFRESH_MS = 2000
DEFAULT_PORT = 5000
DEFAULT_ADMIN_URL = f"http://127.0.0.1:{DEFAULT_PORT}/admin/status"
DEFAULT_BASE_URL = f"http://127.0.0.1:{DEFAULT_PORT}/"


@dataclass
class RuntimeInfo:
    running: bool = False
    pid: Optional[int] = None
    source: str = "off"
    host_name: str = "-"
    host_state_raw: str = ""
    admin_state_raw: str = ""
    tailnet_state: str = "unknown"
    note: str = ""


class ChatBucketManager(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.minsize(860, 560)

        self.repo_root = Path(__file__).resolve().parent
        self.pidfile = self.repo_root / ".chatbucket-manager.pid"
        self.host_state_file = self.repo_root / "state" / "host-state.json"
        self.log_file = self.repo_root / "logs" / "chatbucket.log"

        self.proc: Optional[subprocess.Popen] = None
        self.proc_lock = threading.Lock()
        self.last_status = RuntimeInfo()

        self._build_ui()
        self._load_config()
        self.after(300, self.refresh_all)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=12)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        self.title_label = ttk.Label(header, text=APP_NAME, font=("TkDefaultFont", 16, "bold"))
        self.title_label.grid(row=0, column=0, sticky="w")

        self.status_badge = ttk.Label(header, text="OFF", padding=(10, 4))
        self.status_badge.grid(row=0, column=1, sticky="e")

        main = ttk.Frame(self, padding=(12, 0, 12, 12))
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        # Left panel: controls
        controls = ttk.LabelFrame(main, text="Control", padding=12)
        controls.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 8), pady=(0, 8))
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Launch command:").grid(row=0, column=0, sticky="w", pady=4)
        self.command_var = tk.StringVar()
        self.command_entry = ttk.Entry(controls, textvariable=self.command_var)
        self.command_entry.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(controls, text="Working dir:").grid(row=1, column=0, sticky="w", pady=4)
        self.workdir_var = tk.StringVar(value=str(self.repo_root))
        ttk.Entry(controls, textvariable=self.workdir_var).grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(controls, text="Admin URL:").grid(row=2, column=0, sticky="w", pady=4)
        self.admin_url_var = tk.StringVar(value=DEFAULT_ADMIN_URL)
        ttk.Entry(controls, textvariable=self.admin_url_var).grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(controls, text="Base URL:").grid(row=3, column=0, sticky="w", pady=4)
        self.base_url_var = tk.StringVar(value=DEFAULT_BASE_URL)
        ttk.Entry(controls, textvariable=self.base_url_var).grid(row=3, column=1, sticky="ew", pady=4)

        self.autostart_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Auto-restart on crash", variable=self.autostart_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 4)
        )

        btns = ttk.Frame(controls)
        btns.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 6))
        for i in range(4):
            btns.columnconfigure(i, weight=1)

        ttk.Button(btns, text="Start", command=self.start_chatbucket).grid(row=0, column=0, sticky="ew", padx=3)
        ttk.Button(btns, text="Stop", command=self.stop_chatbucket).grid(row=0, column=1, sticky="ew", padx=3)
        ttk.Button(btns, text="Restart", command=self.restart_chatbucket).grid(row=0, column=2, sticky="ew", padx=3)
        ttk.Button(btns, text="Open", command=self.open_dashboard).grid(row=0, column=3, sticky="ew", padx=3)

        btns2 = ttk.Frame(controls)
        btns2.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        for i in range(3):
            btns2.columnconfigure(i, weight=1)
        ttk.Button(btns2, text="Open Repo", command=self.open_repo).grid(row=0, column=0, sticky="ew", padx=3)
        ttk.Button(btns2, text="Open State", command=self.open_state_folder).grid(row=0, column=1, sticky="ew", padx=3)
        ttk.Button(btns2, text="Reload", command=self.refresh_all).grid(row=0, column=2, sticky="ew", padx=3)

        # Right panel: status & logs
        status_box = ttk.LabelFrame(main, text="Status", padding=12)
        status_box.grid(row=0, column=1, sticky="nsew", pady=(0, 8))
        status_box.columnconfigure(1, weight=1)

        self.pid_value = tk.StringVar(value="-")
        self.role_value = tk.StringVar(value="-")
        self.host_value = tk.StringVar(value="-")
        self.tailnet_value = tk.StringVar(value="unknown")
        self.note_value = tk.StringVar(value="")
        self.state_value = tk.StringVar(value="off")

        rows = [
            ("Process", self.pid_value),
            ("Role", self.role_value),
            ("Host", self.host_value),
            ("Tailscale", self.tailnet_value),
            ("Note", self.note_value),
        ]
        for r, (label, var) in enumerate(rows):
            ttk.Label(status_box, text=label + ":").grid(row=r, column=0, sticky="w", pady=3)
            ttk.Label(status_box, textvariable=var).grid(row=r, column=1, sticky="w", pady=3)

        logs_box = ttk.LabelFrame(main, text="Logs", padding=12)
        logs_box.grid(row=1, column=1, sticky="nsew")
        logs_box.rowconfigure(0, weight=1)
        logs_box.columnconfigure(0, weight=1)

        self.log_text = ScrolledText(logs_box, height=16, wrap="none")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

        footer = ttk.Frame(self, padding=(12, 0, 12, 10))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.footer_var = tk.StringVar(value="Ready.")
        ttk.Label(footer, textvariable=self.footer_var).grid(row=0, column=0, sticky="w")

    # ---------- Config ----------
    def _load_config(self) -> None:
        if platform.system().lower().startswith("win"):
            default_cmd = f'"{sys.executable}" main.py'
        else:
            start_sh = self.repo_root / "scripts" / "chatbucket-start.sh"
            if start_sh.exists():
                default_cmd = f'bash "{start_sh}"'
            else:
                default_cmd = f'"{sys.executable}" main.py'
        self.command_var.set(default_cmd)

    def _parse_command(self, cmd: str) -> Sequence[str]:
        # Very small shell-like parser; sufficient for quoted paths.
        import shlex

        return shlex.split(cmd, posix=not platform.system().lower().startswith("win"))

    # ---------- Process control ----------
    def _is_process_alive(self, pid: int) -> bool:
        try:
            if pid <= 0:
                return False
            if platform.system().lower().startswith("win"):
                # os.kill(pid, 0) works on Windows for existence checks.
                os.kill(pid, 0)
            else:
                os.kill(pid, 0)
            return True
        except Exception:
            return False

    def _terminate_process(self, proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is not None:
                return

            if os.name == "nt":
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                    return
                except Exception:
                    pass
                try:
                    proc.kill()
                except Exception:
                    pass
                return

            # POSIX: try process group first.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
                return
            except Exception:
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        except Exception as exc:
            self._set_footer(f"Stop failed: {exc}")

    def start_chatbucket(self) -> None:
        with self.proc_lock:
            if self.proc and self.proc.poll() is None:
                self._set_footer("Already running.")
                return

            cmd = self._parse_command(self.command_var.get().strip())
            if not cmd:
                messagebox.showerror("ChatBucket Manager", "Launch command is empty.")
                return

            cwd = self.workdir_var.get().strip() or str(self.repo_root)
            if not Path(cwd).exists():
                messagebox.showerror("ChatBucket Manager", f"Working directory not found:\n{cwd}")
                return

            creationflags = 0
            start_new_session = False
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            else:
                start_new_session = True

            try:
                self.proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    creationflags=creationflags,
                    start_new_session=start_new_session,
                )
                self._write_pidfile(self.proc.pid)
                self._set_footer(f"Started: PID {self.proc.pid}")
            except FileNotFoundError as exc:
                messagebox.showerror("ChatBucket Manager", f"Command not found:\n{exc}")
            except Exception as exc:
                messagebox.showerror("ChatBucket Manager", f"Start failed:\n{exc}")

        self.refresh_all()

    def stop_chatbucket(self) -> None:
        with self.proc_lock:
            proc = self.proc
            self.proc = None

        if proc is not None:
            self._terminate_process(proc)
        else:
            pid = self._read_pidfile()
            if pid and self._is_process_alive(pid):
                try:
                    if os.name == "nt":
                        os.kill(pid, signal.SIGTERM)
                    else:
                        os.killpg(pid, signal.SIGTERM)
                except Exception:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except Exception:
                        pass
        self._clear_pidfile_if_owned()
        self._set_footer("Stopped.")
        self.refresh_all()

    def restart_chatbucket(self) -> None:
        self.stop_chatbucket()
        time.sleep(0.4)
        self.start_chatbucket()

    # ---------- File helpers ----------
    def _write_pidfile(self, pid: int) -> None:
        try:
            self.pidfile.write_text(str(pid), encoding="utf-8")
        except Exception:
            pass

    def _read_pidfile(self) -> Optional[int]:
        try:
            raw = self.pidfile.read_text(encoding="utf-8").strip()
            return int(raw)
        except Exception:
            return None

    def _clear_pidfile_if_owned(self) -> None:
        try:
            if self.pidfile.exists():
                self.pidfile.unlink()
        except Exception:
            pass

    def _read_host_state(self) -> tuple[str, str]:
        try:
            raw = self.host_state_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            host = str(data.get("machine") or data.get("host") or data.get("name") or "-")
            return host, raw
        except Exception:
            return "-", ""

    def _read_tailnet_status(self) -> str:
        # Lightweight: ask the tailscale CLI only if it exists.
        try:
            from shutil import which
            if which("tailscale") is None:
                return "tailscale CLI not found"
            proc = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=4,
            )
            if proc.returncode != 0:
                return "tailscale status failed"
            try:
                data = json.loads(proc.stdout)
                backend = data.get("BackendState") or data.get("backendState") or "connected"
                return str(backend)
            except Exception:
                return "connected"
        except Exception:
            return "unknown"

    def _fetch_admin_status(self) -> str:
        url = self.admin_url_var.get().strip()
        if not url:
            return ""
        try:
            req = Request(url, headers={"User-Agent": APP_NAME})
            with urlopen(req, timeout=2.5) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
                return payload
        except (HTTPError, URLError, TimeoutError, OSError):
            return ""
        except Exception:
            return ""

    def _read_recent_logs(self, max_lines: int = 120) -> str:
        try:
            if not self.log_file.exists():
                return "(log file not found yet)\n"
            lines = self.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-max_lines:]
            return "\n".join(tail) + ("\n" if tail else "")
        except Exception as exc:
            return f"(cannot read logs: {exc})\n"

    # ---------- Status update ----------
    def refresh_all(self) -> None:
        running = False
        pid = None
        source = "off"
        note = ""

        with self.proc_lock:
            proc = self.proc

        if proc is not None and proc.poll() is None:
            running = True
            pid = proc.pid
            source = "local launcher"
        else:
            file_pid = self._read_pidfile()
            if file_pid and self._is_process_alive(file_pid):
                running = True
                pid = file_pid
                source = "pidfile"
            else:
                self._clear_pidfile_if_owned()

        host_name, host_raw = self._read_host_state()
        admin_raw = self._fetch_admin_status() if running else ""
        tailnet_state = self._read_tailnet_status() if running else "unknown"

        # Try to derive a tiny bit of meaning from admin payload.
        if admin_raw:
            if "host" in admin_raw.lower():
                note = "admin endpoint reachable"
            else:
                note = "running"
        else:
            note = "off" if not running else "running"

        info = RuntimeInfo(
            running=running,
            pid=pid,
            source=source,
            host_name=host_name,
            host_state_raw=host_raw,
            admin_state_raw=admin_raw,
            tailnet_state=tailnet_state,
            note=note,
        )
        self.last_status = info
        self._render_status(info)
        self._render_logs(info)
        self.after(REFRESH_MS, self.refresh_all)

    def _render_status(self, info: RuntimeInfo) -> None:
        self.status_badge.configure(text="RUNNING" if info.running else "OFF")
        self.pid_value.set(str(info.pid) if info.pid else "-")
        self.role_value.set("unknown" if info.running else "off")
        self.host_value.set(info.host_name)
        self.tailnet_value.set(info.tailnet_state)
        self.note_value.set(info.note)

        if info.running:
            self.footer_var.set(f"Running via {info.source}.")
        else:
            self.footer_var.set("Not running.")

    def _render_logs(self, info: RuntimeInfo) -> None:
        tail = self._read_recent_logs()
        if info.host_state_raw:
            host_block = f"\n--- host-state.json ---\n{info.host_state_raw}\n"
        else:
            host_block = "\n--- host-state.json ---\n(not found / empty)\n"

        admin_block = ""
        if info.admin_state_raw:
            admin_block = f"\n--- admin status ---\n{info.admin_state_raw}\n"

        text = tail + host_block + admin_block
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.insert(tk.END, text)
        self.log_text.configure(state="disabled")

    # ---------- Launch helpers ----------
    def open_dashboard(self) -> None:
        webbrowser.open(self.base_url_var.get().strip() or DEFAULT_BASE_URL)

    def open_repo(self) -> None:
        path = self.workdir_var.get().strip() or str(self.repo_root)
        if platform.system().lower().startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def open_state_folder(self) -> None:
        path = str(self.host_state_file.parent)
        if platform.system().lower().startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _set_footer(self, text: str) -> None:
        self.footer_var.set(text)


def main() -> int:
    app = ChatBucketManager()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
