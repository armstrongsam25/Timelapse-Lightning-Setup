# Sky Sentry

A sky-watching rig: a Raspberry Pi 5 captures a long-running timelapse, and a companion Arduino with lightning + light sensors signals the Pi to switch into a higher-rate "storm mode" when something interesting is happening overhead. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full system diagram.

## Hardware

- Raspberry Pi 5
- Raspberry Pi HQ Camera (FB2) + Arducam wide-angle M12 lens, aimed at the sky
- External SSD mounted at `/mnt/ssd`
- Arduino Uno R3 + TEMT6000 phototransistor *(wired to A0)* + DFRobot SEN0290 AS3935 lightning sensor *(pending — arriving later)*

The Arduino sketch lives in [arduino/sky_sentry/](arduino/sky_sentry/) and uses the `DFRobot_AS3935` library. The sketch tolerates a missing AS3935 — if the sensor doesn't ACK on I2C at boot it emits a `WARN as3935_not_present` line and runs in TEMT6000-only mode, so you can fly the rig today and just plug the SEN0290 in when it arrives.

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

## Arduino

Two sketches live under [arduino/](arduino/):

- [arduino/sky_sentry/](arduino/sky_sentry/) — the production sketch. Reads the TEMT6000 on A0 and (if present) the SEN0290 on I2C, and emits line-based events over USB serial at 115200 baud. Tolerates a missing AS3935.
- [arduino/temt6000_test/](arduino/temt6000_test/) — a tiny standalone sketch with no library dependencies, useful for sanity-checking the TEMT6000 wiring. Streams `raw=… volts=… min=… max=… baseline=…` rows and prints `*** FLASH ***` when the reading jumps far above baseline.

### Flashing the Arduino from the Pi

The Pi is headless, so flash with `arduino-cli` instead of the Arduino IDE.

One-time setup on the Pi:

```bash
# 1. Install arduino-cli
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh
sudo mv bin/arduino-cli /usr/local/bin/

# 2. Bootstrap config + AVR core
arduino-cli config init
arduino-cli core update-index
arduino-cli core install arduino:avr

# 3. Library for sky_sentry (NOT needed for temt6000_test)
arduino-cli lib install "DFRobot_AS3935"

# 4. Allow your user to access the serial port. Log out + back in afterwards
#    so the new group membership takes effect; `groups` should then list dialout.
sudo usermod -aG dialout $USER
```

Each flash:

```bash
# Pull latest code onto the Pi
cd ~/Timelapse-Lightning-Setup && git pull

# Confirm the Arduino is detected (note the port + FQBN it reports)
arduino-cli board list
# expected: /dev/ttyACM0   Serial Port (USB)   arduino:avr:uno   Arduino Uno

# Test sketch (no library needed)
arduino-cli compile --fqbn arduino:avr:uno arduino/temt6000_test
arduino-cli upload  -p /dev/ttyACM0 --fqbn arduino:avr:uno arduino/temt6000_test

# Production sketch
arduino-cli compile --fqbn arduino:avr:uno arduino/sky_sentry
arduino-cli upload  -p /dev/ttyACM0 --fqbn arduino:avr:uno arduino/sky_sentry

# Watch serial output (Ctrl+C to exit)
arduino-cli monitor -p /dev/ttyACM0 -c baudrate=115200
```

Notes:
- Cheap Uno clones with a CH340 USB chip enumerate as `/dev/ttyUSB0` instead of `/dev/ttyACM0`. The FQBN is still `arduino:avr:uno`.
- Only one process can hold the serial port at a time. If `arduino-cli upload` complains the port is busy, quit any open `arduino-cli monitor`, `screen`, `picocom`, or Python `pyserial` reader first.

## One-shot launcher: flash Arduino + start timelapse

[pi/sky-sentry.sh](pi/sky-sentry.sh) compiles + uploads `arduino/sky_sentry` and then `exec`s into `pi/timelapse.py`. Useful both as a manual launcher and as the `ExecStart` of the systemd service below.

```
chmod +x pi/sky-sentry.sh
pi/sky-sentry.sh                                 # flash + run forever, defaults
TIMELAPSE_ARGS="--interval 10 --duration 60" pi/sky-sentry.sh   # smoke test
SKIP_FLASH=1 pi/sky-sentry.sh                    # skip flash, just start the camera
ARDUINO_PORT=/dev/ttyUSB0 pi/sky-sentry.sh       # CH340 clone
```

Flash is skipped automatically (with a warning) if `arduino-cli` isn't installed or `$ARDUINO_PORT` doesn't exist, so it won't block the camera from starting.

## Running as a systemd service

Run the rig at boot, restart on failure, and capture stdout/stderr to the journal. [pi/sky-sentry.service](pi/sky-sentry.service) is the unit; install it once with:

```bash
# 1. Edit User=, Group=, WorkingDirectory= and any Environment= lines in the unit
#    to match your setup. The shipped defaults assume user `pi` and the repo at
#    /home/pi/Timelapse-Lightning-Setup.
nano pi/sky-sentry.service

# 2. Drop it into systemd. Symlink so `git pull` keeps it in sync; copy if you'd
#    rather decouple the deployed unit from the repo.
sudo ln -sf "$PWD/pi/sky-sentry.service" /etc/systemd/system/sky-sentry.service
sudo systemctl daemon-reload

# 3. Smoke test in the foreground first.
sudo systemctl start sky-sentry
sudo systemctl status sky-sentry           # should be "active (running)"
journalctl -u sky-sentry -f                # tail logs; Ctrl+C to detach

# 4. Once you're happy, enable at boot.
sudo systemctl enable sky-sentry
```

Day-to-day:

```bash
sudo systemctl stop sky-sentry             # graceful: SIGINT, finish current frame
sudo systemctl restart sky-sentry          # picks up new code after `git pull`
sudo systemctl disable sky-sentry          # don't start on next boot
journalctl -u sky-sentry --since "1 hour ago"
```

Gotchas:
- The user named in `User=` must be in the `dialout` group for `arduino-cli upload` to work, and in the `video` group for `picamera2` to access the camera. `sudo usermod -aG dialout,video <user>` and reboot if you're not sure.
- Only one process can hold the camera at a time. Stop `pi/focus_stream.py` before starting the service.
- The unit re-flashes the Arduino on every start. If the Arduino isn't plugged in (`/dev/ttyACM0` missing), the script logs a warning and skips the flash — the camera still starts.
- To temporarily disable re-flashing without editing the unit: `sudo systemctl set-environment SKIP_FLASH=1` then restart. Clear with `sudo systemctl unset-environment SKIP_FLASH`.

## Project layout

- [pi/](pi/) — Raspberry Pi capture and (later) sensor-reader scripts
- [pi/sky-sentry.sh](pi/sky-sentry.sh) — flash-Arduino-then-launch-timelapse one-shot launcher
- [pi/sky-sentry.service](pi/sky-sentry.service) — systemd unit that runs the launcher at boot
- [arduino/sky_sentry/](arduino/sky_sentry/) — production Arduino sketch (TEMT6000 + optional SEN0290)
- [arduino/temt6000_test/](arduino/temt6000_test/) — standalone TEMT6000 sanity-check sketch
- [ARCHITECTURE.md](ARCHITECTURE.md) — system diagram
- [requirements.txt](requirements.txt) — Python deps (note: picamera2 must be installed via apt, not pip)

## Roadmap

- [x] Pi timelapse capture script
- [x] Wire up TEMT6000 to the Arduino (A0)
- [ ] Wire up SEN0290 (waiting on hardware)
- [ ] Pi serial reader for the Arduino event stream
- [ ] Storm-mode capture (long exposures / burst)
- [ ] Frame tagger to mark keepers around `LIGHTNING` / `FLASH` events
