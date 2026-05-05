/*
 * TEMT6000 wiring + flash-detection sanity check.
 *
 * Intentionally has no library dependencies so it can be flashed from a
 * headless Pi (arduino-cli) without first installing DFRobot_AS3935.
 *
 * Wiring (matches sky_sentry.ino):
 *   TEMT6000 VCC -> Arduino 5V
 *   TEMT6000 GND -> Arduino GND
 *   TEMT6000 SIG -> Arduino A0
 *
 * Open the serial monitor at 115200 baud. Roughly every 100 ms you'll see:
 *   raw=<n> volts=<f> min=<n> max=<n> baseline=<n>
 * and a "*** FLASH delta=<n> ***" line whenever the reading jumps far above
 * the rolling baseline. The flash threshold (80 ADC units) and EMA alpha
 * (0.0005) are deliberately the same numbers sky_sentry.ino uses, so what
 * you see here is what the production sketch will see.
 *
 * Quick acceptance checks:
 *   - Cover the sensor with a finger -> raw drops toward 0, min updates.
 *   - Shine a phone flashlight at it -> raw jumps near 1023, FLASH fires.
 *   - Sit still in normal room light -> raw within ~5 of baseline.
 *
 * Send any byte over the serial monitor to reset the rolling min/max.
 */

#define LIGHT_PIN   A0

// Match sky_sentry.ino so the values you see during testing line up with
// what the production sketch will see.
const float BASELINE_ALPHA = 0.0005;
const int   FLASH_DELTA_THRESHOLD = 80;
const unsigned long FLASH_COOLDOWN_MS = 500;
const unsigned long PRINT_INTERVAL_MS = 100;

float baseline = 0;
int   sessionMin = 1023;
int   sessionMax = 0;
unsigned long lastFlashMs = 0;
unsigned long lastPrintMs = 0;

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}

  // Seed baseline with 16 quick samples (same as sky_sentry.ino).
  long sum = 0;
  for (int i = 0; i < 16; i++) {
    sum += analogRead(LIGHT_PIN);
    delay(10);
  }
  baseline = sum / 16.0;
  sessionMin = (int)baseline;
  sessionMax = (int)baseline;

  Serial.println(F("# TEMT6000 test sketch - 115200 baud"));
  Serial.println(F("# columns: raw=ADC(0..1023) volts=raw*5/1023 min/max=session baseline=EMA"));
  Serial.println(F("# send any byte to reset min/max"));
  Serial.print(F("# seeded baseline="));
  Serial.println((int)baseline);
}

void loop() {
  unsigned long now = millis();
  int reading = analogRead(LIGHT_PIN);
  float delta = reading - baseline;

  if (reading < sessionMin) sessionMin = reading;
  if (reading > sessionMax) sessionMax = reading;

  // Flash detection (same rule as sky_sentry.ino).
  if (delta > FLASH_DELTA_THRESHOLD && (now - lastFlashMs) > FLASH_COOLDOWN_MS) {
    Serial.print(F("*** FLASH delta="));
    Serial.print((int)delta);
    Serial.println(F(" ***"));
    lastFlashMs = now;
  }

  // Don't drag the baseline up during a flash.
  if ((now - lastFlashMs) > FLASH_COOLDOWN_MS) {
    baseline = baseline * (1.0 - BASELINE_ALPHA) + reading * BASELINE_ALPHA;
  }

  if (now - lastPrintMs >= PRINT_INTERVAL_MS) {
    lastPrintMs = now;
    float volts = reading * (5.0 / 1023.0);
    Serial.print(F("raw="));
    Serial.print(reading);
    Serial.print(F(" volts="));
    Serial.print(volts, 2);
    Serial.print(F(" min="));
    Serial.print(sessionMin);
    Serial.print(F(" max="));
    Serial.print(sessionMax);
    Serial.print(F(" baseline="));
    Serial.println((int)baseline);
  }

  // Drain any input and use it as a reset trigger.
  if (Serial.available()) {
    while (Serial.available()) Serial.read();
    sessionMin = reading;
    sessionMax = reading;
    Serial.println(F("# min/max reset"));
  }
}
