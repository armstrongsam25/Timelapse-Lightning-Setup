#!/usr/bin/env python3
"""Assemble Sky Sentry capture frames into per-day timelapse videos.

Reads `images/<YYYY-MM-DD>/img_HHMMSS[_LIGHTNING].jpg` (the layout produced by
pi/timelapse.py) and writes one MP4 per date folder into `videos/`.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

DEFAULT_INPUT = Path("images")
DEFAULT_OUTPUT = Path("videos")
DEFAULT_FPS = 60
DEFAULT_CRF = 18
DATE_FMT = "%Y-%m-%d"

log = logging.getLogger("make_timelapse")


def parse_date(s: str) -> date:
    return datetime.strptime(s, DATE_FMT).date()


def parse_resolution(s: str) -> tuple[int, int]:
    w, _, h = s.lower().partition("x")
    return int(w), int(h)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                   help=f"directory containing per-day subfolders (default {DEFAULT_INPUT})")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                   help=f"directory to write MP4s into (default {DEFAULT_OUTPUT})")
    p.add_argument("--start", type=parse_date, default=None,
                   help="only encode folders on or after this date (YYYY-MM-DD)")
    p.add_argument("--end", type=parse_date, default=None,
                   help="only encode folders on or before this date (YYYY-MM-DD)")
    p.add_argument("--fps", type=int, default=DEFAULT_FPS,
                   help=f"output frame rate (default {DEFAULT_FPS})")
    p.add_argument("--crf", type=int, default=DEFAULT_CRF,
                   help=f"libx264 CRF, lower = better quality (default {DEFAULT_CRF})")
    p.add_argument("--resolution", type=parse_resolution, default=None,
                   help="downscale to WxH (default: native capture resolution)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the ffmpeg invocations without running them")
    return p.parse_args()


def discover_sessions(
    input_dir: Path,
    start: date | None,
    end: date | None,
) -> list[tuple[date, Path]]:
    sessions: list[tuple[date, Path]] = []
    for child in sorted(input_dir.iterdir()):
        if not child.is_dir():
            continue
        try:
            d = datetime.strptime(child.name, DATE_FMT).date()
        except ValueError:
            log.info("skipping %s (folder name is not YYYY-MM-DD)", child)
            continue
        if start is not None and d < start:
            continue
        if end is not None and d > end:
            continue
        sessions.append((d, child))
    return sessions


def collect_frames(session_dir: Path) -> list[Path]:
    # Filename sort is chronological: img_HHMMSS.jpg precedes img_HHMMSS_NNNNNN_LIGHTNING.jpg.
    return sorted(p for p in session_dir.iterdir()
                  if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg"))


def write_concat_list(frames: list[Path], list_path: Path) -> None:
    # ffmpeg concat demuxer: each line "file '<absolute path>'", with single
    # quotes inside the path escaped as '\''. Absolute paths sidestep -safe 0
    # and any cwd surprises.
    with list_path.open("w", encoding="utf-8") as f:
        for frame in frames:
            escaped = str(frame.resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")


def build_ffmpeg_cmd(
    list_path: Path,
    output_path: Path,
    fps: int,
    crf: int,
    resolution: tuple[int, int] | None,
) -> list[str]:
    cmd: list[str] = [
        "ffmpeg", "-y",
        "-r", str(fps),
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
    ]
    if resolution is not None:
        cmd += ["-vf", f"scale={resolution[0]}:{resolution[1]}"]
    cmd += [
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", str(crf),
        "-preset", "medium",
        "-movflags", "+faststart",
        str(output_path),
    ]
    return cmd


def encode_session(
    session_date: date,
    session_dir: Path,
    output_dir: Path,
    fps: int,
    crf: int,
    resolution: tuple[int, int] | None,
    dry_run: bool,
) -> bool:
    frames = collect_frames(session_dir)
    if not frames:
        log.warning("%s: no JPEGs found, skipping", session_dir)
        return False

    output_path = output_dir / f"{session_date.isoformat()}.mp4"
    log.info("%s: %d frames -> %s", session_date, len(frames), output_path)

    with tempfile.TemporaryDirectory(prefix="sky-sentry-concat-") as tmp:
        list_path = Path(tmp) / "frames.txt"
        write_concat_list(frames, list_path)
        cmd = build_ffmpeg_cmd(list_path, output_path, fps, crf, resolution)

        if dry_run:
            log.info("dry-run cmd: %s", " ".join(cmd))
            log.info("dry-run list: %s (%d entries)", list_path, len(frames))
            # Print first/last few entries so the user can sanity-check ordering.
            with list_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            preview = lines[:3] + (["...\n"] if len(lines) > 6 else []) + lines[-3:]
            for line in preview:
                sys.stdout.write(line)
            return True

        result = subprocess.run(cmd)
        if result.returncode != 0:
            log.error("%s: ffmpeg exited with code %d", session_date, result.returncode)
            return False
    return True


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )
    args = parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit(
            "ffmpeg not found on PATH. Install it first:\n"
            "  Linux:   sudo apt install ffmpeg\n"
            "  macOS:   brew install ffmpeg\n"
            "  Windows: winget install Gyan.FFmpeg"
        )

    if not args.input.is_dir():
        sys.exit(f"input directory does not exist: {args.input}")

    if args.start and args.end and args.start > args.end:
        sys.exit(f"--start ({args.start}) is after --end ({args.end})")

    sessions = discover_sessions(args.input, args.start, args.end)
    if not sessions:
        log.warning("no date folders matched in %s", args.input)
        return 0

    args.output.mkdir(parents=True, exist_ok=True)

    log.info("encoding %d session(s) from %s -> %s (fps=%d, crf=%d%s)",
             len(sessions), args.input, args.output, args.fps, args.crf,
             f", scale={args.resolution[0]}x{args.resolution[1]}" if args.resolution else "")

    failures = 0
    for session_date, session_dir in sessions:
        ok = encode_session(
            session_date, session_dir, args.output,
            args.fps, args.crf, args.resolution, args.dry_run,
        )
        if not ok:
            failures += 1

    if failures:
        log.error("%d session(s) failed", failures)
        return 1
    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
