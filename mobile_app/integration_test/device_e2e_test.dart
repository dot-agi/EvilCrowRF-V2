// Explicitly opt in to tests against an owned, powered EvilCrow.
// Uses passive receivers and temporary files only. Never transmits RF, formats
// storage, resets user settings, or installs firmware.
import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:evilcrow_rf2_controller/main.dart' as app;
import 'package:evilcrow_rf2_controller/l10n/app_localizations.dart';
import 'package:evilcrow_rf2_controller/providers/ble_provider.dart';
import 'package:evilcrow_rf2_controller/providers/firmware_protocol.dart';
import 'package:evilcrow_rf2_controller/screens/home_screen.dart';
import 'package:evilcrow_rf2_controller/screens/subghz_screen.dart';
import 'package:evilcrow_rf2_controller/screens/nrf_screen.dart';
import 'package:evilcrow_rf2_controller/screens/files_screen.dart';
import 'package:evilcrow_rf2_controller/screens/settings_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:provider/provider.dart';

import 'fixtures/synthetic_signals.dart';

const deviceId = String.fromEnvironment('EVILCROW_E2E_DEVICE_ID');
const expectedVersion = String.fromEnvironment('EVILCROW_EXPECTED_VERSION');
// Optional exact file left by an interrupted earlier run; never a directory.
const cleanupFile = String.fromEnvironment('EVILCROW_E2E_CLEANUP_FILE');

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  testWidgets('owned device: passive macOS workflows', (tester) async {
    app.main();
    await tester.pump();
    final home = tester.element(find.byType(HomeScreen));
    final ble = Provider.of<BleProvider>(home, listen: false);
    final results = <String, dynamic>{};
    final testRoot = '/ec-e2e-${DateTime.now().microsecondsSinceEpoch}';

    Future<void> until(bool Function() condition, String description,
        {Duration timeout = const Duration(seconds: 20)}) async {
      final deadline = DateTime.now().add(timeout);
      while (!condition()) {
        if (DateTime.now().isAfter(deadline)) {
          throw TimeoutException(description, timeout);
        }
        await tester.pump(const Duration(milliseconds: 100));
      }
    }

    Future<void> check(String name, Future<void> Function() action) async {
      final watch = Stopwatch()..start();
      try {
        await action();
        results[name] = {
          'passed': true,
          'milliseconds': watch.elapsedMilliseconds
        };
        debugPrint('E2E_PASS: $name');
      } catch (error, stack) {
        results[name] = {'passed': false, 'error': error.toString()};
        debugPrint('E2E_FAIL: $name: $error\n$stack');
      }
      binding.reportData = {'device_checks': results};
    }

    Future<void> freshState() async {
      final before = ble.cc1101Modules;
      await ble.sendGetStateCommand();
      await until(
          () => !identical(before, ble.cc1101Modules), 'fresh device state');
    }

    Future<void> deviceState(
        bool Function() condition, String description) async {
      final deadline = DateTime.now().add(const Duration(seconds: 20));
      do {
        await freshState();
        if (condition()) return;
        await tester.pump(const Duration(milliseconds: 200));
      } while (DateTime.now().isBefore(deadline));
      throw TimeoutException(description);
    }

    Future<void> tab(IconData icon) async {
      await tester.tap(find
          .descendant(
              of: find.byType(BottomNavigationBar), matching: find.byIcon(icon))
          .first);
      await tester.pump(const Duration(milliseconds: 400));
    }

    Future<void> filesReady() =>
        until(() => !ble.isLoadingFiles, 'file listing');

    Future<void> storageRoundTrip(int storage) async {
      await filesReady();
      await ble.switchPathType(storage);
      await filesReady();
      final root = '$testRoot-$storage';
      expect(ble.fileList.any((f) => f.name == root.substring(1)), isFalse);
      final original = '$root/original.txt';
      final renamed = '$root/renamed.txt';
      final copied = '$root/copied.txt';
      final moved = '$root/moved.txt';
      final content =
          List.generate(40, (i) => 'EvilCrow E2E $i: $storage\n').join();
      var created = false;
      try {
        expect(await ble.createDirectory(root, pathType: storage), isTrue);
        created = true;
        await filesReady();
        final upload = await ble.uploadFileFromBytes(
            Uint8List.fromList(utf8.encode(content)), original,
            pathType: storage);
        expect(upload['success'], isTrue);
        expect(await ble.readFileContent(original, pathType: storage), content);
        expect(await ble.renameFile(original, 'renamed.txt', pathType: storage),
            isTrue);
        await filesReady();
        expect(await ble.readFileContent(renamed, pathType: storage), content);
        expect(await ble.copyFile(renamed, copied), isTrue);
        await filesReady();
        expect(await ble.downloadFile(copied), content);
        expect(
            await ble.moveFile(copied, moved,
                sourcePathType: storage, destPathType: storage),
            isTrue);
        await filesReady();
        expect(await ble.readFileContent(moved, pathType: storage), content);
        final tree = await ble.getDirectoryTree(pathType: storage);
        expect(tree, isNotEmpty);
      } finally {
        // Only remove names inside the unique directory created by this run.
        if (created) {
          await filesReady();
          await ble.navigateToDirectory(root.substring(1));
          await filesReady();
          final ownedFiles =
              ble.fileList.where((f) => f.isFile).map((f) => f.name).toList();
          for (final file in ownedFiles) {
            expect(
                await ble.deleteFile('$root/$file', pathType: storage), isTrue);
            await filesReady();
          }
          await ble.switchPathType(storage);
          await filesReady();
          expect(await ble.deleteFile(root, pathType: storage), isTrue);
          await filesReady();
          await ble.refreshFileList(forceRefresh: true);
          await filesReady();
          expect(ble.fileList.any((f) => f.name == root.substring(1)), isFalse);
        }
      }
    }

    try {
      // Exercise the remembered-device path before any scan or test-side
      // adapter readiness wait. The provider must handle CoreBluetooth startup.
      await check('cold_cached_connect', () async {
        try {
          await ble.connectToDevice(BluetoothDevice.fromId(deviceId));
          await until(() => ble.isConnected && ble.settingsSynced,
              'direct connection without a discovery scan');
        } finally {
          await ble.disconnect();
          await until(() => !ble.isConnected, 'direct-connection cleanup');
        }
      });
      await until(
          () => FlutterBluePlus.adapterStateNow == BluetoothAdapterState.on,
          'Bluetooth enabled',
          timeout: const Duration(seconds: 40));
      await ble.startScan();
      expect(
          ble.supportedScanResults
              .any((r) => r.device.remoteId.str == deviceId),
          isTrue,
          reason:
              'The specified owned device must be discovered before connecting');
      await ble.connectToDevice(BluetoothDevice.fromId(deviceId));
      await until(
          () =>
              ble.isConnected &&
              ble.settingsSynced &&
              ble.firmwareVersion.isNotEmpty,
          'BLE connection and firmware settings');
      await freshState();
      expect(ble.cc1101Modules, hasLength(2));
      if (expectedVersion.isNotEmpty) {
        expect(ble.firmwareVersion, expectedVersion);
      }
      results['discovery_connection_state'] = {
        'passed': true,
        'firmware': ble.firmwareVersion,
        'sd_mounted': ble.sdMounted,
        'sd_total_mb': ble.sdTotalMB,
        'nrf_present': ble.nrfPresent
      };
      debugPrint('E2E_PASS: discovery_connection_state');
      if (cleanupFile.isNotEmpty) {
        expect(
            RegExp(r'^/ec-e2e-[0-9]+-kia\.sub$').hasMatch(cleanupFile), isTrue);
        await check('prior_run_fixture_cleanup', () async {
          expect(await ble.deleteFile(cleanupFile, pathType: 5), isTrue);
          await filesReady();
        });
      }

      await check('connected_navigation_views', () async {
        for (final entry in <IconData, Type>{
          Icons.settings_input_antenna: SubGhzScreen,
          Icons.wifi_tethering: NrfScreen,
          Icons.folder: FilesScreen,
          Icons.settings: SettingsScreen,
        }.entries) {
          await tab(entry.key);
          expect(find.byType(entry.value), findsOneWidget);
          expect(tester.takeException(), isNull);
        }
        await filesReady();
        await tab(Icons.home);
      });

      await check('device_name_settings_readback_restore', () async {
        final name = ble.deviceName;
        final rssi = ble.scannerRssi;
        final changedRssi = rssi == -65 ? -64 : -65;
        final settings = Uint8List.fromList([
          rssi & 0xff,
          ble.bruterPower,
          ble.bruterDelayMs & 0xff,
          (ble.bruterDelayMs >> 8) & 0xff,
          ble.bruterRepeats,
          ble.radioPowerMod1 & 0xff,
          ble.radioPowerMod2 & 0xff,
          ble.cpuTempOffsetDeciC & 0xff,
          (ble.cpuTempOffsetDeciC >> 8) & 0xff,
        ]);
        Future<void> setRssi(int value) async {
          final payload = Uint8List.fromList(settings)..[0] = value & 0xff;
          await ble.sendBinaryCommand(
              FirmwareBinaryProtocol.createSettingsUpdateCommand(payload));
          await deviceState(
              () => ble.scannerRssi == value, 'settings readback');
        }

        try {
          await ble.sendBinaryCommand(
              FirmwareBinaryProtocol.createSetDeviceNameCommand(
                  'EvilCrow_E2E'));
          await freshState();
          await until(
              () => ble.deviceName == 'EvilCrow_E2E', 'device name readback');
          await setRssi(changedRssi);
          expect(ble.scannerRssi, changedRssi);
        } finally {
          try {
            await ble.sendBinaryCommand(
                FirmwareBinaryProtocol.createSetDeviceNameCommand(name));
            await freshState();
            await until(() => ble.deviceName == name, 'restored device name');
          } finally {
            await setRssi(rssi);
          }
          expect(ble.scannerRssi, rssi);
        }
      });

      await check('scanner_ui_start_navigation_cleanup', () async {
        try {
          await tab(Icons.home);
          final l10n =
              AppLocalizations.of(tester.element(find.byType(HomeScreen)))!;
          await tester.tap(find.text('1+2'));
          await tester.pump();
          await tester.tap(find.byTooltip('${l10n.start} ${l10n.scanner}'));
          await tester.pump(const Duration(seconds: 1));
          await freshState();
          expect(ble.cc1101Modules!.every((m) => m['mode'] != 'Idle'), isTrue);
          await tab(Icons.settings);
          await freshState();
          await deviceState(
              () => ble.cc1101Modules!.every((m) => m['mode'] == 'Idle'),
              'both scanner radios stopped after navigation');
          expect(ble.cc1101Modules!.every((m) => m['mode'] == 'Idle'), isTrue);
        } finally {
          await tab(Icons.settings);
          for (final module in [0, 1]) {
            await ble.sendIdleCommand(module);
          }
          await freshState();
        }
        await tab(Icons.home);
      });

      for (final storage in [4, 5]) {
        await check('storage_${storage == 4 ? 'internal' : 'sd'}_round_trip',
            () => storageRoundTrip(storage));
      }

      for (final button in [1, 2]) {
        await check('button_${button}_mapping_readback_and_restore', () async {
          int action() =>
              button == 1 ? ble.deviceBtn1Action : ble.deviceBtn2Action;
          final original = action();
          expect(original, inInclusiveRange(0, 6));
          try {
            await ble.sendBinaryCommand(
                FirmwareBinaryProtocol.createHwButtonConfigCommand(button, 4));
            await freshState();
            await until(() => action() == 4, 'button LED mapping');
          } finally {
            await ble.sendBinaryCommand(
                FirmwareBinaryProtocol.createHwButtonConfigCommand(
                    button, original));
            await freshState();
            await until(() => action() == original, 'restored button mapping');
          }
        });
      }

      await check('passive_record_start_stop', () async {
        final before =
            ble.recordedRuntimeFiles.map((f) => f['filename']).toSet();
        try {
          await ble.sendRecordCommand(
              frequency: 433.92, module: 0, preset: 'Ook650');
          await deviceState(() => ble.getModuleStatus(0) == 'RecordSignal',
              'passive recorder running');
          expect(ble.getModuleStatus(0), 'RecordSignal');
        } finally {
          await ble.sendIdleCommand(0);
          await deviceState(() => ble.getModuleStatus(0) == 'Idle',
              'passive recorder stopped');
          expect(ble.getModuleStatus(0), 'Idle');
          final captures = ble.recordedRuntimeFiles
              .map((f) => f['filename'])
              .where((name) => !before.contains(name))
              .toList();
          if (captures.isNotEmpty) {
            debugPrint('E2E_RETAINED_CAPTURE_FILES: ${jsonEncode(captures)}');
          }
        }
      });

      await check('protopirate_file_decoder', () async {
        final fixturePath = '$testRoot-kia.sub';
        try {
          final upload = await ble.uploadFileFromBytes(
              Uint8List.fromList(utf8.encode(kiaV0SubFile)), fixturePath,
              pathType: 5);
          expect(upload['success'], isTrue);
          ble.ppClearResults();
          await ble.ppLoadSubFile(fixturePath);
          await until(
              () => ble.ppResults.any((r) =>
                  r.protocolName == 'Kia V0' &&
                  r.serial == 0x0abcdef &&
                  r.button == 3 &&
                  r.counter == 1 &&
                  r.dataBits == 61 &&
                  r.data == 0x0000010ABCDEF306 &&
                  r.crcValid),
              'synthetic Kia V0 file decoding');
        } finally {
          await ble.switchPathType(5);
          await filesReady();
          await ble.refreshFileList(forceRefresh: true);
          await filesReady();
          if (ble.fileList.any((f) => f.name == fixturePath.substring(1))) {
            expect(await ble.deleteFile(fixturePath, pathType: 5), isTrue);
            await filesReady();
          }
        }
      });

      await check('nrf_passive_scan_start_stop', () async {
        try {
          await ble.sendBinaryCommand(
              FirmwareBinaryProtocol.createNrfScanStartCommand());
          await deviceState(() => ble.nrfScanning, 'NRF passive scan running');
          await ble.sendBinaryCommand(
              FirmwareBinaryProtocol.createNrfScanStatusCommand());
        } finally {
          await ble.sendBinaryCommand(
              FirmwareBinaryProtocol.createNrfStopAllCommand());
          await deviceState(() => !ble.nrfScanning, 'NRF passive scan stopped');
        }
      });

      await check('protopirate_passive_decode_start_stop', () async {
        try {
          await ble.ppStartDecode();
          await ble.ppGetStatus();
          await deviceState(() => ble.getModuleStatus(0) == 'ProtoPirate',
              'ProtoPirate started');
          expect(ble.getModuleStatus(0), 'ProtoPirate');
          expect(ble.ppDecoding, isTrue);
          await ble.ppGetHistoryCount();
        } finally {
          await ble.ppStopDecode();
          await ble.ppGetStatus();
          await deviceState(
              () => ble.getModuleStatus(0) == 'Idle', 'ProtoPirate stopped');
          expect(ble.getModuleStatus(0), 'Idle');
          expect(ble.ppDecoding, isFalse);
        }
      });

      await check('nrf_passive_spectrum_and_stop', () async {
        expect(ble.nrfPresent, isTrue);
        await tab(Icons.wifi_tethering);
        final before = ble.nrfSpectrumLevels;
        try {
          await ble.sendBinaryCommand(
              FirmwareBinaryProtocol.createNrfSpectrumStartCommand());
          await until(() => !identical(before, ble.nrfSpectrumLevels),
              'NRF spectrum notification');
          expect(ble.nrfSpectrumLevels, hasLength(126));
          await deviceState(
              () => ble.nrfSpectrumRunning, 'NRF spectrum running state');
          await tab(Icons.home);
          await deviceState(() => !ble.nrfSpectrumRunning,
              'NRF spectrum stopped after navigation');
        } finally {
          await ble.sendBinaryCommand(
              FirmwareBinaryProtocol.createNrfStopAllCommand());
          await deviceState(
              () => !ble.nrfSpectrumRunning, 'NRF spectrum stopped state');
        }
      });

      await check('disconnect_reconnect', () async {
        await ble.disconnect();
        await until(() => !ble.isConnected, 'disconnect');
        await ble.connectToDevice(BluetoothDevice.fromId(deviceId));
        await until(() => ble.isConnected && ble.settingsSynced, 'reconnect');
        await freshState();
        expect(ble.cc1101Modules!.every((m) => m['mode'] == 'Idle'), isTrue);
      });
    } finally {
      if (ble.isConnected) {
        try {
          for (final module in [0, 1]) {
            await check(
                'cleanup_radio_$module', () => ble.sendIdleCommand(module));
          }
          await check(
              'cleanup_nrf',
              () => ble.sendBinaryCommand(
                  FirmwareBinaryProtocol.createNrfStopAllCommand()));
        } finally {
          await ble.disconnect();
        }
      }
      binding.reportData = {'device_checks': results};
      debugPrint('E2E_RESULTS: ${jsonEncode(results)}');
    }
    expect(results.values.where((r) => r['passed'] != true), isEmpty);
    expect(tester.takeException(), isNull);
  }, skip: deviceId.isEmpty, timeout: const Timeout(Duration(minutes: 12)));
}
