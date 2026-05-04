#!/usr/bin/env python3
"""MJPEG focus-helper stream. Open http://<pi-ip>:8000/ in a browser."""

from __future__ import annotations

import argparse
import io
import logging
import socketserver
from http import server
from threading import Condition

from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

PAGE = """\
<!doctype html>
<html><head><title>Sky Sentry focus stream</title>
<style>
  body { margin: 0; background: #111; color: #ddd; font-family: sans-serif; }
  .wrap { display: flex; flex-direction: column; align-items: center; }
  img { max-width: 100vw; max-height: 100vh; }
  .hint { padding: 6px 12px; font-size: 13px; opacity: 0.7; }
</style></head>
<body><div class="wrap">
  <img src="stream.mjpg" />
  <div class="hint">Sky Sentry — focus stream. Ctrl+C in the terminal to stop.</div>
</div></body></html>
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

    def log_message(self, format, *args):
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
    ))
    output = StreamingOutput()
    cam.start_recording(MJPEGEncoder(bitrate=args.bitrate), FileOutput(output))

    if args.zoom > 1.0:
        crop_w = int(sensor_w / args.zoom)
        crop_h = int(sensor_h / args.zoom)
        crop_x = (sensor_w - crop_w) // 2
        crop_y = (sensor_h - crop_h) // 2
        cam.set_controls({"ScalerCrop": (crop_x, crop_y, crop_w, crop_h)})
        logging.info("zoom %.1fx: cropping %dx%d from sensor center", args.zoom, crop_w, crop_h)

    StreamingHandler.output = output
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
