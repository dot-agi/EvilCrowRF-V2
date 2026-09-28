import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:evilcrow_rf2_controller/services/usb_backup_files.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  late Directory workspace;
  setUp(() async {
    workspace = await Directory.systemTemp.createTemp('usb-backup-test-');
  });
  tearDown(() async {
    await workspace.delete(recursive: true);
  });

  test('exported checksum names the renamed recovery image and still verifies',
      () async {
    final bytes = Uint8List(4 * 1024 * 1024)..[0] = 0xe9;
    final digest = sha256.convert(bytes).toString();
    final source =
        await File('${workspace.path}/temporary.bin').writeAsBytes(bytes);
    await File('${source.path}.sha256')
        .writeAsString('$digest  temporary.bin\n');
    final destination = File('${workspace.path}/saved-backup.bin');

    await exportUsbBackup(source, destination);

    expect(await File('${destination.path}.sha256').readAsString(),
        '$digest  saved-backup.bin\n');
    expect(await verifyUsbBackup(destination), digest);
  });

  test('a changed recovery image cannot be exported as verified', () async {
    final source = await File('${workspace.path}/bad.bin')
        .writeAsBytes(Uint8List(4 * 1024 * 1024));
    await File('${source.path}.sha256').writeAsString('${'0' * 64}  bad.bin\n');
    final destination = File('${workspace.path}/saved.bin');

    await expectLater(
        exportUsbBackup(source, destination), throwsFormatException);
    expect(await destination.exists(), isFalse);
  });
}
