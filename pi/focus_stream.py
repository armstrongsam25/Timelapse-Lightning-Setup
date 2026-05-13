#!/usr/bin/env python3
"""MJPEG focus-helper stream. Open http://<pi-ip>:8000/ in a browser."""

from __future__ import annotations

import argparse
import io
import json
import logging
import socketserver
import urllib.parse
from http import server
from threading import Condition

from libcamera import Transform, controls
from picamera2 import MappedArray, Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

try:
    import cv2
except ImportError:
    cv2 = None  # type: ignore[assignment]


class FocusMeter:
    """Laplacian-variance focus score with a slowly-decaying rolling peak."""

    def __init__(self, roi_frac: float = 0.25, peak_decay: float = 0.997):
        self.roi_frac = roi_frac
        self.peak_decay = peak_decay
        self.peak = 0.0

    def annotate(self, frame):
        h, w = frame.shape[:2]
        rw, rh = int(w * self.roi_frac), int(h * self.roi_frac)
        x0, y0 = (w - rw) // 2, (h - rh) // 2
        roi = frame[y0:y0 + rh, x0:x0 + rw]
        bgr = roi[..., :3] if roi.ndim == 3 and roi.shape[2] >= 3 else roi
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
        score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        self.peak = max(self.peak * self.peak_decay, score)
        ratio = score / self.peak if self.peak > 0 else 0.0

        if ratio > 0.95:
            color, label = (0, 255, 0), "IN FOCUS"
        elif ratio > 0.80:
            color, label = (0, 215, 255), "CLOSE"
        else:
            color, label = (0, 80, 255), "ADJUST"

        cv2.rectangle(frame, (x0, y0), (x0 + rw, y0 + rh), color, 2)
        text = f"{label}  score {score:>6.0f}  peak {self.peak:>6.0f}  {ratio * 100:>3.0f}%"
        cv2.putText(frame, text, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 5)
        cv2.putText(frame, text, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)


class CameraControls:
    """Live-tunable Picamera2 controls for low-light focusing and aiming.

    Mirrors the control keys used in pi/timelapse.py so values dialed in here
    can be copied straight into that script's DEFAULT_MAX_EXPOSURE_US /
    DEFAULT_MAX_GAIN.
    """

    EXPOSURE_MIN_US = 1_000
    GAIN_MIN = 1.0
    FRAME_DURATION_MIN_US = 33_333

    def __init__(self, cam: Picamera2, max_exposure_us: int, max_gain: float,
                 ae_enable: bool = True, exposure_us: int = 100_000,
                 gain: float = 1.0):
        self.cam = cam
        self.max_exposure_us = max_exposure_us
        self.max_gain = max_gain
        self.ae_enable = ae_enable
        self.exposure_us = max(self.EXPOSURE_MIN_US, min(exposure_us, max_exposure_us))
        self.gain = max(self.GAIN_MIN, min(gain, max_gain))

    def update(self, ae_enable=None, exposure_us=None, gain=None) -> None:
        if ae_enable is not None:
            self.ae_enable = bool(ae_enable)
        if exposure_us is not None:
            self.exposure_us = max(self.EXPOSURE_MIN_US,
                                   min(int(exposure_us), self.max_exposure_us))
        if gain is not None:
            self.gain = max(self.GAIN_MIN, min(float(gain), self.max_gain))

    def apply(self) -> None:
        if self.ae_enable:
            self.cam.set_controls({
                "AeEnable": True,
                "AeExposureMode": controls.AeExposureModeEnum.Long,
                "FrameDurationLimits": (self.FRAME_DURATION_MIN_US, self.max_exposure_us),
            })
        else:
            self.cam.set_controls({
                "AeEnable": False,
                "ExposureTime": self.exposure_us,
                "AnalogueGain": self.gain,
                "FrameDurationLimits": (self.FRAME_DURATION_MIN_US, self.max_exposure_us),
            })

    def snapshot(self) -> dict:
        meta = self.cam.capture_metadata() or {}
        return {
            "ae_enable": self.ae_enable,
            "exposure_us": self.exposure_us,
            "gain": self.gain,
            "max_exposure_us": self.max_exposure_us,
            "max_gain": self.max_gain,
            "live": {
                "ExposureTime": meta.get("ExposureTime"),
                "AnalogueGain": meta.get("AnalogueGain"),
                "Lux": meta.get("Lux"),
            },
        }


PAGE = """\
<!doctype html>
<html><head><title>Sky Sentry focus stream</title>
<style>
  body { margin: 0; background: #111; color: #ddd; font-family: sans-serif; }
  .wrap { display: flex; flex-direction: column; align-items: center; }
  img { max-width: 100vw; max-height: 78vh; }
  .hint { padding: 6px 12px; font-size: 13px; opacity: 0.7; }
  .panel { display: grid; grid-template-columns: auto 1fr auto; gap: 8px 14px;
           padding: 12px 20px; background: #1c1c1c; border-radius: 6px;
           margin: 8px; min-width: 420px; align-items: center; }
  .panel label { font-size: 13px; }
  .panel input[type=range] { width: 100%; }
  .panel input:disabled { opacity: 0.3; }
  .val { font-family: monospace; font-size: 13px; min-width: 110px; text-align: right; }
  .live { font-family: monospace; font-size: 12px; opacity: 0.75;
          padding: 4px 20px; text-align: center; }
  .ae-row { grid-column: 1 / -1; }
</style></head>
<body><div class="wrap">
  <img src="stream.mjpg" />
  <div class="panel">
    <div class="ae-row"><label><input type="checkbox" id="ae" checked /> AE auto (uncheck for manual exposure/gain)</label></div>
    <label for="exp">Exposure</label>
    <input id="exp" type="range" min="1000" max="8000000" step="1000" value="100000" disabled />
    <span class="val" id="expVal">100 ms</span>
    <label for="gain">Gain</label>
    <input id="gain" type="range" min="1.0" max="16.0" step="0.1" value="1.0" disabled />
    <span class="val" id="gainVal">1.0x</span>
  </div>
  <div class="live" id="live">live: --</div>
  <div class="hint">Sky Sentry focus stream. Exposures &gt; 1 s drop the preview to &le; 1 fps. Ctrl+C in the terminal to stop.</div>
</div>
<script>
const $ = id => document.getElementById(id);
const fmtExp = us => !us ? '--' : (us >= 1000000 ? (us/1000000).toFixed(2) + ' s' : (us/1000).toFixed(1) + ' ms');
const fmtGain = g => g == null ? '--' : g.toFixed(2) + 'x';

let suppress = false;

function render(s) {
  suppress = true;
  $('ae').checked = s.ae_enable;
  $('exp').max = s.max_exposure_us;
  $('gain').max = s.max_gain;
  if (!s.ae_enable) {
    $('exp').value = s.exposure_us;
    $('gain').value = s.gain;
  }
  $('exp').disabled = s.ae_enable;
  $('gain').disabled = s.ae_enable;
  $('expVal').textContent = fmtExp(+$('exp').value);
  $('gainVal').textContent = (+$('gain').value).toFixed(1) + 'x';
  const ex = s.live.ExposureTime, ga = s.live.AnalogueGain, lx = s.live.Lux;
  $('live').textContent = 'live  exp ' + fmtExp(ex) + '   gain ' + fmtGain(ga) + '   lux ' + (lx == null ? '--' : lx.toFixed(1));
  suppress = false;
}

async function post() {
  if (suppress) return;
  const body = new URLSearchParams({
    ae: $('ae').checked ? 'on' : 'off',
    exposure_us: $('exp').value,
    gain: $('gain').value,
  });
  try {
    const r = await fetch('/controls', { method: 'POST', body });
    render(await r.json());
  } catch (e) { /* ignore transient errors */ }
}

['ae', 'exp', 'gain'].forEach(id => $(id).addEventListener('change', post));
$('exp').addEventListener('input', () => $('expVal').textContent = fmtExp(+$('exp').value));
$('gain').addEventListener('input', () => $('gainVal').textContent = (+$('gain').value).toFixed(1) + 'x');

async function poll() {
  try { const r = await fetch('/controls'); render(await r.json()); } catch (e) {}
}
poll();
setInterval(poll, 500);
</script>
</body></html>
"""


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


class StreamingHandler(server.BaseHTTPRequestHandler):
    output: StreamingOutput  # set on the class below
    controls: CameraControls

    def _write_json(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self.send_response(301)
            self.send_header("Location", "/index.html")
            self.end_headers()
        elif self.path == "/index.html":
            content = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/controls":
            self._write_json(self.controls.snapshot())
        elif self.path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=FRAME")
            self.end_headers()
            try:
                while True:
                    with self.output.condition:
                        self.output.condition.wait()
                        frame = self.output.frame
                    self.wfile.write(b"--FRAME\r\n")
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(frame)))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                logging.info("client %s disconnected", self.client_address[0])
        else:
            self.send_error(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/controls":
            self.send_error(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        params = urllib.parse.parse_qs(body)
        kwargs: dict = {}
        if "ae" in params:
            kwargs["ae_enable"] = params["ae"][0] == "on"
        if "exposure_us" in params:
            try:
                kwargs["exposure_us"] = int(params["exposure_us"][0])
            except ValueError:
                pass
        if "gain" in params:
            try:
                kwargs["gain"] = float(params["gain"][0])
            except ValueError:
                pass
        self.controls.update(**kwargs)
        self.controls.apply()
        self._write_json(self.controls.snapshot())

    def log_message(self, format, *args):
        # Don't log the every-500-ms /controls polling — it would drown out everything else.
        if args and isinstance(args[0], str) and "/controls" in args[0]:
            return
        logging.info("%s - %s", self.client_address[0], format % args)


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    p = argparse.ArgumentParser(description="Sky Sentry MJPEG focus stream.")
    p.add_argument("--port", type=int, default=8000, help="HTTP port (default 8000)")
    p.add_argument("--width", type=int, default=1920, help="stream width (default 1920)")
    p.add_argument("--height", type=int, default=1080, help="stream height (default 1080)")
    p.add_argument("--bitrate", type=int, default=25_000_000,
                   help="MJPEG bitrate bps (default 25 Mbps — high for sharp focus check)")
    p.add_argument("--zoom", type=float, default=1.0,
                   help="digital zoom factor (e.g. 4 = crop center 1/4 of sensor for pixel-peep focus)")
    p.add_argument("--no-focus-overlay", action="store_true",
                   help="disable the on-frame focus-score overlay")
    p.add_argument("--max-exposure-us", type=int, default=8_000_000,
                   help="upper bound for the live ExposureTime slider (default 8_000_000 = 8 s)")
    p.add_argument("--max-gain", type=float, default=16.0,
                   help="upper bound for the live AnalogueGain slider (default 16.0 = IMX477 ceiling)")
    args = p.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        level=logging.INFO,
    )

    cam = Picamera2()
    # Use the largest raw stream so the ISP downscales from full sensor — sharpest result.
    sensor_w, sensor_h = cam.camera_properties["PixelArraySize"]
    cam.configure(cam.create_video_configuration(
        main={"size": (args.width, args.height)},
        raw={"size": (sensor_w, sensor_h)},
        transform=Transform(hflip=1, vflip=1),
    ))
    if not args.no_focus_overlay:
        if cv2 is None:
            logging.warning("cv2 not installed — focus overlay disabled (apt install python3-opencv)")
        else:
            meter = FocusMeter()

            def _draw_overlay(request):
                with MappedArray(request, "main") as m:
                    meter.annotate(m.array)

            cam.pre_callback = _draw_overlay

    output = StreamingOutput()
    cam.start_recording(MJPEGEncoder(bitrate=args.bitrate), FileOutput(output))

    cam_controls = CameraControls(cam, args.max_exposure_us, args.max_gain)
    cam_controls.apply()

    if args.zoom > 1.0:
        crop_w = int(sensor_w / args.zoom)
        crop_h = int(sensor_h / args.zoom)
        crop_x = (sensor_w - crop_w) // 2
        crop_y = (sensor_h - crop_h) // 2
        cam.set_controls({"ScalerCrop": (crop_x, crop_y, crop_w, crop_h)})
        logging.info("zoom %.1fx: cropping %dx%d from sensor center", args.zoom, crop_w, crop_h)

    StreamingHandler.output = output
    StreamingHandler.controls = cam_controls
    try:
        addr = ("", args.port)
        logging.info("streaming on http://<pi-ip>:%d/  (%dx%d, %.1f Mbps)",
                     args.port, args.width, args.height, args.bitrate / 1_000_000)
        StreamingServer(addr, StreamingHandler).serve_forever()
    except KeyboardInterrupt:
        logging.info("stopping")
    finally:
        cam.stop_recording()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
