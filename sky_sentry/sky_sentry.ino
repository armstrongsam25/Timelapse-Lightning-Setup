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
 *   READY baseline=<n>
 *   LIGHTNING distance_km=<n> intensity=<n>     <- AS3935 detected a real strike
 *   DISTURBER                                    <- AS3935 saw a man-made signal
 *   NOISE                                        <- AS3935 reports too much RF noise
 *   FLASH baseline=<n> peak=<n> delta=<n>        <- TEMT6000 saw a sudden brightening
 *   HB baseline=<n> raw=<n>                      <- heartbeat every 30s
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

// Heartbeat interval. Pi uses this to confirm the Arduino is alive and to
// see what the current baseline reading looks like.
const unsigned long HEARTBEAT_INTERVAL_MS = 30000;

float baseline = 0;
unsigned long lastFlashMs = 0;
unsigned long lastHeartbeatMs = 0;

// =========================================================================
// setup()
// =========================================================================
void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}  // brief wait, but do not block forever
  Serial.println(F("BOOT"));

  // ---- Lightning sensor init ----
  if (lightning.begin() != 0) {
    Serial.println(F("ERROR as3935_init_failed"));
    while (1) delay(1000);   // halt - check wiring and I2C address
  }
  lightning.defInit();
  lightning.powerUp();
  if (AS3935_OUTDOOR_MODE) {
    lightning.setOutdoors();
  } else {
    lightning.setIndoors();
  }
  lightning.setTuningCaps(AS3935_CAPACITANCE);

  attachInterrupt(digitalPinToInterrupt(IRQ_PIN), lightningISR, RISING);

  // ---- Light sensor init: seed the baseline with 16 samples ----
  long sum = 0;
  for (int i = 0; i < 16; i++) {
    sum += analogRead(LIGHT_PIN);
    delay(10);
  }
  baseline = sum / 16.0;

  Serial.print(F("READY baseline="));
  Serial.println((int)baseline);
}

// =========================================================================
// loop()
// =========================================================================
void loop() {
  unsigned long now = millis();

  // ---------- TEMT6000 flash detection ----------
  int reading = analogRead(LIGHT_PIN);
  float delta = reading - baseline;

  if (delta > FLASH_DELTA_THRESHOLD && (now - lastFlashMs) > FLASH_COOLDOWN_MS) {
    Serial.print(F("FLASH baseline="));
    Serial.print((int)baseline);
    Serial.print(F(" peak="));
    Serial.print(reading);
    Serial.print(F(" delta="));
    Serial.println((int)delta);
    lastFlashMs = now;
  }

  // Only update baseline when not in flash cooldown. Otherwise the flash
  // itself drags the baseline upward and masks subsequent strokes.
  if ((now - lastFlashMs) > FLASH_COOLDOWN_MS) {
    baseline = baseline * (1.0 - BASELINE_ALPHA) + reading * BASELINE_ALPHA;
  }

  // ---------- SEN0290 lightning interrupt handling ----------
  if (lightningIsrTrig) {
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
