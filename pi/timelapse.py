#!/usr/bin/env python3
"""Sky Sentry timelapse capture."""

from __future__ import annotations

import argparse
import logging
import queue
import shutil
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image

from libcamera import Transform, controls
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

# Adaptive exposure controller.
# Instead of a binary night/auto toggle, we run a continuous feedback loop:
# measure actual image brightness after each capture, compare to a target,
# and adjust exposure time + gain with multiplicative steps.  This gives
# smooth transitions across the full day/night cycle — no more blown-out
# white frames at dawn.
DEFAULT_MAX_EXPOSURE_US = 20_000_000   # 20 s — enough for night sky detail
DEFAULT_MAX_GAIN = 16.0                # IMX477 analog gain ceiling
DEFAULT_MIN_EXPOSURE_US = 100          # ~1/10000 s floor for bright daylight
DEFAULT_MIN_GAIN = 1.0                 # minimum analog gain
MIN_FRAME_DURATION_US = 33_333         # ~30 fps lower bound

# Brightness feedback loop tuning
DEFAULT_TARGET_BRIGHTNESS = 110        # target mean pixel value (0-255)
BRIGHTNESS_DEADZONE = 15               # no adjustment within ±this of target
STEP_NORMAL_DOWN = 0.7                 # multiply exposure/gain by this when moderately bright
STEP_NORMAL_UP = 1.4                   # multiply when moderately dark
STEP_AGGRESSIVE_DOWN = 0.5             # multiply when severely overexposed (error > 60)
STEP_AGGRESSIVE_UP = 2.0               # multiply when severely underexposed
SEVERE_ERROR_THRESHOLD = 60            # |error| above this uses aggressive steps
BRIGHTNESS_MEASURE_SIZE = 320          # thumbnail width for fast brightness calc

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


class NtfyNotifier:
    """Background pusher: PUT a JPEG to ntfy when lightning is detected.

    The capture loop calls `enqueue()` once per burst (on the first tagged
    _LIGHTNING frame). A single daemon worker drains a bounded queue and
    sends each image as the body of a PUT request, with title/priority/tags
    in headers. All network errors are logged and swallowed — the capture
    loop is never blocked by a slow or downed ntfy.
    """

    _SENTINEL = object()

    def __init__(self, base_url: str, topic: str, enabled: bool) -> None:
        self.url = f"{base_url.rstrip('/')}/{topic}"
        self.enabled = enabled
        self._q: queue.Queue = queue.Queue(maxsize=4)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not self.enabled:
            log.info("ntfy disabled; lightning notifications will not be sent")
            return
        self._thread = threading.Thread(
            target=self._worker, name="ntfy-sender", daemon=True
        )
        self._thread.start()
        log.info("ntfy notifier started -> %s", self.url)

    def enqueue(self, image_path: Path, when: datetime) -> None:
        if not self.enabled:
            return
        try:
            self._q.put_nowait((image_path, when))
        except queue.Full:
            log.warning("ntfy queue full, dropping notification for %s", image_path)

    def stop(self) -> None:
        if not self.enabled or self._thread is None:
            return
        self._stop.set()
        # Nudge the worker out of q.get() promptly.
        try:
            self._q.put_nowait(self._SENTINEL)
        except queue.Full:
            pass

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=1)
            except queue.Empty:
                continue
            if item is self._SENTINEL:
                return
            image_path, when = item
            self._send_one(image_path, when)

    def _send_one(self, image_path: Path, when: datetime) -> None:
        try:
            data = image_path.read_bytes()
        except OSError as e:
            log.warning("ntfy: could not read %s: %s", image_path, e)
            return
        headers = {
            "Title": "Lightning detected",
            "Message": f"{when:%H:%M:%S} - burst started ({image_path.name})",
            "Priority": "urgent",
            "Tags": "zap,camera_flash",
            "Filename": image_path.name,
        }
        req = urllib.request.Request(
            self.url, data=data, method="PUT", headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status >= 400:
                    log.warning("ntfy: server returned %d for %s",
                                resp.status, image_path.name)
                else:
                    log.info("ntfy: pushed %s (%d bytes)",
                             image_path.name, len(data))
        except urllib.error.HTTPError as e:
            log.warning("ntfy: HTTP %d sending %s: %s",
                        e.code, image_path.name, e.reason)
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            log.warning("ntfy: send failed for %s: %s", image_path.name, e)
        except Exception as e:  # never let the worker die on an unexpected error
            log.warning("ntfy: unexpected error sending %s: %s",
                        image_path.name, e)


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
    p.add_argument("--ntfy-url", default="http://localhost:8080",
                   help="base URL of the ntfy server (default http://localhost:8080)")
    p.add_argument("--ntfy-topic", default="timelapse",
                   help="ntfy topic to publish lightning notifications to (default 'timelapse')")
    p.add_argument("--no-ntfy", action="store_true",
                   help="disable ntfy push notifications on lightning")
    p.add_argument("--max-exposure-us", type=int, default=DEFAULT_MAX_EXPOSURE_US,
                   help=f"upper bound on shutter speed in microseconds (default {DEFAULT_MAX_EXPOSURE_US})")
    p.add_argument("--max-gain", type=float, default=DEFAULT_MAX_GAIN,
                   help=f"upper bound on analog gain (default {DEFAULT_MAX_GAIN})")
    p.add_argument("--target-brightness", type=int, default=DEFAULT_TARGET_BRIGHTNESS,
                   help=f"target mean pixel brightness 0-255 (default {DEFAULT_TARGET_BRIGHTNESS})")
    p.add_argument("--no-adaptive", action="store_true",
                   help="disable adaptive exposure controller (AE-only fallback)")
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


class AdaptiveExposureController:
    """Closed-loop brightness controller for the timelapse camera.

    After each frame, `update()` measures the mean brightness of the captured
    JPEG and adjusts exposure time and analog gain with multiplicative steps
    to converge on `target_brightness`.  Priority ordering:

      Ramp-down (too bright): reduce gain first, then exposure.
      Ramp-up  (too dark):    increase exposure first, then gain.

    This replaces the old binary night/auto mode system with a smooth,
    continuous controller that handles the full day/night cycle.
    """

    def __init__(self, target_brightness: int = DEFAULT_TARGET_BRIGHTNESS,
                 min_exposure_us: int = DEFAULT_MIN_EXPOSURE_US,
                 max_exposure_us: int = DEFAULT_MAX_EXPOSURE_US,
                 min_gain: float = DEFAULT_MIN_GAIN,
                 max_gain: float = DEFAULT_MAX_GAIN) -> None:
        self.target = target_brightness
        self.min_exp = min_exposure_us
        self.max_exp = max_exposure_us
        self.min_gain = min_gain
        self.max_gain = max_gain
        # Current state — seeded from AE or set manually.
        self.exposure_us: int = 10_000     # 10 ms default starting point
        self.gain: float = 1.0
        self.last_brightness: float = -1.0  # last measured brightness

    def measure_brightness(self, image_path: Path) -> float:
        """Return mean pixel brightness (0-255) of the captured JPEG.

        Downsamples to a small thumbnail for speed (~5 ms on Pi 5).
        """
        try:
            with Image.open(image_path) as img:
                img.thumbnail((BRIGHTNESS_MEASURE_SIZE,
                               BRIGHTNESS_MEASURE_SIZE))
                gray = img.convert("L")
                pixels = gray.getdata()
                mean_val = sum(pixels) / len(pixels)
                return mean_val
        except Exception as e:
            log.warning("brightness measurement failed for %s: %s",
                        image_path, e)
            return -1.0

    def update(self, brightness: float) -> tuple[int, float]:
        """Compute new exposure/gain based on measured brightness.

        Returns (exposure_us, gain) to apply for the next frame.
        """
        self.last_brightness = brightness
        if brightness < 0:
            # Measurement failed — hold current settings.
            return self.exposure_us, self.gain

        error = brightness - self.target

        if abs(error) <= BRIGHTNESS_DEADZONE:
            # Within acceptable range — don't adjust.
            return self.exposure_us, self.gain

        # Choose step factor based on error magnitude.
        if abs(error) > SEVERE_ERROR_THRESHOLD:
            step_down = STEP_AGGRESSIVE_DOWN
            step_up = STEP_AGGRESSIVE_UP
        else:
            step_down = STEP_NORMAL_DOWN
            step_up = STEP_NORMAL_UP

        if error > 0:
            # Too bright — reduce.  Gain first (faster response), then exposure.
            self._ramp_down(step_down)
        else:
            # Too dark — increase.  Exposure first (cleaner), then gain.
            self._ramp_up(step_up)

        return self.exposure_us, self.gain

    def _ramp_down(self, factor: float) -> None:
        """Reduce brightness: cut gain first, then exposure."""
        if self.gain > self.min_gain:
            self.gain = max(self.min_gain, self.gain * factor)
        else:
            self.exposure_us = max(self.min_exp,
                                  int(self.exposure_us * factor))

    def _ramp_up(self, factor: float) -> None:
        """Increase brightness: raise exposure first, then gain."""
        if self.exposure_us < self.max_exp:
            self.exposure_us = min(self.max_exp,
                                  int(self.exposure_us * factor))
        else:
            self.gain = min(self.max_gain, self.gain * factor)

    def apply(self, cam: Picamera2) -> None:
        """Push current exposure/gain to the camera as manual controls."""
        cam.set_controls({
            "AeEnable": False,
            "ExposureTime": self.exposure_us,
            "AnalogueGain": self.gain,
            "FrameDurationLimits": (MIN_FRAME_DURATION_US, self.max_exp),
        })

    def seed_from_ae(self, cam: Picamera2) -> None:
        """Read AE's current exposure/gain choice and use as starting point.

        Call this after AE has settled (camera started, waited ~2 s) so the
        controller inherits a reasonable initial state rather than starting
        from arbitrary defaults.
        """
        md = cam.capture_metadata()
        if md:
            exp = md.get("ExposureTime")
            gain = md.get("AnalogueGain")
            if exp is not None:
                self.exposure_us = max(self.min_exp, min(int(exp), self.max_exp))
            if gain is not None:
                self.gain = max(self.min_gain, min(float(gain), self.max_gain))
        log.info("adaptive exposure seeded from AE: %d µs, %.1fx gain",
                 self.exposure_us, self.gain)

    def status_str(self) -> str:
        """One-line summary for log heartbeats."""
        return (f"exp={self.exposure_us / 1000:.1f}ms "
                f"gain={self.gain:.1f}x "
                f"brightness={self.last_brightness:.0f}/255 "
                f"target={self.target}")


def build_camera(resolution: tuple[int, int], quality: int,
                 max_exposure_us: int) -> Picamera2:
    cam = Picamera2()
    config = cam.create_still_configuration(
        main={"size": resolution},
        transform=Transform(hflip=1, vflip=1),
    )
    cam.configure(config)
    cam.options["quality"] = quality
    cam.set_controls({
        "AeEnable": True,
        "AeExposureMode": controls.AeExposureModeEnum.Long,
        "FrameDurationLimits": (MIN_FRAME_DURATION_US, max_exposure_us),
    })
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

    cam = build_camera(args.resolution, args.quality, args.max_exposure_us)

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

    notifier = NtfyNotifier(args.ntfy_url, args.ntfy_topic, enabled=not args.no_ntfy)
    notifier.start()

    # ---------- Adaptive exposure controller ----------
    if not args.no_adaptive:
        aec = AdaptiveExposureController(
            target_brightness=args.target_brightness,
            min_exposure_us=DEFAULT_MIN_EXPOSURE_US,
            max_exposure_us=args.max_exposure_us,
            min_gain=DEFAULT_MIN_GAIN,
            max_gain=args.max_gain,
        )
        aec.seed_from_ae(cam)
        aec.apply(cam)
        log.info("adaptive exposure controller active (target=%d)",
                 args.target_brightness)
    else:
        aec = None
        log.info("adaptive exposure disabled — using camera AE only")

    frame_count = 0
    flash_frame_count = 0
    total_bytes = 0
    start_mono = time.monotonic()
    next_deadline = start_mono
    end_mono = start_mono + args.duration if args.duration else None
    in_burst = False
    burst_notified = False

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
                burst_notified = False
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
                    if not burst_notified:
                        notifier.enqueue(path, now)
                        burst_notified = True
                except OSError as e:
                    log.warning("could not rename %s -> %s: %s", path, tagged, e)

            size = path.stat().st_size
            total_bytes += size
            frame_count += 1

            # ---------- Adaptive exposure feedback ----------
            if aec is not None and not in_burst:
                brightness = aec.measure_brightness(path)
                prev_exp, prev_gain = aec.exposure_us, aec.gain
                aec.update(brightness)
                aec.apply(cam)
                log.info("captured %s (%.0f KB, %.2fs) | bright=%.0f "
                         "exp=%d→%dµs gain=%.1f→%.1fx",
                         path, size / 1024, t1 - t0, brightness,
                         prev_exp, aec.exposure_us, prev_gain, aec.gain)
            else:
                log.info("captured %s (%.0f KB, %.2fs)%s",
                         path, size / 1024, t1 - t0,
                         " [burst]" if in_burst else "")

            if frame_count % HEARTBEAT_EVERY == 0:
                elapsed = time.monotonic() - start_mono
                aec_status = f" | {aec.status_str()}" if aec else ""
                log.info("heartbeat: %d frames in %.0fs, %s total%s",
                         frame_count, elapsed, format_mb(total_bytes),
                         aec_status)

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
        notifier.stop()
        cam.stop()
        cam.close()
        elapsed = time.monotonic() - start_mono
        log.info("session done: %d frames (%d tagged _LIGHTNING) in %.0fs, %s total",
                 frame_count, flash_frame_count, elapsed, format_mb(total_bytes))

    return 0


if __name__ == "__main__":
    sys.exit(main())
