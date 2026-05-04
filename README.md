# Sky Sentry

A sky-watching rig: a Raspberry Pi 5 captures a long-running timelapse, and a companion Arduino with lightning + light sensors signals the Pi to switch into a higher-rate "storm mode" when something interesting is happening overhead. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full system diagram.

## Hardware

- Raspberry Pi 5
- Raspberry Pi HQ Camera (FB2) + Arducam wide-angle M12 lens, aimed at the sky
- External SSD mounted at `/mnt/ssd`
- Arduino Uno R3 + DFRobot SEN0290 (AS3935 lightning sensor) + TEMT6000 phototransistor *(arriving later)*

The Arduino sketch lives in [arduino/sky_sentry/](arduino/sky_sentry/) and requires the `DFRobot_AS3935` library (install via the Arduino IDE Library Manager).

## Pi setup

1. Enable the camera interface: `sudo raspi-config` → Interface Options → Camera.
2. Install picamera2 from apt (it does **not** install cleanly from pip on Raspberry Pi OS):
   ```
   sudo apt update
   sudo apt install -y python3-picamera2
   ```
3. Confirm the SSD is mounted and writable: `ls -la /mnt/ssd && touch /mnt/ssd/.test && rm /mnt/ssd/.test`.

## Running the timelapse

Defaults to one frame every 30 seconds, full HQ Camera resolution, written to `/mnt/ssd/timelapse/<date>/`:

```
python3 pi/timelapse.py
```

Quick smoke test (one frame every 5s, stop after 30s):

```
python3 pi/timelapse.py --interval 5 --duration 30
```

All flags:

```
--interval SECONDS    seconds between frames (default 30)
--output PATH         base output directory (default /mnt/ssd/timelapse)
--duration SECONDS    stop after N seconds (default: run forever)
--resolution WxH      capture size (default 4056x3040)
--quality N           JPEG quality 1-100 (default 90)
```

Ctrl+C exits cleanly and prints a session summary.

### Focus stream

To aim and focus the lens, run the MJPEG streamer and open it in a browser on any device on your LAN:

```
python3 pi/focus_stream.py
```

Then visit `http://<pi-ip>:8000/`. Defaults to 1920x1080 @ 25 Mbps, downscaled from the full sensor for maximum sharpness.

For pixel-peep focus checking, use `--zoom`:

```
python3 pi/focus_stream.py --zoom 4    # crops center 1/4 of sensor → effective 4x focus zoom
```

Stop with Ctrl+C, then start `pi/timelapse.py` for the actual capture (only one process can hold the camera at a time).

## Project layout

- [pi/](pi/) — Raspberry Pi capture and (later) sensor-reader scripts
- [arduino/sky_sentry/](arduino/sky_sentry/) — Arduino sketch for the lightning + flash sensors
- [ARCHITECTURE.md](ARCHITECTURE.md) — system diagram
- [requirements.txt](requirements.txt) — Python deps (note: picamera2 must be installed via apt, not pip)

## Roadmap

- [x] Pi timelapse capture script
- [ ] Wire up SEN0290 + TEMT6000 to the Arduino
- [ ] Pi serial reader for the Arduino event stream
- [ ] Storm-mode capture (long exposures / burst)
- [ ] Frame tagger to mark keepers around `LIGHTNING` / `FLASH` events
