#include "menu_timing.h"
#include "firmware_version.h"
#include <AvianPartialTiming.h>
#include <esp_timer.h>
#include <time.h>

static_assert(AVIAN_PARTIAL_TIMING_VERSION == 1, "Unsupported timing hook version");
namespace {
struct Sample {
  uint32_t magic;
  uint32_t build;
  uint32_t captured;
  uint32_t setup;
  uint32_t entry;
  uint32_t cache;
  uint32_t graphics;
  uint32_t drawing;
  uint32_t returned;
  uint32_t phases[6];
  uint8_t mask;
};
RTC_DATA_ATTR Sample latest{};
Sample active{};
uint32_t setupStarted = 0;
bool recording = false;
constexpr uint32_t kMagic = 0x4d544d01;
uint32_t nowUs() { return static_cast<uint32_t>(esp_timer_get_time()); }
void field(String &json, const char *name, uint32_t value) {
  json += ",\"";
  json += name;
  json += "\":";
  json += String(value);
}
}

extern "C" void avianInkplatePartialTiming(uint8_t phase) {
  if (!recording || phase >= 6) return;
  active.phases[phase] = nowUs();
  active.mask |= 1U << phase;
}

void menuTimingSetupStart() { setupStarted = nowUs(); }
void menuTimingStart() {
  active = {};
  active.build = AVIAN_FIRMWARE_BUILD;
  active.setup = setupStarted;
  active.entry = nowUs();
  const time_t now = time(nullptr);
  active.captured = now >= 1704067200 ? static_cast<uint32_t>(now) : 0;
  recording = false;
}
void menuTimingCacheLoaded() { active.cache = nowUs(); }
void menuTimingGraphicsReady() { active.graphics = nowUs(); }
void menuTimingDrawingReady() {
  active.drawing = nowUs();
  recording = true;
}
void menuTimingFinished() {
  active.returned = nowUs();
  recording = false;  // Dismissal/full refreshes must never overwrite opening timings.
  active.magic = kMagic;
  latest = active;
}

String menuTimingJson() {
  if (latest.magic != kMagic || latest.build != AVIAN_FIRMWARE_BUILD) return "";
  const bool hooks = latest.mask == 0x3f;
  String json = "{\"schema\":1";
  json.reserve(700);
  field(json, "firmware_build", latest.build);
  field(json, "captured_at", latest.captured);
  field(json, "boot_to_setup_us", latest.setup);
  field(json, "setup_to_menu_us", latest.entry - latest.setup);
  field(json, "cache_us", latest.cache - latest.entry);
  field(json, "graphics_init_us", latest.graphics - latest.cache);
  field(json, "draw_us", latest.drawing - latest.graphics);
  field(json, "driver_prepare_us", hooks ? latest.phases[1] - latest.phases[0] : 0);
  field(json, "panel_init_us", hooks ? latest.phases[2] - latest.phases[1] : 0);
  field(json, "transfer_us", hooks ? latest.phases[3] - latest.phases[2] : 0);
  field(json, "refresh_us", hooks ? latest.phases[4] - latest.phases[3] : 0);
  field(json, "poweroff_us", hooks ? latest.phases[5] - latest.phases[4] : 0);
  field(json, "driver_total_us", latest.returned - latest.drawing);
  field(json, "menu_total_us", latest.returned - latest.entry);
  field(json, "boot_to_ready_us", hooks ? latest.phases[4] : latest.returned);
  json += hooks ? ",\"driver_hooks\":true}" : ",\"driver_hooks\":false}";
  return json;
}
