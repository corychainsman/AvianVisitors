#pragma once

#include <Arduino.h>
#include <Preferences.h>

// Confirms a newly installed build after it has successfully rejoined Wi-Fi.
// A build-number mismatch is blocked and, when appropriate, rolled back.
void otaHandleBootConfirmation(Preferences &preferences, const String &backend);

// Checks the server manifest and installs a newer signed build. A successful
// install reboots and does not return. Failures leave the running app intact.
void otaCheckAndInstall(Preferences &preferences, const String &backend);

// Best-effort last-seen/status beacon; failures never affect frame refreshes.
void otaReportStatus(const String &backend, const String &status, const String &detail);
