#include "cache_recovery.h"
#include "ota_update.h"
#include <HTTPClient.h>
#include <Preferences.h>
#include <SPIFFS.h>
#include <WiFi.h>
#include <esp_partition.h>
#include <esp_spiffs.h>

namespace {
class PartitionStream : public Stream {
 public:
  explicit PartitionStream(const esp_partition_t *partition) : partition_(partition) {}
  int available() override { return failed_ ? -1 : partition_->size - offset_; }
  int read() override { uint8_t value; return readBytes(&value, 1) == 1 ? value : -1; }
  int peek() override {
    uint8_t value;
    return available() > 0 && esp_partition_read(partition_, offset_, &value, 1) == ESP_OK ? value : -1;
  }
  size_t write(uint8_t) override { return 0; }
  size_t readBytes(uint8_t *buffer, size_t length) override {
    if (failed_) return 0;
    length = min<size_t>(length, partition_->size - offset_);
    if (length && esp_partition_read(partition_, offset_, buffer, length) != ESP_OK) {
      failed_ = true;
      return 0;
    }
    offset_ += length;
    return length;
  }
 private:
  const esp_partition_t *partition_;
  size_t offset_ = 0;
  bool failed_ = false;
};

bool backupPartition(const String &backend, const esp_partition_t *partition) {
  uint8_t digest[32];
  if (esp_partition_get_sha256(partition, digest) != ESP_OK) return false;
  char hex[65];
  for (size_t i = 0; i < sizeof(digest); ++i) snprintf(hex + i * 2, 3, "%02x", digest[i]);
  String device = WiFi.macAddress();
  device.replace(":", "");
  device.toLowerCase();
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(60000);
  http.useHTTP10(true);
  if (!http.begin(backend + "/avian/api/inkplate-cache-backup.php")) return false;
  http.addHeader("Content-Type", "application/octet-stream");
  http.addHeader("X-Inkplate-Device", device);
  http.addHeader("X-Backup-Sha256", hex);
  http.addHeader("X-Backup-Offset", String(partition->address));
  PartitionStream stream(partition);
  const int status = http.sendRequest("POST", &stream, partition->size);
  String receipt = status == HTTP_CODE_OK ? http.getString() : String("");
  receipt.trim();
  http.end();
  return status == HTTP_CODE_OK && receipt == "VERIFIED " + String(hex) + " " + String(partition->size);
}
}  // namespace

bool recoverMenuStorage(const String &backend) {
  // Never operate on application slots, credentials, or another partition map.
  const esp_partition_t *partition = esp_partition_find_first(
      ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, "spiffs");
  if (!partition || partition->address != 0x1910000 || partition->size != 0x6c0000 ||
      WiFi.status() != WL_CONNECTED) return false;
  Preferences recovery;
  if (!recovery.begin("avianMenu", false)) return false;
  if (esp_spiffs_mounted("spiffs")) {
    return !recovery.getBool("repairBackedUp", false) ||
           recovery.putBool("repairBackedUp", false) == 1;
  }
  // A power interruption during formatting can resume using the original
  // verified backup, rather than backing up the half-formatted replacement.
  if (!recovery.getBool("repairBackedUp", false)) {
    otaReportStatus(backend, "cache-backup", "Preserving unreadable cache partition before repair");
    if (!backupPartition(backend, partition) || recovery.putBool("repairBackedUp", true) != 1) {
      otaReportStatus(backend, "cache-backup-failed", "Partition preserved; verified backup unavailable");
      return false;
    }
  }
  if (recovery.putBool("valid", false) != 1) return false;
  SPIFFS.end();
  otaReportStatus(backend, "cache-repair", "Backup verified; initializing menu cache storage");
  if (esp_spiffs_format("spiffs") != ESP_OK || !SPIFFS.begin(false, "/spiffs", 10, "spiffs")) {
    otaReportStatus(backend, "cache-repair-failed", "Verified backup retained on server");
    return false;
  }
  return recovery.putBool("repairBackedUp", false) == 1;
}
