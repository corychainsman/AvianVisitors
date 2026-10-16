#pragma once
#include <Arduino.h>
// Only the expected menu-cache partition is eligible, after verified backup.
bool recoverMenuStorage(const String &backend);
