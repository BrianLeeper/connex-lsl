#!/usr/bin/env python3
"""
Transparent NeuroWorks <-> Xltek/Natus Connex TCP proxy with passive LSL output.

Developed and maintained by Brian Leeper <brian.leeper@gmail.com>
Protocol analysis, hardware validation, testing, and project direction
by Brian Leeper, with AI-assisted code generation using ChatGPT/OpenAI.

Version: 0.1b
License: MIT

Typical use:
    python connex_lsl_proxy.py --device 192.168.2.2

Configure NeuroWorks to connect to 127.0.0.1:2200 (default proxy listener).
Every NeuroWorks connection gets its own matching TCP connection to the real
Connex. Bytes are forwarded unchanged in both directions. The proxy passively
watches for D6 calibration replies, 256/512-Hz start commands, 140-byte scans,
patient-event state, OSAT, and pulse rate, then republishes decoded data to LSL.

Requires pylsl for LSL output:
    pip install pylsl

Experimental reverse-engineered interoperability software; not for clinical use.
"""
from __future__ import annotations

import argparse
import itertools
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

VERSION = "0.1b"
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_PORT = 2200
SCAN_LENGTH = 140
SCAN_CHANNELS = 50
SCAN_INDEX_OFFSET = 0x20
SCAN_FLAGS_OFFSET = 0x22
SCAN_COUNT_OFFSET = 0x24
SCAN_DATA_OFFSET = 0x26
EEG_CHANNELS = 38

EEG_LABELS = [
    "FP1", "FPZ", "FP2",
    "F7", "F3", "FZ", "F4", "F8",
    "A1", "T3", "C3", "CZ", "C4", "T4", "A2",
    "T5", "P3", "PZ", "P4", "T6",
    "O1", "O2",
    "LOC", "CHIN1", "ECGL", "LAT1", "RAT1", "ROC",
    "CHIN2", "ECGR", "LAT2", "RAT2",
    "DIF1", "DIF2", "DIF3", "DIF4", "DIF5", "DIF6",
]
AUX_LABELS = [f"DC{i}" for i in range(1, 11)] + ["OSAT", "PR", "PATIENT_EVENT"]


def hx(data: bytes) -> str:
    return data.hex(" ")


def txn_id(msg: bytes) -> Optional[int]:
    return struct.unpack_from("<H", msg, 2)[0] if len(msg) >= 4 else None


class ConnexFramer:
    """Turn arbitrary TCP chunks into Connex length-prefixed messages."""

    def __init__(self, on_message: Callable[[bytes], None], name: str):
        self.buffer = bytearray()
        self.on_message = on_message
        self.name = name

    def feed(self, data: bytes) -> None:
        self.buffer.extend(data)
        while True:
            if len(self.buffer) < 2:
                return
            total_len = struct.unpack_from("<H", self.buffer, 0)[0]
            if total_len < 2 or total_len > 65535:
                print(f"[{self.name}] parser lost framing (length={total_len}); resetting", file=sys.stderr)
                self.buffer.clear()
                return
            if len(self.buffer) < total_len:
                return
            msg = bytes(self.buffer[:total_len])
            del self.buffer[:total_len]
            try:
                self.on_message(msg)
            except Exception as exc:
                print(f"[{self.name}] passive parser error: {exc}", file=sys.stderr)


@dataclass
class ConnectionState:
    connection_id: int
    sampling_rate: Optional[int] = None
    d6_requests: Dict[int, int] = field(default_factory=dict)
    calibration: Dict[int, int] = field(default_factory=dict)
    scan_count: int = 0
    previous_scan_index: Optional[int] = None
    patient_event_previous: bool = False
    publisher: Optional["LSLPublisher"] = None


class LSLPublisher:
    """EEG, AUX, and marker outlets for one acquisition connection."""

    def __init__(self, connection_id: int, rate: int, calibration: Dict[int, int], raw_eeg: bool):
        try:
            from pylsl import StreamInfo, StreamOutlet
        except ImportError as exc:
            raise RuntimeError("pylsl is not installed; run: pip install pylsl") from exc

        self.rate = rate
        self.raw_eeg = raw_eeg
        self.calibration = dict(calibration)

        eeg_info = StreamInfo(f"Connex_EEG_{connection_id}", "EEG", 38, float(rate), "float32", f"connex-eeg-{connection_id}")
        channels = eeg_info.desc().append_child("channels")
        for i, label in enumerate(EEG_LABELS):
            ch = channels.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("type", "EEG")
            ch.append_child_value("unit", "count" if raw_eeg else "microvolts")
            ch.append_child_value("source_index", str(i))
            if i in calibration:
                ch.append_child_value("d6", str(calibration[i]))
                ch.append_child_value("d6_factor", f"{calibration[i] / 32768.0:.10f}")

        aux_info = StreamInfo(f"Connex_AUX_{connection_id}", "AUX", len(AUX_LABELS), float(rate), "float32", f"connex-aux-{connection_id}")
        channels = aux_info.desc().append_child("channels")
        for label in AUX_LABELS:
            ch = channels.append_child("channel")
            ch.append_child_value("label", label)
            if label.startswith("DC"):
                ch.append_child_value("type", "DC")
                ch.append_child_value("unit", "raw_count")
            elif label == "OSAT":
                ch.append_child_value("type", "Oximetry")
                ch.append_child_value("unit", "percent")
            elif label == "PR":
                ch.append_child_value("type", "PulseRate")
                ch.append_child_value("unit", "bpm")
            else:
                ch.append_child_value("type", "Digital")
                ch.append_child_value("unit", "boolean")

        marker_info = StreamInfo(f"Connex_Markers_{connection_id}", "Markers", 1, 0.0, "string", f"connex-markers-{connection_id}")

        self.eeg_outlet = StreamOutlet(eeg_info, chunk_size=1, max_buffered=360)
        self.aux_outlet = StreamOutlet(aux_info, chunk_size=1, max_buffered=360)
        self.marker_outlet = StreamOutlet(marker_info, chunk_size=1, max_buffered=360)
        print(f"[C{connection_id}] LSL outlets created at {rate} Hz")

    def update_calibration(self, channel: int, coefficient: int) -> None:
        self.calibration[channel] = coefficient

    def _eeg_values(self, samples) -> list[float]:
        if self.raw_eeg:
            return [float(samples[i]) for i in range(38)]
        out = []
        for i in range(38):
            coeff = self.calibration.get(i)
            factor = coeff / 32768.0 if coeff is not None else 1.0
            nominal_uv_per_count = 0.3 if i < 32 else 0.6
            out.append(float(samples[i]) * factor * nominal_uv_per_count)
        return out

    def push_scan(self, samples, patient_event: bool, timestamp: float) -> None:
        eeg = self._eeg_values(samples)
        dc = [float(samples[i]) for i in range(38, 48)]
        aux = dc + [float(samples[48]) / 10.0, float(samples[49]), 1.0 if patient_event else 0.0]
        self.eeg_outlet.push_sample(eeg, timestamp)
        self.aux_outlet.push_sample(aux, timestamp)

    def push_marker(self, text: str, timestamp: Optional[float] = None) -> None:
        if timestamp is None:
            self.marker_outlet.push_sample([text])
        else:
            self.marker_outlet.push_sample([text], timestamp)


class PassiveConnexDecoder:
    def __init__(self, state: ConnectionState, enable_lsl: bool, raw_eeg: bool, verbose: bool):
        self.state = state
        self.enable_lsl = enable_lsl
        self.raw_eeg = raw_eeg
        self.verbose = verbose
        self.pc_framer = ConnexFramer(self.on_pc_message, f"C{state.connection_id} NW->BASE")
        self.base_framer = ConnexFramer(self.on_base_message, f"C{state.connection_id} BASE->NW")

    def feed_pc(self, data: bytes) -> None:
        self.pc_framer.feed(data)

    def feed_base(self, data: bytes) -> None:
        self.base_framer.feed(data)

    def on_pc_message(self, msg: bytes) -> None:
        txn = txn_id(msg)
        if len(msg) >= 3 and msg[-3] == 0xD6 and msg[-1] == 0x00 and txn is not None:
            ch = msg[-2]
            self.state.d6_requests[txn] = ch
            if self.verbose:
                print(f"[C{self.state.connection_id}] D6 request ch {ch:02d} txn=0x{txn:04X}")

        rate = None
        if msg.endswith(b"\x06\x20\x01\x00"):
            rate = 256
        elif msg.endswith(b"\x06\x20\x02\x00"):
            rate = 512
        if rate:
            self.state.sampling_rate = rate
            print(f"[C{self.state.connection_id}] acquisition start detected: {rate} Hz")

        if msg.endswith(b"\x03\x00\x05\x01\x04\x21"):
            print(f"[C{self.state.connection_id}] acquisition stop detected")
            if self.state.publisher:
                self.state.publisher.push_marker("ACQUISITION_STOP")

        if self.verbose and len(msg) <= 32:
            print(f"[C{self.state.connection_id}] NW->BASE len={len(msg)} txn={txn}: {hx(msg)}")

    def on_base_message(self, msg: bytes) -> None:
        txn = txn_id(msg)
        if txn is not None and txn in self.state.d6_requests:
            ch = self.state.d6_requests.pop(txn)
            if len(msg) >= 28 and msg[0x18:0x1A] == b"\xaa\xd6":
                coeff = struct.unpack_from("<H", msg, 0x1A)[0]
                self.state.calibration[ch] = coeff
                if self.state.publisher:
                    self.state.publisher.update_calibration(ch, coeff)
                print(f"[C{self.state.connection_id}] D6 ch {ch:02d} = {coeff} factor={coeff / 32768.0:.8f}")

        scan = self.parse_scan(msg)
        if scan is not None:
            self.handle_scan(*scan)
        elif self.verbose and len(msg) <= 64:
            print(f"[C{self.state.connection_id}] BASE->NW len={len(msg)} txn={txn}: {hx(msg)}")

    @staticmethod
    def parse_scan(msg: bytes):
        if len(msg) != SCAN_LENGTH:
            return None
        if struct.unpack_from("<H", msg, SCAN_COUNT_OFFSET)[0] != SCAN_CHANNELS:
            return None
        sample_index = struct.unpack_from("<H", msg, SCAN_INDEX_OFFSET)[0]
        flags = msg[SCAN_FLAGS_OFFSET]
        samples = struct.unpack_from("<50h", msg, SCAN_DATA_OFFSET)
        return sample_index, flags, samples, bool(flags & 0x01)

    def _ensure_publisher(self) -> None:
        if not self.enable_lsl or self.state.publisher is not None:
            return
        rate = self.state.sampling_rate
        if rate not in (256, 512):
            if self.state.scan_count == 1:
                print(f"[C{self.state.connection_id}] scans seen before rate identification; deferring LSL", file=sys.stderr)
            return
        missing = [i for i in range(38) if i not in self.state.calibration]
        if missing and not self.raw_eeg:
            print(f"[C{self.state.connection_id}] WARNING: {len(missing)} D6 coefficients missing; factor 1.0 used until seen", file=sys.stderr)
        try:
            self.state.publisher = LSLPublisher(self.state.connection_id, rate, self.state.calibration, self.raw_eeg)
            self.state.publisher.push_marker("ACQUISITION_START")
        except Exception as exc:
            print(f"[C{self.state.connection_id}] LSL disabled: {exc}", file=sys.stderr)
            self.enable_lsl = False

    def handle_scan(self, sample_index: int, flags: int, samples, patient_event: bool) -> None:
        self.state.scan_count += 1
        prev = self.state.previous_scan_index
        if prev is not None:
            expected = (prev + 1) & 0xFFFF
            if sample_index != expected:
                print(f"[C{self.state.connection_id}] WARNING scan-index jump {prev}->{sample_index} expected {expected}", file=sys.stderr)
        self.state.previous_scan_index = sample_index
        self._ensure_publisher()

        try:
            from pylsl import local_clock
            ts = local_clock()
        except Exception:
            ts = time.time()

        if self.state.publisher:
            self.state.publisher.push_scan(samples, patient_event, ts)
            if patient_event and not self.state.patient_event_previous:
                self.state.publisher.push_marker("PATIENT_EVENT", ts)
        self.state.patient_event_previous = patient_event

        if self.state.scan_count <= 3:
            print(f"[C{self.state.connection_id}] scan {self.state.scan_count}: index={sample_index} flags=0x{flags:02X} OSAT={samples[48]/10.0:.1f}% PR={samples[49]}")
        elif self.state.scan_count % 4096 == 0:
            print(f"[C{self.state.connection_id}] {self.state.scan_count} scans (last index={sample_index})")


def safe_close(sock: Optional[socket.socket]) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def pump(source: socket.socket, destination: socket.socket, parser_feed: Callable[[bytes], None], stop: threading.Event, label: str) -> None:
    """Forward first, then inspect a copy. Parser failure cannot alter wire data."""
    try:
        while not stop.is_set():
            data = source.recv(65536)
            if not data:
                break
            destination.sendall(data)
            try:
                parser_feed(data)
            except Exception as exc:
                print(f"[{label}] passive inspection failed: {exc}", file=sys.stderr)
    except (ConnectionError, OSError) as exc:
        if not stop.is_set():
            print(f"[{label}] connection ended: {exc}", file=sys.stderr)
    finally:
        stop.set()
        safe_close(source)
        safe_close(destination)


def handle_client(nw_sock: socket.socket, nw_addr, connection_id: int, args) -> None:
    base_sock = None
    stop = threading.Event()
    state = ConnectionState(connection_id)
    decoder = PassiveConnexDecoder(state, not args.no_lsl, args.raw_eeg, args.verbose)
    print(f"[C{connection_id}] Neuroworks connected from {nw_addr[0]}:{nw_addr[1]}")
    try:
        print(f"[C{connection_id}] connecting to real Connex {args.device}:{args.device_port} ...")
        base_sock = socket.create_connection((args.device, args.device_port), timeout=args.connect_timeout)
        base_sock.settimeout(None)
        nw_sock.settimeout(None)
        print(f"[C{connection_id}] paired; transparent forwarding active")

        t1 = threading.Thread(target=pump, args=(nw_sock, base_sock, decoder.feed_pc, stop, f"C{connection_id} NW->BASE"), daemon=True)
        t2 = threading.Thread(target=pump, args=(base_sock, nw_sock, decoder.feed_base, stop, f"C{connection_id} BASE->NW"), daemon=True)
        t1.start(); t2.start()
        while not stop.wait(0.25):
            pass
        safe_close(nw_sock); safe_close(base_sock)
        t1.join(timeout=1.0); t2.join(timeout=1.0)
    except (ConnectionError, OSError) as exc:
        print(f"[C{connection_id}] real Connex unavailable / connection failed: {exc}", file=sys.stderr)
    finally:
        safe_close(nw_sock); safe_close(base_sock)
        if state.scan_count:
            print(f"[C{connection_id}] closed after {state.scan_count} scans; D6 captured={len(state.calibration)}")
        else:
            print(f"[C{connection_id}] closed (no acquisition stream on this connection)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Transparent Neuroworks/Connex TCP proxy with passive LSL output")
    ap.add_argument("--device", required=True, help="real Connex base IP/hostname")
    ap.add_argument("--device-port", type=int, default=2200)
    ap.add_argument("--listen", default="127.0.0.1", help="local address Neuroworks connects to")
    ap.add_argument("--listen-port", type=int, default=2200)
    ap.add_argument("--connect-timeout", type=float, default=2.0)
    ap.add_argument("--no-lsl", action="store_true", help="proxy only; disable LSL")
    ap.add_argument("--raw-eeg", action="store_true", help="publish first 38 analog channels as raw counts")
    ap.add_argument("--verbose", action="store_true", help="print small protocol messages")
    args = ap.parse_args()

    print(f"Connex transparent LSL proxy {VERSION}")
    print(f"  Neuroworks target : {args.listen}:{args.listen_port}")
    print(f"  Real Connex       : {args.device}:{args.device_port}")
    print(f"  LSL               : {'disabled' if args.no_lsl else 'enabled'}")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.listen, args.listen_port))
    server.listen(16)
    server.settimeout(1.0)

    counter = itertools.count(1)
    threads = []
    try:
        while True:
            try:
                nw_sock, nw_addr = server.accept()
            except socket.timeout:
                continue
            cid = next(counter)
            t = threading.Thread(target=handle_client, args=(nw_sock, nw_addr, cid, args), daemon=True)
            t.start()
            threads.append(t)
            threads = [x for x in threads if x.is_alive()]
    except KeyboardInterrupt:
        print("\nStopping proxy listener.")
    finally:
        safe_close(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
