import 'dart:async';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../providers/ble_provider.dart';
import '../services/usb_backend_service.dart';
import '../services/usb_backup_files.dart';
import '../services/radio_integration_service.dart';

class UsbToolsScreen extends StatefulWidget {
  const UsbToolsScreen({super.key});

  @override
  State<UsbToolsScreen> createState() => _UsbToolsScreenState();
}

class _UsbToolsScreenState extends State<UsbToolsScreen> {
  final _backend = UsbBackendService();
  final _frequency = TextEditingController(text: '433.92');
  final _duration = TextEditingController(text: '5');
  final _tcpPort = TextEditingController(text: '1234');
  late final Future<Directory> _workspace = _createWorkspace();
  List<Map<String, dynamic>> _ports = [];
  String? _port;
  File? _firmware;
  File? _backup;
  File? _capture;
  String _firmwareLabel = '';
  String _backupLabel = '';
  bool _preparing = false;
  final _radioApps = RadioIntegrationService();
  RadioIntegrationBundle? _radioBundle;
  String _radioStatus = '';
  String? _urhExecutable;
  String? _gnuPython;

  bool get _busy => _preparing || _backend.busy;

  Future<Directory> _createWorkspace() async {
    final root = await getApplicationSupportDirectory();
    await root.create(recursive: true);
    return root.createTemp('usb-tools-');
  }

  @override
  void initState() {
    super.initState();
    _backend.addListener(_updated);
    unawaited(_loadRadioExecutables());
  }

  Future<void> _loadRadioExecutables() async {
    final preferences = await SharedPreferences.getInstance();
    if (!mounted) return;
    setState(() {
      _urhExecutable = preferences.getString('radio_urh_executable');
      _gnuPython = preferences.getString('radio_gnuradio_executable');
    });
  }

  Future<void> _chooseRadioExecutable(String target) async {
    final selection = await FilePicker.platform.pickFiles(
        dialogTitle: target == 'urh'
            ? 'Choose URH executable or Python with URH installed'
            : 'Choose Python with GNU Radio installed',
        type: FileType.any);
    final path = selection?.files.single.path;
    if (path == null || !mounted) return;
    final preferences = await SharedPreferences.getInstance();
    await preferences.setString('radio_${target}_executable', path);
    if (!mounted) return;
    setState(() {
      if (target == 'urh') {
        _urhExecutable = path;
      } else {
        _gnuPython = path;
      }
    });
  }

  Future<void> _resetRadioExecutables() async {
    final preferences = await SharedPreferences.getInstance();
    await preferences.remove('radio_urh_executable');
    await preferences.remove('radio_gnuradio_executable');
    if (mounted)
      setState(() {
        _urhExecutable = null;
        _gnuPython = null;
      });
  }

  void _updated() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    _backend.removeListener(_updated);
    _backend.dispose();
    unawaited(_backend.finished.then((_) async {
      try {
        final directory = await _workspace;
        await directory.delete(recursive: true);
      } catch (_) {}
    }));
    _frequency.dispose();
    _duration.dispose();
    _tcpPort.dispose();
    super.dispose();
  }

  void _message(Object message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(message.toString()),
        duration: const Duration(seconds: 6)));
  }

  Future<void> _action(Future<void> Function() operation) async {
    if (_busy) return;
    setState(() => _preparing = true);
    try {
      await operation();
    } catch (error) {
      _message(error);
    } finally {
      if (mounted) setState(() => _preparing = false);
    }
  }

  Future<Map<String, dynamic>> _run(String command,
      {List<String> arguments = const [], bool critical = false}) async {
    if (command != 'list-ports' &&
        command != 'flash-preview' &&
        command != 'integration-export') {
      final ble = context.read<BleProvider>();
      if (ble.isConnected) await ble.disconnect();
    }
    return _backend.run(command,
        port: _port, arguments: arguments, critical: critical);
  }

  Future<void> _listPorts() async {
    final result = await _run('list-ports');
    if (!mounted) return;
    setState(() {
      _ports = (result['ports'] as List)
          .map((p) => Map<String, dynamic>.from(p as Map))
          .toList();
      if (!_ports.any((p) => p['path'] == _port)) _port = null;
    });
  }

  List<String> _receiveArguments() {
    final mhz = double.tryParse(_frequency.text);
    final seconds = double.tryParse(_duration.text);
    if (mhz == null || !mhz.isFinite || mhz <= 0) {
      throw const FormatException('Enter a valid receive frequency in MHz');
    }
    if (seconds == null || !seconds.isFinite || seconds < 0.1 || seconds > 60) {
      throw const FormatException('Receive duration must be 0.1–60 seconds');
    }
    return ['--frequency', '${mhz * 1000000}', '--duration', '$seconds'];
  }

  Future<void> _captureBytes(String command) async {
    final arguments = _receiveArguments();
    final workspace = await _workspace;
    final extension = command == 'rx' ? 'bin' : 'complex64';
    final output = File(
        '${workspace.path}/$command-${DateTime.now().microsecondsSinceEpoch}.$extension');
    final result =
        await _run(command, arguments: [...arguments, '--output', output.path]);
    if (await output.exists() && mounted) setState(() => _capture = output);
    if (result['cancelled'] != true) {
      _message('Capture complete. Use Save capture to export it.');
    }
  }

  Future<void> _startBridge() async {
    final frequency = _receiveArguments()[1];
    final tcp = int.tryParse(_tcpPort.text);
    if (tcp == null || tcp < 1024 || tcp > 65535) {
      throw const FormatException('TCP port must be 1024–65535');
    }
    await _run('urh', arguments: [
      '--frequency',
      frequency,
      '--tcp-port',
      '$tcp',
      '--sample-format',
      'bits'
    ]);
  }

  Future<RadioIntegrationBundle?> _prepareRadioIntegration(
      String target) async {
    final frequency = _receiveArguments()[1];
    final tcp = int.tryParse(_tcpPort.text);
    if (tcp == null || tcp < 1024 || tcp > 65535) {
      throw const FormatException('TCP port must be 1024–65535');
    }
    final folder = await FilePicker.platform.getDirectoryPath(
        dialogTitle:
            'Save ${target == 'urh' ? 'URH project' : 'GNU Radio flowgraph'}');
    if (folder == null || !mounted) return null;
    final id = DateTime.now().microsecondsSinceEpoch;
    final staging = Directory('${(await _workspace).path}/$target-$id');
    final executable = target == 'urh' ? _urhExecutable : _gnuPython;
    final manifest = await _run('integration-export', arguments: [
      '--target',
      target,
      '--output',
      staging.path,
      '--frequency',
      frequency,
      '--tcp-port',
      '$tcp',
      if (executable != null) ...['--executable', executable],
      if (_capture != null && _capture!.path.endsWith('.bin')) ...[
        '--input',
        _capture!.path
      ],
    ]);
    final bundle = await _radioApps.exportBundle(
        staging: staging,
        destination: Directory('$folder/evilcrow-$target-$id'),
        manifest: manifest);
    if (mounted)
      setState(() {
        _radioBundle = bundle;
        _radioStatus = 'Saved ${bundle.directory.path}';
      });
    return bundle;
  }

  Future<void> _startRadioApplication(String target) async {
    final bundle = await _prepareRadioIntegration(target);
    if (bundle == null || !mounted) return;
    final ble = context.read<BleProvider>();
    if (ble.isConnected) await ble.disconnect();
    final run = _backend.run('urh', port: _port, arguments: [
      '--frequency',
      _receiveArguments()[1],
      '--tcp-port',
      _tcpPort.text,
      '--sample-format',
      'bits',
    ]);
    // Attach immediately so a failed bridge startup cannot become an unhandled
    // asynchronous error while waiting for its ready event.
    run.ignore();
    try {
      await _backend.whenReady.timeout(const Duration(seconds: 45));
      if (!_backend.busy || _backend.stopRequested) {
        throw StateError('The receive bridge has stopped');
      }
      await _radioApps.launch(bundle);
      if (mounted)
        setState(() {
          _radioStatus =
              'Launch requested. Keep this bridge running while receiving. '
              'Use Stop here when finished.';
        });
      await run;
    } catch (_) {
      await _backend.stop();
      try {
        await run;
      } catch (_) {}
      rethrow;
    }
  }

  Future<void> _pickFirmware() async {
    final selection = await FilePicker.platform.pickFiles(
        dialogTitle: 'Choose ESP32 application firmware.bin',
        type: FileType.any);
    final path = selection?.files.single.path;
    if (path == null || !mounted) return;
    final directory = await _workspace;
    final file = await File(path).copy(
        '${directory.path}/firmware-${DateTime.now().microsecondsSinceEpoch}.bin');
    if (mounted) {
      setState(() {
        _firmware = file;
        _firmwareLabel = File(path).uri.pathSegments.last;
      });
    }
  }

  Future<void> _pickBackup() async {
    // A folder grant includes both the backup and its SHA-256 sidecar.
    final folder = await FilePicker.platform.getDirectoryPath(
        dialogTitle: 'Choose the folder containing your verified flash backup');
    if (folder == null || !mounted) return;
    final candidates = <File>[];
    await for (final item in Directory(folder).list()) {
      if (item is File &&
          item.path.toLowerCase().endsWith('.bin') &&
          await File('${item.path}.sha256').exists()) {
        candidates.add(item);
      }
    }
    candidates.sort((a, b) => a.path.compareTo(b.path));
    if (candidates.isEmpty) {
      throw StateError(
          'No .bin backup with a matching .bin.sha256 file in this folder');
    }
    if (!mounted) return;
    final selected = candidates.length == 1
        ? candidates.single
        : await showDialog<File>(
            context: context,
            builder: (context) => SimpleDialog(
                title: const Text('Choose verified backup'),
                children: candidates
                    .map((file) => SimpleDialogOption(
                          onPressed: () => Navigator.pop(context, file),
                          child: Text(file.uri.pathSegments.last),
                        ))
                    .toList()),
          );
    if (selected == null) return;
    final workspace = await _workspace;
    final staged = await selected.copy(
        '${workspace.path}/backup-${DateTime.now().microsecondsSinceEpoch}.bin');
    await File('${selected.path}.sha256').copy('${staged.path}.sha256');
    await verifyUsbBackup(staged);
    if (mounted) {
      setState(() {
        _backup = staged;
        _backupLabel = selected.uri.pathSegments.last;
      });
    }
  }

  Future<void> _exportBackup(File source, String folder) async {
    final name = 'evilcrow-backup-${DateTime.now().millisecondsSinceEpoch}.bin';
    final destination = File('$folder/$name');
    if (await destination.exists() ||
        await File('${destination.path}.sha256').exists()) {
      throw StateError('Backup destination already exists');
    }
    await exportUsbBackup(source, destination);
    _message('Verified backup saved: ${destination.path}');
  }

  Future<void> _createBackup() async {
    final folder = await FilePicker.platform.getDirectoryPath(
        dialogTitle: 'Choose where to save the verified flash backup');
    if (folder == null || !mounted) return;
    final workspace = await _workspace;
    final output = File(
        '${workspace.path}/full-flash-${DateTime.now().microsecondsSinceEpoch}.bin');
    await _run('backup', arguments: ['--output', output.path], critical: true);
    await _exportBackup(output, folder);
    if (mounted) {
      setState(() {
        _backup = output;
        _backupLabel = output.uri.pathSegments.last;
      });
    }
  }

  Future<void> _saveBackup() async {
    final backup = _backup!;
    final folder = await FilePicker.platform.getDirectoryPath(
        dialogTitle: 'Save verified backup and SHA-256 sidecar');
    if (folder != null) await _exportBackup(backup, folder);
  }

  Future<void> _flash() async {
    final firmware = _firmware!;
    final backup = _backup!;
    final arguments = ['--firmware', firmware.path, '--backup', backup.path];
    await _run('flash-preview', arguments: arguments);
    final plan = _backend.plan;
    if (plan == null) {
      throw StateError('The backend did not return a firmware plan');
    }
    final digest = plan['sha256'] as String;
    final partition = plan['partition'] as Map;
    if (!mounted) return;
    final approved = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Install this firmware?'),
        content: SizedBox(
            width: 560,
            child: SingleChildScrollView(
                child: SelectableText(
              'Firmware: $_firmwareLabel\nBackup: $_backupLabel\n\n'
              'Application: ${plan['firmware_bytes']} bytes\n'
              'Partition: ${partition['name']} at 0x${(partition['offset'] as int).toRadixString(16)}\n\n'
              'SHA-256\n$digest\n\n'
              'The verified backup will be checked against the connected device. '
              'Bootloader, settings, LittleFS and microSD contents are preserved. '
              'Keep USB connected until the write and verification finish.',
            ))),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Install firmware')),
        ],
      ),
    );
    if (approved != true || !mounted) return;
    await _run('flash',
        arguments: [...arguments, '--expected-sha256', digest, '--yes'],
        critical: true);
    _message('Firmware installed and verified. Reconnect Bluetooth from Home.');
  }

  Future<void> _saveCapture() async {
    final capture = _capture!;
    final target = await FilePicker.platform.saveFile(
        dialogTitle: 'Save capture', fileName: capture.uri.pathSegments.last);
    if (target != null) {
      await capture.copy(target);
      _message('Capture saved: $target');
    }
  }

  Future<void> _back() async {
    if (_backend.critical || (_preparing && !_backend.busy)) {
      _message('Wait for the current operation to finish.');
      return;
    }
    if (_backend.busy && !await _backend.stop()) return;
    if (mounted) {
      setState(() => _preparing = false);
      Navigator.of(context).pop();
    }
  }

  Widget _button(String label, IconData icon, Future<void> Function() action,
          {bool enabled = true}) =>
      OutlinedButton.icon(
          onPressed: _busy || !enabled ? null : () => _action(action),
          icon: Icon(icon, size: 18),
          label: Text(label));

  @override
  Widget build(BuildContext context) => PopScope(
        canPop: !_busy,
        onPopInvokedWithResult: (didPop, result) {
          if (!didPop) unawaited(_back());
        },
        child: Scaffold(
          appBar: AppBar(title: const Text('USB Tools')),
          body: Column(children: [
            Expanded(
                child: SingleChildScrollView(
                    padding: const EdgeInsets.all(20),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        const Text(
                            'USB operations disconnect Bluetooth. Reconnect from Home when finished. '
                            'One USB operation runs at a time.'),
                        const SizedBox(height: 16),
                        Row(children: [
                          Expanded(
                              child: DropdownButtonFormField<String>(
                            initialValue: _port ?? '',
                            decoration: const InputDecoration(
                                labelText: 'USB serial port'),
                            items: [
                              const DropdownMenuItem(
                                  value: '',
                                  child: Text('Auto-detect EvilCrow')),
                              ..._ports.map((port) => DropdownMenuItem(
                                  value: port['path'] as String,
                                  child: Text(
                                      '${port['path']} — ${port['description']}')))
                            ],
                            onChanged: _busy
                                ? null
                                : (value) => setState(() => _port = value),
                          )),
                          const SizedBox(width: 12),
                          _button('Refresh ports', Icons.refresh, _listPorts),
                        ]),
                        const SizedBox(height: 12),
                        Wrap(spacing: 8, runSpacing: 8, children: [
                          _button('Device status', Icons.info_outline,
                              () async {
                            await _run('status');
                          }),
                          _button('Restart device', Icons.restart_alt,
                              () async {
                            await _run('reboot', critical: true);
                          }),
                          _button('Enter bootloader', Icons.developer_board,
                              () async {
                            await _run('bootloader', critical: true);
                          }),
                        ]),
                        const Divider(height: 28),
                        Text('Receive and capture',
                            style: Theme.of(context).textTheme.titleMedium),
                        const SizedBox(height: 10),
                        Row(children: [
                          Expanded(
                              child: TextField(
                                  controller: _frequency,
                                  enabled: !_busy,
                                  decoration: const InputDecoration(
                                      labelText: 'Frequency (MHz)'))),
                          const SizedBox(width: 12),
                          Expanded(
                              child: TextField(
                                  controller: _duration,
                                  enabled: !_busy,
                                  decoration: const InputDecoration(
                                      labelText: 'Duration (seconds)'))),
                          const SizedBox(width: 12),
                          Expanded(
                              child: TextField(
                                  controller: _tcpPort,
                                  enabled: !_busy,
                                  decoration: const InputDecoration(
                                      labelText: 'Local TCP port'))),
                        ]),
                        const SizedBox(height: 10),
                        Wrap(spacing: 8, runSpacing: 8, children: [
                          _button('Receive demo', Icons.radio,
                              () => _captureBytes('rx')),
                          _button('Project receive bridge', Icons.cable,
                              _startBridge),
                          _button('GNU Radio capture', Icons.graphic_eq,
                              () => _captureBytes('gnuradio')),
                          _button('Save capture', Icons.save_alt, _saveCapture,
                              enabled: _capture != null),
                        ]),
                        const Text(
                            'CC1101 output is demodulated data. URH/GNU Radio formats use synthetic samples; '
                            'they are not true IQ. The URH desktop application is installed separately.'),
                        const SizedBox(height: 20),
                        Text('External radio applications',
                            style: Theme.of(context).textTheme.titleMedium),
                        const SizedBox(height: 8),
                        const Text(
                            'Save a project or flowgraph, start the local receive bridge, '
                            'then open the application. URH opens its Receive window; press Start there. '
                            'GNU Radio opens a live time plot. The stream contains demodulated ASK bits.'),
                        const SizedBox(height: 10),
                        Wrap(spacing: 8, runSpacing: 8, children: [
                          _button('Start URH', Icons.open_in_new,
                              () => _startRadioApplication('urh')),
                          _button('Start GNU Radio', Icons.show_chart,
                              () => _startRadioApplication('gnuradio')),
                          _button(
                              'Export URH project', Icons.folder_copy_outlined,
                              () async {
                            await _prepareRadioIntegration('urh');
                          }),
                          _button('Export GNU Radio flowgraph', Icons.save_alt,
                              () async {
                            await _prepareRadioIntegration('gnuradio');
                          }),
                          OutlinedButton.icon(
                              onPressed: _radioBundle == null
                                  ? null
                                  : () async {
                                      try {
                                        await _radioApps.reveal(_radioBundle!);
                                      } catch (error) {
                                        _message(error);
                                      }
                                    },
                              icon: const Icon(Icons.folder_open, size: 18),
                              label: const Text('Show exported folder')),
                        ]),
                        ExpansionTile(
                          title: const Text('Application locations'),
                          subtitle: const Text(
                              'Homebrew locations are detected automatically.'),
                          children: [
                            ListTile(
                                title: const Text('URH executable or Python'),
                                subtitle: Text(_urhExecutable ?? 'Automatic'),
                                trailing: _button('Choose', Icons.file_open,
                                    () => _chooseRadioExecutable('urh'))),
                            ListTile(
                                title: const Text('GNU Radio Python'),
                                subtitle: Text(_gnuPython ?? 'Automatic'),
                                trailing: _button('Choose', Icons.file_open,
                                    () => _chooseRadioExecutable('gnuradio'))),
                            _button('Use automatic locations', Icons.restore,
                                _resetRadioExecutables),
                          ],
                        ),
                        if (_radioStatus.isNotEmpty)
                          SelectableText(_radioStatus),
                        const Divider(height: 28),
                        Text('Firmware and recovery',
                            style: Theme.of(context).textTheme.titleMedium),
                        const SizedBox(height: 10),
                        Wrap(spacing: 8, runSpacing: 8, children: [
                          _button('Create verified backup', Icons.backup,
                              _createBackup),
                          _button('Choose backup folder', Icons.folder_open,
                              _pickBackup),
                          _button(
                              'Save backup copy', Icons.save_alt, _saveBackup,
                              enabled: _backup != null),
                          _button('Choose firmware', Icons.file_open,
                              _pickFirmware),
                          _button(
                              'Review and install', Icons.system_update, _flash,
                              enabled: _firmware != null && _backup != null),
                        ]),
                        Text(
                            'Firmware: ${_firmware == null ? 'not selected' : _firmwareLabel}\n'
                            'Backup: ${_backup == null ? 'not selected' : _backupLabel}'),
                        const SizedBox(height: 16),
                        Row(children: [
                          Expanded(child: Text(_backend.status)),
                          if (_backend.busy)
                            const SizedBox(
                                width: 16,
                                height: 16,
                                child:
                                    CircularProgressIndicator(strokeWidth: 2)),
                          const SizedBox(width: 12),
                          FilledButton.icon(
                              onPressed: _backend.canStop
                                  ? () async {
                                      await _backend.stop();
                                    }
                                  : null,
                              icon: const Icon(Icons.stop),
                              label: const Text('Stop')),
                        ]),
                        if (_backend.critical)
                          const Padding(
                              padding: EdgeInsets.only(top: 8),
                              child: Text(
                                  'Keep the device connected. Closing is available after verification finishes.')),
                        const SizedBox(height: 8),
                        Container(
                            height: 240,
                            padding: const EdgeInsets.all(12),
                            color: Theme.of(context)
                                .colorScheme
                                .surfaceContainerHighest,
                            child: SingleChildScrollView(
                                reverse: true,
                                child: SelectableText(_backend.lines.join('\n'),
                                    style: const TextStyle(
                                        fontFamily: 'Menlo', fontSize: 11)))),
                      ],
                    ))),
          ]),
        ),
      );
}
