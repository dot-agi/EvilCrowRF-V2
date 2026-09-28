import 'dart:io';

import 'package:crypto/crypto.dart';

/// Check the full 4 MiB recovery image before selecting or exporting it.
Future<String> verifyUsbBackup(File backup) async {
  if (await backup.length() != 4 * 1024 * 1024) {
    throw const FormatException(
        'The recovery backup must be a full 4 MiB flash image');
  }
  final sidecar = await File('${backup.path}.sha256').readAsString();
  final expected = sidecar.trim().split(RegExp(r'\s+')).first.toLowerCase();
  if (!RegExp(r'^[0-9a-f]{64}$').hasMatch(expected)) {
    throw const FormatException('The backup SHA-256 sidecar is invalid');
  }
  final digest = (await sha256.bind(backup.openRead()).first).toString();
  if (digest != expected) {
    throw const FormatException(
        'The backup does not match its SHA-256 sidecar');
  }
  return digest;
}

Future<void> exportUsbBackup(File source, File destination) async {
  final digest = await verifyUsbBackup(source);
  await source.copy(destination.path);
  // Keep the standard checksum filename aligned with the exported name.
  await File('${destination.path}.sha256').writeAsString(
    '$digest  ${destination.uri.pathSegments.last}\n',
    flush: true,
  );
}
