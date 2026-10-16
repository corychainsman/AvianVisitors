"""Verify observational driver patching and isolation from installed libraries."""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "inkplate_timing", ROOT / "scripts/prepare_inkplate_timing_library.py"
)
timing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(timing)

# Small driver with the same reviewed anchors; pin is explicitly overridden only
# in these fixture tests. Production rejects any hash except its reviewed driver.
DRIVER = b'''#include "driver.h"
void EPDDriver::display() { setPanelState(true); }
void EPDDriver::displayPartial(int x)
{
    displayStart();
    if (x < 0) { displayEnd(); return; }
    setPanelState(true);
    transferData();
    waitForBusy();
    sendCommand(SPECTRA133_REGISTER_DRF, SPECTRA133_REGISTER_DRF_V, sizeof(SPECTRA133_REGISTER_DRF_V), eChipIdBoth);
    waitForBusy();

    if (!_leaveOn)
        setPanelState(false);
    displayEnd();
}

/** Next function */
void EPDDriver::other() { displayEnd(); }
'''


class TimingLibraryTests(unittest.TestCase):
    def fixture_pin(self, data=DRIVER):
        return mock.patch.object(timing, "EXPECTED_DRIVER_SHA256", hashlib.sha256(data).hexdigest())

    def test_rejects_unreviewed_driver(self):
        with self.assertRaisesRegex(RuntimeError, "Unsupported.*SHA256"):
            timing.instrument_driver(DRIVER)

    def test_only_hooks_added_in_order_without_behavior_changes(self):
        with self.fixture_pin():
            patched = timing.instrument_driver(DRIVER).decode()
        positions = [patched.index(timing.hook(phase)) for phase in range(6)]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(positions[0], patched.index("    displayStart();"))
        self.assertLess(positions[1], patched.index("    setPanelState(true);"))
        self.assertLess(patched.index("    setPanelState(true);"), positions[2])
        self.assertIn("    waitForBusy();\n" + timing.hook(3) + "    sendCommand", patched)
        self.assertIn("    waitForBusy();\n" + timing.hook(4), patched)
        self.assertIn("        setPanelState(false);\n" + timing.hook(5) + "    displayEnd();", patched)
        restored = patched.replace(timing.DECLARATION, "", 1)
        for phase in range(6):
            self.assertEqual(restored.count(timing.hook(phase)), 1)
            restored = restored.replace(timing.hook(phase), "", 1)
        self.assertEqual(restored.encode(), DRIVER)

    def test_ambiguous_or_missing_anchor_rejected_even_with_reviewed_hash(self):
        for data in (
            DRIVER.replace(b"    setPanelState(true);", b"    setPanelState(true);\n    setPanelState(true);"),
            DRIVER.replace(b"    setPanelState(true);", b"    initializePanel();"),
        ):
            with self.subTest(data=data), self.fixture_pin(data):
                with self.assertRaisesRegex(RuntimeError, "Expected one partial timing anchor"):
                    timing.instrument_driver(data)

    def test_copy_is_repeatable_and_does_not_modify_source(self):
        with tempfile.TemporaryDirectory() as temp, self.fixture_pin():
            source = Path(temp) / "installed"
            destination = Path(temp) / "build/InkplateLibrary"
            (source / timing.DRIVER).parent.mkdir(parents=True)
            (source / timing.DRIVER).write_bytes(DRIVER)
            (source / "library.properties").write_text("name=InkplateLibrary\n")
            (source / "src/other.cpp").write_text("unchanged")
            (source / "examples").mkdir()
            (source / "examples/example.ino").write_text("excluded")
            before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
            with mock.patch.dict(os.environ, {"INKPLATE_LIBRARY_DIR": str(source)}):
                self.assertEqual(timing.prepare_timing_library(destination=destination), destination)
            first = (destination / timing.DRIVER).read_bytes()
            (destination / "src/stale.cpp").write_text("must disappear")
            timing.prepare_timing_library(source, destination)
            self.assertEqual((destination / timing.DRIVER).read_bytes(), first)
            self.assertFalse((destination / "src/stale.cpp").exists())
            self.assertFalse((destination / "examples").exists())
            self.assertEqual((destination / "src/AvianPartialTiming.h").read_text(), timing.STAMP)
            self.assertEqual((destination / "src/other.cpp").read_text(), "unchanged")
            after = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            (source / timing.DRIVER).write_bytes(DRIVER + b"// changed\n")
            with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                timing.prepare_timing_library(source, destination)
            self.assertEqual((destination / timing.DRIVER).read_bytes(), first)

    def test_rejects_overlapping_source_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "installed"
            for destination in (source, source / "copy", source.parent):
                with self.subTest(destination=destination):
                    with self.assertRaisesRegex(RuntimeError, "must not overlap"):
                        timing.prepare_timing_library(source, destination)


if __name__ == "__main__":
    unittest.main()
