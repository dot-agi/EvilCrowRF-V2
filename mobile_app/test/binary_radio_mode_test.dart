import 'dart:typed_data';

import 'package:evilcrow_rf2_controller/services/binary_message_parser.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('mode notifications identify ProtoPirate when starting and stopping', () {
    final started = BinaryMessageParser.parseBinaryMessage(
      Uint8List.fromList([0x80, 0, 6, 0]),
    );
    final stopped = BinaryMessageParser.parseBinaryMessage(
      Uint8List.fromList([0x80, 0, 0, 6]),
    );

    expect(started, {
      'type': 'ModeSwitch',
      'data': {'module': '0', 'mode': 'ProtoPirate', 'previousMode': 'Idle'},
    });
    expect(stopped, {
      'type': 'ModeSwitch',
      'data': {'module': '0', 'mode': 'Idle', 'previousMode': 'ProtoPirate'},
    });
  });

  for (final length in [102, 108]) {
    test('status packet ($length bytes) identifies ProtoPirate on either radio',
        () {
      // Firmware status: type, two radio modes, register count, device telemetry,
      // then 47 register values for each CC1101. Mode 6 is ProtoPirate RX.
      final packet = Uint8List(length)
        ..setRange(0, 4, [0x81, 6, 6, 47]);
      final parsed = BinaryMessageParser.parseBinaryMessage(packet)!;
      final modules = parsed['data']['cc1101'] as List;

      expect(parsed['type'], 'State');
      expect(modules.map((m) => m['id']), [0, 1]);
      expect(modules.map((m) => m['mode']), ['ProtoPirate', 'ProtoPirate']);
    });
  }
}
