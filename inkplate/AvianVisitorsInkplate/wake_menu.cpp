#include "wake_menu.h"
#include "firmware_version.h"

#include <SPIFFS.h>
#include <Preferences.h>
#include <esp_partition.h>
#include <math.h>

namespace {
// Rotation 0 maps to native (1199-x,1599-y). Keep every edge aligned
// to the driver's four-column/two-row partial-window requirements.
constexpr int kX = 16, kY = 16, kWidth = 544, kHeight = 300;
static_assert(kX % 4 == 0 && kWidth % 4 == 0 && kY % 2 == 0 && kHeight % 2 == 0,
              "Partial rectangle must not expand beyond cached pixels");
constexpr int kStride = 1200 / 2;
constexpr int kNativeCol = 1200 - kX - kWidth;
constexpr int kNativeRow = 1600 - kY - kHeight;
constexpr size_t kRegionBytes = kWidth / 2 * kHeight;
constexpr size_t kBytes = kRegionBytes + 8;
constexpr char kCachePath[] = "/avian-wake-background.bin";
constexpr uint32_t kMagic = 0x41574D02;
struct CacheHeader {
  uint32_t magic;
  uint32_t checksum;
  char signature[80];
};
uint8_t *background = nullptr;
bool visible = false;
Preferences cachePreferences;
bool preferencesReady = false;
bool mounted = false;
bool mountAttempted = false;
const char *diagnostic = "not checked";
const char *mountError = "filesystem unavailable";
bool fail(const char *reason) {
  diagnostic = reason;
  Serial.printf("Wake menu: %s\n", reason);
  return false;
}

bool openPreferences() {
  if (!preferencesReady) preferencesReady = cachePreferences.begin("avianMenu", false);
  return preferencesReady;
}

bool mountCache() {
  if (mountAttempted) return mounted || fail(mountError);
  mountAttempted = true;
  mounted = SPIFFS.begin(false);
  if (mounted) return true;
  // Never autoformat a populated or damaged filesystem. Initial provisioning
  // may initialize only a completely erased SPIFFS data partition.
  const esp_partition_t *partition = esp_partition_find_first(
      ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, "spiffs");
  if (!partition) { mountError = "SPIFFS partition missing"; return fail(mountError); }
  uint8_t block[1024];
  for (size_t offset = 0; offset < partition->size; offset += sizeof(block)) {
    const size_t length = min<size_t>(sizeof(block), partition->size - offset);
    if (esp_partition_read(partition, offset, block, length) != ESP_OK) {
      mountError = "flash read failed"; return fail(mountError);
    }
    for (size_t i = 0; i < length; ++i) {
      if (block[i] != 0xff) {
        mountError = "SPIFFS mount failed; nonblank partition preserved";
        return fail(mountError);
      }
    }
    delay(1);
  }
  mounted = SPIFFS.begin(true);
  if (!mounted) { mountError = "SPIFFS initialization failed"; return fail(mountError); }
  return true;
}

uint32_t checksum(const uint8_t *data) {
  uint32_t result = 2166136261UL;
  for (size_t i = 0; i < kBytes; ++i) result = (result ^ data[i]) * 16777619UL;
  return result;
}

bool allocateBackground() {
  if (!background) background = static_cast<uint8_t *>(ps_malloc(kBytes));
  if (!background) background = static_cast<uint8_t *>(malloc(kBytes));
  return background != nullptr || fail("background allocation failed");
}

bool loadBackground(const String &signature) {
  if (!openPreferences()) return fail("cache preferences unavailable");
  if (!cachePreferences.getBool("valid", false)) return fail("background not saved");
  if (signature.isEmpty() || signature.length() >= sizeof(CacheHeader::signature))
    return fail("frame signature missing or invalid");
  if (!mountCache() || !allocateBackground()) return false;
  File file = SPIFFS.open(kCachePath, FILE_READ);
  CacheHeader header{};
  if (!file) return fail("background file missing");
  if (file.size() != sizeof(header) + kBytes) return fail("background size mismatch");
  if (file.read(reinterpret_cast<uint8_t *>(&header), sizeof(header)) != sizeof(header))
    return fail("background header read failed");
  if (header.magic != kMagic) return fail("background format outdated");
  if (header.signature[sizeof(header.signature) - 1] != '\0' || signature != header.signature)
    return fail("background frame mismatch");
  if (file.read(background, kBytes) != kBytes) return fail("background read failed");
  if (header.checksum != checksum(background)) return fail("background checksum mismatch");
  diagnostic = "background ready";
  return true;
}

void copyRegion(Inkplate &display, bool restore) {
  for (int row = 0; row < kHeight; ++row) {
    uint8_t *frame = display.DMemory4Bit + (kNativeRow + row) * kStride + kNativeCol / 2;
    uint8_t *cache = background + row * kWidth / 2;
    if (restore) memcpy(frame, cache, kWidth / 2);
    else memcpy(cache, frame, kWidth / 2);
  }
  // The driver refreshes a 4x4 null window on the unused master chip too.
  // RAM is cleared by begin() after deep sleep, so preserve these pixels.
  for (int row = 0; row < 4; ++row) {
    uint8_t *frame = display.DMemory4Bit + row * kStride;
    uint8_t *cache = background + kRegionBytes + row * 2;
    if (restore) memcpy(frame, cache, 2);
    else memcpy(cache, frame, 2);
  }
}

void line(Inkplate &display, int y, String text) {
  // SSIDs and backend-derived labels cannot wrap outside the cached region.
  for (size_t i = 0; i < text.length(); ++i) {
    if (text[i] < 32 || text[i] > 126) text.setCharAt(i, '?');
  }
  if (text.length() > 42) text = text.substring(0, 39) + "...";
  display.setCursor(kX + 16, kY + y);
  display.print(text);
}
}  // namespace

const char *wakeMenuDiagnostic() { return diagnostic; }

void wakeMenuBegin(Inkplate &display) {
  display.begin();
  display.setRotation(0);
}

bool wakeMenuHasBackground(const String &frameSignature) {
  return loadBackground(frameSignature);
}

bool wakeMenuInvalidate() {
  // NVS marker prevents stale restoration even if filesystem deletion fails.
  // The caller must abort a full display update if this cannot be committed.
  return openPreferences() && cachePreferences.putBool("valid", false) == 1;
}

bool wakeMenuCaptureBackground(Inkplate &display, const String &frameSignature) {
  if (!display.DMemory4Bit || frameSignature.isEmpty() ||
      frameSignature.length() >= sizeof(CacheHeader::signature) ||
      !mountCache() || !allocateBackground()) return false;
  copyRegion(display, false);
  CacheHeader header{};
  header.magic = kMagic;
  header.checksum = checksum(background);
  frameSignature.toCharArray(header.signature, sizeof(header.signature));
  File file = SPIFFS.open(kCachePath, FILE_WRITE);
  bool saved = file && file.write(reinterpret_cast<const uint8_t *>(&header), sizeof(header)) == sizeof(header)
      && file.write(background, kBytes) == kBytes;
  file.close();
  if (!saved) {
    SPIFFS.remove(kCachePath);
    return fail("background write failed");
  }
  if (!openPreferences() || cachePreferences.putBool("valid", true) != 1)
    return fail("cache validity write failed");
  return loadBackground(frameSignature);
}

bool wakeMenuShow(Inkplate &display, const String &frameSignature,
                  const WakeMenuStatus &status) {
  if (visible || !loadBackground(frameSignature)) return false;
  wakeMenuBegin(display);
  if (!display.DMemory4Bit) return false;
  copyRegion(display, true);
  display.fillRect(kX, kY, kWidth, kHeight, INKPLATE_WHITE);
  display.drawRect(kX, kY, kWidth, kHeight, INKPLATE_BLACK);
  display.setTextWrap(false);
  display.setTextColor(INKPLATE_BLACK);
  display.setTextSize(2);
  line(display, 14, "INKPLATE STATUS");
  line(display, 42, "Wi-Fi: " + status.wifiStatus);
  line(display, 68, "Network: " + (status.wifiName.isEmpty() ? String("Unknown") : status.wifiName));
  const double volts = display.readBattery();
  if (isfinite(volts) && volts >= 2.5 && volts <= 4.5) {
    // Voltage-only estimate: charging/load/temperature affect accuracy.
    const int percent = constrain(static_cast<int>((volts - 3.3) / 0.9 * 100.0 + 0.5), 0, 100);
    line(display, 94, "Battery: ~" + String(percent) + "% (" + String(volts, 2) + " V)");
  } else {
    line(display, 94, "Battery: unavailable");
  }
  line(display, 120, "Last update: " + status.lastUpdate);
  line(display, 146, "Next update: " + status.nextUpdate);
  line(display, 172, "Server (last seen): " + status.serverVersion);
  line(display, 198, "E-ink: " + String(AVIAN_FIRMWARE_VERSION));
  line(display, 224, "E-ink build: " + String(AVIAN_FIRMWARE_BUILD));
  line(display, 252, "Press Wake again to refresh data now");
  line(display, 278, "Closes in 10 seconds");
  const uint32_t started = millis();
  display.displayPartial(kX, kY, kWidth, kHeight);
  Serial.printf("Wake menu show partial refresh: %lu ms\n", static_cast<unsigned long>(millis() - started));
  display.setTextWrap(true);
  visible = true;
  return true;
}

void wakeMenuHide(Inkplate &display) {
  if (!visible || !background) return;
  copyRegion(display, true);
  const uint32_t started = millis();
  display.displayPartial(kX, kY, kWidth, kHeight);
  Serial.printf("Wake menu hide partial refresh: %lu ms\n", static_cast<unsigned long>(millis() - started));
  visible = false;
}
