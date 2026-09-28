// Explicit opt-in: this test writes firmware to an owned device and reboots it.
// It uses the same controller as the OTA screen, without exercising file pickers.
// Stage the image, recovery backup and its .sha256 sidecar inside the macOS app
// container first. Never run this alongside another Bluetooth or USB session.
//
// Required --dart-define values:
// EVILCROW_OTA_WRITE_ENABLED=true, EVILCROW_E2E_DEVICE_ID,
// EVILCROW_OTA_IMAGE, EVILCROW_OTA_SHA256, EVILCROW_OTA_BACKUP,
// EVILCROW_EXPECTED_VERSION.
// This is a same-version reinstall check. It does not test cancellation, power
// loss, rollback, RF transmission, or the USB recovery procedure.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:evilcrow_rf2_controller/main.dart' as app;
import 'package:evilcrow_rf2_controller/providers/ble_provider.dart';
import 'package:evilcrow_rf2_controller/providers/firmware_protocol.dart';
import 'package:evilcrow_rf2_controller/screens/home_screen.dart';
import 'package:evilcrow_rf2_controller/services/ota_firmware_image.dart';
import 'package:evilcrow_rf2_controller/services/ota_transfer_service.dart';
import 'package:evilcrow_rf2_controller/services/usb_backup_files.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:provider/provider.dart';

const writeEnabled = bool.fromEnvironment('EVILCROW_OTA_WRITE_ENABLED');
const deviceId = String.fromEnvironment('EVILCROW_E2E_DEVICE_ID');
const imagePath = String.fromEnvironment('EVILCROW_OTA_IMAGE');
const imageSha256 = String.fromEnvironment('EVILCROW_OTA_SHA256');
const backupPath = String.fromEnvironment('EVILCROW_OTA_BACKUP');
const expectedVersion = String.fromEnvironment('EVILCROW_EXPECTED_VERSION');

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  testWidgets('owned device: verified BLE OTA same-version reinstall',
      (tester) async {
    for (final entry in {
      'EVILCROW_E2E_DEVICE_ID': deviceId,
      'EVILCROW_OTA_IMAGE': imagePath,
      'EVILCROW_OTA_SHA256': imageSha256,
      'EVILCROW_OTA_BACKUP': backupPath,
      'EVILCROW_EXPECTED_VERSION': expectedVersion,
    }.entries) {
      expect(entry.value, isNotEmpty, reason: '${entry.key} is required');
    }
    expect(RegExp(r'^[0-9a-fA-F]{64}$').hasMatch(imageSha256), isTrue);

    // All local checks precede app startup and the first BLE connection.
    final image = OtaFirmwareImage.parse(await File(imagePath).readAsBytes());
    expect(image.sha256, imageSha256.toLowerCase());
    final backupSha256 = await verifyUsbBackup(File(backupPath));
    final report = <String, dynamic>{
      'image_sha256': image.sha256,
      'image_md5': image.md5,
      'image_bytes': image.bytes.length,
      'backup_sha256': backupSha256,
      'expected_version': expectedVersion,
      'backup_device_freshness': 'operator must verify before running',
    };
    binding.reportData = {'ota_check': report};

    app.main();
    await tester.pump();
    final ble = Provider.of<BleProvider>(
        tester.element(find.byType(HomeScreen)),
        listen: false);
    final transfer = ble.otaTransfer;
    final stages = <String>[];
    var lastAcknowledged = 0;
    var progressDecreased = false;
    var invalidProgress = false;
    var cancelAllowedAfterEnd = false;
    var observedRestartDisconnect = false;

    Future<void> until(bool Function() condition, String description) async {
      final deadline = DateTime.now().add(const Duration(seconds: 30));
      while (!condition()) {
        if (DateTime.now().isAfter(deadline)) {
          throw TimeoutException(description);
        }
        await tester.pump(const Duration(milliseconds: 100));
      }
    }

    void observeTransfer() {
      final stage = transfer.stage.name;
      if (stages.isEmpty || stages.last != stage) stages.add(stage);
      progressDecreased |= transfer.acknowledgedBytes < lastAcknowledged;
      invalidProgress |= transfer.acknowledgedBytes < 0 ||
          transfer.acknowledgedBytes > image.bytes.length;
      lastAcknowledged = transfer.acknowledgedBytes;
      if (transfer.stage == OtaStage.verifying ||
          transfer.stage == OtaStage.rebooting) {
        cancelAllowedAfterEnd |= transfer.canCancel;
      }
    }

    void observeConnection() {
      if (transfer.stage == OtaStage.rebooting && !ble.isConnected) {
        observedRestartDisconnect = true;
      }
    }

    transfer.addListener(observeTransfer);
    ble.addListener(observeConnection);
    final watch = Stopwatch()..start();
    try {
      await ble.connectToDevice(BluetoothDevice.fromId(deviceId));
      await until(
          () =>
              ble.isConnected &&
              ble.settingsSynced &&
              ble.firmwareVersion.isNotEmpty,
          'initial BLE settings and version');
      expect(ble.firmwareVersion, expectedVersion,
          reason: 'This test reinstalls the currently installed version');
      report['version_before'] = ble.firmwareVersion;
      report['negotiated_mtu'] = ble.connectedDevice!.mtuNow;

      // Stop owned receivers, then obtain fresh state before the write.
      await ble.sendIdleCommand(0);
      await ble.sendIdleCommand(1);
      await ble
          .sendBinaryCommand(FirmwareBinaryProtocol.createNrfStopAllCommand());
      if (ble.sdrModeActive) {
        await ble.sendBinaryCommand(
            FirmwareBinaryProtocol.createSdrDisableCommand());
      }
      final before = ble.cc1101Modules;
      await ble.sendGetStateCommand();
      await until(
          () =>
              !identical(before, ble.cc1101Modules) &&
              ble.cc1101Modules!.length == 2 &&
              ble.cc1101Modules!.every((module) => module['mode'] == 'Idle') &&
              !ble.nrfScanning &&
              !ble.nrfAttacking &&
              !ble.nrfSpectrumRunning &&
              !ble.nrfJammerRunning &&
              !ble.sdrModeActive,
          'all radios idle');

      final result =
          await ble.installOtaFirmware(image, expectedVersion: expectedVersion);
      expect(result.sha256, image.sha256);
      expect(result.bytes, image.bytes.length);
      expect(result.version, expectedVersion);
      expect(transfer.stage, OtaStage.complete);
      expect(transfer.busy, isFalse);
      expect(transfer.acknowledgedBytes, image.bytes.length);
      expect(progressDecreased || invalidProgress, isFalse);
      expect(cancelAllowedAfterEnd, isFalse);
      expect(observedRestartDisconnect, isTrue);
      expect(
          stages,
          containsAllInOrder([
            'starting',
            'transferring',
            'verifying',
            'rebooting',
            'complete',
          ]));
      expect(ble.isConnected && ble.settingsSynced, isTrue);
      expect(ble.firmwareVersion, expectedVersion);
      report.addAll({
        'passed': true,
        'acknowledged_bytes': transfer.acknowledgedBytes,
        'chunk_bytes': transfer.chunkSize,
        'restart_disconnect_observed': observedRestartDisconnect,
        'version_after': result.version,
        'device_image_hash_readback': false,
      });
    } catch (error) {
      report.addAll({
        'passed': false,
        'error': error.toString(),
        'stage': transfer.stage.name,
        'status': transfer.status
      });
      rethrow;
    } finally {
      transfer.removeListener(observeTransfer);
      ble.removeListener(observeConnection);
      try {
        // The awaited production controller owns abort/reboot recovery. This
        // test never sends a second abort or retries firmware after a failure.
        if (!transfer.busy) await ble.disconnect();
      } finally {
        report['milliseconds'] = watch.elapsedMilliseconds;
        report['stages'] = stages;
        report['transfer_busy_at_cleanup'] = transfer.busy;
        binding.reportData = {'ota_check': report};
        debugPrint('OTA_E2E_RESULTS: ${jsonEncode(report)}');
      }
    }
    expect(tester.takeException(), isNull);
  }, skip: !writeEnabled, timeout: const Timeout(Duration(minutes: 20)));
}
