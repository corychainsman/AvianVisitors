"""Host execution of the firmware's button handling, with a simulated GPIO clock.

Run with: python3 -m unittest discover -s tests -p test_inkplate_wake.py
Only a C++ compiler is needed; no Arduino installation or hardware is used.
"""
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKETCH = ROOT / "inkplate/AvianVisitorsInkplate/AvianVisitorsInkplate.ino"


def function(source, name):
    """Copy the production function verbatim, not a Python reimplementation."""
    match = re.search(r"^[\w:<>]+\s+" + re.escape(name) + r"\([^\n]*\)\s*\{", source, re.M)
    if match is None:
        raise AssertionError(f"Missing production function {name}")
    end = match.end()
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end]


@unittest.skipUnless(shutil.which("g++"), "g++ is required for firmware host tests")
class WakeGestureTests(unittest.TestCase):
    def test_menu_timeout_preserves_schedule(self):
        source = SKETCH.read_text()
        setup = function(source, "setup")
        # Execute the entire production menu branch up to the network/update
        # branch. Reaching the latter is a failure for an idle button wake.
        branch = setup[setup.index("  WakeGesture gesture"):setup.index("  const bool forceRefresh")]
        enum = re.search(r"enum class WakeGesture \{[^}]+\};", source).group()
        harness = r'''
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <ctime>
#include <initializer_list>
using String = const char *;
constexpr uint64_t kMicrosecondsPerSecond=1000000, kNormalSleepUs=900000000;
constexpr uint32_t kGestureWindowMs=10000;
constexpr time_t kValidClockThreshold=1704067200;
time_t currentTime=1800000000;
uint64_t scheduled=0;
int display=0, shows=0, hides=0, sampling=0;
bool showSucceeded=true, valid=true, active=true;
struct {
  uint64_t getULong64(const char *, int) { return scheduled; }
  String getString(const char *, const char *) { return "frame"; }
} preferences;
uint32_t millis() { return 0; }
time_t fakeTime(void *) { return currentTime; }
#define time fakeTime
bool clockIsValid() { return valid; }
bool isActiveTime(time_t) { return active; }
uint64_t sleepUntilActive(time_t) { return 3600000000; }
void startButtonSampling() { ++sampling; }
void stopButtonSampling() { --sampling; }
int menuStatus() { return 0; }
bool wakeMenuShow(int, String, int) { ++shows; return showSucceeded; }
void wakeMenuHide(int) { ++hides; }
struct Sleep { uint64_t duration; };
[[noreturn]] void sleepFor(uint64_t duration) { throw Sleep{duration}; }
''' + enum + r'''
WakeGesture detectWakeGesture(uint32_t) { return WakeGesture::kNone; }
void run() {
  const bool buttonWake=true;
''' + branch + r'''
  std::fprintf(stderr,"Timeout reached network/update branch\n"); std::exit(1);
}
void check(uint64_t next, uint64_t expected) {
  scheduled=next; shows=hides=sampling=0;
  try { run(); } catch (Sleep result) {
    if (result.duration==expected && shows==1 && hides==int(showSucceeded) && sampling==0) return;
    std::fprintf(stderr,"Wrong sleep or cleanup: %llu expected %llu\n",
                 (unsigned long long)result.duration,(unsigned long long)expected);
  }
  std::exit(1);
}
int main() {
  for(bool available : {true,false}) {
    showSucceeded=available;
    check(currentTime+321,321000000);
    check(currentTime-1,1000000);
    check(currentTime,1000000);
    check(0,kNormalSleepUs);
    active=false; check(0,3600000000); active=true;
    valid=false; check(currentTime+321,kNormalSleepUs); valid=true;
  }
}
'''
        with tempfile.TemporaryDirectory(prefix="inkplate-timeout-test-") as tmp:
            cpp = pathlib.Path(tmp) / "timeout.cpp"
            binary = pathlib.Path(tmp) / "timeout"
            cpp.write_text(harness)
            subprocess.run(["g++", "-std=c++11", "-Wall", "-Wextra", "-Werror",
                            str(cpp), "-o", str(binary)], check=True, capture_output=True, text=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_gesture_timing(self):
        source = SKETCH.read_text()
        constants = "\n".join(re.findall(
            r"^constexpr uint32_t k(?:GestureWindow|ForceHold|InterpressTimeout|Debounce)Ms = .*;", source, re.M))
        enum = re.search(r"enum class WakeGesture \{[^}]+\};", source).group()
        logic = "\n".join(function(source, name) for name in
                          ("sampleButton", "waitForButtonState", "interpressDeadline", "detectWakeGesture"))
        harness = r'''
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <initializer_list>
#include <deque>
#include <utility>
#include <vector>
using std::min;
constexpr int LOW = 0, HIGH = 1, kWakeButtonPin = 18;
uint32_t origin = 0, elapsed = 0;
std::vector<std::pair<uint32_t, int>> edges;
uint32_t millis() { return origin + elapsed; }
void delay(uint32_t n) { elapsed += n; if (elapsed > 20000) std::abort(); }
int digitalRead(int) {
  int state = LOW;
  for (const auto &edge : edges) {
    if (edge.first > elapsed) break;
    state = edge.second;
  }
  return state;
}
struct { void println(const char *) {} } Serial;
struct ButtonEvent { int state; uint32_t at; };
void *buttonEvents = nullptr;
uint32_t lastButtonEventMs = 0;
std::deque<ButtonEvent> queue;
constexpr int pdTRUE = 1;
uint32_t pdMS_TO_TICKS(uint32_t value) { return value; }
void vTaskDelay(uint32_t value) { delay(value); if (elapsed >= 11000) throw 1; }
int xQueueSend(void *, ButtonEvent *event, int) { queue.push_back(*event); return 1; }
int xQueuePeek(void *, ButtonEvent *event, int) {
  if (queue.empty() || int32_t(millis()-queue.front().at)<0) return 0;
  *event=queue.front(); return 1;
}
int xQueueReceive(void *handle, ButtonEvent *event, int wait) {
  if (!xQueuePeek(handle,event,wait)) return 0;
  queue.pop_front(); return 1;
}
'''+ constants + "\n" + enum + "\n" + logic + r'''
bool queued = false;
uint32_t renderDelay = 0;
void check(const char *label, uint32_t start,
           std::initializer_list<std::pair<uint32_t,int>> changes,
           WakeGesture expected, uint32_t minElapsed, uint32_t maxElapsed) {
  origin=start; elapsed=0; edges=changes; lastButtonEventMs=0;
  queue.clear(); buttonEvents=queued ? &queue : nullptr;
  if (queued) { try { sampleButton(nullptr); } catch (int) {} }
  elapsed=renderDelay;
  const auto actual=detectWakeGesture(millis()+kGestureWindowMs);
  if (actual!=expected || elapsed<minElapsed || elapsed>maxElapsed) {
    std::fprintf(stderr,"%s: gesture %d expected %d; elapsed %u expected [%u,%u]\n",
                 label,int(actual),int(expected),elapsed,minElapsed,maxElapsed);
    std::exit(1);
  }
}
int main() {
  for (bool useQueue : {false,true}) {
  queued=useQueue;
  for (uint32_t start : {0u, UINT32_MAX-5000u, UINT32_MAX-100u}) {
    check("release then timeout",start,{{100,HIGH}},WakeGesture::kNone,10000,10005);
    check("held initial button",start,{},WakeGesture::kNone,10000,10005);
    check("second press",start,{{100,HIGH},{1000,LOW},{1100,HIGH}},WakeGesture::kRefresh,1800,1950);
    check("late second press",start,{{100,HIGH},{9000,LOW},{9100,HIGH}},WakeGesture::kRefresh,9800,10005);
    check("triple press",start,{{100,HIGH},{300,LOW},{400,HIGH},{600,LOW},{700,HIGH}},WakeGesture::kToggleTrains,1400,1550);
    check("fifth held",start,{{100,HIGH},{300,LOW},{400,HIGH},{600,LOW},{700,HIGH},{900,LOW},{1000,HIGH},{1200,LOW}},WakeGesture::kForceOta,4200,4300);
    check("fifth released",start,{{100,HIGH},{300,LOW},{400,HIGH},{600,LOW},{700,HIGH},{900,LOW},{1000,HIGH},{1200,LOW},{1300,HIGH}},WakeGesture::kRefresh,1300,1370);
    check("second held",start,{{100,HIGH},{300,LOW}},WakeGesture::kRefresh,10000,10005);
    check("short bounce",start,{{100,HIGH},{1000,LOW},{1010,HIGH}},WakeGesture::kNone,10000,10005);
  }
  }
  queued=true; renderDelay=4000;
  check("press during waveform",0,{{100,HIGH},{300,LOW},{400,HIGH}},WakeGesture::kRefresh,4000,4005);
  check("triple during waveform",0,{{100,HIGH},{300,LOW},{400,HIGH},{600,LOW},{700,HIGH}},WakeGesture::kToggleTrains,4000,4005);
  check("hold during waveform",0,{{100,HIGH},{300,LOW},{400,HIGH},{600,LOW},{700,HIGH},{900,LOW},{1000,HIGH},{1200,LOW}},WakeGesture::kForceOta,4200,4300);
}
''' 
        with tempfile.TemporaryDirectory(prefix="inkplate-wake-test-") as tmp:
            cpp = pathlib.Path(tmp) / "wake.cpp"
            binary = pathlib.Path(tmp) / "wake"
            cpp.write_text(harness)
            subprocess.run(["g++", "-std=c++11", "-Wall", "-Wextra", "-Werror",
                            str(cpp), "-o", str(binary)], check=True, capture_output=True, text=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
