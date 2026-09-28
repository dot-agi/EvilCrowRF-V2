import 'dart:io';
import 'dart:typed_data';

import 'package:evilcrow_rf2_controller/services/radio_integration_service.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  late Directory root;
  late Directory staging;
  final service = RadioIntegrationService();
  final manifest = {
    'files': ['Launch URH.command', 'URHProject.xml'],
    'launch_file': 'Launch URH.command',
    'entry_file': 'URHProject.xml',
  };

  setUp(() async {
    root = await Directory.systemTemp.createTemp('radio-export-test-');
    staging = await Directory('${root.path}/stage').create();
    await File('${staging.path}/Launch URH.command')
        .writeAsString('#!/bin/sh\n');
    await File('${staging.path}/URHProject.xml').writeAsString('<project/>');
  });
  tearDown(() => root.delete(recursive: true));

  test('export survives deletion of temporary staging files', () async {
    final bundle = await service.exportBundle(
        staging: staging,
        destination: Directory('${root.path}/saved'),
        manifest: manifest);
    await staging.delete(recursive: true);
    expect(await bundle.launcher.readAsString(), '#!/bin/sh\n');
    expect(await bundle.entry.readAsString(), '<project/>');
  });

  test('exported launcher is executable before any application launch',
      () async {
    final bundle = await service.exportBundle(
        staging: staging,
        destination: Directory('${root.path}/saved'),
        manifest: manifest);
    expect((await bundle.launcher.stat()).mode & 0x1ff, 0x1ed); // 0755.
    expect((await bundle.entry.stat()).mode & 0x49, 0); // No execute bits.
  }, skip: !(Platform.isMacOS || Platform.isLinux));

  test('export preserves binary contents across multiple stream chunks',
      () async {
    final bytes = Uint8List.fromList(
        List<int>.generate(256 * 1024 + 7, (index) => index % 256));
    await File('${staging.path}/capture.bits').writeAsBytes(bytes);
    final bundle = await service.exportBundle(
        staging: staging,
        destination: Directory('${root.path}/saved'),
        manifest: {
          ...manifest,
          'files': [...manifest['files'] as List<String>, 'capture.bits'],
        });
    await staging.delete(recursive: true);
    expect(await File('${bundle.directory.path}/capture.bits').readAsBytes(),
        orderedEquals(bytes));
  });

  test('export creates fresh files without staging extended attributes',
      () async {
    const attribute = 'com.evilcrow.export-test';
    final source = '${staging.path}/Launch URH.command';
    final tagged = await Process.run(
        '/usr/bin/xattr', ['-w', attribute, 'temporary-staging', source]);
    expect(tagged.exitCode, 0, reason: tagged.stderr.toString());

    final bundle = await service.exportBundle(
        staging: staging,
        destination: Directory('${root.path}/saved'),
        manifest: manifest);
    final sourceAttributes = await Process.run('/usr/bin/xattr', [source]);
    final exportedAttributes =
        await Process.run('/usr/bin/xattr', [bundle.launcher.path]);
    expect(sourceAttributes.exitCode, 0);
    expect(sourceAttributes.stdout.toString(), contains(attribute));
    expect(exportedAttributes.exitCode, 0);
    expect(exportedAttributes.stdout.toString(), isNot(contains(attribute)));
    expect(await bundle.launcher.readAsString(), '#!/bin/sh\n');
  }, skip: !Platform.isMacOS);

  test('refuse path traversal and preserve existing output', () async {
    final saved = await Directory('${root.path}/saved').create();
    await expectLater(
        service.exportBundle(
            staging: staging, destination: saved, manifest: manifest),
        throwsA(isA<FileSystemException>()));
    await expectLater(
        service.exportBundle(
            staging: staging,
            destination: Directory('${root.path}/new'),
            manifest: {
              ...manifest,
              'files': ['../escape.command', 'URHProject.xml'],
              'launch_file': '../escape.command',
            }),
        throwsFormatException);
    expect(await Directory('${root.path}/new').exists(), isFalse);
  });

  test('refuse symlinks and incomplete exports before creating output',
      () async {
    await File('${staging.path}/URHProject.xml').delete();
    await Link('${staging.path}/URHProject.xml')
        .create('${staging.path}/Launch URH.command');
    final destination = Directory('${root.path}/saved');
    await expectLater(
        service.exportBundle(
            staging: staging, destination: destination, manifest: manifest),
        throwsA(isA<FileSystemException>()));
    expect(await destination.exists(), isFalse);
  });
}
