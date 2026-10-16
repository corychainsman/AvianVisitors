"""Execute the production recovery guard on a host; no flash/network needed."""
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "inkplate/AvianVisitorsInkplate/cache_recovery.cpp"


def function(source, name):
    start = re.search(r"^bool " + name + r"\([^\n]*\)\s*\{", source, re.M)
    if not start:
        raise AssertionError(f"Missing {name}")
    end, depth = start.end(), 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start.start():end]


@unittest.skipUnless(shutil.which("g++"), "g++ required")
class CacheRecoveryTests(unittest.TestCase):
    def test_production_recovery_guards_and_retry(self):
        harness = r'''
#include <string>
#include <cstring>
#include <cstdio>
#include <cstdlib>
using String=std::string;
constexpr int ESP_PARTITION_TYPE_DATA=1, ESP_PARTITION_SUBTYPE_DATA_SPIFFS=130;
constexpr int WL_CONNECTED=3, ESP_OK=0;
struct esp_partition_t { unsigned address, size; } partition{0x1910000,0x6c0000};
bool found=true, connected=true, preferencesOk=true, backupOk=true;
bool saveBackupOk=true, invalidateOk=true, clearBackupOk=true;
bool backedUp=false, valid=true, formatOk=true, mountOk=true;
bool alreadyMounted=false;
int backups=0, formats=0;
bool esp_spiffs_mounted(const char *label) {
  if (std::strcmp(label,"spiffs")) std::abort();
  return alreadyMounted;
}
const esp_partition_t *esp_partition_find_first(int type,int subtype,const char *label) {
  if (type!=1 || subtype!=130 || std::strcmp(label,"spiffs")) std::abort();
  return found ? &partition : nullptr;
}
struct { int status() { return connected ? WL_CONNECTED : 0; } } WiFi;
struct Preferences {
  bool begin(const char *,bool) { return preferencesOk; }
  bool getBool(const char *key,bool fallback) {
    return !std::strcmp(key,"repairBackedUp") ? backedUp : fallback;
  }
  unsigned putBool(const char *key,bool value) {
    if (!std::strcmp(key,"repairBackedUp")) {
      if ((value && !saveBackupOk) || (!value && !clearBackupOk)) return 0;
      backedUp=value;
    } else if (!std::strcmp(key,"valid")) {
      if (!invalidateOk) return 0;
      valid=value;
    } else std::abort();
    return 1;
  }
};
bool backupPartition(const String &,const esp_partition_t *) { ++backups; return backupOk; }
void otaReportStatus(const String &,const char *,const char *) {}
bool doFormat() {
  if (!backedUp || valid) { std::fprintf(stderr,"Erase without durable backup/invalidation\n"); std::abort(); }
  ++formats;
  return formatOk;
}
int esp_spiffs_format(const char *label) {
  if (std::strcmp(label,"spiffs")) std::abort();
  return doFormat() ? ESP_OK : -1;
}
struct {
  void end() { alreadyMounted=false; }
  bool format() { return doFormat(); }
  bool begin(bool,const char *,int,const char *label) {
    if (std::strcmp(label,"spiffs")) std::abort();
    alreadyMounted=mountOk; return mountOk;
  }
} SPIFFS;
''' + function(SOURCE.read_text(), "recoverMenuStorage") + r'''
void reset() {
  partition={0x1910000,0x6c0000};
  found=connected=preferencesOk=backupOk=saveBackupOk=invalidateOk=clearBackupOk=true;
  backedUp=alreadyMounted=false; valid=formatOk=mountOk=true; backups=formats=0;
}
void check(bool condition,const char *message) {
  if (!condition) { std::fprintf(stderr,"%s\n",message); std::exit(1); }
}
int main() {
  reset(); found=false;
  check(!recoverMenuStorage("backend") && backups==0 && formats==0,"Missing partition");
  reset(); partition.address++;
  check(!recoverMenuStorage("backend") && backups==0 && formats==0,"Wrong address");
  reset(); partition.size++;
  check(!recoverMenuStorage("backend") && backups==0 && formats==0,"Wrong size");
  reset(); connected=false;
  check(!recoverMenuStorage("backend") && backups==0 && formats==0,"Offline");
  reset(); preferencesOk=false;
  check(!recoverMenuStorage("backend") && backups==0 && formats==0,"NVS unavailable");
  reset(); backupOk=false;
  check(!recoverMenuStorage("backend") && backups==1 && formats==0 && !backedUp,"Unverified backup");
  reset(); saveBackupOk=false;
  check(!recoverMenuStorage("backend") && backups==1 && formats==0,"Backup receipt not persisted");
  reset(); invalidateOk=false;
  check(!recoverMenuStorage("backend") && formats==0,"Invalidation not persisted");
  reset(); formatOk=false;
  check(!recoverMenuStorage("backend") && backups==1 && formats==1 && backedUp,"Format failure loses receipt");
  formatOk=true;
  check(recoverMenuStorage("backend") && backups==1 && formats==2 && !backedUp && !valid,"Format retry rebacks damaged data");
  reset(); mountOk=false;
  check(!recoverMenuStorage("backend") && backedUp,"Mount failure loses receipt");
  mountOk=true;
  check(recoverMenuStorage("backend") && backups==1 && !backedUp,"Mount retry rebacks damaged data");
  reset(); clearBackupOk=false;
  check(!recoverMenuStorage("backend") && backedUp && alreadyMounted,"Clear failure reported success");
  check(!recoverMenuStorage("backend") && backups==1 && formats==1,"Mounted retry ignores clear failure");
  clearBackupOk=true;
  check(recoverMenuStorage("backend") && backups==1 && formats==1 && !backedUp,"Mounted retry reformats");
  reset(); alreadyMounted=true;
  check(recoverMenuStorage("backend") && backups==0 && formats==0,"Healthy partition reformatted");
  reset(); alreadyMounted=backedUp=true;
  check(recoverMenuStorage("backend") && backups==0 && formats==0 && !backedUp,"Interrupted marker cleanup");
  reset();
  check(recoverMenuStorage("backend") && backups==1 && formats==1 && !backedUp && !valid,"Successful recovery");
}
'''
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "recovery.cpp"
            binary = pathlib.Path(temp) / "recovery"
            source.write_text(harness)
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Werror", str(source), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
