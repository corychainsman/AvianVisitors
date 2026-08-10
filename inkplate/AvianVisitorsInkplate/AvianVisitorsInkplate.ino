/*
 * AvianVisitors client for the Soldered Inkplate 13 Spectra.
 *
 * BirdNET and the USB microphone run on the NUC. This sketch wakes every
 * fifteen minutes between 6 a.m. and midnight Wellesley time, checks the
 * published frame hash, downloads a changed 1200x1600 JPEG, refreshes the
 * e-paper, and goes back to deep sleep. Firmware checks are limited to once
 * per hour unless the wake-button force-update gesture is used.
 * The last good e-paper image is never cleared on a network/backend failure.
 */

#ifndef ARDUINO_INKPLATE13SPECTRA
#error "Select Soldered Inkplate 13SPECTRA before compiling."
#endif

#include <DNSServer.h>
#include <HTTPClient.h>
#include <Inkplate.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <time.h>

#include "firmware_version.h"
#include "ota_update.h"

#ifndef AVIAN_WIFI_SSID
#define AVIAN_WIFI_SSID ""
#endif

#ifndef AVIAN_WIFI_PASSWORD
#define AVIAN_WIFI_PASSWORD ""
#endif

namespace {
constexpr char kDefaultBackend[] = "http://192.168.1.100:8091";
constexpr char kSetupAp[] = "AvianVisitors-Setup";
constexpr uint64_t kNormalSleepUs = 15ULL * 60ULL * 1000000ULL;
constexpr uint64_t kRetrySleepUs = 5ULL * 60ULL * 1000000ULL;
constexpr uint64_t kMicrosecondsPerSecond = 1000000ULL;
constexpr uint16_t kDailyHealPolls = 72;  // 18 active hours x four polls/hour.
constexpr gpio_num_t kWakeButtonPin = GPIO_NUM_18;
constexpr uint8_t kFirstActiveHour = 6;
constexpr uint32_t kOtaIntervalSeconds = 60UL * 60UL;
constexpr uint32_t kGestureWindowMs = 10000;
constexpr uint32_t kForceHoldMs = 3000;
constexpr uint32_t kInterpressTimeoutMs = 750;
constexpr uint32_t kDebounceMs = 35;
constexpr time_t kValidClockThreshold = 1704067200;  // 2024-01-01 UTC
constexpr char kWellesleyTimezone[] = "EST5EDT,M3.2.0,M11.1.0";

Inkplate display;
Preferences preferences;
RTC_DATA_ATTR uint16_t pollsSinceRefresh = kDailyHealPolls;

enum class WakeGesture { kNone, kToggleTrains, kForceOta };

String backendUrl() {
  String value = preferences.getString("backend", kDefaultBackend);
  while (value.endsWith("/")) value.remove(value.length() - 1);
  return value;
}

void sleepFor(uint64_t microseconds) {
  WiFi.disconnect(false, false);
  WiFi.mode(WIFI_OFF);
  Serial.flush();
  // A held level-low button would otherwise wake the ESP32 immediately and
  // create a tight reboot loop. Give the user a chance to release it first.
  const uint32_t releaseDeadline = millis() + 5000;
  while (digitalRead(kWakeButtonPin) == LOW &&
         static_cast<int32_t>(releaseDeadline - millis()) > 0) {
    delay(20);
  }
  esp_sleep_enable_ext0_wakeup(kWakeButtonPin, LOW);
  esp_sleep_enable_timer_wakeup(microseconds);
  esp_deep_sleep_start();
}

bool waitForButtonState(int state, uint32_t deadline) {
  while (static_cast<int32_t>(deadline - millis()) > 0) {
    if (digitalRead(kWakeButtonPin) == state) {
      delay(kDebounceMs);
      if (digitalRead(kWakeButtonPin) == state) return true;
    }
    delay(5);
  }
  return false;
}

WakeGesture detectWakeGesture() {
  if (esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_EXT0) {
    return WakeGesture::kNone;
  }

  // The low level that caused EXT0 wake is press one. A pause after the third
  // release selects the train toggle; continuing to five presses, with the
  // fifth held for three seconds, selects forced OTA instead.
  const uint32_t deadline = millis() + kGestureWindowMs;
  if (!waitForButtonState(HIGH, deadline)) return WakeGesture::kNone;
  for (uint8_t press = 2; press <= 5; ++press) {
    uint32_t pressDeadline = deadline;
    if (press == 2 || press == 4) {
      pressDeadline = min<uint32_t>(deadline, millis() + kInterpressTimeoutMs);
    } else if (press == 3) {
      pressDeadline = min<uint32_t>(deadline, millis() + kInterpressTimeoutMs);
    }
    if (!waitForButtonState(LOW, pressDeadline)) {
      return WakeGesture::kNone;
    }
    if (press == 5) {
      const uint32_t holdStarted = millis();
      while (digitalRead(kWakeButtonPin) == LOW) {
        if (millis() - holdStarted >= kForceHoldMs) {
          Serial.println("Force-update button gesture recognized");
          return WakeGesture::kForceOta;
        }
        if (static_cast<int32_t>(deadline - millis()) <= 0) {
          return WakeGesture::kNone;
        }
        delay(10);
      }
      return WakeGesture::kNone;
    }
    if (!waitForButtonState(HIGH, deadline)) return WakeGesture::kNone;
    if (press == 3) {
      const uint32_t fourthPressDeadline =
          min<uint32_t>(deadline, millis() + kInterpressTimeoutMs);
      if (!waitForButtonState(LOW, fourthPressDeadline)) {
        Serial.println("Train-toggle button gesture recognized");
        return WakeGesture::kToggleTrains;
      }
      // Press four was consumed while resolving the ambiguous triple/five
      // prefix. Finish its release here and continue directly to press five.
      if (!waitForButtonState(HIGH, deadline)) return WakeGesture::kNone;
      press = 4;
    }
  }
  return WakeGesture::kNone;
}

void configureWellesleyTimezone() {
  setenv("TZ", kWellesleyTimezone, 1);
  tzset();
}

bool clockIsValid() { return time(nullptr) >= kValidClockThreshold; }

bool syncClock(uint32_t timeoutMs = 15000) {
  configTzTime(kWellesleyTimezone, "time.cloudflare.com", "pool.ntp.org");
  const uint32_t deadline = millis() + timeoutMs;
  while (!clockIsValid() && static_cast<int32_t>(deadline - millis()) > 0) {
    delay(200);
  }
  return clockIsValid();
}

bool isActiveTime(time_t now) {
  struct tm localTime;
  localtime_r(&now, &localTime);
  return localTime.tm_hour >= kFirstActiveHour;
}

uint64_t sleepUntilActive(time_t now) {
  struct tm wakeTime;
  localtime_r(&now, &wakeTime);
  wakeTime.tm_hour = kFirstActiveHour;
  wakeTime.tm_min = 0;
  wakeTime.tm_sec = 0;
  wakeTime.tm_isdst = -1;
  const time_t wake = mktime(&wakeTime);
  const time_t seconds = max<time_t>(60, wake - now);
  Serial.printf("Outside the 06:00-midnight window; sleeping %lld seconds\n",
                static_cast<long long>(seconds));
  return static_cast<uint64_t>(seconds) * kMicrosecondsPerSecond;
}

bool otaCheckIsDue(Preferences &prefs, time_t now, bool forced) {
  const uint32_t lastCheck = prefs.getUInt("lastOtaCheck", 0);
  if (!forced && lastCheck != 0 && now >= lastCheck &&
      static_cast<uint32_t>(now - lastCheck) < kOtaIntervalSeconds) {
    return false;
  }
  // Record attempts, not just successful manifest fetches, so an unreachable
  // server cannot turn the 15-minute display poll into repeated OTA traffic.
  prefs.putUInt("lastOtaCheck", static_cast<uint32_t>(now));
  return true;
}

bool waitForWifi(uint32_t timeoutMs) {
  const uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < timeoutMs) {
    delay(250);
  }
  return WiFi.status() == WL_CONNECTED;
}

bool connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);

  const String savedSsid = preferences.getString("ssid", "");
  const String savedPassword = preferences.getString("password", "");
  if (!savedSsid.isEmpty()) {
    WiFi.begin(savedSsid.c_str(), savedPassword.c_str());
  } else if (strlen(AVIAN_WIFI_SSID) > 0) {
    WiFi.begin(AVIAN_WIFI_SSID, AVIAN_WIFI_PASSWORD);
  } else {
    // Arduino-ESP32 keeps station credentials in NVS. This reuses credentials
    // from the sketch that was on the Inkplate before this one was flashed.
    WiFi.begin();
  }

  if (!waitForWifi(30000)) return false;
  Serial.print("Wi-Fi connected; address: ");
  Serial.println(WiFi.localIP());
  return true;
}

String fetchText(const String &url) {
  HTTPClient http;
  http.setConnectTimeout(10000);
  http.setTimeout(15000);
  http.useHTTP10(true);
  if (!http.begin(url)) return "";
  const int status = http.GET();
  String body;
  if (status == HTTP_CODE_OK) {
    body = http.getString();
    body.trim();
  }
  http.end();
  return body;
}

void showProvisioningScreen() {
  display.begin();
  // The panel's native portrait space is 1200x1600 (rotation 0). The driver
  // defaults to landscape rotation 1, which would clip a portrait frame.
  display.setRotation(0);
  display.clearDisplay();
  display.setTextColor(INKPLATE_BLACK);
  display.setTextSize(3);
  display.setCursor(90, 180);
  display.println("AvianVisitors Wi-Fi setup");
  display.setTextSize(2);
  display.setCursor(90, 300);
  display.println("1. Connect your phone to:");
  display.setTextSize(3);
  display.setCursor(130, 370);
  display.println(kSetupAp);
  display.setTextSize(2);
  display.setCursor(90, 470);
  display.println("2. Open http://192.168.4.1");
  display.setCursor(90, 540);
  display.println("3. Enter your home Wi-Fi password");
  display.display();
}

String setupPage(const String &message = "") {
  String page;
  page.reserve(1600);
  page += F("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>");
  page += F("<title>AvianVisitors setup</title><style>body{font:18px system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem;color:#222}label{display:block;margin:1rem 0}input{box-sizing:border-box;width:100%;padding:.75rem;font:inherit}button{padding:.8rem 1.2rem;font:inherit;background:#222;color:#fff;border:0;border-radius:.4rem}.msg{color:#075}</style>");
  page += F("<h1>AvianVisitors</h1><p>Connect this Inkplate to the same network as the bird microphone.</p>");
  if (!message.isEmpty()) {
    page += F("<p class=msg>");
    page += message;
    page += F("</p>");
  }
  page += F("<form method=post action=/save><label>Wi-Fi name<input name=ssid required autocomplete='username'></label><label>Wi-Fi password<input name=password type=password autocomplete='current-password'></label><label>Backend URL<input name=backend value='");
  page += backendUrl();
  page += F("' required></label><button>Save and connect</button></form>");
  return page;
}

[[noreturn]] void startProvisioning() {
  showProvisioningScreen();
  WiFi.disconnect(false, false);
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(kSetupAp);

  DNSServer dns;
  WebServer server(80);
  dns.start(53, "*", WiFi.softAPIP());
  server.on("/", HTTP_GET, [&server]() { server.send(200, "text/html", setupPage()); });
  server.on("/save", HTTP_POST, [&server]() {
    const String ssid = server.arg("ssid");
    const String password = server.arg("password");
    String backend = server.arg("backend");
    backend.trim();
    if (ssid.isEmpty() || !backend.startsWith("http://")) {
      server.send(400, "text/html", setupPage("Please enter a Wi-Fi name and an http:// backend URL."));
      return;
    }
    preferences.putString("ssid", ssid);
    preferences.putString("password", password);
    preferences.putString("backend", backend);
    server.send(200, "text/html", setupPage("Saved. The Inkplate is restarting now."));
    delay(1200);
    ESP.restart();
  });
  server.onNotFound([&server]() {
    server.sendHeader("Location", "http://192.168.4.1/", true);
    server.send(302, "text/plain", "");
  });
  server.begin();

  while (true) {
    dns.processNextRequest();
    server.handleClient();
    delay(2);
  }
}

bool refreshFrame(const String &base, const String &imagePath,
                  const String &displaySignature) {
  const String imageUrl = base + imagePath;
  display.begin();
  display.setRotation(0);
  display.clearDisplay();
  Serial.print("Downloading frame: ");
  Serial.println(imageUrl);
  if (!display.image.drawJpegFromWeb(imageUrl.c_str(), 0, 0, true, false)) {
    Serial.println("Frame download/decode failed; preserving the previous e-paper image");
    return false;
  }
  display.display();
  preferences.putString("frameHash", displaySignature);
  pollsSinceRefresh = 0;
  Serial.println("E-paper refresh complete");
  return true;
}
}  // namespace

void setup() {
  Serial.begin(115200);
  delay(500);
  pinMode(kWakeButtonPin, INPUT_PULLUP);
  configureWellesleyTimezone();
  preferences.begin("avian", false);

  const WakeGesture gesture = detectWakeGesture();
  const bool forceOta = gesture == WakeGesture::kForceOta;
  const bool toggleTrains = gesture == WakeGesture::kToggleTrains;
  bool showTrains = preferences.getBool("showTrains", true);
  if (toggleTrains) {
    showTrains = !showTrains;
    preferences.putBool("showTrains", showTrains);
    Serial.printf("Inkplate trains are now %s\n", showTrains ? "enabled" : "disabled");
  }
  const bool pendingOtaConfirmation = preferences.getUInt("pendingOta", 0) != 0;

  // Time survives normal ESP32 deep sleep. On a cold boot, briefly join Wi-Fi
  // only to establish Wellesley local time before enforcing the quiet window.
  bool wifiConnected = false;
  if (!clockIsValid() || forceOta || toggleTrains || pendingOtaConfirmation) {
    wifiConnected = connectWifi();
    if (!wifiConnected) {
      Serial.println("No saved Wi-Fi connection; starting setup portal");
      startProvisioning();
    }
    if (!syncClock()) {
      Serial.println("Could not establish local time; retrying in five minutes");
      sleepFor(kRetrySleepUs);
    }
  }

  const time_t now = time(nullptr);
  const bool activeTime = isActiveTime(now);
  if (!activeTime && !forceOta && !toggleTrains && !pendingOtaConfirmation) {
    sleepFor(sleepUntilActive(now));
  }

  if (!wifiConnected && !connectWifi()) {
    Serial.println("No saved Wi-Fi connection; starting setup portal");
    startProvisioning();
  }

  const String base = backendUrl();
  otaHandleBootConfirmation(preferences, base);
  const char *wakeDetail = forceOta ? "force-update gesture"
                           : toggleTrains ? (showTrains ? "trains enabled" : "trains disabled")
                                          : "normal wake";
  otaReportStatus(base, "online", wakeDetail);
  if (activeTime || forceOta) {
    if (otaCheckIsDue(preferences, now, forceOta)) {
      otaCheckAndInstall(preferences, base);
    } else {
      Serial.println("Hourly OTA interval has not elapsed; skipping firmware check");
    }
  }

  // A forced check during quiet hours does not also fetch display data.
  if (!activeTime && !toggleTrains) sleepFor(sleepUntilActive(now));

  const String frameBase = showTrains ? "/frame" : "/frame-birds";
  const String signature = fetchText(base + frameBase + ".sha256");
  if (signature.length() != 64) {
    Serial.println("Backend frame is not ready; retrying in five minutes");
    sleepFor(kRetrySleepUs);
  }

  const String displaySignature = (showTrains ? "trains:" : "birds:") + signature;
  const String previous = preferences.getString("frameHash", "");
  const bool dailyHeal = pollsSinceRefresh >= kDailyHealPolls;
  if (displaySignature != previous || dailyHeal || toggleTrains) {
    if (!refreshFrame(base, frameBase + ".jpg", displaySignature)) {
      sleepFor(kRetrySleepUs);
    }
  } else {
    ++pollsSinceRefresh;
    Serial.println("Frame unchanged; skipping the e-paper refresh");
  }

  sleepFor(kNormalSleepUs);
}

void loop() {}
