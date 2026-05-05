#!/usr/bin/env python3
"""Sky Sentry timelapse capture."""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from picamera2 import Picamera2

try:
    import serial  # pyserial; optional - timelapse runs without it
except ImportError:
    serial = None  # type: ignore[assignment]

DEFAULT_OUTPUT = Path("/mnt/ssd/timelapse")
DEFAULT_INTERVAL = 30
DEFAULT_RESOLUTION = (4056, 3040)  # HQ Camera native
DEFAULT_QUALITY = 90
HEARTBEAT_EVERY = 10
DISK_WARN_BYTES = 5 * 1024**3      # 5 GB
DISK_ABORT_BYTES = 500 * 1024**2   # 500 MB

DEFAULT_ARDUINO_PORT = "/dev/ttyACM0"
DEFAULT_ARDUINO_BAUD = 115200
LIGHTNING_SUFFIX = "_LIGHTNING"

# Burst mode: when a FLASH/LIGHTNING event arrives, capture as fast as the
# camera can for BURST_DURATION seconds. Every burst frame gets _LIGHTNING.
# A new flash inside the window extends it (the watcher timestamp bumps
# forward, so "now - last_flash < duration" stays true longer).
DEFAULT_BURST_INTERVAL = 0.0   # 0 = "as fast as capture_file returns"
DEFAULT_BURST_DURATION = 10.0  # seconds of burst per flash

log = logging.getLogger("timelapse")
_stop = False


class FlashWatcher:
    """Tracks the monotonic timestamp of the most recent FLASH/LIGHTNING line.

    A background thread reads the Arduino serial stream and bumps `last_event`
    when it sees a relevant line. The capture loop checks `last_event` against
    the start time of each frame and renames the file with _LIGHTNING if a
    flash happened during the capture window.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_event = 0.0

    def mark(self) -> None:
        with self._lock:
            self._last_event = time.monotonic()

    def last_event_mono(self) -> float:
        with self._lock:
            return self._last_event


def arduino_reader(port: str, baud: int, watcher: FlashWatcher) -> None:
    """Background thread: tail the Arduino, mark watcher on FLASH/LIGHTNING."""
    if serial is None:
        log.warning("pyserial not installed; not reading Arduino. `pip install pyserial` to enable.")
        return
    try:
        # dsrdtr=False keeps the Uno from auto-resetting when we open the port.
        ser = serial.Serial(port, baud, timeout=1, dsrdtr=False)
    except (OSError, serial.SerialException) as e:
        log.warning("Arduino serial unavailable on %s: %s (frames won't be tagged)", port, e)
        return

    log.info("Arduino reader connected on %s @ %d baud", port, baud)
    try:
        while not _stop:
            try:
                raw = ser.readline()
            except (OSError, serial.SerialException) as e:
                log.warning("Arduino serial read failed: %s; reader thread exiting", e)
                return
            if not raw:
                continue
            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue
            # Anything that starts with FLASH or LIGHTNING is a "tag this frame" event.
            # Other lines (BOOT, READY, HB, WARN, DISTURBER, NOISE) are logged at debug.
            if line.startswith("FLASH") or line.startswith("LIGHTNING"):
                watcher.mark()
                log.info("arduino: %s", line)
            else:
                log.debug("arduino: %s", line)
    finally:
        try:
            ser.close()
        except Exception:
            pass


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
    p.add_argument("--arduino-port", default=DEFAULT_ARDUINO_PORT,
                   help=f"Arduino serial device for FLASH tagging (default {DEFAULT_ARDUINO_PORT})")
    p.add_argument("--no-arduino", action="store_true",
                   help="don't read the Arduino; capture frames untagged")
    p.add_argument("--burst-interval", type=float, default=DEFAULT_BURST_INTERVAL,
                   help=f"seconds between frames during burst mode (default {DEFAULT_BURST_INTERVAL}, 0 = as fast as possible)")
    p.add_argument("--burst-duration", type=float, default=DEFAULT_BURST_DURATION,
                   help=f"seconds to stay in burst mode after a flash (default {DEFAULT_BURST_DURATION})")
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

    watcher = FlashWatcher()
    reader_thread: threading.Thread | None = None
    if not args.no_arduino:
        reader_thread = threading.Thread(
            target=arduino_reader,
            args=(args.arduino_port, DEFAULT_ARDUINO_BAUD, watcher),
            name="arduino-reader",
            daemon=True,
        )
        reader_thread.start()

    frame_count = 0
    flash_frame_count = 0
    total_bytes = 0
    start_mono = time.monotonic()
    next_deadline = start_mono
    end_mono = start_mono + args.duration if args.duration else None
    in_burst = False

    def is_burst_active() -> bool:
        last = watcher.last_event_mono()
        return last > 0 and (time.monotonic() - last) < args.burst_duration

    try:
        while not _stop:
            if end_mono is not None and time.monotonic() >= end_mono:
                break

            # Mode transition (logged once per change so the journal is readable).
            burst_now = is_burst_active()
            if burst_now and not in_burst:
                log.info("FLASH detected -> entering burst mode (interval=%.2fs, duration=%.0fs)",
                         args.burst_interval, args.burst_duration)
                in_burst = True
            elif not burst_now and in_burst:
                log.info("burst mode ended -> back to %.1fs interval", args.interval)
                in_burst = False
                # Reset cadence so the next normal frame fires "interval" from now,
                # not from a stale next_deadline rooted before the burst.
                next_deadline = time.monotonic()

            now = datetime.now()
            out_dir = session_dir(args.output)
            base_name = now.strftime("img_%H%M%S")
            if in_burst:
                # Microsecond suffix so back-to-back burst frames don't collide
                # at sub-second cadence.
                base_name += f"_{now.microsecond:06d}"
            path = out_dir / f"{base_name}.jpg"

            t0 = time.monotonic()
            cam.capture_file(str(path))
            t1 = time.monotonic()

            # Tag every burst frame with _LIGHTNING.
            if in_burst:
                tagged = path.with_name(path.stem + LIGHTNING_SUFFIX + path.suffix)
                try:
                    path.rename(tagged)
                    path = tagged
                    flash_frame_count += 1
                except OSError as e:
                    log.warning("could not rename %s -> %s: %s", path, tagged, e)

            size = path.stat().st_size
            total_bytes += size
            frame_count += 1
            log.info("captured %s (%.0f KB, %.2fs)%s",
                     path, size / 1024, t1 - t0, " [burst]" if in_burst else "")

            if frame_count % HEARTBEAT_EVERY == 0:
                elapsed = time.monotonic() - start_mono
                log.info("heartbeat: %d frames in %.0fs, %s total",
                         frame_count, elapsed, format_mb(total_bytes))

            # ---------- Inter-frame wait ----------
            if in_burst:
                # Burst: short sleep (or none), but check the burst window
                # frequently so we exit as soon as the duration has elapsed.
                sleep_for = args.burst_interval
                while sleep_for > 0 and not _stop and is_burst_active():
                    chunk = min(sleep_for, 0.1)
                    time.sleep(chunk)
                    sleep_for -= chunk
                continue

            # Normal cadence: deadline-based, but break out early if a flash
            # arrives so we capture the post-flash frame immediately rather
            # than waiting up to args.interval for the next slot.
            next_deadline += args.interval
            sleep_for = next_deadline - time.monotonic()
            if sleep_for < 0:
                missed = int(-sleep_for / args.interval) + 1
                log.warning("capture overran interval by %.2fs; skipping %d slot(s)",
                            -sleep_for, missed)
                next_deadline += missed * args.interval
                sleep_for = next_deadline - time.monotonic()

            flash_at_sleep_start = watcher.last_event_mono()
            while sleep_for > 0 and not _stop:
                chunk = min(sleep_for, 1.0)
                time.sleep(chunk)
                if watcher.last_event_mono() > flash_at_sleep_start:
                    break  # new flash -> capture now, burst mode kicks in next iter
                sleep_for = next_deadline - time.monotonic()
    finally:
        cam.stop()
        cam.close()
        elapsed = time.monotonic() - start_mono
        log.info("session done: %d frames (%d tagged _LIGHTNING) in %.0fs, %s total",
                 frame_count, flash_frame_count, elapsed, format_mb(total_bytes))

    return 0


if __name__ == "__main__":
    sys.exit(main())
