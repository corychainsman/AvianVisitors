#pragma once

#include <Inkplate.h>

struct WakeMenuStatus {
  String wifiName;
  String wifiStatus;
  String lastUpdate;
  String nextUpdate;
  String serverVersion;
};

void wakeMenuBegin(Inkplate &display);
bool wakeMenuHasBackground(const String &frameSignature);
// Invalidate before any full-panel update, capture after it completes.
bool wakeMenuInvalidate();
bool wakeMenuCaptureBackground(Inkplate &display, const String &frameSignature);
bool wakeMenuShow(Inkplate &display, const String &frameSignature,
                  const WakeMenuStatus &status);
void wakeMenuHide(Inkplate &display);
