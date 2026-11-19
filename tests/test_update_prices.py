import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import update_prices as U  # noqa: E402

M = 1e-6


class Convert(unittest.TestCase):
    def test_long_context_prices_become_a_long_block(self):
        e = {"input_cost_per_token": 8 * M, "cache_read_input_token_cost": 0.4 * M, "output_cost_per_token": 40 * M,
             "input_cost_per_token_above_272k_tokens": 16 * M,
             "cache_read_input_token_cost_above_272k_tokens": 0.8 * M,
             "output_cost_per_token_above_272k_tokens": 60 * M}
        self.assertEqual(U.convert(e), {"input": 8.0, "cached": 0.4, "output": 40.0, "long_context_over": 272000,
                                        "long": {"input": 16.0, "cached": 0.8, "output": 60.0}})

    def test_off_peak_windows_become_peak_hours_on_weekdays(self):
        e = {"input_cost_per_token": 0.3 * M, "cache_read_input_token_cost": 0.006 * M,
             "output_cost_per_token": 1.2 * M,
             "off_peak_pricing": {"input_cost_per_token": 0.15 * M, "cache_read_input_token_cost": 0.003 * M,
                                  "output_cost_per_token": 0.6 * M,
                                  "windows": [{"hours_utc": ["00:00-01:00", "04:00-06:00", "10:00-00:00"],
                                               "weekdays": [1, 2, 3, 4, 5]},
                                              {"hours_utc": "00:00-00:00", "weekdays": [6, 7]}]}}
        self.assertEqual(U.convert(e), {"input": 0.15, "cached": 0.003, "output": 0.6,
                                        "peak": {"input": 0.3, "cached": 0.006, "output": 1.2},
                                        "peak_utc_weekdays": [[1, 4], [6, 10]]})

    def test_long_context_and_off_peak_together_are_refused(self):
        e = {"input_cost_per_token": 1 * M, "output_cost_per_token": 2 * M,
             "input_cost_per_token_above_272k_tokens": 2 * M, "output_cost_per_token_above_272k_tokens": 4 * M,
             "off_peak_pricing": {"input_cost_per_token": 0.5 * M, "output_cost_per_token": 1 * M,
                                  "windows": [{"hours_utc": "00:00-00:00", "weekdays": [1, 2, 3, 4, 5, 6, 7]}]}}
        with self.assertRaises(ValueError):
            U.convert(e)

    def test_a_missing_cache_price_is_the_input_price(self):
        e = {"input_cost_per_token": 2 * M, "output_cost_per_token": 4 * M}
        self.assertEqual(U.convert(e), {"input": 2.0, "cached": 2.0, "output": 4.0})

    def test_windows_prices_json_cannot_hold_are_refused(self):
        for windows in ([{"hours_utc": "00:00-12:00", "weekdays": [1, 2, 3, 4, 5, 6, 7]}],  # weekend peak
                        [{"hours_utc": "00:00-12:00", "weekdays": [1]},                       # weekdays differ
                         {"hours_utc": "00:00-00:00", "weekdays": [2, 3, 4, 5, 6, 7]}],
                        [{"hours_utc": "00:30-01:00", "weekdays": [1, 2, 3, 4, 5, 6, 7]}],    # not whole hours
                        [{"hours_utc": "22:00-02:00", "weekdays": [1, 2, 3, 4, 5]},           # crosses midnight
                         {"hours_utc": "00:00-00:00", "weekdays": [6, 7]}]):
            with self.subTest(windows=windows), self.assertRaises(ValueError):
                U.peak_ranges(windows)


if __name__ == "__main__":
    unittest.main()
