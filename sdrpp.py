"""Launch and tune the laptop's SDR++ receiver over its local Rigctl module."""

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

    def __init__(self):
        self._lock = threading.Lock()
        self._process = None

    def _tune_running(self, port, hz):
        address = ("127.0.0.1", port)
        probe = socket.create_connection(address, timeout=0.8)
        try:
            with probe, probe.makefile("rb") as response:
                probe.sendall(b"\\dump_state\n")
                expected = (b"0", b"2", b"1", self._RX_RANGE, self._RANGE_END, self._RANGE_END)
                if not all(response.readline(160).strip() == line for line in expected):
                    raise ReceiverError("The local Rigctl service did not identify as a supported SDR++ receiver.")
            with socket.create_connection(address, timeout=0.8) as connection:
                with connection.makefile("rb") as response:
                    connection.sendall(f"F {hz}\n".encode("ascii"))
                    if response.readline(32).strip() != b"RPRT 0":
                        raise ReceiverError("SDR++ rejected that frequency.")
                    connection.sendall(b"f\n")
                    if response.readline(32).strip() != str(hz).encode("ascii"):
                        raise ReceiverError("SDR++ did not confirm the frequency. Enable tuning in its Rigctl module.")
        except OSError as exc:
            raise ReceiverError("SDR++ Rigctl stopped responding. Check the local module and try again.") from exc

    def tune(self, hz, config):
        if not valid_frequency(hz):
            raise ReceiverError("Enter a valid receive frequency in Hz.")
        port = config.get("sdrpp_rigctl_port", 4532)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ReceiverError("Set sdrpp_rigctl_port to the local Rigctl port in config.json.")
        if not self._lock.acquire(blocking=False):
            raise ReceiverError("A receiver request is already running. Try again in a moment.")
        try:
            try:
                self._tune_running(port, hz)
                return
            except ConnectionRefusedError:
                pass
            except OSError as exc:
                raise ReceiverError("Could not connect to the local SDR++ Rigctl service.") from exc

            if self._process is None or self._process.poll() is not None:
                executable = config.get("sdrpp_executable", "")
                if (not isinstance(executable, str) or not os.path.isabs(executable)
                        or os.path.basename(executable).lower() not in {"sdrpp.exe", "sdrpp"}
                        or not os.path.isfile(executable)):
                    raise ReceiverError("Start SDR++ with Rigctl enabled, or set its absolute sdrpp_executable path in config.json.")
                try:
                    self._process = subprocess.Popen(
                        [executable], cwd=os.path.dirname(executable),
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                except OSError as exc:
                    raise ReceiverError("Could not open SDR++. Check its executable path on the laptop.") from exc

            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                time.sleep(0.25)
                try:
                    self._tune_running(port, hz)
                    return
                except ConnectionRefusedError:
                    if self._process.poll() is not None:
                        raise ReceiverError("SDR++ closed before Rigctl started. Open it on the laptop and check its module.")
                except OSError as exc:
                    raise ReceiverError("Could not connect to the local SDR++ Rigctl service.") from exc
            raise ReceiverError("SDR++ started, but Rigctl is not listening. Enable Listen on startup in SDR++ and try again.")
        finally:
            self._lock.release()
