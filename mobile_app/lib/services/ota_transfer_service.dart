import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';

import '../providers/firmware_protocol.dart';
import 'ota_firmware_image.dart';

enum OtaStage {
  idle,
  starting,
  transferring,
  cancelling,
  verifying,
  rebooting,
  complete,
  failed,
  cancelled
}

class OtaEvent {
  const OtaEvent.progress(this.received, this.total)
      : kind = 'progress',
        message = null;
  const OtaEvent.complete()
      : kind = 'complete',
        received = 0,
        total = 0,
        message = null;
  const OtaEvent.error(this.message)
      : kind = 'error',
        received = 0,
        total = 0;
  const OtaEvent.disconnected()
      : kind = 'disconnected',
        received = 0,
        total = 0,
        message = null;
  const OtaEvent.commandSuccess()
      : kind = 'success',
        received = 0,
        total = 0,
        message = null;
  final String kind;
  final int received;
  final int total;
  final String? message;
}

class OtaResult {
  const OtaResult(
      {required this.version, required this.sha256, required this.bytes});
  final String version;
  final String sha256;
  final int bytes;
}

class OtaCancelled implements Exception {
  @override
  String toString() => 'OTA transfer cancelled.';
}

/// Transfers one chunk at a time. The firmware has no offset/sequence field,
/// so an unacknowledged data chunk must never be retransmitted blindly.
class OtaTransferService extends ChangeNotifier {
  OtaTransferService(
      {required this.events,
      required this.write,
      required this.maximumPacketBytes,
      required this.rebootAndReconnect,
      required this.setQuitGuard,
      this.ackTimeout = const Duration(seconds: 10)});

  final Stream<OtaEvent> events;
  final Future<void> Function(Uint8List command) write;
  final int Function() maximumPacketBytes;
  final Future<String> Function() rebootAndReconnect;
  final Future<void> Function(bool enabled) setQuitGuard;
  final Duration ackTimeout;

  bool busy = false;
  OtaStage stage = OtaStage.idle;
  int acknowledgedBytes = 0;
  int totalBytes = 0;
  int chunkSize = 0;
  String status = 'Ready';
  bool _cancelRequested = false;
  bool _disposed = false;
  Object? _fatalError;
  Completer<OtaEvent>? _pending;
  bool Function(OtaEvent)? _accept;
  bool get canCancel =>
      busy && (stage == OtaStage.starting || stage == OtaStage.transferring);
  double get progress => totalBytes == 0 ? 0 : acknowledgedBytes / totalBytes;

  void _changed() {
    if (!_disposed) notifyListeners();
  }

  void requestCancel() {
    if (!canCancel) return;
    _cancelRequested = true;
    status = 'Cancelling after the current write...';
    _changed();
  }

  void _receive(OtaEvent event) {
    if (event.kind == 'error' ||
        (event.kind == 'disconnected' && stage != OtaStage.rebooting)) {
      _fatalError =
          StateError(event.message ?? 'Bluetooth disconnected during OTA.');
      final pending = _pending;
      if (pending != null && !pending.isCompleted) {
        pending.completeError(_fatalError!);
      }
      return;
    }
    final pending = _pending;
    if (pending != null &&
        !pending.isCompleted &&
        (_accept?.call(event) ?? false)) {
      pending.complete(event);
    }
  }

  Future<OtaEvent> _command(Uint8List command, bool Function(OtaEvent) accept,
      {bool ignorePreviousError = false}) async {
    if (_fatalError != null && !ignorePreviousError) throw _fatalError!;
    final pending = Completer<OtaEvent>();
    pending.future
        .ignore(); // A firmware error can arrive before the write completes.
    _pending = pending;
    _accept = accept;
    try {
      await write(command);
      return await pending.future.timeout(ackTimeout,
          onTimeout: () =>
              throw TimeoutException('OTA acknowledgement timed out.'));
    } finally {
      if (identical(_pending, pending)) {
        _pending = null;
        _accept = null;
      }
    }
  }

  Future<void> _progressCommand(Uint8List command, int expectedBytes) async {
    bool progress(OtaEvent event) =>
        event.kind == 'progress' &&
        (event.received >= expectedBytes || event.total != totalBytes);
    OtaEvent ack;
    try {
      ack = await _command(command, progress);
    } on TimeoutException {
      // Query the committed offset; never resend non-idempotent DATA.
      ack = await _command(FirmwareBinaryProtocol.createOtaStatusCommand(),
          (event) => event.kind == 'progress');
    }
    if (ack.total != totalBytes || ack.received != expectedBytes) {
      throw StateError('Unexpected OTA acknowledgement: '
          '${ack.received}/${ack.total}, expected $expectedBytes/$totalBytes.');
    }
    acknowledgedBytes = ack.received;
    _changed();
  }

  Future<OtaResult> install(OtaFirmwareImage image,
      {required String expectedVersion}) async {
    if (busy || _disposed) {
      throw StateError('An OTA update is already active or closed.');
    }
    if (expectedVersion.trim().isEmpty) {
      throw ArgumentError('Enter the expected firmware version after restart.');
    }
    final packetLimit = math.min(512, maximumPacketBytes());
    if (packetLimit < 45) {
      throw StateError(
          'Negotiated Bluetooth MTU is too small for verified OTA. Reconnect and retry.');
    }
    busy = true;
    stage = OtaStage.starting;
    status = 'Starting verified OTA...';
    totalBytes = image.bytes.length;
    acknowledgedBytes = 0;
    chunkSize = math.min(500, packetLimit - 9);
    _cancelRequested = false;
    _fatalError = null;
    _changed();
    var began = false;
    var finalizing = false;
    var guarded = false;
    final subscription = events.listen(_receive);
    try {
      await setQuitGuard(true);
      guarded = true;
      if (_cancelRequested) throw OtaCancelled();
      began = true;
      await _progressCommand(
          FirmwareBinaryProtocol.createOtaBeginCommand(totalBytes, image.md5),
          0);
      stage = OtaStage.transferring;
      while (acknowledgedBytes < totalBytes) {
        if (_cancelRequested) throw OtaCancelled();
        if (_fatalError != null) throw _fatalError!;
        final end = math.min(acknowledgedBytes + chunkSize, totalBytes);
        status = 'Device received $acknowledgedBytes / $totalBytes bytes';
        _changed();
        await _progressCommand(
            FirmwareBinaryProtocol.createOtaDataCommand(
                Uint8List.sublistView(image.bytes, acknowledgedBytes, end)),
            end);
      }
      if (_cancelRequested) throw OtaCancelled();
      finalizing = true;
      stage = OtaStage.verifying;
      status = 'Waiting for the device to verify the firmware MD5...';
      _changed();
      await _command(FirmwareBinaryProtocol.createOtaEndCommand(),
          (event) => event.kind == 'complete');
      stage = OtaStage.rebooting;
      status =
          'Firmware verified. Restarting and checking the installed version...';
      _changed();
      final version = await rebootAndReconnect();
      if (version.replaceFirst(RegExp(r'^v'), '') !=
          expectedVersion.trim().replaceFirst(RegExp(r'^v'), '')) {
        throw StateError(
            'Firmware was written, but restart reported version $version; '
            'expected $expectedVersion. Installation is not verified.');
      }
      stage = OtaStage.complete;
      status = 'Firmware $version verified after restart.';
      return OtaResult(
          version: version, sha256: image.sha256, bytes: totalBytes);
    } catch (error) {
      var cleanup = '';
      if (began && !finalizing) {
        stage = OtaStage.cancelling;
        status = 'Stopping the incomplete OTA transfer...';
        _changed();
        try {
          await _command(FirmwareBinaryProtocol.createOtaAbortCommand(),
              (event) => event.kind == 'success',
              ignorePreviousError: true);
          cleanup =
              ' Device acknowledged abort. Restart it before another update.';
        } catch (_) {
          cleanup =
              ' Abort was not acknowledged. Restart the device before another update.';
        }
      } else if (finalizing) {
        cleanup =
            ' Firmware may already be selected for boot; reconnect and verify its version.';
      }
      stage = error is OtaCancelled ? OtaStage.cancelled : OtaStage.failed;
      status = '$error$cleanup';
      rethrow;
    } finally {
      await subscription.cancel();
      try {
        if (guarded) await setQuitGuard(false);
      } catch (error) {
        stage = OtaStage.failed;
        status =
            '$status Window-close protection could not be released: $error';
        rethrow;
      } finally {
        busy = false;
        _changed();
      }
    }
  }

  @override
  void dispose() {
    _disposed = true;
    requestCancel();
    super.dispose();
  }
}
