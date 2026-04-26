#!/usr/bin/env python3
"""cc_buddy_daemon: bridges Claude Code hooks <-> M5Stick over USB or BLE.

Hooks (Stop, PreToolUse) talk to this daemon over a Unix socket.
The daemon owns the transport, sends a 10s keepalive, and reads back
permission decisions from the stick.

Socket protocol (line-delimited JSON):

  hook -> daemon:
    {"op":"snapshot","session_id":"...","snapshot":{...heartbeat fields...}}
    {"op":"approve","session_id":"...","request_id":"req_xxx","tool":"Bash","hint":"..."}

  daemon -> hook (only for approve):
    {"decision":"allow"|"deny"|"timeout"}

Configuration via env vars:
    BUDDY_TRANSPORT   "serial" | "ble" (default: auto)
    BUDDY_PORT        explicit serial device path (forces serial)
    BUDDY_BLE_NAME    BLE device name prefix to match (default: "Claude")
    BUDDY_SOCK        Unix socket path (default: /tmp/cc_buddy.sock)
    BUDDY_LOG         log file path (default: /tmp/cc_buddy.log)
"""
import asyncio
import glob
import json
import os
import queue
import socket
import threading
import time
import traceback
from datetime import datetime

SOCK_PATH = os.environ.get("BUDDY_SOCK", "/tmp/cc_buddy.sock")
LOG_PATH = os.environ.get("BUDDY_LOG", "/tmp/cc_buddy.log")
KEEPALIVE_S = 10
APPROVE_TIMEOUT_S = 30
SERIAL_BAUD = 115200

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_CHAR = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


def log(msg):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} [d] {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class SerialTransport:
    def __init__(self, port):
        import serial
        self._serial_mod = serial
        self.port = port
        self.serial = serial.Serial(port, SERIAL_BAUD, timeout=0.2)
        self._write_lock = threading.Lock()
        log(f"transport: serial {port}")

    def write_bytes(self, data):
        with self._write_lock:
            try:
                self.serial.write(data)
            except (self._serial_mod.SerialException, OSError) as e:
                log(f"serial write failed: {e}")

    def iter_lines(self):
        buf = b""
        while True:
            try:
                chunk = self.serial.read(256)
            except (self._serial_mod.SerialException, OSError) as e:
                log(f"serial read failed: {e}; sleeping 1s")
                time.sleep(1)
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    yield line


class BleTransport:
    """Owns an asyncio loop in a background thread, surfaces sync API."""

    def __init__(self, name_prefix="Claude"):
        from bleak import BleakClient, BleakScanner  # noqa: F401  (verify import)
        self.name_prefix = name_prefix
        self.line_queue = queue.Queue()
        self.client = None
        self.loop = None
        self.connected = threading.Event()
        self._stop = False
        self._loop_ready = threading.Event()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self._loop_ready.wait(timeout=5)
        log(f"transport: ble (matching name prefix {name_prefix!r})")

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._loop_ready.set()
        self.loop.run_until_complete(self._connect_forever())

    async def _connect_forever(self):
        from bleak import BleakClient, BleakScanner
        rx_buf = bytearray()

        def on_notify(_handle, data):
            rx_buf.extend(data)
            while b"\n" in rx_buf:
                idx = rx_buf.index(b"\n")
                line = bytes(rx_buf[:idx]).strip()
                del rx_buf[: idx + 1]
                if line:
                    self.line_queue.put(line)

        while not self._stop:
            try:
                log("[ble] scanning…")
                target = None
                devices = await BleakScanner.discover(timeout=8.0, return_adv=True)
                for dev, adv in devices.values():
                    name = dev.name or (adv.local_name if adv else "") or ""
                    if name.startswith(self.name_prefix):
                        target = dev
                        break
                if not target:
                    log("[ble] no match; retrying in 5s")
                    await asyncio.sleep(5)
                    continue
                log(f"[ble] connecting to {target.name} ({target.address})")
                async with BleakClient(target.address) as client:
                    self.client = client
                    # The firmware's NUS characteristics are encrypted-only
                    # (LE Secure Connections). On macOS, just subscribing to
                    # the TX char isn't always enough to make CoreBluetooth
                    # pop the passkey dialog — explicitly request pairing
                    # first. On backends where pair() is a no-op or
                    # unimplemented, it's harmless.
                    try:
                        await client.pair()
                        log("[ble] pair() returned")
                    except NotImplementedError:
                        log("[ble] pair() not implemented on this backend; relying on auto-pair")
                    except Exception as e:
                        log(f"[ble] pair() raised: {e}")
                    # First encrypted access: an empty write to RX. If the
                    # link still isn't bonded, this forces CoreBluetooth to
                    # initiate pairing now (and surface the passkey dialog),
                    # not when we later subscribe.
                    try:
                        await client.write_gatt_char(NUS_RX_CHAR, b"\n", response=False)
                    except Exception as e:
                        log(f"[ble] initial write failed (pairing in progress?): {e}")
                        await asyncio.sleep(3)
                    await client.start_notify(NUS_TX_CHAR, on_notify)
                    self.connected.set()
                    log("[ble] connected and subscribed")
                    while client.is_connected and not self._stop:
                        await asyncio.sleep(0.5)
                    log("[ble] link dropped")
            except Exception as e:
                log(f"[ble] error: {e}")
            finally:
                self.client = None
                self.connected.clear()
                rx_buf.clear()
            await asyncio.sleep(2)

    def write_bytes(self, data):
        if not self.client or not self.connected.is_set():
            log("[ble] write while not connected; dropping")
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.client.write_gatt_char(NUS_RX_CHAR, data, response=False),
                self.loop,
            )
            fut.result(timeout=5)
        except Exception as e:
            log(f"[ble] write failed: {e}")

    def iter_lines(self):
        while True:
            line = self.line_queue.get()
            if line:
                yield line


def make_transport():
    explicit = os.environ.get("BUDDY_TRANSPORT", "").lower()
    port = os.environ.get("BUDDY_PORT")
    if explicit == "serial" or port:
        return SerialTransport(port or _autodetect_serial())
    if explicit == "ble":
        return BleTransport(os.environ.get("BUDDY_BLE_NAME", "Claude"))
    # auto: prefer serial if a usbserial device is present
    auto_port = _autodetect_serial(strict=False)
    if auto_port:
        return SerialTransport(auto_port)
    return BleTransport(os.environ.get("BUDDY_BLE_NAME", "Claude"))


def _autodetect_serial(strict=True):
    matches = glob.glob("/dev/cu.usbserial-*")
    if matches:
        return matches[0]
    if strict:
        raise SystemExit("no /dev/cu.usbserial-* found; set BUDDY_PORT or BUDDY_TRANSPORT=ble")
    return None


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class Daemon:
    def __init__(self, transport):
        self.transport = transport
        self.snapshot = {
            "total": 0, "running": 0, "waiting": 0,
            "msg": "idle", "entries": [], "tokens": 0, "tokens_today": 0,
        }
        self.snapshot_lock = threading.Lock()
        self.session_tokens = {}
        self.session_tokens_lock = threading.Lock()
        # Firmware shows one prompt at a time, so serialize approvals.
        self.approve_lock = threading.Lock()
        self.pending_id = None
        self.pending_event = threading.Event()
        self.pending_decision = None
        self.pending_state_lock = threading.Lock()
        log("daemon up")

    def write_line(self, payload):
        line = (json.dumps(payload) + "\n").encode()
        self.transport.write_bytes(line)

    def push_snapshot(self):
        with self.snapshot_lock:
            snap = dict(self.snapshot)
        self.write_line(snap)

    def read_loop(self):
        for line in self.transport.iter_lines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.handle_stick_message(obj)

    def handle_stick_message(self, obj):
        if obj.get("cmd") == "permission":
            rid = obj.get("id", "")
            decision = obj.get("decision", "")
            log(f"stick decision: id={rid} decision={decision}")
            with self.pending_state_lock:
                if rid and rid == self.pending_id:
                    self.pending_decision = "allow" if decision == "once" else "deny"
                    self.pending_event.set()

    def keepalive_loop(self):
        while True:
            time.sleep(KEEPALIVE_S)
            self.push_snapshot()

    def serve_socket(self):
        try:
            os.unlink(SOCK_PATH)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o600)
        srv.listen(8)
        log(f"socket listening at {SOCK_PATH}")
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self.handle_client, args=(conn,), daemon=True).start()

    def handle_client(self, conn):
        try:
            conn.settimeout(2.0)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            line = data.partition(b"\n")[0]
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                log(f"bad request: {e}")
                return
            op = req.get("op")
            if op == "snapshot":
                self.handle_snapshot(req)
            elif op == "approve":
                self.handle_approve(conn, req)
            else:
                log(f"unknown op: {op}")
        except Exception:
            log("client handler error:\n" + traceback.format_exc())
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def handle_snapshot(self, req):
        snap = dict(req.get("snapshot", {}))
        sid = req.get("session_id", "")

        if sid:
            with self.session_tokens_lock:
                self.session_tokens[sid] = (
                    snap.get("tokens", 0),
                    snap.get("tokens_today", 0),
                )
                total = sum(t for t, _ in self.session_tokens.values())
                today = sum(td for _, td in self.session_tokens.values())
        else:
            total = snap.get("tokens", 0)
            today = snap.get("tokens_today", 0)
        snap["tokens"] = total
        snap["tokens_today"] = today

        with self.snapshot_lock:
            with self.pending_state_lock:
                preserve = self.pending_id is not None and "prompt" in self.snapshot
                pending_prompt = self.snapshot.get("prompt") if preserve else None
            self.snapshot = snap
            if pending_prompt:
                self.snapshot["prompt"] = pending_prompt
                self.snapshot["waiting"] = max(1, self.snapshot.get("waiting", 0))
        self.push_snapshot()
        log(f"snapshot from session={sid[:8]} tokens={total} today={today}")

    def handle_approve(self, conn, req):
        rid = req.get("request_id", "")
        tool = req.get("tool", "tool")
        hint = req.get("hint", "")
        with self.approve_lock:
            log(f"approve req: id={rid} tool={tool} hint={hint[:40]!r}")
            with self.pending_state_lock:
                self.pending_id = rid
                self.pending_decision = None
                self.pending_event.clear()
            with self.snapshot_lock:
                self.snapshot["waiting"] = max(1, self.snapshot.get("waiting", 0))
                self.snapshot["prompt"] = {"id": rid, "tool": tool[:19], "hint": hint[:43]}
            self.push_snapshot()

            got = self.pending_event.wait(APPROVE_TIMEOUT_S)
            with self.pending_state_lock:
                decision = self.pending_decision if got else "timeout"
                self.pending_id = None

            with self.snapshot_lock:
                self.snapshot.pop("prompt", None)
                self.snapshot["waiting"] = 0
            self.push_snapshot()

            log(f"approve resolved: id={rid} decision={decision}")
            try:
                conn.sendall((json.dumps({"decision": decision}) + "\n").encode())
            except OSError:
                pass


def main():
    transport = make_transport()
    d = Daemon(transport)
    threading.Thread(target=d.read_loop, daemon=True).start()
    threading.Thread(target=d.keepalive_loop, daemon=True).start()
    d.serve_socket()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("daemon stopped")
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass
