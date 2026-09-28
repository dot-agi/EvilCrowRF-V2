# BLE OTA and external radio applications on macOS

This follow-up builds on the macOS Flutter controller. The app remains the
place to select firmware, manage Bluetooth updates, prepare radio projects,
and start or stop the USB receive bridge.

## Bluetooth firmware installation

1. Keep a verified full flash backup and its SHA-256 sidecar. The USB Tools
   backup workflow remains available for recovery.
2. Connect to the EvilCrow from Home and stop any active radio operation.
3. Open **Settings → OTA Update**. Choose the application `firmware.bin` and
   enter the firmware version expected after reboot, or use a release update.
4. Review the image size and SHA-256, then install it. Keep the device powered
   and the app open through transfer, device validation, restart and reconnect.

The app validates the ESP32 application layout, segment checksum and embedded
SHA-256 before transfer. It sends the image MD5 to the device, sizes packets
to the negotiated Bluetooth MTU, and waits for the device's cumulative byte
acknowledgement after each chunk. Progress reports acknowledged bytes.

The device's verification response must arrive before the app reboots it.
Completion requires an observed disconnect followed by a fresh firmware
version response matching the expected version. Reinstalling the same version
is supported; matching version strings alone do not prove flash-byte identity.

Cancel is available during transfer. After finalization starts, the new
partition may be selected for boot, so the app keeps the operation protected
until reconnect verification finishes. Back, window close and normal Quit are
guarded while the update is active.

The existing firmware has no resumable OTA offsets. The app can query progress
after a missing acknowledgement, but never resends an uncertain data chunk.
An aborted or failed transfer requires a device restart before another attempt.
After a successful OTA, make a new recovery backup before using USB firmware
installation: the selected application partition has changed.

## URH and GNU Radio

Install the external applications separately. The controller includes the
receive bridge and integration exporters; it uses the installed applications
for protocol analysis and GNU Radio processing.

### Install the external runtimes

The integration was tested with **URH 2.10.0** in a Python 3.13 virtual
environment and **GNU Radio 3.10.12.0** from Homebrew, using its Python 3.14
runtime and PyQt5 time plot. From the repository root:

```sh
python3.13 -m venv .tools/urh-venv
.tools/urh-venv/bin/python -m pip install urh==2.10.0
brew install gnuradio
```

In **Application locations**, select `.tools/urh-venv/bin/urh` for URH and
the Python interpreter containing GNU Radio, normally
`/opt/homebrew/bin/python3` on Apple Silicon or `/usr/local/bin/python3` on
Intel. An interpreter can be checked with
`python3 -c 'from gnuradio import gr; print(gr.version())'`.

The URH launcher resolves a pip-installed `urh` executable to its own Python
environment; a Python interpreter with URH installed can also be selected.
It does not support frozen third-party URH application bundles. The bootstrap
uses URH's [MainController](https://github.com/jopohl/urh/blob/master/src/urh/controller/MainController.py)
and [ReceiveDialog](https://github.com/jopohl/urh/blob/master/src/urh/controller/dialogs/ReceiveDialog.py)
to load the project and configure the real receive window. Its application
preferences stay inside the exported folder. These application APIs were
verified against URH 2.10.0; a later incompatible URH version may require an
updated bootstrap.

The GNU Radio export is a standalone Python
[flowgraph](https://wiki.gnuradio.org/index.php/Handling_Flowgraphs): its TCP
source feeds a native file sink or Qt time sink. It needs no custom GNU Radio
module or serial driver. The first Qt launch can take longer while Matplotlib
builds its macOS font cache.

### Prepare and launch a project

In **Settings → USB Tools → External radio applications**:

1. Select the USB port, receive frequency and local TCP port.
2. Use **Application locations** for a custom URH executable/Python or a Python
   interpreter with GNU Radio installed. Homebrew locations are detected by
   the exported launchers; **Use automatic locations** clears custom paths.
3. Click **Start URH** or **Start GNU Radio** and choose an export folder.
4. Flutter writes a new, persistent project folder, starts the receive bridge,
   waits for its ready event, then opens the exported launcher in Terminal.
5. URH opens its configured Receive window; press Start there. GNU Radio opens
   a live time plot. Keep Flutter running and use its **Stop** when finished.

Terminal shows application import or startup errors. Selecting an executable
does not install its dependencies. Exported folders include instructions and
can also be opened later through their `.command` launchers. The bridge must
be running with the same frequency and TCP port for live reception.

**Export URH project** and **Export GNU Radio flowgraph** prepare files without
opening USB or disconnecting Bluetooth. **Show exported folder** opens Finder.
If a raw capture is selected in USB Tools, it is included when preparing the
integration. Exported files remain available after leaving USB Tools.

### Stream format

The new integrations use the bridge's explicit `--sample-format bits` mode.
It unpacks CC1101 FIFO data into real-valued ASK display samples. These are
demodulated bits, not RF IQ; frequency-domain measurements cannot recover the
original carrier. There are no fabricated idle samples in this mode. The
previous byte-format bridge and standalone captures remain available.

For an existing exported project, use **Project receive bridge** before opening
its launcher. This button uses the same bit sample format as the exports.

The server binds only to `127.0.0.1` and serves one receive client at a time.
Stop one application before switching to the other. Flutter's Stop closes the
receive stream and returns the radio to idle; the external application's
window can remain open for analysis.

The bridge waits for the client's receiver settings before sending data so
URH's [RTL-TCP receiver](https://github.com/jopohl/urh/blob/master/src/urh/dev/native/RTLSDRTCP.py)
can read its separate 12-byte header. The export sets 3794 demodulated bits per
second, the integer RTL-TCP rate nearest the tested 3793.72 Baud setting.
FIFO bits are unpacked most significant bit first; missing time and UART loss
cannot be reconstructed from this stream.

## Build and tests

Build the complete app with `mobile_app/scripts/build_macos.sh` as described in
[the macOS guide](macos.md). No firmware source changes are introduced here.

Host tests cover transfer acknowledgement races, MTU limits, local image
validation, cancellation and failure handling, bridge readiness, and durable
project export. The native OTA test is deliberately opt-in because it writes
firmware. It uses the same provider/service as the UI and requires:

- `EVILCROW_OTA_WRITE_ENABLED=true`
- `EVILCROW_E2E_DEVICE_ID`
- `EVILCROW_OTA_IMAGE` and `EVILCROW_OTA_SHA256`
- `EVILCROW_OTA_BACKUP`, with its matching `.sha256` sidecar
- `EVILCROW_EXPECTED_VERSION`

For macOS integration tests, stage these files inside the test app's sandbox
container. Run `flutter test integration_test/ota_e2e_test.dart -d macos` with
the defines above. The ordinary passive device suite remains separate.
