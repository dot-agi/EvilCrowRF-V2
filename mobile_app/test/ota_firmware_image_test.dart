import 'dart:io';
import 'dart:typed_data';

import 'package:evilcrow_rf2_controller/services/ota_firmware_image.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  final fixture = File('test/fixtures/ota_valid_esp32.bin').readAsBytesSync();
  test('accepts an esptool-generated ESP32 image and binds immutable bytes',
      () {
    final source = Uint8List.fromList(fixture);
    final image = OtaFirmwareImage.parse(source);
    expect(image.sha256,
        '3c00ee3b00adec6d0c88b0663ac9adeb02154a41425cbcac7a7d68baf6ab1c86');
    source[100] ^= 1;
    expect(image.bytes, fixture);
    expect(() => image.bytes[100] = 0, throwsUnsupportedError);
  });
  test('rejects corrupt segments, digest, chip, descriptor, and truncation',
      () {
    for (final offset in [0, 12, 23, 32, 100, fixture.length - 1]) {
      final corrupted = Uint8List.fromList(fixture);
      corrupted[offset] ^= 1;
      expect(() => OtaFirmwareImage.parse(corrupted), throwsFormatException,
          reason: 'byte $offset must be validated');
    }
    expect(() => OtaFirmwareImage.parse(Uint8List.sublistView(fixture, 0, 300)),
        throwsFormatException);
  });
  test('rejects merged/padded images and oversized application files', () {
    expect(() => OtaFirmwareImage.parse(Uint8List.fromList([...fixture, 0xff])),
        throwsFormatException);
    expect(
        () =>
            OtaFirmwareImage.parse(Uint8List(OtaFirmwareImage.maximumSize + 1)),
        throwsFormatException);
  });
}
