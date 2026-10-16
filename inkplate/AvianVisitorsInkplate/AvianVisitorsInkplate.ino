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
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

#include "firmware_version.h"
#include "ota_update.h"
#include "wake_menu.h"

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

enum class WakeGesture { kNone, kRefresh, kToggleTrains, kForceOta };

String backendUrl() {
  String value = preferences.getString("backend", kDefaultBackend);
  while (value.endsWith("/")) value.remove(value.length() - 1);
  return value;
}

void sleepFor(uint64_t microseconds) {
  const time_t started = time(nullptr);
  const uint64_t targetWake = started >= kValidClockThreshold
      ? static_cast<uint64_t>(started) + (microseconds + kMicrosecondsPerSecond - 1) / kMicrosecondsPerSecond
      : 0;
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
  // A stuck/held button must not cause an endless wake/reset loop.
  if (digitalRead(kWakeButtonPin) == HIGH) {
    esp_sleep_enable_ext0_wakeup(kWakeButtonPin, LOW);
  } else {
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_EXT0);
  }
  if (targetWake != 0) {
    const uint64_t current = static_cast<uint64_t>(time(nullptr));
    microseconds = (targetWake > current ? targetWake - current : 1) * kMicrosecondsPerSecond;
    preferences.putULong64("nextWake", targetWake > current ? targetWake : current + 1);
  }
  esp_sleep_enable_timer_wakeup(microseconds);
  esp_deep_sleep_start();
}

struct ButtonEvent { int state; uint32_t at; };
QueueHandle_t buttonEvents = nullptr;
TaskHandle_t buttonSampler = nullptr;
uint32_t lastButtonEventMs = 0;

void sampleButton(void *) {
  int stable = LOW;  // EXT0 already recorded press one.
  int candidate = digitalRead(kWakeButtonPin);
  uint32_t changed = millis();
  for (;;) {
    const int state = digitalRead(kWakeButtonPin);
    if (state != candidate) {
      candidate = state;
      changed = millis();
    }
    if (candidate != stable && millis() - changed >= kDebounceMs) {
      stable = candidate;
      ButtonEvent event{stable, changed + kDebounceMs};
      xQueueSend(buttonEvents, &event, 0);
    }
    vTaskDelay(pdMS_TO_TICKS(5));
  }
}

void startButtonSampling() {
  buttonEvents = xQueueCreate(32, sizeof(ButtonEvent));
  if (buttonEvents && xTaskCreate(sampleButton, "wake-button", 2048, nullptr, 1,
                                  &buttonSampler) != pdPASS) {
    vQueueDelete(buttonEvents);
    buttonEvents = nullptr;
  }
}

void stopButtonSampling() {
  if (buttonSampler) vTaskDelete(buttonSampler);
  buttonSampler = nullptr;
  if (buttonEvents) vQueueDelete(buttonEvents);
  buttonEvents = nullptr;
}

bool waitForButtonState(int state, uint32_t deadline) {
  if (buttonEvents) {
    for (;;) {
      ButtonEvent event;
      if (xQueuePeek(buttonEvents, &event, 0) == pdTRUE) {
        if (static_cast<int32_t>(deadline - event.at) < 0) return false;
        xQueueReceive(buttonEvents, &event, 0);
        if (event.state == state) {
          lastButtonEventMs = event.at;
          return true;
        }
      } else {
        if (static_cast<int32_t>(deadline - millis()) <= 0) return false;
        delay(5);
      }
    }
  }
  // Low-memory fallback still offers normal post-render button interaction.
  while (static_cast<int32_t>(deadline - millis()) > 0) {
    if (digitalRead(kWakeButtonPin) == state) {
      delay(kDebounceMs);
      if (digitalRead(kWakeButtonPin) == state) {
        lastButtonEventMs = millis();
        return true;
      }
    }
    delay(5);
  }
  return false;
}

uint32_t interpressDeadline(uint32_t deadline) {
  const uint32_t next = lastButtonEventMs + kInterpressTimeoutMs;
  return static_cast<int32_t>(deadline - next) < 0 ? deadline : next;
}

WakeGesture detectWakeGesture(uint32_t deadline) {
  // EXT0 is press one. Allow the full menu interval for press two, then
  // preserve the existing triple press and five-press/hold shortcuts.
  if (!waitForButtonState(HIGH, deadline)) return WakeGesture::kNone;
  if (!waitForButtonState(LOW, deadline)) return WakeGesture::kNone;
  if (!waitForButtonState(HIGH, deadline)) return WakeGesture::kRefresh;
  if (!waitForButtonState(LOW, interpressDeadline(deadline))) {
    return WakeGesture::kRefresh;
  }
  if (!waitForButtonState(HIGH, deadline)) return WakeGesture::kRefresh;
  if (!waitForButtonState(LOW, interpressDeadline(deadline))) {
    Serial.println("Train-toggle button gesture recognized");
    return WakeGesture::kToggleTrains;
  }
  if (!waitForButtonState(HIGH, deadline)) return WakeGesture::kRefresh;
  if (!waitForButtonState(LOW, interpressDeadline(deadline))) {
    return WakeGesture::kRefresh;
  }
  const uint32_t holdDeadline = lastButtonEventMs + kForceHoldMs;
  const uint32_t boundedHold = static_cast<int32_t>(deadline - holdDeadline) < 0
                                   ? deadline : holdDeadline;
  if (!waitForButtonState(HIGH, boundedHold) && boundedHold == holdDeadline) {
    Serial.println("Force-update button gesture recognized");
    return WakeGesture::kForceOta;
  }
  return WakeGesture::kRefresh;
}

String menuTime(uint64_t epoch) {
  if (epoch < static_cast<uint64_t>(kValidClockThreshold)) return "Unknown";
  const time_t value = static_cast<time_t>(epoch);
  struct tm local;
  localtime_r(&value, &local);
  char text[32];
  strftime(text, sizeof(text), "%b %d %I:%M %p", &local);
  return String(text);
}

WakeMenuStatus menuStatus() {
  WakeMenuStatus status;
  status.wifiName = preferences.getString("lastSsid", "Unknown network");
  if (!preferences.isKey("wifiOk")) {
    status.wifiStatus = "Radio asleep; no connection history";
  } else if (preferences.getBool("wifiOk", false)) {
    status.wifiStatus = "Asleep; last OK " + preferences.getString("lastIp", "") +
                        " / " + String(preferences.getInt("lastRssi", 0)) + " dBm";
  } else {
    status.wifiStatus = "Asleep; last connection failed";
  }
  status.lastUpdate = menuTime(preferences.getULong64("lastDisplay", 0));
  status.nextUpdate = menuTime(preferences.getULong64("nextWake", 0));
  status.serverVersion = preferences.getString("serverVersion", "Unknown");
  return status;
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

  const bool connected = waitForWifi(30000);
  preferences.putBool("wifiOk", connected);
  const String attemptedSsid = WiFi.SSID();
  if (!attemptedSsid.isEmpty()) preferences.putString("lastSsid", attemptedSsid);
  else if (!savedSsid.isEmpty()) preferences.putString("lastSsid", savedSsid);
  else if (strlen(AVIAN_WIFI_SSID) > 0) preferences.putString("lastSsid", AVIAN_WIFI_SSID);
  if (!connected) return false;
  preferences.putString("lastIp", WiFi.localIP().toString());
  preferences.putInt("lastRssi", WiFi.RSSI());
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
  if (!wakeMenuInvalidate()) {
    Serial.println("Cannot invalidate menu cache; preserving panel while setup portal runs");
    return;
  }
  wakeMenuBegin(display);
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
  wakeMenuBegin(display);
  display.setRotation(0);
  display.clearDisplay();
  Serial.print("Downloading frame: ");
  Serial.println(imageUrl);
  if (!display.image.drawJpegFromWeb(imageUrl.c_str(), 0, 0, true, false)) {
    Serial.println("Frame download/decode failed; preserving the previous e-paper image");
    return false;
  }
  if (!wakeMenuInvalidate()) {
    Serial.println("Cannot invalidate menu cache; preserving the previous e-paper image");
    return false;
  }
  display.display();
  preferences.putString("frameHash", displaySignature);
  preferences.putULong64("lastDisplay", static_cast<uint64_t>(time(nullptr)));
  const bool menuReady = wakeMenuCaptureBackground(display, displaySignature);
  otaReportStatus(base, menuReady ? "menu-ready" : "menu-unavailable", wakeMenuDiagnostic());
  pollsSinceRefresh = 0;
  Serial.println("E-paper refresh complete");
  return true;
}
}  // namespace

void setup() {
  Serial.begin(115200);
  const bool buttonWake = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT0;
  if (!buttonWake) delay(500);
  pinMode(kWakeButtonPin, INPUT_PULLUP);
  configureWellesleyTimezone();
  preferences.begin("avian", false);

  WakeGesture gesture = WakeGesture::kNone;
  if (buttonWake) {
    const uint64_t scheduledWake = preferences.getULong64("nextWake", 0);
    startButtonSampling();
    const bool menuShown = wakeMenuShow(display, preferences.getString("frameHash", ""), menuStatus());
    gesture = detectWakeGesture(millis() + kGestureWindowMs);
    stopButtonSampling();
    if (menuShown) wakeMenuHide(display);
    if (gesture == WakeGesture::kNone) {
      const time_t current = time(nullptr);
      if (scheduledWake >= static_cast<uint64_t>(kValidClockThreshold) && clockIsValid()) {
        const uint64_t remaining = scheduledWake > static_cast<uint64_t>(current)
                                       ? scheduledWake - static_cast<uint64_t>(current) : 1;
        sleepFor(remaining * kMicrosecondsPerSecond);
      }
      sleepFor(clockIsValid() && !isActiveTime(current) ? sleepUntilActive(current) : kNormalSleepUs);
    }
  }
  const bool forceRefresh = gesture == WakeGesture::kRefresh;
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
  if (!clockIsValid() || forceOta || forceRefresh || toggleTrains || pendingOtaConfirmation) {
    wifiConnected = connectWifi();
    if (!wifiConnected) {
      if (buttonWake) sleepFor(kRetrySleepUs);
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
  if (!activeTime && !forceOta && !forceRefresh && !toggleTrains && !pendingOtaConfirmation) {
    sleepFor(sleepUntilActive(now));
  }

  if (!wifiConnected && !connectWifi()) {
    if (buttonWake) sleepFor(kRetrySleepUs);
    Serial.println("No saved Wi-Fi connection; starting setup portal");
    startProvisioning();
  }

  const String base = backendUrl();
  // Fetch only during an existing network wake; menu opening stays offline.
  const String serverVersion = fetchText(base + "/avian/server-version.txt");
  bool validServerVersion = !serverVersion.isEmpty() && serverVersion.length() <= 23;
  for (size_t i = 0; validServerVersion && i < serverVersion.length(); ++i) {
    const char c = serverVersion[i];
    validServerVersion = (c >= '0' && c <= '9') || c == '.';
  }
  if (validServerVersion) preferences.putString("serverVersion", serverVersion);
  otaHandleBootConfirmation(preferences, base);
  const char *wakeDetail = forceOta ? "force-update gesture"
                           : forceRefresh ? "manual data refresh"
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
  if (!activeTime && !forceRefresh && !toggleTrains) sleepFor(sleepUntilActive(now));

  if (!wakeMenuPrepareStorage(base)) {
    // Avoid repeatedly flashing the whole panel when the menu cannot be saved.
    sleepFor(kRetrySleepUs);
  }

  const String frameBase = showTrains ? "/frame" : "/frame-birds";
  const String signature = fetchText(base + frameBase + ".sha256");
  if (signature.length() != 64) {
    Serial.println("Backend frame is not ready; retrying in five minutes");
    sleepFor(kRetrySleepUs);
  }

  const String displaySignature = (showTrains ? "trains:" : "birds:") + signature;
  const String previous = preferences.getString("frameHash", "");
  const bool dailyHeal = pollsSinceRefresh >= kDailyHealPolls;
  const bool needsMenuBackground = !wakeMenuHasBackground(displaySignature);
  if (displaySignature != previous || dailyHeal || toggleTrains || forceRefresh || needsMenuBackground) {
    if (!refreshFrame(base, frameBase + ".jpg", displaySignature)) {
      sleepFor(kRetrySleepUs);
    }
  } else {
    ++pollsSinceRefresh;
    Serial.println("Frame unchanged; skipping the e-paper refresh");
    otaReportStatus(base, "menu-ready", wakeMenuDiagnostic());
  }

  const time_t finished = time(nullptr);
  const time_t nextPoll = finished + kNormalSleepUs / kMicrosecondsPerSecond;
  sleepFor(isActiveTime(nextPoll) ? kNormalSleepUs
                                : (nextPoll - finished) * kMicrosecondsPerSecond + sleepUntilActive(nextPoll));
}

void loop() {}
