"""Run the real firmware timing collector against a deterministic host clock."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "inkplate/AvianVisitorsInkplate"


@unittest.skipUnless(shutil.which("g++"), "g++ required")
class MenuTimingTests(unittest.TestCase):
    def test_durations_retention_and_opening_only(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "Arduino.h").write_text(r'''
#pragma once
#include <cstdint>
#include <string>
#define RTC_DATA_ATTR
class String : public std::string {
 public:
  using std::string::string;
  String(uint32_t value) : std::string(std::to_string(value)) {}
};
''')
            (directory / "AvianPartialTiming.h").write_text("#define AVIAN_PARTIAL_TIMING_VERSION 1\n")
            (directory / "esp_timer.h").write_text("#include <cstdint>\nint64_t esp_timer_get_time();\n")
            (directory / "main.cpp").write_text(r'''
#include "menu_timing.h"
#include <cstdio>
int64_t clockUs=0;
int64_t esp_timer_get_time() { return clockUs; }
extern "C" void avianInkplatePartialTiming(uint8_t);
void emit() { std::puts(menuTimingJson().c_str()); }
int main() {
  emit(); // No successful opening yet.
  clockUs=1000; menuTimingSetupStart();
  clockUs=2000; menuTimingStart();
  emit(); // An incomplete first opening remains absent.
  clockUs=5000; menuTimingCacheLoaded();
  clockUs=20000; menuTimingGraphicsReady();
  clockUs=30000; menuTimingDrawingReady();
  const int64_t phases[]={40000,50000,150000,180000,2000000,2050000};
  for (uint8_t i=0; i<6; ++i) { clockUs=phases[i]; avianInkplatePartialTiming(i); }
  avianInkplatePartialTiming(6); // Invalid phases cannot alter/corrupt sample.
  clockUs=2100000; menuTimingFinished();
  emit();
  // Dismissal happens later; the same driver hooks must not overwrite opening.
  clockUs=12100000;
  for (uint8_t i=0; i<6; ++i) { clockUs+=100000; avianInkplatePartialTiming(i); }
  emit();
  // A later cache failure/new incomplete attempt retains the last good sample.
  clockUs=14000000; menuTimingStart();
  emit();
  clockUs=14100000; menuTimingCacheLoaded();
  clockUs=14200000; menuTimingGraphicsReady();
  clockUs=14300000; menuTimingDrawingReady();
  avianInkplatePartialTiming(0);
  emit();
  // A fresh attempt resets the unfinished active sample and recording flag.
  clockUs=15000000; menuTimingStart();
  avianInkplatePartialTiming(5);
  emit();
}
''')
            binary = directory / "timing"
            subprocess.run([
                "g++", "-std=c++17", "-Wall", "-Werror", "-I", str(directory),
                "-I", str(FIRMWARE), str(directory / "main.cpp"),
                str(FIRMWARE / "menu_timing.cpp"), "-o", str(binary),
            ], check=True)
            lines = subprocess.check_output([str(binary)], text=True).splitlines()
            self.assertEqual(lines[:2], ["", ""])
            self.assertEqual(len(lines), 7)
            sample = json.loads(lines[2])
            expected = {
                "schema": 1, "boot_to_setup_us": 1000, "setup_to_menu_us": 1000,
                "cache_us": 3000, "graphics_init_us": 15000, "draw_us": 10000,
                "driver_prepare_us": 10000, "panel_init_us": 100000,
                "transfer_us": 30000, "refresh_us": 1820000,
                "poweroff_us": 50000, "driver_total_us": 2070000,
                "menu_total_us": 2098000, "boot_to_ready_us": 2000000,
                "driver_hooks": True,
            }
            for field, value in expected.items():
                self.assertEqual(sample[field], value, field)
            self.assertGreater(sample["firmware_build"], 0)
            self.assertGreaterEqual(sample["captured_at"], 1704067200)
            self.assertLess(sample["boot_to_ready_us"], 2100000)
            for retained in lines[3:]:
                self.assertEqual(json.loads(retained), sample)


if __name__ == "__main__":
    unittest.main()
