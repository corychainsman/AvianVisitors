#include "ota_update.h"

#include <HTTPClient.h>
#include <Update.h>
#include <WiFi.h>

#include "firmware_version.h"
#include "menu_timing.h"
#include "ota_public_key.h"

namespace {
constexpr char kManifestPath[] = "/inkplate-ota/manifest.txt";
constexpr char kStatusPath[] = "/avian/api/inkplate-status.php";
constexpr size_t kSignatureBytes = 512;  // RSA-4096 signature.
constexpr size_t kMaximumManifestBytes = 4096;
constexpr uint16_t kDownloadTimeoutMs = 60000;

struct OtaManifest {
  uint32_t build = 0;
  size_t size = 0;
  String version;
  String sha256;
  String url;
};

String joinedUrl(const String &backend, const String &path) {
  if (path.startsWith("http://") || path.startsWith("https://")) return path;
  return backend + (path.startsWith("/") ? path : "/" + path);
}

String urlEncode(const String &value) {
  static const char hex[] = "0123456789ABCDEF";
  String encoded;
  encoded.reserve(value.length() * 2);
  for (size_t i = 0; i < value.length(); ++i) {
    const uint8_t c = static_cast<uint8_t>(value[i]);
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
        (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.') {
      encoded += static_cast<char>(c);
    } else {
      encoded += '%';
      encoded += hex[c >> 4];
      encoded += hex[c & 0x0f];
    }
  }
  return encoded;
}

String deviceId() {
  String id = WiFi.macAddress();
  id.replace(":", "");
  id.toLowerCase();
  return id;
}

String manifestValue(const String &body, const String &key) {
  const String prefix = key + "=";
  int start = 0;
  while (start < static_cast<int>(body.length())) {
    int end = body.indexOf('\n', start);
    if (end < 0) end = body.length();
    String line = body.substring(start, end);
    line.trim();
    if (line.startsWith(prefix)) return line.substring(prefix.length());
    start = end + 1;
  }
  return "";
}

bool parseManifest(const String &body, OtaManifest &manifest) {
  if (body.length() == 0 || body.length() > kMaximumManifestBytes ||
      !body.startsWith("AVIAN-INKPLATE-OTA/1\n")) {
    return false;
  }

  const String build = manifestValue(body, "build");
  const String size = manifestValue(body, "size");
  manifest.version = manifestValue(body, "version");
  manifest.sha256 = manifestValue(body, "sha256");
  manifest.url = manifestValue(body, "url");
  manifest.build = strtoul(build.c_str(), nullptr, 10);
  manifest.size = strtoull(size.c_str(), nullptr, 10);

  // Keep downloads on the expected backend path. Authenticity is enforced by
  // the embedded RSA public key when Update.end() verifies the signed image.
  return manifest.build > 0 && manifest.size > kSignatureBytes &&
         manifest.version.length() > 0 && manifest.sha256.length() == 64 &&
         manifest.url.startsWith("/inkplate-ota/firmware-") &&
         manifest.url.endsWith(".signed.bin") && manifest.url.indexOf("..") < 0;
}

bool fetchManifest(const String &backend, OtaManifest &manifest) {
  HTTPClient http;
  http.setConnectTimeout(10000);
  http.setTimeout(15000);
  http.useHTTP10(true);
  if (!http.begin(backend + kManifestPath)) return false;
  const int status = http.GET();
  if (status != HTTP_CODE_OK) {
    http.end();
    return false;
  }
  const String body = http.getString();
  http.end();
  return parseManifest(body, manifest);
}

bool installManifest(Preferences &preferences, const String &backend,
                     const OtaManifest &manifest) {
  HTTPClient http;
  http.setConnectTimeout(10000);
  http.setTimeout(kDownloadTimeoutMs);
  http.useHTTP10(true);
  if (!http.begin(joinedUrl(backend, manifest.url))) return false;

  const int status = http.GET();
  const int contentLength = http.getSize();
  if (status != HTTP_CODE_OK || contentLength <= 0 ||
      static_cast<size_t>(contentLength) != manifest.size) {
    Serial.printf("OTA download rejected: HTTP %d, size %d/%u\n", status,
                  contentLength, static_cast<unsigned>(manifest.size));
    http.end();
    return false;
  }

  UpdaterRSAVerifier verifier(kOtaPublicKey, sizeof(kOtaPublicKey), HASH_SHA256);
  if (!Update.installSignature(&verifier) || !Update.begin(manifest.size, U_FLASH)) {
    Serial.printf("OTA staging failed: %s\n", Update.errorString());
    http.end();
    return false;
  }

  Serial.printf("Installing signed OTA build %lu (%s), %u bytes\n",
                static_cast<unsigned long>(manifest.build),
                manifest.version.c_str(), static_cast<unsigned>(manifest.size));
  const size_t written = Update.writeStream(*http.getStreamPtr());
  http.end();
  if (written != manifest.size) {
    Serial.printf("OTA stream ended early: %u/%u bytes\n",
                  static_cast<unsigned>(written),
                  static_cast<unsigned>(manifest.size));
    Update.abort();
    return false;
  }

  // end() validates the ESP app image and its RSA signature before switching
  // the boot partition. A bad or interrupted image never becomes bootable.
  if (!Update.end() || !Update.isFinished()) {
    Serial.printf("OTA signature/image validation failed: %s\n",
                  Update.errorString());
    return false;
  }

  const uint32_t current = max<uint32_t>(
      AVIAN_FIRMWARE_BUILD, preferences.getUInt("otaBuild", 0));
  preferences.putUInt("previousOta", current);
  preferences.putUInt("pendingOta", manifest.build);
  preferences.putString("pendingVer", manifest.version);
  otaReportStatus(backend, "rebooting", "signed update installed");
  Serial.println("Signed OTA verified; rebooting into the new partition");
  delay(750);
  ESP.restart();
  return true;
}
}  // namespace

void otaReportStatus(const String &backend, const String &status,
                     const String &detail) {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(5000);
  if (!http.begin(backend + kStatusPath)) return;
  http.addHeader("Content-Type", "application/x-www-form-urlencoded");
  String payload = "device=" + urlEncode(deviceId());
  payload += "&build=" + String(AVIAN_FIRMWARE_BUILD);
  payload += "&version=" + urlEncode(AVIAN_FIRMWARE_VERSION);
  payload += "&status=" + urlEncode(status);
  payload += "&detail=" + urlEncode(detail);
  payload += "&rssi=" + String(WiFi.RSSI());
  const String timing = menuTimingJson();
  if (!timing.isEmpty()) payload += "&menu_opening=" + urlEncode(timing);
  http.POST(payload);
  http.end();
}

void otaHandleBootConfirmation(Preferences &preferences,
                               const String &backend) {
  const uint32_t pending = preferences.getUInt("pendingOta", 0);
  if (pending == 0) {
    const uint32_t highest = preferences.getUInt("otaBuild", 0);
    if (AVIAN_FIRMWARE_BUILD > highest) {
      preferences.putUInt("otaBuild", AVIAN_FIRMWARE_BUILD);
    }
    return;
  }

  if (pending == AVIAN_FIRMWARE_BUILD) {
    preferences.putUInt("otaBuild", AVIAN_FIRMWARE_BUILD);
    preferences.remove("pendingOta");
    preferences.remove("previousOta");
    preferences.remove("pendingVer");
    otaReportStatus(backend, "installed", "new build confirmed after Wi-Fi");
    Serial.printf("OTA build %lu confirmed healthy\n",
                  static_cast<unsigned long>(AVIAN_FIRMWARE_BUILD));
    return;
  }

  const uint32_t previous = preferences.getUInt("previousOta", 0);
  preferences.putUInt("blockedOta", pending);
  preferences.remove("pendingOta");
  preferences.remove("pendingVer");
  otaReportStatus(backend, "rejected", "manifest and embedded build mismatch");
  Serial.printf("OTA build mismatch: expected %lu, running %lu\n",
                static_cast<unsigned long>(pending),
                static_cast<unsigned long>(AVIAN_FIRMWARE_BUILD));

  // If this is not the previous known-good build, the just-booted image lied
  // about its build number. Return to the other OTA partition immediately.
  if (AVIAN_FIRMWARE_BUILD != previous && Update.canRollBack() &&
      Update.rollBack()) {
    Serial.println("Rolling back to the previous signed firmware");
    delay(500);
    ESP.restart();
  }
}

void otaCheckAndInstall(Preferences &preferences, const String &backend) {
  OtaManifest manifest;
  if (!fetchManifest(backend, manifest)) {
    Serial.println("No valid OTA manifest available");
    return;
  }

  const uint32_t highest = max<uint32_t>(
      AVIAN_FIRMWARE_BUILD, preferences.getUInt("otaBuild", 0));
  if (manifest.build <= highest) return;
  if (manifest.build == preferences.getUInt("blockedOta", 0)) {
    Serial.println("Skipping a previously rejected OTA build");
    return;
  }

  if (!installManifest(preferences, backend, manifest)) {
    otaReportStatus(backend, "update-failed", Update.errorString());
    Serial.println("OTA failed; retaining the current firmware");
  }
}
