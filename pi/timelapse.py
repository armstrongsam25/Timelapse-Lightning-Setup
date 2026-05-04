#!/usr/bin/env python3
"""Sky Sentry timelapse capture (Phase 1: camera only, no sensors)."""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from picamera2 import Picamera2

DEFAULT_OUTPUT = Path("/mnt/ssd/timelapse")
DEFAULT_INTERVAL = 30
DEFAULT_RESOLUTION = (4056, 3040)  # HQ Camera native
DEFAULT_QUALITY = 90
HEARTBEAT_EVERY = 10
DISK_WARN_BYTES = 5 * 1024**3      # 5 GB
DISK_ABORT_BYTES = 500 * 1024**2   # 500 MB

log = logging.getLogger("timelapse")
_stop = False


def _handle_signal(signum, _frame):
    global _stop
    log.info("received signal %s, stopping after current frame", signum)
    _stop = True


def parse_resolution(s: str) -> tuple[int, int]:
    w, _, h = s.lower().partition("x")
    return int(w), int(h)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sky Sentry timelapse capture.")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                   help=f"seconds between frames (default {DEFAULT_INTERVAL})")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                   help=f"base output directory (default {DEFAULT_OUTPUT})")
    p.add_argument("--duration", type=float, default=None,
                   help="stop after N seconds (default: run forever)")
    p.add_argument("--resolution", type=parse_resolution, default=DEFAULT_RESOLUTION,
                   help="capture size WxH (default 4056x3040)")
    p.add_argument("--quality", type=int, default=DEFAULT_QUALITY,
                   help="JPEG quality 1-100 (default 90)")
    return p.parse_args()


def preflight(output: Path) -> None:
    if not output.exists():
        output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir():
        sys.exit(f"output path is not a directory: {output}")

    probe = output / ".write_test"
    try:
        probe.touch()
        probe.unlink()
    except OSError as e:
        sys.exit(f"output path is not writable: {output} ({e})")

    free = shutil.disk_usage(output).free
    free_gb = free / 1024**3
    if free < DISK_ABORT_BYTES:
        sys.exit(f"aborting: only {free_gb:.2f} GB free at {output}")
    if free < DISK_WARN_BYTES:
        log.warning("low disk space: %.2f GB free at %s", free_gb, output)
    else:
        log.info("disk free: %.2f GB at %s", free_gb, output)


def session_dir(base: Path) -> Path:
    d = base / datetime.now().strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_camera(resolution: tuple[int, int], quality: int) -> Picamera2:
    cam = Picamera2()
    config = cam.create_still_configuration(main={"size": resolution})
    cam.configure(config)
    cam.options["quality"] = quality
    cam.start()
    time.sleep(2)  # AE/AWB settle
    return cam


def format_mb(n_bytes: int) -> str:
    return f"{n_bytes / 1024**2:.1f} MB"


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )
    args = parse_args()
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    preflight(args.output)

    log.info("starting: interval=%.1fs resolution=%dx%d quality=%d output=%s",
             args.interval, args.resolution[0], args.resolution[1],
             args.quality, args.output)
    if args.duration is not None:
        log.info("duration capped at %.0fs", args.duration)

    cam = build_camera(args.resolution, args.quality)

    frame_count = 0
    total_bytes = 0
    start_mono = time.monotonic()
    next_deadline = start_mono
    end_mono = start_mono + args.duration if args.duration else None

    try:
        while not _stop:
            if end_mono is not None and time.monotonic() >= end_mono:
                break

            now = datetime.now()
            out_dir = session_dir(args.output)
            path = out_dir / now.strftime("img_%H%M%S.jpg")

            t0 = time.monotonic()
            cam.capture_file(str(path))
            t1 = time.monotonic()

            size = path.stat().st_size
            total_bytes += size
            frame_count += 1
            log.info("captured %s (%.0f KB, %.2fs)", path, size / 1024, t1 - t0)

            if frame_count % HEARTBEAT_EVERY == 0:
                elapsed = time.monotonic() - start_mono
                log.info("heartbeat: %d frames in %.0fs, %s total",
                         frame_count, elapsed, format_mb(total_bytes))

            next_deadline += args.interval
            sleep_for = next_deadline - time.monotonic()
            if sleep_for < 0:
                missed = int(-sleep_for / args.interval) + 1
                log.warning("capture overran interval by %.2fs; skipping %d slot(s)",
                            -sleep_for, missed)
                next_deadline += missed * args.interval
                sleep_for = next_deadline - time.monotonic()

            # Wake periodically so SIGINT is responsive even with long intervals.
            while sleep_for > 0 and not _stop:
                chunk = min(sleep_for, 1.0)
                time.sleep(chunk)
                sleep_for = next_deadline - time.monotonic()
    finally:
        cam.stop()
        cam.close()
        elapsed = time.monotonic() - start_mono
        log.info("session done: %d frames in %.0fs, %s total",
                 frame_count, elapsed, format_mb(total_bytes))

    return 0


if __name__ == "__main__":
    sys.exit(main())
