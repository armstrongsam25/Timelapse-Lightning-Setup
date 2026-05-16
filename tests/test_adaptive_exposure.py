"""Unit tests for the adaptive exposure controller."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Add the pi/ directory to the path so we can import timelapse without
# the picamera2/libcamera dependencies being present.
# We mock those modules before importing.
sys.modules["libcamera"] = MagicMock()
sys.modules["picamera2"] = MagicMock()
sys.modules["PIL"] = MagicMock()
sys.modules["PIL.Image"] = MagicMock()

# Now import (the mocked picamera2/libcamera won't blow up on Windows/CI)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pi"))
import timelapse as tl


class TestAdaptiveExposureController(unittest.TestCase):
    def _make(self, **kw):
        defaults = dict(
            target_brightness=110,
            min_exposure_us=100,
            max_exposure_us=20_000_000,
            min_gain=1.0,
            max_gain=16.0,
        )
        defaults.update(kw)
        return tl.AdaptiveExposureController(**defaults)

    def test_deadzone_no_change(self):
        """Brightness within ±DEADZONE of target should not adjust anything."""
        aec = self._make()
        aec.exposure_us = 50_000
        aec.gain = 4.0

        # Exactly at target
        aec.update(110)
        self.assertEqual(aec.exposure_us, 50_000)
        self.assertEqual(aec.gain, 4.0)

        # Just inside deadzone (bright side)
        aec.update(110 + tl.BRIGHTNESS_DEADZONE)
        self.assertEqual(aec.exposure_us, 50_000)
        self.assertEqual(aec.gain, 4.0)

        # Just inside deadzone (dark side)
        aec.update(110 - tl.BRIGHTNESS_DEADZONE)
        self.assertEqual(aec.exposure_us, 50_000)
        self.assertEqual(aec.gain, 4.0)

    def test_too_bright_reduces_gain_first(self):
        """When image is too bright and gain > min, gain should decrease first."""
        aec = self._make()
        aec.exposure_us = 100_000
        aec.gain = 8.0

        aec.update(160)  # 50 above target, outside deadzone
        self.assertLess(aec.gain, 8.0, "gain should decrease")
        self.assertEqual(aec.exposure_us, 100_000, "exposure should stay when gain can decrease")

    def test_too_bright_reduces_exposure_when_gain_at_min(self):
        """When gain is already at minimum, exposure should decrease."""
        aec = self._make()
        aec.exposure_us = 100_000
        aec.gain = 1.0  # at minimum

        aec.update(160)
        self.assertLess(aec.exposure_us, 100_000, "exposure should decrease")
        self.assertEqual(aec.gain, 1.0, "gain should stay at min")

    def test_too_dark_increases_exposure_first(self):
        """When image is too dark and exposure < max, exposure should increase first."""
        aec = self._make()
        aec.exposure_us = 100_000
        aec.gain = 4.0

        aec.update(50)  # 60 below target, outside deadzone
        self.assertGreater(aec.exposure_us, 100_000, "exposure should increase")
        self.assertEqual(aec.gain, 4.0, "gain should stay when exposure can increase")

    def test_too_dark_increases_gain_when_exposure_at_max(self):
        """When exposure is already at max, gain should increase."""
        aec = self._make()
        aec.exposure_us = 20_000_000  # at maximum
        aec.gain = 4.0

        aec.update(50)
        self.assertEqual(aec.exposure_us, 20_000_000, "exposure should stay at max")
        self.assertGreater(aec.gain, 4.0, "gain should increase")

    def test_exposure_clamped_at_min(self):
        """Exposure should never go below min_exposure_us."""
        aec = self._make()
        aec.exposure_us = 150  # near minimum
        aec.gain = 1.0  # at minimum, so exposure gets reduced

        aec.update(250)  # severely overexposed
        self.assertGreaterEqual(aec.exposure_us, 100, "exposure should not go below min")

    def test_gain_clamped_at_max(self):
        """Gain should never exceed max_gain."""
        aec = self._make()
        aec.exposure_us = 20_000_000  # at max
        aec.gain = 15.5  # near max

        aec.update(10)  # severely underexposed
        self.assertLessEqual(aec.gain, 16.0, "gain should not exceed max")

    def test_aggressive_steps_for_severe_error(self):
        """Severe errors (|error| > 60) should use more aggressive step factors."""
        aec_moderate = self._make()
        aec_moderate.exposure_us = 100_000
        aec_moderate.gain = 1.0

        aec_severe = self._make()
        aec_severe.exposure_us = 100_000
        aec_severe.gain = 1.0

        # Moderate error: brightness 150, error = 40
        aec_moderate.update(150)
        # Severe error: brightness 250, error = 140
        aec_severe.update(250)

        self.assertLess(aec_severe.exposure_us, aec_moderate.exposure_us,
                        "severe error should reduce exposure more aggressively")

    def test_negative_brightness_holds_state(self):
        """Measurement failure (brightness < 0) should not change settings."""
        aec = self._make()
        aec.exposure_us = 50_000
        aec.gain = 4.0

        aec.update(-1.0)
        self.assertEqual(aec.exposure_us, 50_000)
        self.assertEqual(aec.gain, 4.0)

    def test_convergence_from_overexposed(self):
        """Simulate a dawn transition: max exposure/gain with bright scene."""
        aec = self._make()
        aec.exposure_us = 20_000_000  # max
        aec.gain = 16.0               # max

        # Simulate several frames of "way too bright" (255 = solid white)
        for _ in range(20):
            aec.update(255)

        # Should have ramped down significantly
        self.assertLess(aec.gain, 4.0, "gain should have come way down")

    def test_convergence_from_underexposed(self):
        """Simulate dusk: low exposure/gain with dark scene."""
        aec = self._make()
        aec.exposure_us = 1_000  # 1 ms
        aec.gain = 1.0

        # Simulate several frames of "way too dark" (5 ≈ pitch black)
        for _ in range(20):
            aec.update(5)

        # Should have ramped up significantly
        self.assertGreater(aec.exposure_us, 100_000,
                           "exposure should have ramped up significantly")

    def test_status_str(self):
        """status_str() should return a parseable summary."""
        aec = self._make()
        aec.exposure_us = 50_000
        aec.gain = 4.0
        aec.last_brightness = 115.3

        s = aec.status_str()
        self.assertIn("exp=", s)
        self.assertIn("gain=", s)
        self.assertIn("brightness=", s)
        self.assertIn("target=", s)


if __name__ == "__main__":
    unittest.main()
