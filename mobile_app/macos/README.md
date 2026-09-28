# EvilCrow RF for macOS

The desktop app controls an EvilCrow RF v2 over Bluetooth Low Energy (BLE)
and USB. Use **Settings → USB Tools** for wired receive, capture, recovery and
firmware installation. The USB backend runs inside this app without opening
the separate SDR launcher.

The app builds independently of the firmware PR and retains package version
1.1.3+31. Existing Bluetooth controls use the baseline firmware protocol.
Live hardware validation used firmware 1.1.4; an older-firmware device was
not reflashed for a separate compatibility run.
Reliable raw USB capture and accurate requested nRF state require the separate
[device firmware 1.1.4 fixes](https://github.com/dot-agi/EvilCrowRF-V2/pull/1)
installed on the device; they are not needed to build the macOS app.

## Build and run

Install Xcode with its macOS SDK and a Flutter SDK. The local build was tested
with Flutter 3.47.5, Dart 3.13.4, and Xcode 27.0 on Apple silicon. The target's
minimum macOS version is 12.0. This target uses Swift Package Manager.
The embedded USB backend also needs Python with the dependencies in
`SDR/requirements.txt`, including PyInstaller. The build script uses the
repository `.venv/bin/python`; set `PYTHON` to select another interpreter.

From the repository root:

```sh
mobile_app/scripts/build_macos.sh
open 'mobile_app/build/macos/Build/Products/Release/EvilCrow RF.app'
```

The script rebuilds the USB backend before packaging the Flutter app, so one
command produces the complete app. It uses `.tools/flutter/bin/flutter` when it exists, otherwise `flutter`
on `PATH`. Set `FLUTTER=/absolute/path/to/flutter/bin/flutter` to select an SDK.
Set `EVILCROW_USB_HELPER` to an already built `EvilCrow SDR.app` to reuse it;
the script checks its headless entry point before embedding it. A direct
`flutter build macos` or `flutter run -d macos` builds the Bluetooth controller;
use this script when packaging USB Tools.

The app can be copied to Applications. These local builds use ad hoc signing;
they have no Developer ID notarization. The build script verifies the bundle's
signature after building.

## Connect and use

1. Power on the EvilCrow RF v2 and enable Bluetooth on the Mac.
2. Open **EvilCrow RF**, choose **Scan for Devices**, and connect to
   **EvilCrow_RF2**. Allow the macOS Bluetooth prompt when it appears.
3. Expand the status bar to inspect the device's radio and storage status.

If Bluetooth access was denied, enable it in **System Settings → Privacy &
Security → Bluetooth**, then reopen the app. CoreBluetooth manages the system
permission prompt and negotiated MTU; Android permission checks are skipped
on macOS and iOS.

On later launches, **Connect** can use the remembered device without scanning.
It waits up to ten seconds for Bluetooth initialization. Permission denial and
powered-off Bluetooth show separate messages.

The **Home** RF scanner starts idle. Select **M1**, **M2**, or **1+2**, then
press **Start Scanner** at the top right. **Scanning stopped** is the normal
idle label before the first scan. **Stop Scanner** stops reception. Leaving
Home also stops the selected radios. A disconnect clears the scanning state;
reconnect and start manually to scan again.

**Files → Internal** browses LittleFS. **Files → SD Card** requires a mounted
FAT32 card in the device. **Settings → HW Buttons** configures the two user
buttons (GPIO34 and GPIO35); the third physical button is hardware Reset and
cannot be remapped. Button recording is disabled because the current firmware
does not implement that action.

## USB Tools

Open **Settings → USB Tools**. Connect the device by USB, click **Refresh
ports**, and select its serial port or keep automatic detection. **Device
status** reads the firmware identity and radio state. Device operations
disconnect Bluetooth before taking USB ownership; reconnect from Home when
finished.

- **Receive demo** captures demodulated CC1101 bytes for the selected duration.
- **GNU Radio capture** saves synthetic complex samples. These are not true IQ.
- **URH bridge** listens on the selected local TCP port until **Stop**. Connect
  a separately installed URH desktop application to `127.0.0.1` at that port.
- **Save capture** exports the most recent capture through the native save panel.
- **Enter bootloader** and **Restart device** use USB reset signals without
  requiring the physical buttons.

Only one USB helper runs at a time. Stop and leaving the page wait for passive
receive cleanup; a second operation remains blocked until the helper exits.

### Backup and firmware installation

1. Click **Create verified backup** and choose a destination folder. The app
   reads the full 4 MiB flash, verifies it against the device, and saves the
   `.bin` image and its matching `.bin.sha256` checksum file. Keep this pair.
2. Alternatively, **Choose backup folder** imports an existing pair. The app
   checks its size and SHA-256 before selecting it.
3. **Choose firmware** selects an ESP32 application `.bin`. Click **Review and
   install** to inspect its size, target partition, and SHA-256.
4. **Install firmware** confirms that exact image. The backend checks the
   firmware hash again, validates the backup against the connected device,
   writes the application partition, verifies it, and restarts the device.

The application update preserves the bootloader, settings, LittleFS and SD
contents. Stop, Back, window close and Quit are blocked while backup, firmware
write, bootloader entry or restart is running. Keep USB connected until the
operation finishes.

The app retains its macOS sandbox. Native file selections are copied into its
container before the child helper reads them, and the parent app exports
captures and backup files. The embedded helper inherits the parent's static
USB, serial and network permissions. See Apple's [App Sandbox entitlement
reference](https://developer.apple.com/library/archive/documentation/Miscellaneous/Reference/EntitlementKeyReference/Chapters/EnablingAppSandbox.html).

## App icon

`Runner/Resources/EvilCrow.icns` is an exact local copy of the SDR launcher
icon. Its SHA-256 is
`5bb6a3e1ad2cd92af650088b66082936e0c5d2b008a43dc5b65dc5d01714a592`.
The AppIcon asset catalog contains its native size and scale representations.
Xcode compiles the catalog and supplies the icon metadata for all build modes.
The build script does not patch icon metadata after compilation.

To regenerate the catalog's PNGs from this local canonical copy:

```sh
mobile_app/scripts/update_macos_icons.sh
```

If an earlier build is running or pinned in the Dock, quit it and open the
newly built bundle by its full path before checking the icon. The bundle ID
remains `org.evilcrow.controller`, preserving the existing app identity.

## Regression checks

From `mobile_app`:

```sh
flutter test
```

The tests cover scanner start/stop, leaving Home or NRF while a BLE command is
pending, disconnect/reconnect, partial startup of two radios, and ProtoPirate
mode in binary radio notifications.
They also cover USB process ownership during startup and shutdown, fragmented
helper output, cancellation, critical-operation quit protection, and recovery
backup checksum exports.

## Hardware integration test

Run the app with `flutter run -d macos`, scan for your EvilCrow, and copy its
CoreBluetooth device UUID from the scan output. Quit the app before starting
the test. A macOS BLE identifier is a UUID; it is different from the ESP32's
hardware MAC address.

From `mobile_app`, with the device powered and Bluetooth enabled:

```sh
flutter test integration_test/device_e2e_test.dart -d macos \
  --dart-define=EVILCROW_E2E_DEVICE_ID='<your device UUID>' \
  --dart-define=EVILCROW_EXPECTED_VERSION=1.1.4
```

The device UUID explicitly selects the owned device. Without it, the test is
skipped. The expected firmware version is optional; use the version installed
on your device when checking a different build. Allow the native Bluetooth
permission prompt if it appears.

The test checks discovery, reconnect, device status, scanner navigation
cleanup, temporary file create/upload/read/rename/copy/move/delete on LittleFS
and SD, button mapping readback and restoration, passive recording, ProtoPirate
decoding, and the NRF spectrum. It creates uniquely named test files and removes
them during cleanup. Results are printed as `E2E_PASS`, `E2E_FAIL`, and
`E2E_RESULTS` and attached to the integration-test report.

RF transmission, jamming, firmware installation, formatting storage, and native
file-picker interactions need separate manual validation.
