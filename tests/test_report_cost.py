#!/usr/bin/env python3
"""report_cost 的纯函数测试：成本口径必须能从给定输入重算。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_cost import cost_from_window


class TestCostFromWindow(unittest.TestCase):
    def test_known_window(self):
        # 平均功率 50W × 120s = 6000 J = 1/600 kWh；×0.6 元/kWh = 0.001 元
        watts = [50.0] * 10
        c = cost_from_window(watts, wall_seconds=120.0, price_cny_per_kwh=0.6)
        self.assertAlmostEqual(c["mean_power_w"], 50.0)
        self.assertAlmostEqual(c["kwh"], 50.0 * 120 / 3.6e6, places=12)
        self.assertAlmostEqual(c["cny"], 50.0 * 120 / 3.6e6 * 0.6, places=12)

    def test_empty_window_fails_closed(self):
        with self.assertRaises(ValueError):
            cost_from_window([], wall_seconds=10.0, price_cny_per_kwh=0.6)

    def test_negative_wall_fails_closed(self):
        with self.assertRaises(ValueError):
            cost_from_window([50.0], wall_seconds=-1, price_cny_per_kwh=0.6)


if __name__ == "__main__":
    unittest.main()
