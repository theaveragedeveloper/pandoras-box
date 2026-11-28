"""MeshCore companion-radio link for the hub (family check-in feed).

The hub talks to the L1 Pro over USB serial on the Full (USB + BLE)
companion build, using the meshcore library (PyPI meshcore 2.1.24).
The library is asyncio-based; the hub is a sync threaded HTTP
server, so this link runs in its own daemon thread with its own
asyncio event loop and keeps a snapshot the endpoint serves. The
endpoint never blocks on the radio: snapshot() waits at most a few
seconds for the first poll, then serves the cached state.

A radio on the BLE-only build presents no USB serial at all, so the
link reports connected:false with state "no_port" and a reason that
names the reflash — it never invents a radio, a contact, or a
check-in. the owner's spouse has no radio, so family check-ins are the owner-only
until a second companion radio is added.

The Full build is "Seeed Studio Wio Tracker L1 Pro Companion Radio
Full" (USB and BLE Companion interfaces on nRF52, so the phone app
keeps working over BLE while the laptop talks over USB): flash it
from the MeshCore web flasher (flasher.meshcore.io) or drag the UF2
onto the TRACKER L1 drive after a double-press reset.
"""

import asyncio
import copy
import threading
import time
from datetime import datetime, timezone

# How long the endpoint waits for the link's first poll before
# serving the cached (possibly "starting") state.
FIRST_POLL_WAIT = 3.0
# Per-command timeout on the serial link: the radio is local, so a
# command that takes longer than this is a dead link.
COMMAND_TIMEOUT = 10.0
# How often the link re-reads the radio's contacts while connected.
DEFAULT_POLL_INTERVAL = 60
# How many received messages the snapshot keeps (newest first).
MAX_MESSAGES = 50
# How many recently heard adverts the snapshot keeps (newest first).
MAX_ADVERTS = 10

# States the snapshot's "state" field can hold. The tile switches on
# these for the next-step text; "reason" carries the human detail.
STATE_STARTING = "starting"
STATE_CONNECTED = "connected"
STATE_NO_LIBRARY = "no_library"
STATE_NO_PORT = "no_port"
STATE_NO_ANSWER = "no_answer"
STATE_DISCONNECTED = "disconnected"

_FULL_BUILD = "Companion Radio Full"
_NO_PORT_REASON = (
    "No USB serial device found — plug in the L1 Pro. If it is "
    "already plugged in, it is likely on the Bluetooth-only build, "
    "which has no USB serial: reflash it to the '" + _FULL_BUILD +
    "' build (USB + BLE) so the laptop can talk to it.")


def _event_value(event_type):
    """The string value of an event type, real (EventType enum) or fake."""
    return getattr(event_type, "value", event_type)


def _is_event(event, name):
    """True when the event is of the named type (real EventType enum
    or a fake's plain string)."""
    return event is not None and _event_value(event.type) == name


def _utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _default_factory(port):
    """Connect to a companion radio on a serial port (real library).

    The library opens the port through pyserial-asyncio, which
    asserts DTR on open by default (dsrdtr=False) — the "DTR
    asserted" requirement from the BUNKR sketch, handled by the
    serial layer rather than bespoke code.
    """
    from meshcore import MeshCore
    return await MeshCore.create_serial(port, baudrate=115200)


def _list_ports():
    """USB serial ports on this machine, or None if pyserial is missing."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    return [port.device for port in list_ports.comports()]


class MeshCoreLink:
    """One companion radio, polled in a background thread.

    ``port`` pins the serial port (e.g. "COM4"); None auto-detects
    the first port that answers the companion device query.
    ``client_factory`` is the seam for tests: an async callable
    taking a port and returning a client with the meshcore API
    (commands.send_device_query / send_appstart / get_contacts /
    get_bat, subscribe, start_auto_message_fetching, is_connected,
    disconnect). ``ports`` is an explicit port list for tests; None
    enumerates via pyserial.
    """

    def __init__(self, port=None, poll_interval=DEFAULT_POLL_INTERVAL,
                 client_factory=None, ports=None, log=None):
        self._port = port
        self._poll_interval = max(5, int(poll_interval))
        self._uses_real_library = client_factory is None
        self._factory = client_factory or _default_factory
        self._ports = ports
        self._log = log or (lambda msg: None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._first_attempt = threading.Event()
        self._disconnect_flag = threading.Event()
        self._state = {
            "connected": False,
            "state": STATE_STARTING,
            "reason": "MeshCore link starting — checking for the companion radio.",
            "port": None,
            "radio": None,
            "contacts": [],
            "messages": [],
            "recent_adverts": [],
            "updated_at": None,
        }
        self._thread = None

    def start(self):
        """Start the background thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run,
                                        name="meshcore-link", daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the background thread (best-effort; it is a daemon)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def snapshot(self, wait=None):
        """The current link state (a copy). If wait is given, block
        up to that many seconds for the first connect attempt to
        finish, so the endpoint serves a real answer on first load
        instead of "starting"."""
        self.start()
        if wait:
            self._first_attempt.wait(timeout=wait)
        with self._lock:
            return copy.deepcopy(self._state)

    def _set(self, **fields):
        with self._lock:
            self._state.update(fields)

    def _fail(self, state, reason):
        """A terminal not-connected state: connected:false with a
        reason, never an empty success. Marks the first attempt
        done so snapshot() stops waiting."""
        self._set(connected=False, state=state, reason=reason,
                  port=None, radio=None)
        self._first_attempt.set()

    def _run(self):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._loop_forever())
        except Exception as exc:  # noqa: BLE001 - a dead link thread must not kill the hub
            self._log(f"meshcore link thread failed: {exc}")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _loop_forever(self):
        while not self._stop.is_set():
            self._disconnect_flag.clear()
            client = await self._try_connect()
            if client is not None:
                await self._poll_loop(client)
            if self._stop.is_set():
                break
            # Back off before the next attempt: the radio may be
            # unplugged, or the library may not be installed yet.
            for _ in range(5):
                if self._stop.is_set():
                    break
                await asyncio.sleep(1)

    async def _try_connect(self):
        """One connect attempt: returns a live client, or None."""
        # The library is optional: a hub without it still starts, and
        # the endpoint reports the missing library honestly.
        if self._uses_real_library:
            try:
                from meshcore import MeshCore  # noqa: F401 - presence check only
            except ImportError:
                self._fail(STATE_NO_LIBRARY,
                           "The meshcore library is not installed "
                           "(pip install meshcore) — the hub cannot "
                           "talk to the companion radio.")
                return None
        ports = self._ports if self._ports is not None else _list_ports()
        if ports is None:
            # pyserial missing: without it the hub cannot even list ports.
            self._fail(STATE_NO_PORT,
                       "pyserial is not installed, so the hub cannot "
                       "list USB serial ports.")
            return None
        candidates = [self._port] if self._port else list(ports)
        if not candidates:
            self._fail(STATE_NO_PORT, _NO_PORT_REASON)
            return None
        tried = []
        for port in candidates:
            tried.append(port)
            try:
                client = await asyncio.wait_for(
                    self._factory(port), COMMAND_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - a dead port is not a crash
                self._log(f"meshcore: {port} did not open ({exc})")
                continue
            if client is None:
                continue
            try:
                event = await asyncio.wait_for(
                    client.commands.send_device_query(), COMMAND_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - a silent port is not a crash
                self._log(f"meshcore: {port} did not answer the device query ({exc})")
                await self._close_client(client)
                continue
            if not _is_event(event, "device_info"):
                await self._close_client(client)
                continue
            # This port answers the companion protocol: it is the radio.
            device_info = event.payload if isinstance(event.payload, dict) else {}
            self_info = {}
            try:
                appstart = await asyncio.wait_for(
                    client.commands.send_appstart(), COMMAND_TIMEOUT)
                if _is_event(appstart, "self_info"):
                    self_info = appstart.payload if isinstance(appstart.payload, dict) else {}
            except Exception as exc:  # noqa: BLE001 - the device query already proved the radio
                self._log(f"meshcore: appstart failed ({exc})")
            radio = {
                "name": str(self_info.get("name") or ""),
                "public_key": str(self_info.get("public_key") or ""),
                "model": str(device_info.get("model") or ""),
                "firmware": str(device_info.get("ver") or ""),
                "build": str(device_info.get("fw_build") or ""),
                "frequency_mhz": self_info.get("radio_freq"),
                "sf": self_info.get("radio_sf"),
                "bw": self_info.get("radio_bw"),
                "cr": self_info.get("radio_cr"),
                "battery_pct": await self._read_battery(client),
            }
            self._update_contacts(await self._read_contacts(client))
            if self._uses_real_library:
                from meshcore.events import EventType
                contact_type = EventType.CONTACT_MSG_RECV
                advert_type = EventType.ADVERTISEMENT
                disconnect_type = EventType.DISCONNECTED
            else:
                contact_type = "contact_message"
                advert_type = "advertisement"
                disconnect_type = "disconnected"
            try:
                client.subscribe(contact_type, self._on_contact_message)
                client.subscribe(advert_type, self._on_advertisement)
                client.subscribe(disconnect_type, self._on_disconnect)
            except Exception as exc:  # noqa: BLE001 - a missed subscription must not drop the link
                self._log(f"meshcore: subscribe failed ({exc})")
            try:
                # Auto message fetching pulls messages already stored
                # on the radio the moment it starts (the library
                # issues an immediate get_msg), so check-ins received
                # before the hub started still arrive as
                # contact_message events.
                await asyncio.wait_for(
                    client.start_auto_message_fetching(), COMMAND_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - push messages still arrive without auto-fetch
                self._log(f"meshcore: auto message fetching failed ({exc})")
            self._set(connected=True, state=STATE_CONNECTED,
                      reason="Connected to " + (radio["name"] or "the companion radio")
                             + " over USB (" + str(port) + ").",
                      port=str(port), radio=radio,
                      updated_at=_utcnow_iso())
            self._first_attempt.set()
            return client
        self._fail(STATE_NO_ANSWER,
                   "Tried " + ", ".join(str(p) for p in tried) +
                   " — none answered the MeshCore device query. If the "
                   "L1 Pro is plugged in, it is likely on the "
                   "Bluetooth-only build, which has no USB serial: "
                   "reflash it to the '" + _FULL_BUILD +
                   "' build (USB + BLE).")
        return None

    async def _read_battery(self, client):
        """The radio's battery percent, or None when the read fails.
        A failed battery read must never drop the link."""
        try:
            event = await asyncio.wait_for(
                client.commands.get_bat(), COMMAND_TIMEOUT)
        except Exception:
            return None
        if not _is_event(event, "battery_info"):
            return None
        payload = event.payload if isinstance(event.payload, dict) else {}
        return payload.get("battery")

    async def _read_contacts(self, client):
        """The radio's contacts ({} when it has none), or None when
        the read itself fails — a failed read is a dead link, not an
        empty address book."""
        try:
            event = await asyncio.wait_for(
                client.commands.get_contacts(timeout=5), COMMAND_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - a failed read is a dead link, not a crash
            self._log(f"meshcore: get_contacts failed ({exc})")
            return None
        if not _is_event(event, "contacts"):
            return None
        return event.payload if isinstance(event.payload, dict) else {}

    async def _poll_loop(self, client):
        """Re-read contacts while the radio stays connected."""
        while not self._stop.is_set() and not self._disconnect_flag.is_set():
            try:
                if not client.is_connected:
                    break
            except Exception:
                break
            # Sleep in 1s chunks so stop() is prompt.
            for _ in range(self._poll_interval):
                if self._stop.is_set() or self._disconnect_flag.is_set():
                    break
                await asyncio.sleep(1)
            if self._stop.is_set() or self._disconnect_flag.is_set():
                break
            contacts = await self._read_contacts(client)
            if contacts is None:
                # The contact read failed: the link is dead, not
                # merely an empty address book.
                break
            self._update_contacts(contacts)
            battery = await self._read_battery(client)
            if battery is not None:
                with self._lock:
                    if self._state.get("radio") is not None:
                        self._state["radio"]["battery_pct"] = battery
        # The poll loop ended: the radio is gone (or the hub is stopping).
        await self._close_client(client)
        if not self._stop.is_set():
            self._disconnect_flag.clear()
            self._set(connected=False, state=STATE_DISCONNECTED,
                      reason="The companion radio disconnected — check the USB cable.",
                      port=None, radio=None, contacts=[])
            # Messages stay: they are the received record, not live state.

    def _update_contacts(self, payload):
        contacts = []
        if isinstance(payload, dict):
            for key, contact in payload.items():
                if not isinstance(contact, dict):
                    continue
                contacts.append({
                    "name": str(contact.get("adv_name") or ""),
                    "public_key": str(contact.get("public_key") or key),
                    "last_advert": contact.get("last_advert"),
                    "lat": contact.get("adv_lat"),
                    "lon": contact.get("adv_lon"),
                })
        contacts.sort(key=lambda c: (c.get("last_advert") is None,
                                     -(c.get("last_advert") or 0)))
        with self._lock:
            self._state["contacts"] = contacts
            self._state["updated_at"] = _utcnow_iso()

    def _on_contact_message(self, event):
        """A received direct message is a check-in: who, what, when.

        sender_timestamp is a 4-byte unix epoch: the field width
        itself proves it is seconds (milliseconds would overflow a
        4-byte field within weeks of the epoch), so the tile treats
        it as seconds.
        """
        payload = event.payload if isinstance(event.payload, dict) else {}
        text = payload.get("text") or ""
        if not text:
            return
        prefix = str(payload.get("pubkey_prefix") or "")
        at = payload.get("sender_timestamp")
        with self._lock:
            sender = "Unknown"
            for contact in self._state.get("contacts", []):
                if prefix and str(contact.get("public_key") or "").startswith(prefix):
                    sender = contact.get("name") or "Unknown"
                    break
            message = {"from": sender, "from_key": prefix,
                       "text": str(text), "at": at}
            messages = [message] + list(self._state.get("messages", []))
            self._state["messages"] = messages[:MAX_MESSAGES]
            self._state["updated_at"] = _utcnow_iso()

    def _on_advertisement(self, event):
        """A heard advert: the public key, resolved to a contact
        name when the radio knows it. This is how the owner's own
        repeater (Ghost Town, not on the public map) can appear in
        the tile: if the radio hears it advertising, it shows up
        here by name."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        key = str(payload.get("public_key") or "")
        if not key:
            return
        now = int(time.time())
        with self._lock:
            name = "Unknown node"
            for contact in self._state.get("contacts", []):
                contact_key = str(contact.get("public_key") or "")
                if contact_key and (contact_key == key
                                    or contact_key.startswith(key)
                                    or key.startswith(contact_key)):
                    name = contact.get("name") or "Unknown node"
                    break
            adverts = [a for a in self._state.get("recent_adverts", [])
                       if a.get("public_key") != key]
            adverts.insert(0, {"name": name, "public_key": key, "at": now})
            self._state["recent_adverts"] = adverts[:MAX_ADVERTS]
            self._state["updated_at"] = _utcnow_iso()

    def _on_disconnect(self, event):
        self._disconnect_flag.set()

    async def _close_client(self, client):
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5)
        except Exception:
            pass
