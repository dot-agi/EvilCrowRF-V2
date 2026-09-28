import 'dart:io';

import 'package:flutter/services.dart';

/// A persistent integration folder exported with an explicit folder grant.
class RadioIntegrationBundle {
  const RadioIntegrationBundle(this.directory, this.launcher, this.entry);

  final Directory directory;
  final File launcher;
  final File entry;
}

class RadioIntegrationService {
  static const _native = MethodChannel('evilcrow/radio-apps');

  /// Copy only regular files named by the helper into a new output folder.
  /// The helper uses an internal staging directory; external apps need a
  /// durable folder selected by the user, outside that temporary workspace.
  Future<RadioIntegrationBundle> exportBundle({
    required Directory staging,
    required Directory destination,
    required Map<String, dynamic> manifest,
  }) async {
    final files = (manifest['files'] as List).cast<String>();
    final launcher = manifest['launch_file'] as String;
    final entry = manifest['entry_file'] as String;
    bool simpleName(String value) =>
        value.isNotEmpty &&
        value != '.' &&
        value != '..' &&
        !value.contains('/') &&
        !value.contains('\\') &&
        !value.contains('\u0000');
    if (files.isEmpty ||
        files.toSet().length != files.length ||
        !files.every(simpleName) ||
        !files.contains(launcher) ||
        !files.contains(entry) ||
        !launcher.endsWith('.command')) {
      throw const FormatException('Invalid radio integration file manifest');
    }
    if (await destination.exists()) {
      throw FileSystemException(
          'Integration folder already exists', destination.path);
    }
    for (final name in files) {
      if (await FileSystemEntity.type('${staging.path}/$name',
              followLinks: false) !=
          FileSystemEntityType.file) {
        throw FileSystemException(
            'Integration source is not a regular file', name);
      }
    }
    await destination.create();
    try {
      for (final name in files) {
        // Export generated contents as new files under the user's folder grant.
        // File.copy also carries the helper's staging metadata on macOS.
        final output = File('${destination.path}/$name').openWrite();
        try {
          await output.addStream(File('${staging.path}/$name').openRead());
          await output.flush();
        } finally {
          await output.close();
        }
      }
      final launcherFile = File('${destination.path}/$launcher');
      if (Platform.isMacOS || Platform.isLinux) {
        final permissions = await Process.run(
            '/bin/chmod', ['755', launcherFile.absolute.path]);
        if (permissions.exitCode != 0) {
          throw FileSystemException(
              'Could not make the exported launcher executable: '
              '${permissions.stderr.toString().trim()}',
              launcherFile.path);
        }
      }
      return RadioIntegrationBundle(
          destination, launcherFile, File('${destination.path}/$entry'));
    } catch (_) {
      await destination.delete(recursive: true);
      rethrow;
    }
  }

  Future<void> launch(RadioIntegrationBundle bundle) async {
    if (!Platform.isMacOS) {
      throw UnsupportedError(
          'External radio application launch requires macOS');
    }
    await _native.invokeMethod<void>('openLauncher', bundle.launcher.path);
  }

  Future<void> reveal(RadioIntegrationBundle bundle) async {
    await _native.invokeMethod<void>('reveal', bundle.directory.path);
  }
}
