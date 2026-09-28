import 'dart:io';

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
