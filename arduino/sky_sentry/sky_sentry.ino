/*
 * Arduino sketch for Pi + Camera lightning timelapse rig.
 *
 * Reads two sensors and reports interesting events to the Pi over USB serial:
 *   - DFRobot SEN0290 lightning sensor (I2C, IRQ on D2)
 *   - TEMT6000 light sensor (analog A0) - flash detector
 *
 * Required library (install once via Arduino IDE Library Manager):
 *   DFRobot_AS3935   (or grab from https://github.com/DFRobot/DFRobot_AS3935)
 *
 * Serial protocol (115200 baud, line-based, the Pi just needs to readline):
 *   BOOT
 *   WARN as3935_not_present                      <- AS3935 didn't ACK on I2C; sketch runs in TEMT6000-only mode
 *   READY baseline=<n>
 *   LIGHTNING distance_km=<n> intensity=<n>     <- AS3935 detected a real strike
 *   DISTURBER                                    <- AS3935 saw a man-made signal
 *   NOISE                                        <- AS3935 reports too much RF noise
 *   FLASH baseline=<n> peak=<n> delta=<n>        <- TEMT6000 saw a sudden brightening
 *   WARN flash_storm_reseeding                   <- too many flashes in a row, baseline re-seeded
 *   HB baseline=<n> raw=<n>                      <- heartbeat every 30s
 *
 * If the AS3935 isn't wired up (or fails to init), the sketch emits the WARN line
 * once and continues in TEMT6000-only mode. Plug the sensor in, reset the Arduino,
 * and the lightning lines start appearing automatically - no recompile needed.
 */

#include <Wire.h>
#include "DFRobot_AS3935_I2C.h"

// =========================================================================
// Pin assignments
// =========================================================================
#define IRQ_PIN     2     // SEN0290 IRQ -> Arduino D2 (must be interrupt-capable)
#define LIGHT_PIN   A0    // TEMT6000 SIG -> Arduino A0

// =========================================================================
// SEN0290 lightning sensor configuration
// =========================================================================

// I2C address. Default 0x03 with both DIP switches on; change here if you
// flip the switches on the board.
#define AS3935_I2C_ADDR  0x03

// Antenna tuning capacitance (must be a multiple of 8, range 0..120 pF).
// 96 is DFRobot's documented factory default. If the sensor reports many
// disturbers or no events at all, work through DFRobot's tuning procedure
// and set this to whatever value gives you a 500 kHz +/- 3.5% antenna.
#define AS3935_CAPACITANCE  96

// Indoor mode is less sensitive but rejects more household RF noise.
// Outdoor mode is more sensitive but more false-positive-prone.
// Since the rig is at a window pointed at the sky, OUTDOORS is correct.
// Set to 1 for outdoor, 0 for indoor.
#define AS3935_OUTDOOR_MODE  0

DFRobot_AS3935_I2C lightning(IRQ_PIN, AS3935_I2C_ADDR);

// True once we've successfully talked to the AS3935 over I2C. Stays false in
// TEMT6000-only mode so loop() knows to skip the lightning register reads.
bool as3935Available = false;

// Set from the ISR, cleared by loop(). volatile because it crosses contexts.
volatile bool lightningIsrTrig = false;
void lightningISR() { lightningIsrTrig = true; }

// =========================================================================
// TEMT6000 flash detector configuration
// =========================================================================
//
// Strategy: maintain an exponentially-weighted moving average of the ambient
// light level. A "flash" is a reading that's much brighter than the baseline.
// The baseline tracks slow changes (day/night, clouds rolling in) but is
// too sluggish to follow a 100-200ms lightning flash, so the flash shows up
// as a big positive delta.

// EMA decay rate. Smaller = slower baseline adaptation.
// 0.0005 gives the baseline roughly a 2-second time constant at typical loop
// speeds. Slow enough to ignore lightning, fast enough to track sunsets.
const float BASELINE_ALPHA = 0.0005;

// Flash threshold: how many ADC units above baseline counts as a flash.
// The Arduino ADC is 10-bit (0..1023). Tune by watching HB messages -
// the raw value should jitter only a few units around baseline. Start at 80,
// lower if you miss flashes, raise if you see false positives.
const int FLASH_DELTA_THRESHOLD = 80;

// Cooldown after a flash so one bolt does not fire many FLASH messages.
const unsigned long FLASH_COOLDOWN_MS = 500;

// Number of analogReads averaged into one sample. The TEMT6000 + indoor
// LED/fluorescent lighting produces a ~120 Hz envelope (full-wave rectified
// 60 Hz mains; 100 Hz on 50 Hz mains). One sample over ~20 ms covers a
// full cycle of either, so the AC ripple averages out instead of looking
// like a flash. Each analogRead is ~0.1 ms, so 200 reads ~= 20 ms.
const int SAMPLE_AVERAGE_COUNT = 200;

// Heartbeat interval. Pi uses this to confirm the Arduino is alive and to
// see what the current baseline reading looks like.
const unsigned long HEARTBEAT_INTERVAL_MS = 30000;

// Runaway-detector: if more than this many flashes fire inside the window,
// the baseline has clearly gone stale (e.g. someone turned the lights on)
// and we re-seed instead of leaking FLASH lines forever. Real lightning
// storms don't sustain >2 Hz, so this won't trip on actual weather.
const int FLASH_STORM_COUNT = 20;
const unsigned long FLASH_STORM_WINDOW_MS = 5000;

float baseline = 0;
unsigned long lastFlashMs = 0;
unsigned long lastHeartbeatMs = 0;

// Ring of recent flash timestamps, for the runaway detector.
unsigned long flashTimes[FLASH_STORM_COUNT];
int flashTimesIdx = 0;

// Read the light sensor with cycle-length averaging to kill mains ripple.
int readLightAveraged() {
  long sum = 0;
  for (int i = 0; i < SAMPLE_AVERAGE_COUNT; i++) {
    sum += analogRead(LIGHT_PIN);
  }
  return (int)(sum / SAMPLE_AVERAGE_COUNT);
}

// Re-seed baseline from a fresh batch of samples. Used at boot and after a
// flash-storm-driven reset. delay()s between reads so the seed spans enough
// real time to dodge any single mains cycle.
void seedBaseline() {
  long sum = 0;
  for (int i = 0; i < 16; i++) {
    sum += readLightAveraged();
    delay(5);
  }
  baseline = sum / 16.0;
}

// =========================================================================
// setup()
// =========================================================================
void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}  // brief wait, but do not block forever
  Serial.println(F("BOOT"));

  // ---- Lightning sensor init ----
  // A failed begin() means nothing ACKed at AS3935_I2C_ADDR. Most likely the
  // SEN0290 isn't plugged in yet. Warn the Pi once and keep running so the
  // TEMT6000 path still works. When the sensor gets wired up later, a reset
  // will pick it up automatically.
  if (lightning.begin() != 0) {
    Serial.println(F("WARN as3935_not_present"));
  } else {
    lightning.defInit();
    lightning.powerUp();
    if (AS3935_OUTDOOR_MODE) {
      lightning.setOutdoors();
    } else {
      lightning.setIndoors();
    }
    lightning.setTuningCaps(AS3935_CAPACITANCE);
    attachInterrupt(digitalPinToInterrupt(IRQ_PIN), lightningISR, RISING);
    as3935Available = true;
  }

  // ---- Light sensor init: seed the baseline ----
  seedBaseline();

  Serial.print(F("READY baseline="));
  Serial.println((int)baseline);
}

// =========================================================================
// loop()
// =========================================================================
void loop() {
  unsigned long now = millis();

  // ---------- TEMT6000 flash detection ----------
  int reading = readLightAveraged();
  float delta = reading - baseline;

  if (delta > FLASH_DELTA_THRESHOLD && (now - lastFlashMs) > FLASH_COOLDOWN_MS) {
    Serial.print(F("FLASH baseline="));
    Serial.print((int)baseline);
    Serial.print(F(" peak="));
    Serial.print(reading);
    Serial.print(F(" delta="));
    Serial.println((int)delta);
    lastFlashMs = now;

    // Record this flash and check the ring for a storm of flashes that's
    // really just a stale baseline (e.g. lights came on, sensor moved).
    flashTimes[flashTimesIdx] = now;
    flashTimesIdx = (flashTimesIdx + 1) % FLASH_STORM_COUNT;
    unsigned long oldest = flashTimes[flashTimesIdx];   // next slot = oldest after wrap
    if (oldest != 0 && (now - oldest) < FLASH_STORM_WINDOW_MS) {
      Serial.println(F("WARN flash_storm_reseeding"));
      seedBaseline();
      lastFlashMs = 0;
      for (int i = 0; i < FLASH_STORM_COUNT; i++) flashTimes[i] = 0;
      flashTimesIdx = 0;
    }
  }

  // Only update baseline when not in flash cooldown. Otherwise the flash
  // itself drags the baseline upward and masks subsequent strokes.
  if ((now - lastFlashMs) > FLASH_COOLDOWN_MS) {
    baseline = baseline * (1.0 - BASELINE_ALPHA) + reading * BASELINE_ALPHA;
  }

  // ---------- SEN0290 lightning interrupt handling ----------
  if (as3935Available && lightningIsrTrig) {
    lightningIsrTrig = false;
    delay(5);   // give the chip a moment before reading its registers

    uint8_t intSrc = lightning.getInterruptSrc();
    if (intSrc == 1) {
      uint8_t distance = lightning.getLightningDistKm();
      uint32_t energy  = lightning.getStrikeEnergyRaw();
      Serial.print(F("LIGHTNING distance_km="));
      Serial.print(distance);
      Serial.print(F(" intensity="));
      Serial.println(energy);
    } else if (intSrc == 2) {
      Serial.println(F("DISTURBER"));
    } else if (intSrc == 3) {
      Serial.println(F("NOISE"));
    }
  }

  // ---------- Heartbeat ----------
  if (now - lastHeartbeatMs > HEARTBEAT_INTERVAL_MS) {
    Serial.print(F("HB baseline="));
    Serial.print((int)baseline);
    Serial.print(F(" raw="));
    Serial.println(reading);
    lastHeartbeatMs = now;
  }
}
