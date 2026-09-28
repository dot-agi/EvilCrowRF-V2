import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:http/http.dart' as http;
import 'package:crypto/crypto.dart';
import 'package:file_picker/file_picker.dart';
import 'package:wakelock_plus/wakelock_plus.dart';
import 'package:flutter/services.dart';
import '../providers/ble_provider.dart';
import '../services/ota_firmware_image.dart';
import '../services/ota_transfer_service.dart';
import '../providers/settings_provider.dart';
import '../services/update_service.dart';
import '../l10n/app_localizations.dart';
import '../theme/app_colors.dart';
import 'dart:math';
import 'dart:io';

/// OTA firmware update screen — BLE OTA transfer with GitHub release integration.
class OtaScreen extends StatefulWidget {
  const OtaScreen({super.key});

  @override
  State<OtaScreen> createState() => _OtaScreenState();
}

class _OtaScreenState extends State<OtaScreen> with TickerProviderStateMixin {
  // Current device firmware version (from VersionInfo 0xC2)
  String _currentVersion = 'Unknown';

  // GitHub release info
  bool _checkingUpdate = false;
  String? _latestVersion;
  String? _latestChangelog;
  String? _firmwareUrl;
  String? _firmwareMd5;
  bool _updateAvailable = false;

  // OTA transfer state
  bool _downloading = false;
  bool _transferring = false;
  double _transferProgress = 0.0;
  String _statusMessage = '';
  bool _transferComplete = false;
  bool _transferError = false;
  String _errorMessage = '';

  late BleProvider _ble;
  bool _bound = false;
  bool _preparingOta = false;
  bool _usingLocal = false;
  final _expectedVersion = TextEditingController();
  bool get _otaBusy => _preparingOta || (_bound && _ble.otaTransfer.busy);

  // Downloaded firmware binary
  Uint8List? _firmwareBin;

  // Structured changelog data from changelog.json
  List<Map<String, String>>? _latestChanges;

  // Pulse animation for update-available sparkle effect
  late AnimationController _pulseController;
  late Animation<double> _pulseAnimation;

  // Local binary flash (debug mode only)
  String? _localBinPath;
  Uint8List? _localBin;
  bool _localTransferring = false;
  double _localTransferProgress = 0.0;
  String _localStatusMessage = '';
  bool _localTransferComplete = false;
  bool _localTransferError = false;
  String _localErrorMessage = '';

  @override
  void initState() {
    super.initState();
    // Pulse animation for sparkle effect
    _pulseController = AnimationController(
      duration: const Duration(milliseconds: 1500),
      vsync: this,
    );
    _pulseAnimation = Tween<double>(begin: 0.0, end: 1.0).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );
    // Try to get current version from BleProvider
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _loadCurrentVersion();
    });
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (!_bound) {
      _ble = Provider.of<BleProvider>(context, listen: false);
      _ble.otaTransfer.addListener(_otaChanged);
      _bound = true;
    }
  }

  void _otaChanged() {
    if (!mounted) return;
    final transfer = _ble.otaTransfer;
    setState(() {
      if (_usingLocal) {
        _localTransferring = transfer.busy;
        _localTransferProgress = transfer.progress;
        _localStatusMessage = transfer.status;
        _localTransferComplete = transfer.stage == OtaStage.complete;
        _localTransferError = transfer.stage == OtaStage.failed;
        _localErrorMessage = transfer.status;
      } else {
        _transferring = transfer.busy;
        _transferProgress = transfer.progress;
        _statusMessage = transfer.status;
        _transferComplete = transfer.stage == OtaStage.complete;
        _transferError = transfer.stage == OtaStage.failed;
        _errorMessage = transfer.status;
      }
      if (transfer.stage == OtaStage.complete) {
        _currentVersion = _ble.firmwareVersion;
      }
    });
  }

  @override
  void dispose() {
    if (_bound) {
      _ble.otaTransfer.removeListener(_otaChanged);
      _ble.otaTransfer.requestCancel();
    }
    _expectedVersion.dispose();
    _pulseController.dispose();
    super.dispose();
  }

  void _loadCurrentVersion() {
    if (!mounted) return;
    final bleProvider = Provider.of<BleProvider>(context, listen: false);
    final version = bleProvider.firmwareVersion;
    if (version.isNotEmpty) {
      setState(() => _currentVersion = version);
    }
  }

  // ── GitHub Release Check ────────────────────────────────────

  Future<void> _checkForUpdates() async {
    if (_otaBusy || _downloading || _checkingUpdate) return;
    setState(() {
      _checkingUpdate = true;
      _updateAvailable = false;
      _latestVersion = null;
      _latestChangelog = null;
      _firmwareUrl = null;
      _firmwareMd5 = null;
      _firmwareBin = null;
      _transferComplete = false;
      _statusMessage = '';
    });

    try {
      final update = await UpdateService.checkFirmwareUpdate(_currentVersion);
      if (!mounted) return;

      if (update == null) {
        setState(() {
          _checkingUpdate = false;
          _statusMessage = 'No new version available.';
        });
        return;
      }

      // Download MD5 hash if available
      String? md5Hash;
      if (update.md5Url != null) {
        md5Hash = await UpdateService.downloadMd5(update.md5Url!);
      }
      if (!mounted) return;

      setState(() {
        _latestVersion = update.version;
        _latestChangelog = update.changelog;
        _latestChanges = update.structuredChanges;
        _firmwareUrl = update.binUrl;
        _firmwareMd5 = md5Hash;
        _updateAvailable = update.binUrl != null;
        _checkingUpdate = false;
      });

      // Start sparkle animation when update is available
      if (_updateAvailable) {
        _pulseController.repeat(reverse: true);
      } else {
        _pulseController.stop();
        _pulseController.reset();
      }
    } on UpdateServiceException catch (e) {
      if (!mounted) return;
      setState(() {
        _checkingUpdate = false;
        _statusMessage = e.message;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _checkingUpdate = false;
        _statusMessage = 'API Error: $e';
      });
    }
  }

  // ── Download Firmware Binary ────────────────────────────────

  Future<void> _downloadFirmware() async {
    if (_firmwareUrl == null || _otaBusy || _downloading) return;

    setState(() {
      _downloading = true;
      _statusMessage = 'Downloading firmware...';
    });

    try {
      final response = await http
          .get(Uri.parse(_firmwareUrl!))
          .timeout(const Duration(seconds: 60));
      if (!mounted) return;
      if (response.statusCode == 200) {
        _firmwareBin = OtaFirmwareImage.parse(response.bodyBytes).bytes;

        // Verify MD5 if available
        if (_firmwareMd5 != null) {
          final digest = _calculateMd5(_firmwareBin!);
          if (digest != _firmwareMd5!.toLowerCase()) {
            setState(() {
              _downloading = false;
              _transferError = true;
              _errorMessage =
                  'MD5 mismatch!\nExpected: $_firmwareMd5\nGot: $digest';
              _firmwareBin = null;
            });
            return;
          }
        }

        setState(() {
          _downloading = false;
          _statusMessage = 'Download complete (${_firmwareBin!.length} bytes)';
        });
      } else {
        setState(() {
          _downloading = false;
          _statusMessage = 'Download failed: HTTP ${response.statusCode}';
        });
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _downloading = false;
        _firmwareBin = null;
        _statusMessage = 'Download error: $e';
      });
    }
  }

  /// Calculate MD5 hash of firmware bytes
  String _calculateMd5(Uint8List data) {
    return md5.convert(data).toString();
  }

  // ── Reviewed, acknowledgement-driven OTA ─────────────────────

  Future<void> _startOtaTransfer() async {
    if (_firmwareBin == null || _latestVersion == null || _otaBusy) return;
    try {
      final image = OtaFirmwareImage.parse(_firmwareBin!);
      await _reviewAndInstall(image, local: false, version: _latestVersion!);
    } catch (error) {
      if (mounted) {
        setState(() {
          _transferError = true;
          _errorMessage = error.toString();
        });
      }
    }
  }

  Future<void> _pickLocalBinary() async {
    if (_otaBusy) return;
    try {
      final result = await FilePicker.platform.pickFiles(
          type: FileType.custom, allowedExtensions: ['bin'], withData: true);
      if (!mounted || result == null) return;
      final file = result.files.single;
      final bytes = file.bytes ??
          (file.path == null ? null : await File(file.path!).readAsBytes());
      if (bytes == null) {
        throw StateError('The selected firmware could not be read.');
      }
      final image = OtaFirmwareImage.parse(bytes);
      if (!mounted) return;
      setState(() {
        _localBinPath = file.name;
        _localBin = image.bytes;
        _expectedVersion.clear();
        _localStatusMessage =
            'Validated ESP32 application: ${image.bytes.length} bytes';
        _localTransferComplete = false;
        _localTransferError = false;
      });
    } catch (error) {
      if (mounted) {
        setState(() {
          _localBin = null;
          _localBinPath = null;
          _localTransferError = true;
          _localErrorMessage = error.toString();
          _localStatusMessage = 'Firmware selection failed';
        });
      }
    }
  }

  Future<void> _flashLocalBinary() async {
    if (_localBin == null || _otaBusy) return;
    await _reviewAndInstall(OtaFirmwareImage.parse(_localBin!), local: true);
  }

  Future<void> _reviewAndInstall(OtaFirmwareImage image,
      {required bool local, String? version}) async {
    if (_otaBusy || !_ble.isConnected) return;
    setState(() => _preparingOta = true);
    try {
      final confirmed = await showDialog<bool>(
        context: context,
        builder: (dialogContext) =>
            StatefulBuilder(builder: (context, updateDialog) {
          final expected = local ? _expectedVersion.text.trim() : version!;
          return AlertDialog(
            title: const Text('Review Bluetooth firmware update'),
            content: SizedBox(
                width: 540,
                child: SingleChildScrollView(
                    child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                        '${image.bytes.length} bytes · inactive OTA application partition'),
                    const SizedBox(height: 12),
                    const Text('SHA-256'),
                    SelectableText(image.sha256),
                    const SizedBox(height: 12),
                    const Text('MD5 checked by the device'),
                    SelectableText(image.md5),
                    const SizedBox(height: 12),
                    if (local)
                      TextField(
                        controller: _expectedVersion,
                        onChanged: (_) => updateDialog(() {}),
                        decoration: const InputDecoration(
                            labelText:
                                'Expected firmware version after restart',
                            hintText: 'For example: 1.1.4'),
                      )
                    else
                      Text(
                          'Expected firmware version after restart: $expected'),
                    const SizedBox(height: 12),
                    const Text(
                        'The app verifies the image, transfers it, waits for device validation, '
                        'restarts the board, and checks its reported version. Keep the device powered. '
                        'Cancellation is available until final verification starts.'),
                  ],
                ))),
            actions: [
              TextButton(
                  onPressed: () => Navigator.pop(dialogContext, false),
                  child: const Text('Cancel')),
              FilledButton(
                  onPressed: expected.isEmpty
                      ? null
                      : () => Navigator.pop(dialogContext, true),
                  child: const Text('Install and verify')),
            ],
          );
        }),
      );
      if (confirmed != true || !mounted) return;
      _usingLocal = local;
      setState(() {
        if (local) {
          _localTransferComplete = false;
          _localTransferError = false;
          _localTransferring = true;
          _localStatusMessage = 'Checking radio state...';
        } else {
          _transferComplete = false;
          _transferError = false;
          _transferring = true;
          _statusMessage = 'Checking radio state...';
        }
      });
      await WakelockPlus.enable();
      await _ble.installOtaFirmware(image,
          expectedVersion: local ? _expectedVersion.text.trim() : version!);
    } catch (error) {
      if (mounted) {
        setState(() {
          final message = _ble.otaTransfer.stage == OtaStage.failed ||
                  _ble.otaTransfer.stage == OtaStage.cancelled
              ? _ble.otaTransfer.status
              : error.toString();
          if (local) {
            _localTransferError = error is! OtaCancelled;
            _localErrorMessage = message;
            _localStatusMessage = message;
          } else {
            _transferError = error is! OtaCancelled;
            _errorMessage = message;
            _statusMessage = message;
          }
        });
      }
    } finally {
      try {
        await WakelockPlus.disable();
      } catch (_) {}
      if (mounted) {
        setState(() {
          _preparingOta = false;
          _localTransferring = false;
          _transferring = false;
        });
      }
    }
  }

  // ── Build ───────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final settingsProvider = Provider.of<SettingsProvider>(context);
    final isDebugMode = settingsProvider.debugMode;

    return Consumer<BleProvider>(
      builder: (context, bleProvider, _) {
        // Keep current version in sync with BLE provider
        if (bleProvider.firmwareVersion.isNotEmpty &&
            _currentVersion == 'Unknown') {
          _currentVersion = bleProvider.firmwareVersion;
        }
        return PopScope(
            canPop: !_otaBusy,
            onPopInvokedWithResult: (didPop, result) {
              if (!didPop && mounted) {
                ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
                    content: Text(
                        'Wait for OTA to finish, or cancel before verification.')));
              }
            },
            child: Scaffold(
              backgroundColor: AppColors.primaryBackground,
              appBar: AppBar(
                title: Text(AppLocalizations.of(context)!.otaUpdate),
                backgroundColor: AppColors.secondaryBackground,
                foregroundColor: AppColors.primaryText,
                elevation: 0,
                automaticallyImplyLeading: !_otaBusy,
              ),
              body: SingleChildScrollView(
                padding: const EdgeInsets.fromLTRB(16, 16, 16, 100),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    _buildVersionCard(bleProvider),
                    const SizedBox(height: 16),
                    _buildUpdateCheckCard(),
                    if (_updateAvailable && _latestChangelog != null) ...[
                      const SizedBox(height: 16),
                      _buildChangelogCard(),
                    ],
                    if (_firmwareBin != null ||
                        _transferring ||
                        _transferComplete) ...[
                      const SizedBox(height: 16),
                      _buildTransferCard(),
                    ],
                    if (_transferError) ...[
                      const SizedBox(height: 16),
                      _buildErrorCard(),
                    ],
                    // Local application images are available in normal macOS use.
                    if (Platform.isMacOS || isDebugMode) ...[
                      const SizedBox(height: 24),
                      _buildLocalFlashCard(bleProvider),
                    ],
                  ],
                ),
              ),
            ));
      },
    );
  }

  Widget _buildVersionCard(BleProvider bleProvider) {
    return _buildCard(
      title: AppLocalizations.of(context)!.deviceInfo,
      icon: Icons.info_outline,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _buildInfoRow(
              AppLocalizations.of(context)!.currentFirmware, _currentVersion),
          if (bleProvider.freeHeap != null)
            _buildInfoRow('Free Heap', '${bleProvider.freeHeap} bytes'),
          _buildInfoRow(
              AppLocalizations.of(context)!.connection,
              bleProvider.isConnected
                  ? AppLocalizations.of(context)!.connectedStatus
                  : AppLocalizations.of(context)!.disconnectedStatus),
        ],
      ),
    );
  }

  Widget _buildUpdateCheckCard() {
    return _buildCard(
      title: AppLocalizations.of(context)!.firmwareUpdate,
      icon: Icons.system_update,
      child: Column(
        children: [
          if (_latestVersion != null) ...[
            _buildInfoRow(
                AppLocalizations.of(context)!.latestVersion, _latestVersion!),
            _updateAvailable
                ? _buildUpdateAvailableRow()
                : _buildInfoRow(AppLocalizations.of(context)!.updateAvailable,
                    AppLocalizations.of(context)!.upToDate),
            if (_firmwareMd5 != null)
              GestureDetector(
                onTap: () => _showMd5Dialog(),
                child: _buildInfoRow('MD5',
                    '${_firmwareMd5!.substring(0, min(16, _firmwareMd5!.length))}…'),
              ),
            const SizedBox(height: 12),
          ],
          Row(
            children: [
              Expanded(
                child: ElevatedButton.icon(
                  onPressed:
                      (_checkingUpdate || _otaBusy) ? null : _checkForUpdates,
                  icon: _checkingUpdate
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(
                              strokeWidth: 2, color: AppColors.primaryAccent))
                      : const Icon(Icons.refresh),
                  label: Text(_checkingUpdate
                      ? AppLocalizations.of(context)!.checking
                      : AppLocalizations.of(context)!.checkForUpdates),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: AppColors.primaryAccent,
                    foregroundColor: AppColors.primaryBackground,
                  ),
                ),
              ),
              if (_updateAvailable &&
                  _firmwareBin == null &&
                  !_downloading) ...[
                const SizedBox(width: 8),
                Expanded(
                  child: AnimatedBuilder(
                    animation: _pulseAnimation,
                    builder: (context, child) {
                      return Container(
                        decoration: BoxDecoration(
                          borderRadius: BorderRadius.circular(20),
                          boxShadow: [
                            BoxShadow(
                              color: AppColors.warning.withValues(
                                  alpha: 0.3 + 0.35 * _pulseAnimation.value),
                              blurRadius: 6 + 8 * _pulseAnimation.value,
                              spreadRadius: 1 + 2 * _pulseAnimation.value,
                            ),
                          ],
                        ),
                        child: child,
                      );
                    },
                    child: ElevatedButton.icon(
                      onPressed: _otaBusy ? null : _downloadFirmware,
                      icon: const Icon(Icons.download),
                      label: Text(AppLocalizations.of(context)!.download),
                      style: ElevatedButton.styleFrom(
                        backgroundColor: AppColors.warning,
                        foregroundColor: AppColors.primaryBackground,
                      ),
                    ),
                  ),
                ),
              ],
            ],
          ),
          if (_downloading)
            Padding(
              padding: const EdgeInsets.only(top: 12),
              child: LinearProgressIndicator(
                color: AppColors.primaryAccent,
                backgroundColor: AppColors.primaryAccent.withValues(alpha: 0.2),
              ),
            ),
          if (_statusMessage.isNotEmpty && !_transferring && !_transferComplete)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(_statusMessage,
                  style:
                      TextStyle(color: AppColors.secondaryText, fontSize: 12)),
            ),
        ],
      ),
    );
  }

  Widget _buildChangelogCard() {
    return _buildCard(
      title:
          AppLocalizations.of(context)!.changelogVersion(_latestVersion ?? ''),
      icon: Icons.description,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxHeight: 200),
        child: SingleChildScrollView(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (_latestChanges != null && _latestChanges!.isNotEmpty)
                ..._latestChanges!.map((change) {
                  final type = (change['type'] ?? 'improvement').toUpperCase();
                  final text = change['text'] ?? '';
                  return Padding(
                    padding: const EdgeInsets.symmetric(vertical: 3),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Container(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 6, vertical: 2),
                          decoration: BoxDecoration(
                            color: _getChangeTypeColor(type)
                                .withValues(alpha: 0.15),
                            borderRadius: BorderRadius.circular(4),
                          ),
                          child: Text(
                            type,
                            style: TextStyle(
                              color: _getChangeTypeColor(type),
                              fontSize: 10,
                              fontWeight: FontWeight.bold,
                            ),
                          ),
                        ),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            text,
                            style: TextStyle(
                                color: AppColors.primaryText, fontSize: 13),
                          ),
                        ),
                      ],
                    ),
                  );
                })
              else
                Text(
                  _latestChangelog ?? '',
                  style: TextStyle(
                      color: AppColors.primaryText, fontSize: 13, height: 1.5),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildTransferCard() {
    return _buildCard(
      title: AppLocalizations.of(context)!.otaTransfer,
      icon: Icons.upload,
      child: Column(
        children: [
          if (_transferring) ...[
            if (_ble.otaTransfer.canCancel)
              TextButton.icon(
                  onPressed: _ble.otaTransfer.requestCancel,
                  icon: const Icon(Icons.cancel_outlined),
                  label: const Text('Cancel transfer')),
            Text(_statusMessage,
                style: TextStyle(color: AppColors.primaryText, fontSize: 13)),
            const SizedBox(height: 12),
            ClipRRect(
              borderRadius: BorderRadius.circular(6),
              child: LinearProgressIndicator(
                value: _transferProgress,
                minHeight: 12,
                color: AppColors.primaryAccent,
                backgroundColor: AppColors.primaryAccent.withValues(alpha: 0.2),
              ),
            ),
            const SizedBox(height: 8),
            Text('${(_transferProgress * 100).toStringAsFixed(1)}%',
                style: TextStyle(
                    color: AppColors.primaryAccent,
                    fontWeight: FontWeight.bold,
                    fontSize: 16)),
          ],
          if (_transferComplete) ...[
            const Center(
              child:
                  Icon(Icons.check_circle, color: AppColors.success, size: 48),
            ),
            const SizedBox(height: 12),
            Center(
              child: Text('Firmware and restart verified',
                  textAlign: TextAlign.center,
                  style: TextStyle(
                      color: AppColors.success,
                      fontWeight: FontWeight.bold,
                      fontSize: 15)),
            ),
            const SizedBox(height: 8),
            Center(
              child: Text(_statusMessage,
                  textAlign: TextAlign.center,
                  style:
                      TextStyle(color: AppColors.secondaryText, fontSize: 13)),
            ),
          ],
          if (!_transferring && !_transferComplete && _firmwareBin != null) ...[
            Text(
                AppLocalizations.of(context)!
                    .firmwareReady(_firmwareBin!.length),
                style: TextStyle(color: AppColors.primaryText, fontSize: 13)),
            const SizedBox(height: 12),
            SizedBox(
              width: double.infinity,
              child: ElevatedButton.icon(
                onPressed: _otaBusy ? null : _startOtaTransfer,
                icon: const Icon(Icons.upload),
                label: Text(AppLocalizations.of(context)!.startOtaUpdate),
                style: ElevatedButton.styleFrom(
                  backgroundColor: AppColors.primaryAccent,
                  foregroundColor: AppColors.primaryBackground,
                  padding: const EdgeInsets.symmetric(vertical: 14),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildErrorCard() {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: AppColors.error.withValues(alpha: 0.08),
        border: Border.all(color: AppColors.error.withValues(alpha: 0.3)),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.error, color: AppColors.error, size: 20),
              const SizedBox(width: 8),
              Text(AppLocalizations.of(context)!.error,
                  style: TextStyle(
                      color: AppColors.error,
                      fontWeight: FontWeight.bold,
                      fontSize: 14)),
            ],
          ),
          const SizedBox(height: 8),
          Text(_errorMessage,
              style: TextStyle(color: AppColors.error, fontSize: 12)),
        ],
      ),
    );
  }

  // ── Helpers ─────────────────────────────────────────────────

  /// Debug-only card: pick a local .bin file and flash via BLE OTA.
  Widget _buildLocalFlashCard(BleProvider bleProvider) {
    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        color: AppColors.surfaceElevated,
        border: Border.all(color: AppColors.warning.withValues(alpha: 0.4)),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 12, 14, 6),
            child: Row(
              children: [
                Icon(Icons.developer_mode, color: AppColors.warning, size: 18),
                const SizedBox(width: 8),
                Text(AppLocalizations.of(context)!.flashLocalBinary,
                    style: TextStyle(
                        color: AppColors.warning,
                        fontWeight: FontWeight.bold,
                        fontSize: 14)),
                const Spacer(),
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                  decoration: BoxDecoration(
                    color: AppColors.warning.withValues(alpha: 0.15),
                    borderRadius: BorderRadius.circular(4),
                  ),
                  child: Text(Platform.isMacOS ? 'BLE OTA' : 'DEBUG',
                      style: TextStyle(
                          color: AppColors.warning,
                          fontSize: 10,
                          fontWeight: FontWeight.bold)),
                ),
              ],
            ),
          ),
          Divider(color: AppColors.borderDefault, height: 1),
          Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  AppLocalizations.of(context)!.selectBinFileDesc,
                  style:
                      TextStyle(color: AppColors.secondaryText, fontSize: 12),
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: ElevatedButton.icon(
                        onPressed: _otaBusy ? null : _pickLocalBinary,
                        icon: const Icon(Icons.folder_open),
                        label: Text(AppLocalizations.of(context)!.selectBin),
                        style: ElevatedButton.styleFrom(
                          backgroundColor: AppColors.warning,
                          foregroundColor: AppColors.primaryBackground,
                        ),
                      ),
                    ),
                    if (_localBin != null &&
                        !_localTransferring &&
                        !_localTransferComplete) ...[
                      const SizedBox(width: 8),
                      Expanded(
                        child: ElevatedButton.icon(
                          onPressed: bleProvider.isConnected && !_otaBusy
                              ? _flashLocalBinary
                              : null,
                          icon: const Icon(Icons.flash_on),
                          label: Text(AppLocalizations.of(context)!.flash),
                          style: ElevatedButton.styleFrom(
                            backgroundColor: AppColors.error,
                            foregroundColor: Colors.white,
                          ),
                        ),
                      ),
                    ],
                  ],
                ),
                if (_localBinPath != null &&
                    !_localTransferring &&
                    !_localTransferComplete)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text('File: $_localBinPath',
                        style: TextStyle(
                            color: AppColors.primaryText, fontSize: 12)),
                  ),
                if (_localTransferring) ...[
                  if (_ble.otaTransfer.canCancel)
                    TextButton.icon(
                        onPressed: _ble.otaTransfer.requestCancel,
                        icon: const Icon(Icons.cancel_outlined),
                        label: const Text('Cancel transfer')),
                  const SizedBox(height: 12),
                  Text(_localStatusMessage,
                      style: TextStyle(
                          color: AppColors.primaryText, fontSize: 13)),
                  const SizedBox(height: 8),
                  ClipRRect(
                    borderRadius: BorderRadius.circular(6),
                    child: LinearProgressIndicator(
                      value: _localTransferProgress,
                      minHeight: 12,
                      color: AppColors.warning,
                      backgroundColor: AppColors.warning.withValues(alpha: 0.2),
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text('${(_localTransferProgress * 100).toStringAsFixed(1)}%',
                      style: TextStyle(
                          color: AppColors.warning,
                          fontWeight: FontWeight.bold,
                          fontSize: 14)),
                ],
                if (_localTransferComplete) ...[
                  const SizedBox(height: 12),
                  Icon(Icons.check_circle, color: AppColors.success, size: 40),
                  const SizedBox(height: 8),
                  Text(_localStatusMessage,
                      style: TextStyle(
                          color: AppColors.success,
                          fontWeight: FontWeight.bold,
                          fontSize: 14)),
                ],
                if (_localTransferError) ...[
                  const SizedBox(height: 8),
                  Text(_localErrorMessage,
                      style: TextStyle(color: AppColors.error, fontSize: 12)),
                ],
                if (_localStatusMessage.isNotEmpty &&
                    !_localTransferring &&
                    !_localTransferComplete)
                  Padding(
                    padding: const EdgeInsets.only(top: 6),
                    child: Text(_localStatusMessage,
                        style: TextStyle(
                            color: AppColors.secondaryText, fontSize: 12)),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildCard(
      {required String title, required IconData icon, required Widget child}) {
    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        color: AppColors.surfaceElevated,
        border: Border.all(color: AppColors.borderDefault),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 12, 14, 6),
            child: Row(
              children: [
                Icon(icon, color: AppColors.primaryAccent, size: 18),
                const SizedBox(width: 8),
                Text(title,
                    style: TextStyle(
                        color: AppColors.primaryAccent,
                        fontWeight: FontWeight.bold,
                        fontSize: 14)),
              ],
            ),
          ),
          Divider(color: AppColors.borderDefault, height: 1),
          Padding(
            padding: const EdgeInsets.all(12),
            child: child,
          ),
        ],
      ),
    );
  }

  Widget _buildInfoRow(String label, String value) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(label,
              style: TextStyle(color: AppColors.secondaryText, fontSize: 13)),
          Text(value,
              style: TextStyle(
                  color: AppColors.primaryText,
                  fontWeight: FontWeight.w500,
                  fontSize: 13)),
        ],
      ),
    );
  }

  // ── Helper: Update Available row with sparkle ─────────────────

  Widget _buildUpdateAvailableRow() {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(AppLocalizations.of(context)!.updateAvailable,
              style: TextStyle(color: AppColors.secondaryText, fontSize: 13)),
          AnimatedBuilder(
            animation: _pulseAnimation,
            builder: (context, child) {
              return Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 10, vertical: 3),
                decoration: BoxDecoration(
                  color: AppColors.primaryAccent
                      .withValues(alpha: 0.1 + 0.12 * _pulseAnimation.value),
                  borderRadius: BorderRadius.circular(6),
                  boxShadow: [
                    BoxShadow(
                      color: AppColors.primaryAccent
                          .withValues(alpha: 0.35 * _pulseAnimation.value),
                      blurRadius: 10 * _pulseAnimation.value,
                      spreadRadius: 1 * _pulseAnimation.value,
                    ),
                  ],
                ),
                child: Text(
                  AppLocalizations.of(context)!.yes,
                  style: const TextStyle(
                    color: AppColors.primaryAccent,
                    fontWeight: FontWeight.bold,
                    fontSize: 13,
                  ),
                ),
              );
            },
          ),
        ],
      ),
    );
  }

  // ── Helper: MD5 popup dialog ────────────────────────────────

  void _showMd5Dialog() {
    if (_firmwareMd5 == null) return;
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppColors.secondaryBackground,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: const BorderSide(color: AppColors.borderDefault),
        ),
        title: Row(
          children: [
            const Icon(Icons.fingerprint,
                color: AppColors.primaryAccent, size: 20),
            const SizedBox(width: 8),
            const Text('MD5 Checksum',
                style: TextStyle(color: AppColors.primaryAccent, fontSize: 16)),
          ],
        ),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: AppColors.primaryBackground,
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: AppColors.borderDefault),
              ),
              child: SelectableText(
                _firmwareMd5!,
                style: const TextStyle(
                  color: AppColors.primaryText,
                  fontSize: 14,
                  fontFamily: 'monospace',
                  letterSpacing: 0.5,
                ),
              ),
            ),
            const SizedBox(height: 8),
            Text(
              'Long press to select and copy',
              style: TextStyle(color: AppColors.secondaryText, fontSize: 11),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () {
              Clipboard.setData(ClipboardData(text: _firmwareMd5!));
              Navigator.of(ctx).pop();
              ScaffoldMessenger.of(context).showSnackBar(
                const SnackBar(
                  content: Text('MD5 copied to clipboard'),
                  backgroundColor: AppColors.success,
                  duration: Duration(seconds: 2),
                ),
              );
            },
            child: const Text('Copy',
                style: TextStyle(color: AppColors.primaryAccent)),
          ),
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Close',
                style: TextStyle(color: AppColors.secondaryText)),
          ),
        ],
      ),
    );
  }

  // ── Helper: Color for changelog change type ─────────────────

  Color _getChangeTypeColor(String type) {
    switch (type.toUpperCase()) {
      case 'FIX':
        return AppColors.error;
      case 'FEATURE':
        return AppColors.primaryAccent;
      case 'IMPROVEMENT':
        return const Color(0xFF42A5F5); // Blue
      case 'BREAKING':
        return AppColors.warning;
      case 'SECURITY':
        return const Color(0xFFAB47BC); // Purple
      default:
        return AppColors.secondaryText;
    }
  }
}
