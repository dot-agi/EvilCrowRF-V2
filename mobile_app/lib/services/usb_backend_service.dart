import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

typedef UsbProcessStarter = Future<Process> Function(
    String executable, List<String> arguments);

/// Owns one headless USB helper until its process and output streams exit.
class UsbBackendService extends ChangeNotifier {
  UsbBackendService({String? executable, UsbProcessStarter? startProcess})
      : _executable = executable,
        _startProcess = startProcess ??
            ((executable, arguments) => Process.start(executable, arguments,
                environment: {'PYTHONUNBUFFERED': '1'}));

  static const _native = MethodChannel('evilcrow/usb');
  final String? _executable;
  final UsbProcessStarter _startProcess;
  final List<String> _lines = [];
  Process? _process;
  Future<void>? _done;
  Completer<Map<String, dynamic>>? _ready;
  bool _disposed = false;
  bool _cancelRequested = false;
  bool _cancellable = false;

  bool busy = false;
  bool critical = false;
  String status = 'Ready';
  Map<String, dynamic>? plan;
  Map<String, dynamic>? result;
  List<String> get lines => List.unmodifiable(_lines);
  bool get canStop => busy && _cancellable && !critical;
  bool get stopRequested => _cancelRequested;
  Future<void> get finished => _done ?? Future<void>.value();
  Future<Map<String, dynamic>> get whenReady =>
      _ready?.future ??
      Future.error(StateError('No external radio bridge is starting'));

  void _changed() {
    if (!_disposed) notifyListeners();
  }

  void _log(String line) {
    _lines.add(line);
    if (_lines.length > 500) _lines.removeRange(0, _lines.length - 500);
    _changed();
  }

  Future<String> _helper() async {
    final override = _executable;
    if (override != null) return override;
    final contents = File(Platform.resolvedExecutable).parent.parent.path;
    final path =
        '$contents/Helpers/EvilCrow SDR.app/Contents/MacOS/EvilCrow SDR';
    if (!await File(path).exists()) {
      throw StateError('USB backend is not bundled. Build this app with '
          'mobile_app/scripts/build_macos.sh.');
    }
    return path;
  }

  Future<void> _setQuitGuard(bool enabled) async {
    if (!Platform.isMacOS) return;
    try {
      await _native.invokeMethod<void>('setCriticalOperation', enabled);
    } on MissingPluginException {
      if (enabled && _executable == null) {
        throw StateError(
            'USB quit protection is unavailable; rebuild the app.');
      }
    }
  }

  void _receiveLine(String line) {
    try {
      final event = jsonDecode(line) as Map<String, dynamic>;
      switch (event['event']) {
        case 'ready':
          if (_ready != null && !_ready!.isCompleted) {
            final port = event['tcp_port'];
            if (event['host'] != '127.0.0.1' ||
                port is! int ||
                port < 1 ||
                port > 65535) {
              _ready!.completeError(
                  const FormatException('Invalid local bridge endpoint'));
            } else {
              _ready!.complete(event);
            }
          }
          _log('Receive bridge ready on ${event['host']}:${event['tcp_port']}');
          break;
        case 'plan':
          plan = Map<String, dynamic>.from(event['plan'] as Map);
          _log('Firmware plan ready');
          break;
        case 'result':
          result = event;
          _log(jsonEncode(event));
          break;
        case 'error':
          _log('Error: ${event['message']}');
          result = event;
          break;
        default:
          _log(event['message']?.toString() ?? line);
      }
    } catch (_) {
      _log(line);
    }
  }

  Future<Map<String, dynamic>> run(String operation,
      {String? port,
      List<String> arguments = const [],
      bool critical = false}) async {
    if (_disposed) throw StateError('USB tools have closed');
    if (busy) throw StateError('Wait for the current USB operation to finish');
    critical = critical ||
        const {'flash', 'backup', 'bootloader', 'reboot'}.contains(operation);
    _cancellable = const {'rx', 'urh', 'gnuradio'}.contains(operation);
    busy = true; // Reserve ownership before Process.start can yield.
    this.critical = critical;
    _cancelRequested = false;
    plan = null;
    result = null;
    _ready = operation == 'urh' ? Completer<Map<String, dynamic>>() : null;
    _ready?.future.ignore();
    _lines.clear();
    status = 'Running $operation';
    final completion = Completer<void>();
    _done = completion.future;
    _changed();
    try {
      if (critical) await _setQuitGuard(true);
      final executable = await _helper();
      final args = [
        '--backend',
        if (port != null && port.isNotEmpty) ...['--port', port],
        '--watch-stdin',
        operation,
        ...arguments
      ];
      final process = await _startProcess(executable, args);
      _process = process;
      if (_cancelRequested && !critical) process.kill(ProcessSignal.sigint);
      final stdout = process.stdout
          .transform(utf8.decoder)
          .transform(const LineSplitter())
          .forEach(_receiveLine);
      final stderr = process.stderr
          .transform(utf8.decoder)
          .transform(const LineSplitter())
          .forEach(_log);
      final output = Future.wait([stdout, stderr]);
      output.ignore();
      final exitCode = await process.exitCode;
      await output;
      if (exitCode == 130 || result?['cancelled'] == true) {
        status = 'Stopped';
        return result ?? {'ok': true, 'cancelled': true};
      }
      if (exitCode != 0 || result?['ok'] != true) {
        throw StateError(result?['message']?.toString() ??
            'USB backend exited with code $exitCode without a successful result');
      }
      status = 'Completed $operation';
      return result!;
    } catch (error) {
      status = 'Failed: $error';
      _log(status);
      rethrow;
    } finally {
      if (_ready != null && !_ready!.isCompleted) {
        _ready!.completeError(
            StateError('Bridge exited before it was ready: $status'));
      }
      _process = null;
      if (critical) {
        try {
          await _setQuitGuard(false);
        } catch (error) {
          _log('Could not clear the native quit guard: $error');
        }
      }
      busy = false;
      this.critical = false;
      completion.complete();
      _changed();
    }
  }

  /// Keep ownership if cleanup takes longer than this wait; no second process
  /// may start until the original child really exits.
  Future<bool> stop() async {
    if (!busy) return true;
    if (!canStop) return false;
    _cancelRequested = true;
    status = 'Stopping; waiting for device cleanup';
    _changed();
    _process?.kill(ProcessSignal.sigint);
    try {
      await _done?.timeout(const Duration(seconds: 10));
      return true;
    } on TimeoutException {
      _log('Cleanup is still running. Keep the device connected.');
      return false;
    }
  }

  @override
  void dispose() {
    _disposed = true;
    if (canStop) {
      _cancelRequested = true;
      _process?.kill(ProcessSignal.sigint);
    }
    super.dispose();
  }
}
