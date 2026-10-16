#pragma once
#include <Arduino.h>

void menuTimingSetupStart();
void menuTimingStart();
void menuTimingCacheLoaded();
void menuTimingGraphicsReady();
void menuTimingDrawingReady();
void menuTimingFinished();
// Last successful opening survives deep sleep; no network access while opening.
String menuTimingJson();
