import 'package:flutter_blue_plus/flutter_blue_plus.dart';

/// Called only after the device has acknowledged final OTA verification.
/// The disconnect waiter must be armed before issuing REBOOT and have a
/// deadline. Restart can end the BLE connection before the GATT write returns.
Future<void> sendOtaRebootAndWaitForDisconnect({
  required Future<void> Function() sendReboot,
  required Future<void> disconnected,
}) async {
  disconnected.ignore(); // It may fail while the write is still pending.
  try {
    await sendReboot();
  } on FlutterBluePlusException catch (error) {
    if (error.platform != ErrorPlatform.fbp ||
        error.code != FbpErrorCode.deviceIsDisconnected.index) {
      rethrow;
    }
    // This error alone is not evidence of reboot. Still require the expected
    // disconnect, then let the caller reconnect and verify a fresh version.
  }
  await disconnected;
}
