import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:evilcrow_rf2_controller/services/usb_backend_service.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

class _Process implements Process {
  final output = StreamController<List<int>>();
  final errors = StreamController<List<int>>();
  final input = StreamController<List<int>>();
  final exit = Completer<int>();
  final signals = <ProcessSignal>[];

  @override
  int get pid => 42;
  @override
  Stream<List<int>> get stdout => output.stream;
  @override
  Stream<List<int>> get stderr => errors.stream;
  @override
  late final IOSink stdin = IOSink(input.sink);
  @override
  Future<int> get exitCode => exit.future;
  @override
  bool kill([ProcessSignal signal = ProcessSignal.sigterm]) {
    signals.add(signal);
    return true;
  }

  void write(String value) => output.add(utf8.encode(value));
  Future<void> finish([int code = 0]) async {
    exit.complete(code);
    await Future.wait([output.close(), errors.close()]);
  }
}

Future<void> _tick() => Future<void>.delayed(Duration.zero);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final guards = <bool>[];

  setUp(() {
    guards.clear();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(const MethodChannel('evilcrow/usb'),
            (call) async {
      expect(call.method, 'setCriticalOperation');
      guards.add(call.arguments as bool);
      return null;
    });
  });

  test('reserve UART ownership during process startup and queue Stop',
      () async {
    final startup = Completer<Process>();
    final child = _Process();
    final calls = <List<String>>[];
    final service = UsbBackendService(
        executable: '/test/helper',
        startProcess: (_, args) {
          calls.add(args);
          return startup.future;
        });
    final run = service.run('rx', port: '/dev/test');
    await _tick();
    expect(service.busy, isTrue);
    await expectLater(service.run('status'), throwsStateError);
    final stopped = service.stop();
    expect(service.busy, isTrue);
    startup.complete(child);
    await _tick();
    expect(child.signals, [ProcessSignal.sigint]);
    expect(calls.single,
        ['--backend', '--port', '/dev/test', '--watch-stdin', 'rx']);
    child.write('{"event":"result","ok":true,"cancelled":true}\n');
    await child.finish(130);
    expect(await stopped, isTrue);
    expect((await run)['cancelled'], isTrue);
    expect(service.busy, isFalse);
    service.dispose();
  });

  test('parse fragmented JSONL and retain ownership until both streams finish',
      () async {
    final child = _Process();
    final service = UsbBackendService(
        executable: '/test/helper', startProcess: (_, __) async => child);
    final run = service.run('flash-preview');
    await _tick();
    child.write('{"event":"plan","plan":{"sha256":"abc"}}\n{"event":"lo');
    child.write('g","message":"ready"}\n{"event":"result","ok":true}');
    child.exit.complete(0);
    await _tick();
    expect(service.busy, isTrue);
    await expectLater(service.run('status'), throwsStateError);
    await Future.wait([child.output.close(), child.errors.close()]);
    expect((await run)['ok'], isTrue);
    expect(service.plan, {'sha256': 'abc'});
    expect(service.lines, contains('ready'));
    expect(service.busy, isFalse);
    service.dispose();
  });

  test('report nonzero exit without a protocol result', () async {
    final child = _Process();
    final service = UsbBackendService(
        executable: '/test/helper', startProcess: (_, __) async => child);
    final run = service.run('status');
    final failure = expectLater(
        run,
        throwsA(isA<StateError>()
            .having((error) => error.message, 'message', contains('code 7'))));
    await _tick();
    child.errors.add(utf8.encode('device disappeared\n'));
    await child.finish(7);
    await failure;
    expect(service.lines, contains('device disappeared'));
    expect(service.busy, isFalse);
    service.dispose();
  });

  test('dispose requests passive RX cleanup without releasing the child early',
      () async {
    final child = _Process();
    final service = UsbBackendService(
        executable: '/test/helper', startProcess: (_, __) async => child);
    final run = service.run('gnuradio');
    await _tick();
    service.dispose();
    expect(child.signals, [ProcessSignal.sigint]);
    expect(service.busy, isTrue);
    child.write('{"event":"result","ok":true,"cancelled":true}\n');
    await child.finish(130);
    await run;
    await service.finished;
    expect(service.busy, isFalse);
  });

  test('firmware write disables Stop and retains native quit guard until exit',
      () async {
    final child = _Process();
    final service = UsbBackendService(
        executable: '/test/helper', startProcess: (_, __) async => child);
    final run = service.run('flash');
    await _tick();
    expect(service.critical, isTrue);
    expect(service.canStop, isFalse);
    expect(await service.stop(), isFalse);
    expect(guards, Platform.isMacOS ? [true] : isEmpty);
    service.dispose();
    expect(child.signals, isEmpty);
    expect(service.busy, isTrue);
    child.write('{"event":"result","ok":true}\n');
    await child.finish();
    await run;
    expect(guards, Platform.isMacOS ? [true, false] : isEmpty);
    expect(service.busy, isFalse);
  });

  test('failed process startup clears the critical quit guard', () async {
    final service = UsbBackendService(
        executable: '/test/helper',
        startProcess: (_, __) async =>
            throw const ProcessException('helper', [], 'missing'));
    await expectLater(service.run('backup'), throwsA(isA<ProcessException>()));
    expect(guards, Platform.isMacOS ? [true, false] : isEmpty);
    expect(service.busy, isFalse);
    service.dispose();
  });
}
