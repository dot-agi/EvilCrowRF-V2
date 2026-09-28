import 'dart:async';
import 'dart:typed_data';

import 'package:evilcrow_rf2_controller/l10n/app_localizations.dart';
import 'package:evilcrow_rf2_controller/models/detected_signal.dart';
import 'package:evilcrow_rf2_controller/providers/ble_provider.dart';
import 'package:evilcrow_rf2_controller/providers/notification_provider.dart';
import 'package:evilcrow_rf2_controller/screens/signal_scanner_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

class FakeBleProvider extends ChangeNotifier implements BleProvider {
  @override
  bool isConnected = true;
  @override
  List<DetectedSignal> detectedSignals = [];
  final commands = <List<int>>[];
  Completer<void>? nextWrite;
  bool failSecondModule = false;
  int stateRequests = 0;
  int staleStateReplies = 0;

  @override
  Future<void> sendGetStateCommand() async => stateRequests++;

  @override
  String getModuleStatus(int module) =>
      stateRequests <= staleStateReplies ? 'DetectSignal' : 'Idle';

  @override
  Future<void> sendBinaryCommand(Uint8List command,
      {bool withoutResponse = false}) async {
    commands.add([command[7], command[8]]); // Wire command type and module.
    final pending = nextWrite;
    nextWrite = null;
    if (pending != null) await pending.future;
    if (failSecondModule && command[7] == 0x02 && command[8] == 1) {
      throw StateError('Simulated BLE write failure');
    }
  }

  void setConnected(bool connected) {
    isConnected = connected;
    notifyListeners();
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class TestNotifications extends NotificationProvider {
  final errors = <String>[];

  @override
  void showError(String message, {Duration? duration}) => errors.add(message);
}

Widget scanner(FakeBleProvider ble, TestNotifications notifications) {
  return MultiProvider(
    providers: [
      ChangeNotifierProvider<BleProvider>.value(value: ble),
      ChangeNotifierProvider<NotificationProvider>.value(value: notifications),
    ],
    child: MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: const SignalScannerScreen(),
    ),
  );
}

Future<void> settleStops(WidgetTester tester) async {
  for (var attempt = 0; attempt < 3; attempt++) {
    await tester.pump(const Duration(milliseconds: 300));
  }
}

void main() {
  late FakeBleProvider ble;
  late TestNotifications notifications;

  setUp(() {
    ble = FakeBleProvider();
    notifications = TestNotifications();
  });
  tearDown(() {
    ble.dispose();
    notifications.dispose();
  });

  testWidgets('scanning starts manually and sends idle when stopped', (tester) async {
    await tester.pumpWidget(scanner(ble, notifications));
    expect(ble.commands, isEmpty);
    expect(find.text('Scanning stopped'), findsOneWidget);
    await tester.tap(find.byTooltip('Start Scanner'));
    await tester.pump();
    expect(ble.commands, [[0x02, 0]]);
    expect(find.text('Scanning active'), findsOneWidget);
    await tester.tap(find.byTooltip('Stop Scanner'));
    await tester.pump();
    expect(ble.commands, [[0x02, 0], [0x03, 0]]);
    expect(find.text('Scanning stopped'), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
    await settleStops(tester);
    expect(tester.takeException(), isNull);
  });

  for (final failPending in [false, true]) {
    testWidgets('leaving during a pending scan cleans up safely (failure=$failPending)',
        (tester) async {
      final pending = Completer<void>();
      ble.nextWrite = pending;
      await tester.pumpWidget(scanner(ble, notifications));
      await tester.tap(find.text('1+2'));
      await tester.pump();
      await tester.tap(find.byTooltip('Start Scanner'));
      await tester.pump();
      expect(ble.commands, [[0x02, 0]]);

      await tester.pumpWidget(const SizedBox.shrink());
      expect(tester.takeException(), isNull);
      if (failPending) {
        pending.completeError(StateError('Disconnected during write'));
      } else {
        pending.complete();
      }
      await tester.pump();
      // The second radio must never start after leaving; both receive idle.
      expect(ble.commands, [[0x02, 0], [0x03, 0], [0x03, 1]]);
      await settleStops(tester);
      expect(notifications.errors, isEmpty);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('disconnect clears scanning and reconnect needs a manual start',
      (tester) async {
    await tester.pumpWidget(scanner(ble, notifications));
    await tester.tap(find.byTooltip('Start Scanner'));
    await tester.pump();
    ble.setConnected(false);
    await tester.pump();
    expect(find.text('Scanning stopped'), findsOneWidget);
    final start = tester.widget<IconButton>(find.byWidgetPredicate(
        (widget) => widget is IconButton && widget.tooltip == 'Start Scanner'));
    expect(start.onPressed, isNull);
    ble.setConnected(true);
    await tester.pump();
    expect(ble.commands, [[0x02, 0]]);
    expect(find.text('Scanning stopped'), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
    await settleStops(tester);
    expect(tester.takeException(), isNull);
  });

  testWidgets('a partial start failure idles both radios', (tester) async {
    ble.failSecondModule = true;
    await tester.pumpWidget(scanner(ble, notifications));
    await tester.tap(find.text('1+2'));
    await tester.pump();
    await tester.tap(find.byTooltip('Start Scanner'));
    await tester.pump();
    expect(ble.commands, [[0x02, 0], [0x02, 1], [0x03, 0], [0x03, 1]]);
    expect(find.text('Scanning stopped'), findsOneWidget);
    expect(notifications.errors, hasLength(1));
    await tester.pumpWidget(const SizedBox.shrink());
    await settleStops(tester);
    expect(tester.takeException(), isNull);
  });

  for (final staleReplies in [1, 100]) {
    testWidgets('stop refreshes stale radio state with bounded retries ($staleReplies)',
        (tester) async {
      ble.staleStateReplies = staleReplies;
      await tester.pumpWidget(scanner(ble, notifications));
      await tester.tap(find.byTooltip('Start Scanner'));
      await tester.pump();
      await tester.pumpWidget(const SizedBox.shrink());
      await settleStops(tester);
      expect(ble.stateRequests, staleReplies == 1 ? 2 : 3);
      expect(ble.commands, [[0x02, 0], [0x03, 0]]);
      expect(tester.takeException(), isNull);
    });
  }
}
