import 'dart:async';
import 'dart:typed_data';

import 'package:evilcrow_rf2_controller/providers/ble_provider.dart';
import 'package:evilcrow_rf2_controller/screens/nrf_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

class FakeNrfProvider extends ChangeNotifier implements BleProvider {
  @override
  bool isConnected = true;
  @override
  bool nrfInitialized = true;
  @override
  bool nrfScanning = false;
  @override
  bool nrfAttacking = false;
  @override
  bool nrfSpectrumRunning = false;
  @override
  bool nrfJammerRunning = false;
  @override
  List<Map<String, dynamic>> nrfTargets = [];
  @override
  List<int> nrfSpectrumLevels = List.filled(126, 0);
  @override
  Map<int, Map<String, dynamic>> nrfJamModeConfigs = {};
  @override
  Map<int, Map<String, dynamic>> nrfJamModeInfos = {};

  final commands = <int>[];
  Completer<void>? nextWrite;

  @override
  Future<void> sendBinaryCommand(Uint8List command,
      {bool withoutResponse = false}) async {
    commands.add(command[7]);
    final pending = nextWrite;
    nextWrite = null;
    if (pending != null) await pending.future;
  }

  @override
  void nrfNotify() => notifyListeners();

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

Widget screen(FakeNrfProvider ble) => ChangeNotifierProvider<BleProvider>.value(
      value: ble,
      child: const MaterialApp(home: Scaffold(body: NrfScreen())),
    );

void main() {
  late FakeNrfProvider ble;

  setUp(() => ble = FakeNrfProvider());
  tearDown(() => ble.dispose());

  testWidgets('leaving NRF sends stop after its context is deactivated',
      (tester) async {
    await tester.pumpWidget(screen(ble));
    await tester.tap(find.text('Start Analyzer'));
    await tester.pump();
    expect(ble.commands, [0x28]);
    expect(ble.nrfSpectrumRunning, isTrue);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump();
    expect(ble.commands, [0x28, 0x2f]);
    expect(tester.takeException(), isNull);
  });

  for (final failPending in [false, true]) {
    testWidgets('leaving during a spectrum write cleans up (failure=$failPending)',
        (tester) async {
      final pending = Completer<void>();
      ble.nextWrite = pending;
      await tester.pumpWidget(screen(ble));
      await tester.tap(find.text('Start Analyzer'));
      await tester.pump();
      await tester.pumpWidget(const SizedBox.shrink());
      expect(ble.commands, [0x28]);

      if (failPending) {
        pending.completeError(StateError('Disconnected during write'));
      } else {
        pending.complete();
      }
      await tester.pump();
      expect(ble.commands, [0x28, 0x2f]);
      expect(ble.nrfSpectrumRunning, isFalse);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('leaving during initialization does not update disposed UI',
      (tester) async {
    ble.nrfInitialized = false;
    final pending = Completer<void>();
    ble.nextWrite = pending;
    await tester.pumpWidget(screen(ble));
    await tester.tap(find.text('Initialize NRF24'));
    await tester.pump();
    await tester.pumpWidget(const SizedBox.shrink());
    pending.complete();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));
    expect(ble.commands, [0x20, 0x2f]);
    expect(ble.nrfInitialized, isFalse);
    expect(tester.takeException(), isNull);
  });

  testWidgets('leaving while disconnected does not send a stop', (tester) async {
    ble.isConnected = false;
    await tester.pumpWidget(screen(ble));
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump();
    expect(ble.commands, isEmpty);
    expect(tester.takeException(), isNull);
  });
}
