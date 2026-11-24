#!/usr/bin/env python3
"""
connex-control.py

Experimental Xltek/Natus Connex and original Brain Monitor controller derived from a
Neuroworks 8.5 -> Connex 256 Hz TCP conversation.

Purpose:
  1. Open TCP port 2200 on the Connex base.
  2. Replay the observed PC->base startup messages in order.
  3. Read one framed base reply after each startup request.
  4. Extract D6 per-channel factory gain coefficients when seen.
  5. Send the final observed command and then receive 140-byte sample scans.

This is deliberately a reverse-engineering/test harness, not yet an LSL bridge.

IMPORTANT:
  - Run with Neuroworks closed so it does not own the connection.
  - Default base IP is 192.168.2.2; override with --host.
  - Ctrl+C stops reception.
"""

import argparse
import socket
import struct
import sys
import threading
import time

DEFAULT_HOST = "192.168.2.2"
DEFAULT_PORT = 2200

# Exact PC->base startup packets from the first supplied 256 Hz capture.
# The last packet is followed immediately by the sample stream in the capture.
STARTUP_HEX = [
    "1000d107000000000000000004000e00",
    "1000d207000000000000000004001c00",
    "1000d307000000000000000004005600",
    "1400d40700000000000000000800030005010411",
    "1400d50700000000000000000800036505010421",
    "1400d607000000000000000008000300050104e2",
    "1400d707000000000000000008000300050104b0",
    "1400d807000000000000000008000300050104a0",
    "1400d90700000000000000000800037505010428",
    "1400da07000000000000000008000369050104d5",
    "1600db0700000000000000000a000325050106d60000",
    "1600dc0700000000000000000a000300050106d60100",
    "1600dd0700000000000000000a000300050106d60200",
    "1600de0700000000000000000a000300050106d60300",
    "1600df0700000000000000000a000300050106d60400",
    "1600e00700000000000000000a000300050106d60500",
    "1600e10700000000000000000a000325050106d60600",
    "1600e20700000000000000000a000300050106d60700",
    "1600e30700000000000000000a000300050106d60800",
    "1600e40700000000000000000a000300050106d60900",
    "1600e50700000000000000000a000325050106d60a00",
    "1600e60700000000000000000a000300050106d60b00",
    "1600e70700000000000000000a000300050106d60c00",
    "1600e80700000000000000000a000300050106d60d00",
    "1600e90700000000000000000a000300050106d60e00",
    "1600ea0700000000000000000a000301050106d60f00",
    "1600eb0700000000000000000a000300050106d61000",
    "1600ec0700000000000000000a000300050106d61100",
    "1600ed0700000000000000000a000300050106d61200",
    "1600ee0700000000000000000a000300050106d61300",
    "1600ef0700000000000000000a000300050106d61400",
    "1600f00700000000000000000a000300050106d61500",
    "1600f10700000000000000000a000300050106d61600",
    "1600f20700000000000000000a000300050106d61700",
    "1600f30700000000000000000a000300050106d61800",
    "1600f40700000000000000000a000300050106d61900",
    "1600f50700000000000000000a000300050106d61a00",
    "1600f60700000000000000000a000300050106d61b00",
    "1600f70700000000000000000a000325050106d61c00",
    "1600f80700000000000000000a000301050106d61d00",
    "1600f90700000000000000000a000300050106d61e00",
    "1600fa0700000000000000000a000300050106d61f00",
    "1600fb0700000000000000000a000300050106d62000",
    "1600fc0700000000000000000a000300050106d62100",
    "1600fd0700000000000000000a000325050106d62200",
    "1600fe0700000000000000000a000300050106d62300",
    "1600ff0700000000000000000a000300050106d62400",
    "1600000800000000000000000a000300050106d62500",
    "1600010800000000000000000a000301050106d62600",
    "1600020800000000000000000a000300050106d62700",
    "1600030800000000000000000a000325050106d62800",
    "1600040800000000000000000a000300050106d62900",
    "1600050800000000000000000a000300050106d62a00",
    "1600060800000000000000000a000300050106d62b00",
    "1600070800000000000000000a000300050106d62c00",
    "1600080800000000000000000a000300050106d62d00",
    "1600090800000000000000000a000300050106d62e00",
    "16000a0800000000000000000a000301050106d62f00",
    "14000b08000000000000000008000300050104d8",
    "14000c08000000000000000008000365050104d9",
    "10000d08000000000000000004005600",
    "14000e08000000000000000008000375050104b3",
    "1a000f0800000000000000000e00030005010a7bffffffffffff",
    "14001008000000000000000008000300050104c5",
    "1400110800000000000000000800010001003200",
    "10001208000000000000000004000500",
    "14001308000000000000000008000300050104b0",
    "14001408000000000000000008000300050104a0",
    "1400150800000000000000000800030005010428",
    "14001608000000000000000008000300050104c5",
    "14001708000000000000000008000363050104b0",
    "14001808000000000000000008000300050104a0",
    "1400190800000000000000000800030005010428",
    "12001a080000000000000000060002000100",
    "14001b0800000000000000000800010001000000",
    "16001c0800000000000000000a000300050106770100",
    "16001d0800000000000000000a000300050106750000",
    "1a001e0800000000000000000e00030005010a7bffffffffffff",
    "14001f08000000000000000008000302050104b3",
    "1a00200800000000000000000e00030005010ab20000fa006600",
    "1600210800000000000000000a000300050106e30f00",
    "1600220800000000000000000a000300050106200100",
]


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes or raise if the peer closes the connection."""
    chunks = []
    remaining = n
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError(
                f"Connection closed while waiting for {remaining} more byte(s)"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(sock: socket.socket) -> bytes:
    """
    Receive one Connex application message.

    The supplied captures show uint16 little-endian total length in bytes 0..1.
    """
    first2 = recv_exact(sock, 2)
    total_len = struct.unpack_from("<H", first2, 0)[0]

    if total_len < 2 or total_len > 65535:
        raise ValueError(f"Implausible Connex message length: {total_len}")

    return first2 + recv_exact(sock, total_len - 2)


def hx(data: bytes) -> str:
    return data.hex(" ")


def txn_id(msg: bytes):
    if len(msg) >= 4:
        return struct.unpack_from("<H", msg, 2)[0]
    return None


def maybe_extract_d6(reply: bytes, calibration: dict):
    """
    Observed D6 reply format:
      offset 0x18: AA
      offset 0x19: D6
      offset 0x1A: uint16 LE coefficient
    Channel number comes from the corresponding request, handled by caller.
    """
    if len(reply) >= 28 and reply[0x18:0x1A] == b"\xaa\xd6":
        return struct.unpack_from("<H", reply, 0x1A)[0]
    return None


def parse_scan(msg: bytes):
    """
    Parse the observed normal 140-byte acquisition message:
      0x20 uint16 sample index
      0x22 uint16 header field (256 even during verified 512 Hz acquisition)
      0x24 uint16 channel count
      0x26 50 x int16 LE samples
    """
    if len(msg) != 140:
        return None

    sample_index, header_field, channel_count = struct.unpack_from("<HHH", msg, 0x20)
    if channel_count != 50:
        return None

    samples = struct.unpack_from("<50h", msg, 0x26)
    return sample_index, header_field, channel_count, samples


def make_keepalive(txn: int) -> bytes:
    """Build the observed Neuroworks acquisition keepalive (command 0x001B)."""
    return struct.pack("<HH8sHH", 16, txn & 0xFFFF, b"\x00" * 8, 4, 0x001B)


def keepalive_worker(sock: socket.socket, first_txn: int, interval: float, stop_event: threading.Event):
    """Send command 0x001B periodically, incrementing the 16-bit transaction ID."""
    txn = first_txn & 0xFFFF
    while not stop_event.wait(interval):
        packet = make_keepalive(txn)
        try:
            sock.sendall(packet)
        except OSError as exc:
            if not stop_event.is_set():
                print(f"\nKeepalive send failed: {exc}", file=sys.stderr)
            return
        txn = (txn + 1) & 0xFFFF


def main():
    ap = argparse.ArgumentParser(
        description="Control Connex / original Brain Monitor acquisition (256 or 512 Hz)."
    )
    ap.add_argument("--host", default=DEFAULT_HOST, help="Connex base IP")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port")
    ap.add_argument("--rate", type=int, choices=(256, 512), default=512,
                    help="Requested acquisition rate in Hz (default: 512)")
    ap.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Timeout in seconds while waiting for startup replies",
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print every startup request/reply",
    )
    ap.add_argument(
        "--print-every",
        type=int,
        default=None,
        help="Print status every N scans (default: requested rate; 0 disables)",
    )
    ap.add_argument(
        "--show-first",
        type=int,
        default=5,
        help="Print the first N decoded sample scans",
    )
    ap.add_argument(
        "--keepalive-interval",
        type=float,
        default=6.0,
        help="Seconds between observed 0x001B acquisition keepalives (0 disables)",
    )
    args = ap.parse_args()

    if args.print_every is None:
        args.print_every = args.rate

    packets = [bytes.fromhex(x) for x in STARTUP_HEX]
    # Capture-confirmed start command parameter: 01 00 = 256 Hz; 02 00 = 512 Hz.
    packets[-1] = packets[-1][:-2] + struct.pack("<H", args.rate // 256)

    print(f"Connecting to {args.host}:{args.port} ...")
    with socket.create_connection((args.host, args.port), timeout=args.timeout) as s:
        # Keep the startup phase bounded, then allow continuous streaming.
        s.settimeout(args.timeout)
        print("Connected.")

        calibration = {}
        previous_sample_index = None

        # Every observed startup request except the last one had one ordinary reply.
        for i, request in enumerate(packets[:-1]):
            if not args.quiet:
                print(f"\n[{i+1:02d}] PC -> BASE (txn={txn_id(request)})")
                print(hx(request))

            s.sendall(request)
            reply = recv_message(s)

            if not args.quiet:
                print(
                    f"[{i+1:02d}] BASE -> PC "
                    f"(len={len(reply)}, echoed txn={txn_id(reply)})"
                )
                print(hx(reply))

            req_txn = txn_id(request)
            rep_txn = txn_id(reply)
            if req_txn is not None and rep_txn != req_txn:
                print(
                    f"WARNING: response txn {rep_txn} != request txn {req_txn}",
                    file=sys.stderr,
                )

            # Known D6 request tail: D6, channel, 00
            if len(request) >= 3 and request[-3] == 0xD6:
                ch = request[-2]
                coeff = maybe_extract_d6(reply, calibration)
                if coeff is not None:
                    calibration[ch] = coeff
                    if not args.quiet:
                        print(
                            f"      D6 ch {ch:02d} = {coeff} "
                            f"(factor={coeff / 32768.0:.8f})"
                        )

        # Final observed request: acquisition begins rather than returning a
        # normal request/reply message.
        start_request = packets[-1]
        print("\nSending final observed acquisition-start request:")
        print(hx(start_request))
        s.sendall(start_request)

        if calibration:
            print(f"\nCaptured {len(calibration)} D6 calibration coefficients:")
            for ch in sorted(calibration):
                c = calibration[ch]
                print(f"  ch {ch:02d}: {c:5d}  factor={c / 32768.0:.8f}")

        print("\nWaiting for sample stream... Ctrl+C to stop.")
        s.settimeout(None)

        # Neuroworks sends command 0x001B every 6 seconds during acquisition.
        # The first observed keepalive transaction immediately follows the
        # acquisition-start transaction (0x0822 -> 0x0823 in this capture).
        stop_event = threading.Event()
        keepalive_thread = None
        start_txn = txn_id(start_request)
        if args.keepalive_interval > 0 and start_txn is not None:
            first_keepalive_txn = (start_txn + 1) & 0xFFFF
            print(
                f"Keepalive 0x001B enabled every {args.keepalive_interval:g} s; "
                f"first txn=0x{first_keepalive_txn:04X}"
            )
            keepalive_thread = threading.Thread(
                target=keepalive_worker,
                args=(s, first_keepalive_txn, args.keepalive_interval, stop_event),
                daemon=True,
            )
            keepalive_thread.start()

        count = 0
        rate_warning = False
        t0 = time.monotonic()

        try:
            while True:
                msg = recv_message(s)
                scan = parse_scan(msg)

                if scan is None:
                    print(f"Non-scan message while streaming (len={len(msg)}): {hx(msg)}")
                    continue

                sample_index, header_field, nchan, samples = scan

                if previous_sample_index is not None:
                    expected = (previous_sample_index + 1) & 0xFFFF
                    if sample_index != expected:
                        print(
                            f"WARNING: sample-index jump "
                            f"{previous_sample_index} -> {sample_index} "
                            f"(expected {expected})"
                        )
                previous_sample_index = sample_index
                count += 1

                if count <= args.show_first:
                    print(
                        f"scan {count}: index={sample_index} header_0x22={header_field} "
                        f"channels={nchan} first8={samples[:8]}"
                    )

                if args.print_every and count % args.print_every == 0:
                    elapsed = time.monotonic() - t0
                    observed = count / elapsed if elapsed else 0.0
                    if elapsed >= 10:
                        mismatch = abs(observed - args.rate) > args.rate * 0.10
                        if mismatch and not rate_warning:
                            print(f"WARNING: requested {args.rate} Hz, measured {observed:.1f} scans/s", file=sys.stderr)
                        rate_warning = mismatch
                    print(
                        f"received {count} scans; last_index={sample_index}; "
                        f"header_0x22={header_field}; "
                        f"wall-clock receive rate≈{observed:.1f} scans/s"
                    )

        except (KeyboardInterrupt, ConnectionError) as exc:
            elapsed = time.monotonic() - t0
            rate = count / elapsed if elapsed else 0.0
            if isinstance(exc, ConnectionError):
                print(f"\nConnection closed: {exc}", file=sys.stderr)
            print(
                f"\nStopped. Received {count} scans in {elapsed:.2f} s "
                f"(≈{rate:.2f} scans/s)."
            )
        finally:
            stop_event.set()
            if keepalive_thread is not None:
                keepalive_thread.join(timeout=1.0)


if __name__ == "__main__":
    main()
