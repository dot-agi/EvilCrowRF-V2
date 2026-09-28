import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:evilcrow_rf2_controller/services/ota_firmware_image.dart';
import 'package:evilcrow_rf2_controller/services/ota_transfer_service.dart';
import 'package:flutter_test/flutter_test.dart';

class _Device {
  final events = StreamController<OtaEvent>.broadcast(sync: true);
  final commands = <Uint8List>[];
  final received = <int>[];
  final guards = <bool>[];
  int packetLimit = 182; // ATT payload for a negotiated MTU of 185.
  int total = 0;
  int reboots = 0;
  String? imageMd5;
  String version = '1.1.4';
  Future<bool> Function(Uint8List)? intercept;
  Future<String> Function()? reboot;
  bool failGuardRelease = false;

  Future<void> write(Uint8List command) async {
    expect(command.length, lessThanOrEqualTo(packetLimit));
    commands.add(Uint8List.fromList(command));
    if (await intercept?.call(command) ?? false) return;
    switch (command[7]) {
      case 0x30:
        total = ByteData.sublistView(command).getUint32(8, Endian.little);
        imageMd5 = ascii.decode(command.sublist(12, 44));
        events.add(OtaEvent.progress(0, total));
      case 0x31:
        received.addAll(command.sublist(8, command.length - 1));
        events.add(OtaEvent.progress(received.length, total));
      case 0x32:
        events.add(const OtaEvent.complete());
      case 0x33:
        events.add(const OtaEvent.commandSuccess());
      case 0x35:
        events.add(OtaEvent.progress(received.length, total));
      default:
        fail('Unexpected OTA command ${command[7]}');
    }
  }

  OtaTransferService service() => OtaTransferService(
        events: events.stream,
        write: write,
        maximumPacketBytes: () => packetLimit,
        rebootAndReconnect: () async {
          reboots++;
          return await reboot?.call() ?? version;
        },
        setQuitGuard: (enabled) async {
          guards.add(enabled);
          if (!enabled && failGuardRelease) {
            throw StateError('native guard unavailable');
          }
        },
        ackTimeout: const Duration(milliseconds: 30),
      );
}

void main() {
  final image = OtaFirmwareImage.parse(
      File('test/fixtures/ota_valid_esp32.bin').readAsBytesSync());
  late _Device device;
  late OtaTransferService transfer;
  setUp(() {
    device = _Device();
    transfer = device.service();
  });
  tearDown(() async {
    transfer.dispose();
    await device.events.close();
  });

  test(
      'negotiated packets transfer exact bytes and await synchronous device ACKs',
      () async {
    final result = await transfer.install(image, expectedVersion: '1.1.4');
    expect(device.received, image.bytes);
    expect(device.imageMd5, image.md5);
    expect(transfer.chunkSize, 173);
    expect(result.bytes, image.bytes.length);
    expect(result.sha256, image.sha256);
    expect(transfer.stage, OtaStage.complete);
    expect(device.guards, [true, false]);
    expect(device.reboots, 1);
  });

  test('sent bytes are not progress until firmware acknowledges them',
      () async {
    var first = true;
    device.intercept = (command) async {
      if (command[7] != 0x31 || !first) return false;
      first = false;
      device.received.addAll(command.sublist(8, command.length - 1));
      return true;
    };
    final finished = transfer.install(image, expectedVersion: '1.1.4');
    await Future<void>.delayed(Duration.zero);
    expect(transfer.acknowledgedBytes, 0);
    expect(device.commands.where((c) => c[7] == 0x31), hasLength(1));
    device.events.add(OtaEvent.progress(device.received.length, device.total));
    await finished;
    expect(transfer.acknowledgedBytes, image.bytes.length);
  });

  test('a lost data ACK queries status without resending the chunk', () async {
    var first = true;
    device.intercept = (command) async {
      if (command[7] != 0x31 || !first) return false;
      first = false;
      device.received.addAll(command.sublist(8, command.length - 1));
      return true;
    };
    await transfer.install(image, expectedVersion: '1.1.4');
    expect(device.commands.where((c) => c[7] == 0x35), hasLength(1));
    expect(device.received, image.bytes);
  });

  test('begin rejection sends no data and retains the device error', () async {
    device.intercept = (command) async {
      if (command[7] != 0x30) return false;
      device.events
          .add(const OtaEvent.error('OTA already in progress (state=5)'));
      return true;
    };
    await expectLater(
        transfer.install(image, expectedVersion: '1.1.4'), throwsStateError);
    expect(device.commands.map((c) => c[7]), [0x30, 0x33]);
    expect(transfer.status, contains('state=5'));
    expect(transfer.status, contains('Restart'));
    expect(device.reboots, 0);
  });

  test('wrong acknowledged byte count aborts without finalization', () async {
    device.intercept = (command) async {
      if (command[7] != 0x31) return false;
      device.events
          .add(OtaEvent.progress(image.bytes.length + 1, image.bytes.length));
      return true;
    };
    await expectLater(
        transfer.install(image, expectedVersion: '1.1.4'), throwsStateError);
    expect(device.commands.any((c) => c[7] == 0x32), isFalse);
    expect(device.commands.last[7], 0x33);
  });

  test('cancel waits for current write then aborts before another data chunk',
      () async {
    device.intercept = (command) async {
      if (command[7] == 0x31) transfer.requestCancel();
      return false;
    };
    await expectLater(transfer.install(image, expectedVersion: '1.1.4'),
        throwsA(isA<OtaCancelled>()));
    expect(device.commands.where((c) => c[7] == 0x31), hasLength(1));
    expect(device.commands.last[7], 0x33);
    expect(transfer.stage, OtaStage.cancelled);
    expect(device.reboots, 0);
    expect(transfer.busy, isFalse);
  });

  test('END rejection never reboots or claims installation success', () async {
    device.intercept = (command) async {
      if (command[7] != 0x32) return false;
      device.events.add(const OtaEvent.error('Verify failed: MD5 mismatch'));
      return true;
    };
    await expectLater(
        transfer.install(image, expectedVersion: '1.1.4'), throwsStateError);
    expect(transfer.stage, OtaStage.failed);
    expect(device.reboots, 0);
    expect(device.commands.any((c) => c[7] == 0x33), isFalse);
  });

  test('END timeout reports uncertain completion and cannot be cancelled',
      () async {
    device.intercept = (command) async {
      if (command[7] != 0x32) return false;
      expect(transfer.canCancel, isFalse);
      transfer.requestCancel();
      return true;
    };
    await expectLater(transfer.install(image, expectedVersion: '1.1.4'),
        throwsA(isA<TimeoutException>()));
    expect(transfer.stage, OtaStage.failed);
    expect(transfer.status, contains('may already be selected'));
    expect(device.commands.any((c) => c[7] == 0x33), isFalse);
    expect(device.reboots, 0);
  });

  test('quit guard remains held until reconnect and matching version finish',
      () async {
    final reconnected = Completer<String>();
    device.reboot = () => reconnected.future;
    final finished = transfer.install(image, expectedVersion: '1.1.4');
    await Future<void>.delayed(Duration.zero);
    expect(transfer.stage, OtaStage.rebooting);
    expect(transfer.busy, isTrue);
    expect(transfer.canCancel, isFalse);
    expect(device.guards, [true]);
    reconnected.complete('1.1.4');
    await finished;
    expect(device.guards, [true, false]);
  });

  test('a mismatched fresh firmware version is not a successful installation',
      () async {
    device.version = '1.1.3';
    await expectLater(
        transfer.install(image, expectedVersion: '1.1.4'), throwsStateError);
    expect(transfer.stage, OtaStage.failed);
    expect(transfer.status, contains('expected 1.1.4'));
  });

  test('small MTU and duplicate starts cannot begin a second write', () async {
    device.packetLimit = 20;
    await expectLater(
        transfer.install(image, expectedVersion: '1.1.4'), throwsStateError);
    expect(device.commands, isEmpty);
    device.packetLimit = 182;
    final reconnected = Completer<String>();
    device.reboot = () => reconnected.future;
    final finished = transfer.install(image, expectedVersion: '1.1.4');
    await Future<void>.delayed(Duration.zero);
    await expectLater(
        transfer.install(image, expectedVersion: '1.1.4'), throwsStateError);
    reconnected.complete('1.1.4');
    await finished;
    expect(device.commands.where((c) => c[7] == 0x30), hasLength(1));
  });

  test('native guard release failure still clears local busy state', () async {
    device.failGuardRelease = true;
    await expectLater(
        transfer.install(image, expectedVersion: '1.1.4'), throwsStateError);
    expect(transfer.busy, isFalse);
    expect(transfer.stage, OtaStage.failed);
    expect(transfer.status, contains('could not be released'));
  });

  test('disconnect during DATA stops the session and reports unconfirmed abort',
      () async {
    device.intercept = (command) async {
      if (command[7] == 0x33) throw StateError('not connected');
      if (command[7] != 0x31) return false;
      device.events.add(const OtaEvent.disconnected());
      return true;
    };
    await expectLater(
        transfer.install(image, expectedVersion: '1.1.4'), throwsStateError);
    expect(transfer.status, contains('Bluetooth disconnected'));
    expect(transfer.status, contains('Abort was not acknowledged'));
    expect(device.commands.any((c) => c[7] == 0x32), isFalse);
    expect(device.reboots, 0);
    expect(device.guards, [true, false]);
  });

  test('disposal cannot overlap abort with an unfinished characteristic write',
      () async {
    final writeFinished = Completer<void>();
    device.intercept = (command) async {
      if (command[7] == 0x31) await writeFinished.future;
      return false;
    };
    final finished = transfer.install(image, expectedVersion: '1.1.4');
    final failed = expectLater(finished, throwsA(isA<OtaCancelled>()));
    await Future<void>.delayed(Duration.zero);
    transfer.dispose();
    expect(transfer.busy, isTrue);
    expect(device.commands.any((c) => c[7] == 0x33), isFalse);
    expect(device.guards, [true]);
    writeFinished.complete();
    await failed;
    expect(device.commands.last[7], 0x33);
    expect(device.guards, [true, false]);
    // This case already disposed the controller.
    transfer = device.service();
  });
}
