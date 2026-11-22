"""Launch and tune the laptop's SDR++ receiver over its local Rigctl module."""

import errno
import os
import socket
import subprocess
import threading
import time


class ReceiverError(Exception):
    """An actionable SDR++ setup or tuning error."""


def valid_frequency(hz):
    return type(hz) is int and 1_000 <= hz <= 10_000_000_000


class Receiver:
    # SDR++ identifies its emulated rig as model 2 and advertises a
    # distinct receive-only range. Verify that before sending a write.
    _RX_RANGE = b"0.000000 10000000000.000000 0x2ef -1 -1 0x1 0x0"
    _RANGE_END = b"0 0 0 0 0 0 0"
    # The SDR++ dump_state response is 34 lines with no terminator
    # (verified in the 1.0.4 release and current upstream source).
    _STATE_LINES = 34
    _STARTUP_SECONDS = 12

    def __init__(self):
        self._lock = threading.Lock()
        self._process = None

    @staticmethod
    def _connect(port):
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=1.5)
        except ConnectionRefusedError as refused:
            # SDR++ defaults to localhost, which may bind ::1 on Windows.
            try:
                return socket.create_connection(("::1", port), timeout=1.5)
            except OSError as exc:
                if isinstance(exc, ConnectionRefusedError) or exc.errno in {
                    errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL, errno.ENETUNREACH,
                } or getattr(exc, "winerror", None) in {10043, 10047, 10049, 10051}:
                    raise refused from exc
                raise

    def _frequency(self, port, hz=None):
        connection = self._connect(port)
        try:
            with connection, connection.makefile("rb") as response:
                deadline = time.monotonic() + 3

                def line():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    connection.settimeout(remaining)
                    raw = response.readline(160)
                    if not raw.endswith(b"\n"):
                        raise ReceiverError("SDR++ Rigctl returned an incomplete response.")
                    return raw.strip()

                connection.sendall(b"\\dump_state\n")
                expected = (b"0", b"2", b"1", self._RX_RANGE, self._RANGE_END, self._RANGE_END)
                if tuple(line() for _ in expected) != expected:
                    raise ReceiverError("The local Rigctl service did not identify as a supported SDR++ receiver.")
                for _ in range(self._STATE_LINES - len(expected)):
                    line()
                if hz is not None:
                    connection.sendall(f"F {hz}\n".encode("ascii"))
                    if line() != b"RPRT 0":
                        raise ReceiverError("SDR++ rejected that frequency.")
                for _ in range(10 if hz is not None else 1):
                    connection.sendall(b"f\n")
                    value = line()
                    if not value.isdigit() or len(value) > 12:
                        raise ReceiverError("SDR++ Rigctl returned an invalid frequency.")
                    actual = int(value)
                    if hz is None or abs(actual - hz) <= 1:
                        return actual
                    time.sleep(0.1)
                raise ReceiverError(
                    "SDR++ did not confirm the frequency. Enable tuning and select a VFO in the Rigctl Server module."
                )
        except OSError as exc:
            raise ReceiverError("SDR++ Rigctl stopped responding. Close any other Rigctl client and try again.") from exc

    @staticmethod
    def _port(config):
        port = config.get("sdrpp_rigctl_port", 4532)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ReceiverError("Set sdrpp_rigctl_port to the local Rigctl port in config.json.")
        return port

    @staticmethod
    def _launch_args(config):
        executable = config.get("sdrpp_executable", "")
        if (not isinstance(executable, str) or not os.path.isabs(executable)
                or os.path.basename(executable).lower() not in {"sdrpp.exe", "sdrpp"}
                or not os.path.isfile(executable)):
            raise ReceiverError("Set sdrpp_executable to the absolute path of sdrpp.exe in the hub's config.json.")
        command = [executable]
        if "sdrpp_root" in config:
            root = config["sdrpp_root"]
            if not isinstance(root, str) or not os.path.isabs(root) or not os.path.isdir(root):
                raise ReceiverError("Set sdrpp_root to the existing absolute SDR++ profile directory.")
            command.extend(["--root", root])
        return command

    def check(self, config):
        port = self._port(config)
        if not self._lock.acquire(blocking=False):
            raise ReceiverError("A receiver request is already running. Try again in a moment.")
        try:
            return self._frequency(port)
        except ConnectionRefusedError as exc:
            try:
                self._launch_args(config)
                launch = "The hub has a launch path configured."
            except ReceiverError as issue:
                launch = str(issue)
            raise ReceiverError(f"No SDR++ Rigctl listener on local port {port}. {launch} "
                                "Start SDR++ and enable Listen on startup in Rigctl Server.") from exc
        except OSError as exc:
            raise ReceiverError(f"Could not connect to SDR++ Rigctl on local port {port}.") from exc
        finally:
            self._lock.release()

    @staticmethod
    def _already_running():
        """True if an sdrpp.exe process is already running (Windows).

        Uses tasklist (stdlib subprocess only). Returns None when the
        check itself fails, so the caller can fall back to launching.
        """
        if os.name != "nt":
            return None
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq sdrpp.exe", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        return "sdrpp.exe" in out.stdout.lower()

    def tune(self, hz, config):
        if not valid_frequency(hz):
            raise ReceiverError("Enter a valid receive frequency in Hz.")
        port = self._port(config)
        if not self._lock.acquire(blocking=False):
            raise ReceiverError("A receiver request is already running. Try again in a moment.")
        try:
            try:
                return self._frequency(port, hz)
            except ConnectionRefusedError:
                pass
            except OSError as exc:
                raise ReceiverError("Could not connect to the local SDR++ Rigctl service.") from exc

            if self._process is None or self._process.poll() is not None:
                # Do not launch a second SDR++ if one is already
                # running with Rigctl off — a second instance fights
                # the first for the audio device and the Rigctl port.
                # (Skipped when the hub itself already launched one
                # and it is still starting up.)
                if self._already_running():
                    raise ReceiverError(
                        "SDR++ is already running — enable Rigctl Server in it "
                        "(Rigctl Server module, Listen on startup) instead of "
                        "opening a second copy."
                    )
                command = self._launch_args(config)
                try:
                    self._process = subprocess.Popen(
                        command, cwd=os.path.dirname(command[0]),
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                except OSError as exc:
                    raise ReceiverError("Could not open SDR++. Check its executable path on the laptop.") from exc

            deadline = time.monotonic() + self._STARTUP_SECONDS
            while time.monotonic() < deadline:
                time.sleep(0.25)
                try:
                    return self._frequency(port, hz)
                except ConnectionRefusedError:
                    exit_code = self._process.poll()
                    if exit_code is not None:
                        raise ReceiverError(f"SDR++ exited with code {exit_code} before Rigctl started. "
                                            "Open it directly on the laptop to see its startup message.")
                except OSError as exc:
                    raise ReceiverError("Could not connect to the local SDR++ Rigctl service.") from exc
            raise ReceiverError(f"SDR++ started, but Rigctl did not listen on local port {port}. "
                                "Check the host, port, and Listen on startup in its SDR++ profile.")
        finally:
            self._lock.release()
