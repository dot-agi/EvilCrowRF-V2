import 'dart:async';

import 'package:evilcrow_rf2_controller/services/ota_reboot.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';
import 'package:flutter_test/flutter_test.dart';

FlutterBluePlusException disconnectedError() => FlutterBluePlusException(
    ErrorPlatform.fbp,
    'writeCharacteristic',
    FbpErrorCode.deviceIsDisconnected.index,
    'Device is disconnected');

void main() {
  test('observed reboot disconnect can precede GATT write completion',
      () async {
    final disconnected = Completer<void>();
    var readyToReconnect = false;
    await sendOtaRebootAndWaitForDisconnect(
      sendReboot: () async {
        disconnected.complete();
        throw disconnectedError();
      },
      disconnected: disconnected.future,
    );
    readyToReconnect = true;
    expect(readyToReconnect, isTrue);
  });

  test('a disconnect write error still waits for the observed disconnect',
      () async {
    final disconnected = Completer<void>();
    var readyToReconnect = false;
    final reboot = sendOtaRebootAndWaitForDisconnect(
      sendReboot: () async => throw disconnectedError(),
      disconnected: disconnected.future,
    ).then((_) => readyToReconnect = true);
    await Future<void>.delayed(Duration.zero);
    expect(readyToReconnect, isFalse);
    disconnected.complete();
    await reboot;
    expect(readyToReconnect, isTrue);
  });

  test('missing disconnect does not turn a write error into reboot success',
      () async {
    final disconnected = Completer<void>();
    final reboot = sendOtaRebootAndWaitForDisconnect(
      sendReboot: () async => throw disconnectedError(),
      disconnected: disconnected.future,
    );
    final failed = expectLater(reboot, throwsA(isA<TimeoutException>()));
    disconnected.completeError(TimeoutException('No observed disconnect'));
    await failed;
  });

  test('unrelated write errors propagate even after a disconnect', () async {
    final unrelated = FlutterBluePlusException(ErrorPlatform.fbp,
        'writeCharacteristic', FbpErrorCode.timeout.index, 'Write timed out');
    await expectLater(
        sendOtaRebootAndWaitForDisconnect(
          sendReboot: () async => throw unrelated,
          disconnected: Future<void>.value(),
        ),
        throwsA(same(unrelated)));
  });

  test('successful write still requires a subsequent disconnect', () async {
    final disconnected = Completer<void>();
    var readyToReconnect = false;
    final reboot = sendOtaRebootAndWaitForDisconnect(
      sendReboot: () async {},
      disconnected: disconnected.future,
    ).then((_) => readyToReconnect = true);
    await Future<void>.delayed(Duration.zero);
    expect(readyToReconnect, isFalse);
    disconnected.complete();
    await reboot;
    expect(readyToReconnect, isTrue);
  });
}
