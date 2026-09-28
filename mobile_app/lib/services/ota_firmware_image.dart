import 'dart:typed_data';

import 'package:crypto/crypto.dart' as crypto;

/// Validated, immutable ESP32 application bytes, excluding merged flash images.
class OtaFirmwareImage {
  OtaFirmwareImage._(this.bytes, this.sha256, this.md5);

  static const maximumSize = 0x1d0000; // EvilCrow app0/app1 partition size.
  final Uint8List bytes;
  final String sha256;
  final String md5;

  factory OtaFirmwareImage.parse(Uint8List input) {
    final bytes = Uint8List.fromList(input);
    if (bytes.length < 288 || bytes.length > maximumSize) {
      throw const FormatException(
          'Firmware must fit the 1,856 KiB OTA partition.');
    }
    final data = ByteData.sublistView(bytes);
    if (bytes[0] != 0xe9 ||
        data.getUint16(12, Endian.little) != 0 ||
        bytes[1] == 0 ||
        bytes[1] > 16 ||
        data.getUint32(32, Endian.little) != 0xabcd5432) {
      throw const FormatException(
          'Choose an ESP32 application firmware.bin, not a bootloader or merged image.');
    }
    if (bytes[23] != 1) {
      throw const FormatException('Application SHA-256 digest is missing.');
    }
    var offset = 24;
    var checksum = 0xef;
    for (var segment = 0; segment < bytes[1]; segment++) {
      if (offset + 8 > bytes.length) {
        throw const FormatException('Truncated firmware segment header.');
      }
      final length = data.getUint32(offset + 4, Endian.little);
      offset += 8;
      if (length > bytes.length - offset || (segment == 0 && length < 256)) {
        throw const FormatException('Truncated application segment.');
      }
      for (var i = offset; i < offset + length; i++) {
        checksum ^= bytes[i];
      }
      offset += length;
    }
    // ESP images pad the checksum to the last byte of a 16-byte block.
    final checksumOffset = offset + 15 - (offset % 16);
    final digestOffset = checksumOffset + 1;
    if (digestOffset + 32 != bytes.length ||
        bytes[checksumOffset] != checksum) {
      throw const FormatException(
          'Invalid firmware length or segment checksum.');
    }
    final calculated =
        crypto.sha256.convert(bytes.sublist(0, digestOffset)).bytes;
    for (var i = 0; i < calculated.length; i++) {
      if (calculated[i] != bytes[digestOffset + i]) {
        throw const FormatException(
            'Application SHA-256 digest does not match.');
      }
    }
    return OtaFirmwareImage._(
        bytes.asUnmodifiableView(),
        crypto.sha256.convert(bytes).toString(),
        crypto.md5.convert(bytes).toString());
  }
}
