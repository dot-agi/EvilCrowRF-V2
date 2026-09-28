# EvilCrow RF on macOS

## App and connections

The macOS Flutter controller provides both Bluetooth controls and USB tools in
one app. This checkout contains the macOS runner, USB backend, build scripts,
and host tests. It builds directly from the app branch without the firmware PR.

| Connection | Where | Use |
| --- | --- | --- |
| Bluetooth Low Energy | Home, Files, Settings, radio screens | Radio status, scanning, files, button mappings |
| USB serial, 115200 baud | Settings → USB Tools | Device status, receive demo, URH bridge, GNU Radio capture, firmware tools |

The app bundles Python and its USB dependencies as a helper. USB operations run
inside Flutter; no separate launcher window is required. USB Tools disconnects
Bluetooth before accessing the device. Reconnect from Home after the operation.

Open `EvilCrow RF.app` in Finder or copy it to Applications. Builds are local and
are not Developer ID notarized. The app uses the SDR crow artwork. The Flutter
port keeps a copy of `SDR/assets/EvilCrow.icns` and generates its standard Xcode
`AppIcon` asset catalog from that copy.

Use one USB tool at a time, including terminal clients. Opening the UART can
reset the ESP32 and interrupt Bluetooth, even when the client does not
intentionally assert DTR/RTS.

See [test coverage and limitations](testing-macos.md) for verified workflows.

## Build the app

Install Xcode, Flutter, and Python 3.10 or newer with Tk support. The Flutter
build script also builds and embeds the USB helper:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r SDR/requirements.txt
.venv/bin/python -c 'import tkinter; print(tkinter.TkVersion)'
mobile_app/scripts/build_macos.sh
open 'mobile_app/build/macos/Build/Products/Release/EvilCrow RF.app'
```

The helper targets the Python interpreter's architecture. The Flutter build
script uses a local SDK when available, otherwise `flutter` on `PATH`. Set
`PYTHON` or `FLUTTER` to select another interpreter or SDK. The final bundle
embeds and signs the helper and includes Bluetooth, serial, USB, network, and
user-selected file entitlements. Set `EVILCROW_USB_HELPER` to an existing helper
bundle to reuse it; its headless entry point is checked before packaging.
The USB helper can also be built separately with `SDR/build_macos.sh` for CLI
use or the optional standalone launcher.

## USB receive tools

Open **Settings → USB Tools**, refresh the ports, and choose the EvilCrow UART
or auto-detection. **Device status** reads identity and state. **Receive demo**
performs a bounded raw capture; **Save capture** exports it. **Stop** waits for
device cleanup before another USB operation can start.

Read identity and status without enabling SDR:

```sh
.venv/bin/python SDR/check_device.py
```

Discovery suggests a single supported USB UART and verifies the board through
its protocol.
If several adapters are attached, supply `--port` with the device's `/dev/cu.*`
path. USB paths can change when moving to another port.

With firmware 1.1.4 installed, run a bounded receive check:

```sh
.venv/bin/python SDR/check_device.py --rx-seconds 5 \
  --output logs/usb-receive-check.json
```

This receives ASK/OOK at 433.92 MHz, checks the stop response, and verifies that
SDR returns to idle. Demodulated bytes may be noise; receiving bytes alone does
not establish that a signal was decoded.

The CC1101 supplies demodulated FIFO bytes and RSSI measurements. URH/GNU Radio
helpers convert bytes into synthetic sample formats. They cannot provide true
IQ capture. Retuning pauses reception, applies the setting, and starts a new
receive session so command responses stay out of sample data. Serial spectrum
commands provide a summary; RSSI points are sent over Bluetooth.

The URH bridge starts with ASK/OOK, 650 kHz bandwidth and 3,793.72 Baud. RTL-TCP
sample-rate requests change the CC1101 demodulation rate, which must remain
within 600–500,000 Baud; they do not create an IQ sample clock.

The **GNU Radio capture** helper works without GNU Radio installed. Running
the source as an actual GNU Radio block requires GNU Radio. The URH desktop
application is also installed separately.

The headless backend also accepts structured CLI requests and emits one JSON
event per line:

```sh
.venv/bin/python SDR/sdr_launcher.py --backend list-ports
.venv/bin/python SDR/sdr_launcher.py --backend status
.venv/bin/python SDR/sdr_launcher.py --backend rx --duration 5
.venv/bin/python SDR/sdr_launcher.py --backend urh --tcp-port 1234
```

Use Ctrl-C to stop receive tools. A parent process can add `--watch-stdin`
before the subcommand so closing stdin requests receive cleanup.

## Bluetooth controller

Power the device, enable Bluetooth, choose **Scan for Devices**, and connect to
the EvilCrow. Allow the macOS Bluetooth prompt when shown. **Files → Internal**
browses LittleFS; **Files → SD Card** requires a mounted card.
CoreBluetooth handles Apple-platform permission and MTU negotiation.

The **Home** scanner starts idle. Select **M1**, **M2**, or **1+2**, then press
**Start Scanner**. The control becomes **Stop Scanner** while active.
**Scanning stopped** includes the initial idle state. Leaving Home stops its
scan. A disconnect clears the app's scanning state; reconnecting requires a
manual start.

## Runtime firmware compatibility

The app and its USB helper build independently of the firmware changes.
The existing Bluetooth controller uses the baseline firmware's protocol;
the app package version remains **1.1.3+31**. Baseline compatibility is based on
the unchanged protocol; the live device suite was run on firmware 1.1.4.

Reliable raw USB receive, SDR stop acknowledgments, clean binary output, and
accurate requested nRF state require the **1.1.4 device firmware fixes**. Those
are supplied separately in [firmware PR #1](https://github.com/dot-agi/EvilCrowRF-V2/pull/1).
This is a device-runtime requirement, not an app build or merge dependency.
The host cannot repair firmware SPI deadlocks or FIFO configuration by itself.

For firmware build instructions, use the [1.1.4 firmware guide](https://github.com/dot-agi/EvilCrowRF-V2/blob/4a03aef4b8c20a7d3508f23e703f1da4b80685f2/docs/firmware-1.1.4.md).
That branch produces `.pio/build/esp32dev/firmware.bin`; choose the resulting
application image in USB Tools. This app branch does not include the new
firmware build script or replace the baseline embedded sources.

Use the firmware version reported by the device over Bluetooth. Esptool's
image descriptor can contain the prebuilt Arduino core's project version,
which is separate from both the device firmware and Flutter package versions.

The host in this checkout retains partial reads, clears previous capture data,
bounds command deadlines, and permits only one serial reader per session.
Closing a receiver joins its reader before issuing cleanup commands and reports
unacknowledged cleanup. Serial spectrum scans wait for their completion marker.

## USB bootloader and firmware controls

In **Settings → USB Tools**, use **Enter bootloader**, **Restart device**,
**Create verified backup**, and **Review and install**. They use the
USB UART's DTR/RTS auto-reset circuit; the tested EvilCrow board enters the ROM
bootloader without a physical button press. Bluetooth disconnects in that mode.
Restart the device to resume the controller.

The same operations are available from the CLI:

```sh
.venv/bin/python SDR/firmware_tool.py bootloader
.venv/bin/python SDR/firmware_tool.py reboot
.venv/bin/python SDR/firmware_tool.py backup --output logs/evilcrow-backup.bin

# Preview the image and destination without opening USB or writing flash:
.venv/bin/python SDR/firmware_tool.py flash \
  --firmware /path/to/firmware.bin --backup logs/evilcrow-backup.bin
# Add --yes to perform that update.
```

If discovery is ambiguous, put `--port` and the adapter's `/dev/cu.*` path
before the subcommand. The separately built USB helper exposes the same CLI:

```sh
'SDR/dist/EvilCrow SDR.app/Contents/MacOS/EvilCrow SDR' \
  --backend reboot
```

Flashing requires a valid application image and a verified 4 MiB backup with
its `.sha256` sidecar. The tool checks the live partition table, OTA selection,
and installed application against that backup. It writes only the selected
application partition, verifies its device MD5, and restarts. Bootloader,
partition table, NVS, internal files, and SD contents are preserved. Create a
fresh backup before updating an application that differs from the old backup.

The app copies selected inputs into its own workspace so the sandboxed helper
can read them. A new backup is exported with its SHA-256 sidecar before it can
be selected for a firmware write. Keep both exported files for recovery.
The app blocks Stop, navigation, and normal Quit/window-close actions during
backup and firmware operations. The headless backend also ignores SIGINT while
a backup, firmware write, bootloader entry or restart is in progress; passive
receive tools still use SIGINT for graceful Stop. Cmd-Q during a live backup
showed the native Keep Open guard; the backup continued and completed.

The GUI binds the write to the displayed firmware SHA-256. Changing the image
after confirmation causes rejection before USB is opened. Unreadable flash IDs
(`0x000000` or `0xffffff`) also prevent writing: disconnect USB and any other
device power for ten seconds, reconnect, and retry. Keep USB connected during
a write; an interruption may require recovery from the backup.

Local testing occasionally required that full power cycle after an automatic
reset. Automatic bootloader entry is supported, but recovery from this hardware
state still requires disconnecting power; its electrical cause has not been
confirmed. The Flutter GUI bootloader/restart controls passed, including SD
initialization after restart. GUI backup attempts rejected unreadable flash
IDs before writing. After a full power cycle, the full 4 MiB backup verified
against the device and exported with a valid checksum sidecar; two serial-read
retries recovered during that run. The final independent app then imported
that exported backup, previewed and wrote the selected 1.1.4 application image,
verified its device hash, and restarted successfully. Post-update USB reception,
Bluetooth connection, and SD directory listing passed. The final native suite
also passed cold remembered-device connection before any discovery scan,
followed by the remaining workflows and cleanup: 18 checks in 50 seconds.

Backups include internal firmware and settings, but exclude the microSD card.
Keep them private. The backup reader uses small requests, bounded retries, and
a full-device MD5 check before saving a complete image and SHA-256 sidecar.

See [Espressif's automatic bootloader documentation](https://docs.espressif.com/projects/esptool/en/latest/esp32/advanced-topics/boot-mode-selection.html).

## Physical buttons and remapping

| Button | Wiring | Function |
| --- | --- | --- |
| SW1 / RESET | ESP32 EN | Hardware restart; fixed function |
| SW2 / Button 1 | GPIO34 | Configurable firmware shortcut |
| SW3 / Button 2 | GPIO35 | Configurable firmware shortcut |

In the macOS controller's **Settings → Hardware Buttons**, choose an action for
either user button. Mappings are stored in internal flash and survive restart.
Actions include LED toggle, reboot, deep sleep, replay of a selected file, and
nRF jammer toggle. Recording remains unimplemented in the firmware and is
disabled in the macOS UI. Mapping/readback tests do not verify physical presses.

The hardware reference is
[`Schematic_EvilCrow_RF_V4_2021-12-14.pdf`](schematics/Schematic_EvilCrow_RF_V4_2021-12-14.pdf).
It shows RESET plus two user buttons; the draft pinout README's separate BOOT
pushbutton description does not match this schematic.

## SD card troubleshooting

**Not mounted** means initialization failed; it does not establish whether a
card is physically absent. Power off the device before reseating the card.
A successful boot reports `SD card initialized.`

If mounting still fails, back up the card with a separate reader and check its
filesystem. The EvilCrow USB connection is a UART; macOS cannot mount the card
through it. The project documents FAT32 cards up to 32 GB. If reformatting is
needed, preserve the files first and use FAT32 with a Master Boot Record
partition scheme. Safely eject the card before reinserting it into the
powered-off EvilCrow.
