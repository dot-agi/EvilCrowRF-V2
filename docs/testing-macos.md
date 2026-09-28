# macOS test coverage

Validation snapshot: 28 September 2026. A host simulation, a live transport
test, and reception of a known RF signal establish different behavior.

## Live device results

Tested with an owned EvilCrow RF v2, 4 MiB ESP32 flash, USB UART, microSD, and a
Mac. No intentional sub-GHz transmission, replay, jamming, or HID injection was
performed.

The device used for these live tests ran separately built **1.1.4** firmware,
1,205,888 bytes, SHA-256:

```text
92f78f00bc80b70e59cf821cf051318858238133a3850c3c6d429ae822423fc6
```

| Check | Recorded result | Limit |
| --- | --- | --- |
| CLI USB firmware installation | Verified full 4 MiB backup; application written to app0 at `0x10000`; device MD5 matched | Application partition only |
| USB identity and status | Correct board identity; status commands respond | Does not test RF sensitivity |
| USB RX with Flutter connected over BLE | 5,685 host bytes in 12 seconds; 2,374 bytes in 5 seconds | Earlier 1.1.4 image identified below; data may be noise |
| RX stop and cleanup | Both combined runs acknowledged stop; final state inactive and not streaming | Host byte counts exclude data outside the capture window |
| Binary stream cleanliness | No firmware log lines found in either combined capture | Does not establish lossless reception at every data rate |
| Packaged USB CLI demo | Received 1,024 bytes in five seconds; stop acknowledged | Earlier 1.1.4 image identified below |
| Button-free bootloader/restart | CLI and Flutter GUI operations completed; restart logged successful SD initialization | Depends on the board's auto-reset circuit |
| Live RTL-TCP bridge | Two local clients received 1,181 and 1,177 samples excluding padding; retuning, reconnect and stop passed | External URH desktop UI was not used |
| Live GNU source | Received 512 samples, retuned, received another 512; stop passed | Source helper used without a GNU Radio runtime flowgraph |
| Flutter native BLE suite | All 18 checks passed in 50 seconds, including cold connection and three cleanup checks | Detailed coverage below |
| Flutter packaged USB discovery/status | Device discovered and status returned through the embedded helper | Does not test RF sensitivity |
| Flutter packaged receive/export | Captured 2,393 raw bytes and exported them through the native save panel | Demodulated data may be noise |
| Flutter packaged GNU capture/export | Captured 2,376 synthetic complex samples and exported 19,008 bytes in complex64 format | GNU Radio runtime was not used |
| Flutter packaged URH bridge | Local TCP client received 2,374 bytes / 1,187 samples excluding padding; fragmented retuning command and Stop passed | External URH desktop UI was not used |
| Flutter GUI backup/export | Full 4 MiB read and device-MD5 verification passed; exported SHA-256 sidecar names and matches the saved image | Two serial-read retries recovered |
| Flutter GUI firmware installation | Final independent package imported the exported backup, previewed the selected image, wrote 1,205,888 bytes to app0 at `0x10000`, verified its hash and restarted | Application partition only |
| Post-update USB receive | Captured 2,406 bytes in five seconds; stop acknowledged; final state inactive and not streaming | Demodulated data may be noise |
| Post-update BLE and SD | Reported firmware 1.1.4, listed the SD `DATA` folders, and passed a cold direct connection before any discovery scan | Native cold connection completed in 4,061 ms |
| Flutter critical-operation Quit guard | Cmd-Q displayed Keep Open during the active backup; backup continued to completion | Normal application quit, not forced process termination |
| Flutter GUI backup preflight | Rejected unreadable flash IDs `0xffffff` and `0x000000`; no flash write occurred | A full power cycle was needed before the successful backup |
| Button mapping persistence | Mapping survived restart; original mappings restored | No physical button press tested |

The combined USB/BLE RX and packaged demo checks used the preceding 1.1.4
image, SHA-256
`80c4cac4dd35804880a1af5c73fd3b6cbefdbb13372b303096dad41baf991024`.
The current image adds nRF state reporting; the live adapters and native BLE
suite ran on that current image.

The firmware reported 6,005 and 3,033 bytes for the two combined RX runs.
These counters cover firmware streaming, while host counts cover the requested
read windows; they are not a losslessness comparison. Known-transmitter
decoding remains unverified.

Local receipts include `macos_usb_1.1.4_clean_rx.json`,
`macos-firmware-1.1.4-nrf-status-flash-retry.log`,
`macos-live-adapters-e2e.log`, `macos-flutter-device-e2e.log`, and
`macos-independent-final-device-e2e-results.json`. Device logs and full flash
backups remain local because they can contain settings and nearby Bluetooth
identifiers.

## Host tests with simulated hardware

**63 Python tests passed** in 39.338 seconds in the independent app checkout.
These tests replace physical serial/esptool access; adapter cases use actual
localhost TCP sockets and temporary capture files.

| Area | Covered behavior |
| --- | --- |
| Serial protocol | Exact board identification, busy/error responses, bounded commands under continuous binary input, fragmented replies, start trailer boundary, exclusive reader ownership, spectrum completion and receiver-close cleanup |
| Capture buffering | Fresh data for each session; partial reads retain remaining bytes |
| URH bridge | Fragmented/batched RTL-TCP commands, signed gain, pause/configure/resume, clean sample forwarding, reconnect, rejected-stop cleanup, cancellation during startup |
| GNU Radio helper | Configuration/start/stop failures, retuning and message-handler error reporting, exact complex sample conversion, empty captures do not create fabricated samples |
| Firmware tools | Partition/OTA parsing, backup checksums, flash-ID rejection, stale backup rejection, confirmation hash binding, application-only writes and verification |
| Firmware input callbacks | File selection fallback; invalid image or backup rejected before confirmation or USB access |
| CLI workflows | Backup retry, flash-ID rejection before reading and digest failure, preview without USB access, bootloader/restart, status and receive cleanup |
| Headless backend | JSON event framing, exact errors, capture output, preview/hash binding, broken parent pipe, rejected stop, cancellation before startup, passive SIGINT/stdin-EOF cleanup and critical-operation SIGINT protection |

Run from the repository root after installing the USB dependencies:

```sh
PYTHONPATH=SDR .venv/bin/python -m unittest discover -s SDR/tests -v
```

These results do not establish that the external URH desktop application or a
GNU Radio runtime flowgraph has been exercised against the device.

## Flutter tests and native device suite

The Flutter macOS app has **28 passing host tests**: seven scanner
tests, five nRF lifecycle tests, three radio-mode parser tests, one BLE-provider
teardown test, four Bluetooth-readiness tests, six USB service tests, and two
backup-file tests.
They cover manual start/stop, navigation during pending writes, disconnects,
partial startup failure, bounded state refresh, teardown, and ProtoPirate mode
reporting. USB cases cover startup exclusivity, queued Stop, fragmented JSON,
stream draining, process failures, disposal, and quit protection. Backup cases
verify the exported filename/checksum pair and reject changed image contents.
Bluetooth cases cover a delayed powered-on state, repeated Connect clicks,
permission denial, powered-off state, and a ten-second initialization timeout.
These tests use fake BLE/process objects and local temporary files.

The final native macOS integration suite passed all 18 checks in 50 seconds
with the real Flutter app and BLE device. It checks fresh device responses
after asynchronous operations rather than relying on optimistic app state.
The cold connection check runs before any discovery scan or test-side wait for
Bluetooth readiness.

| Case | Result |
| --- | --- |
| Cold remembered-device connection | Passed in 4,061 ms without a preliminary scan |
| Discovery, connection, version and state | Passed |
| Connected navigation views | Passed |
| Device name and settings change/readback/restoration | Passed |
| Scanner start and navigation cleanup | Passed; both radios returned to Idle |
| Internal and SD upload/read/rename/copy/move/tree/delete | Passed |
| Both button mappings/readback/restoration | Passed |
| Passive recording start/stop | Passed |
| Synthetic RAW file upload and ProtoPirate decoder | Passed; expected Kia V0 fields and CRC |
| ProtoPirate passive decode start/stop | Passed |
| nRF passive scan and spectrum start/stop | Passed; 126-channel spectrum data and requested state |
| Disconnect/reconnect | Passed |
| Final cleanup of both CC1101 modules and nRF | Passed |

The packaged Flutter USB screen passed port discovery, device status, raw and
GNU capture/export, the URH bridge lifecycle, bootloader/restart, and full-flash
backup/export. After a full power cycle, the backup completed with two recovered
serial-read errors, matched the device MD5, and exported 4,194,304 bytes plus a
valid SHA-256 sidecar. Cmd-Q during that backup showed the native Keep Open
guard; the backup continued and completed. Earlier attempts correctly rejected
unreadable flash IDs before any write.

The final independent app package then imported that exported backup through
the native folder picker and selected the 1,205,888-byte firmware image whose
SHA-256 is recorded above. The confirmation showed that exact hash and app0 at
`0x10000`. The write transferred 683,460 compressed bytes in 67.1 seconds,
verified the device hash, restarted the board, and returned `ok: true`.
After the update, a five-second USB receive captured 2,406 bytes, acknowledged
Stop, and reported inactive/non-streaming status. Bluetooth reported firmware
1.1.4 and listed the SD `DATA` folders. The rebuilt app then passed the native
cold remembered-device connection check without a preliminary scan, followed
by discovery, reconnect and all remaining workflows and cleanup checks.

The app release was rebuilt directly from `main` plus this app/host change set,
without the firmware PR. The build generated its own USB helper and passed
deep/strict code-signature verification. The 28 Flutter host tests also passed
in this independent checkout; the icon still matches the canonical SDR artwork.

This checkout supplies a synthetic Kia V0 RAW fixture. Feeding its
188 pulses into the actual C++ decoder on the Mac produced key
`0000010ABCDEF306`, serial `0ABCDEF`, button `3`, counter `1`, 61 bits, and valid
CRC `06`. Uploading and decoding that file exercises BLE, SD, parsing, and
decoding; it does not exercise the radio receive path.

The app and host tests build from this branch without the firmware PR. The
live raw-USB and accurate nRF-state checks require the fixed firmware on the
device; see [runtime firmware compatibility](macos.md#runtime-firmware-compatibility).
The BLE command protocol remains compatible by source inspection with the
baseline firmware. No separate live run downgraded the device to that version.
Run from `mobile_app`; set the identifier discovered for your owned device:

```sh
flutter test
flutter test integration_test/device_e2e_test.dart -d macos \
  --dart-define=EVILCROW_E2E_DEVICE_ID="$EVILCROW_E2E_DEVICE_ID" \
  --dart-define=EVILCROW_EXPECTED_VERSION=1.1.4
```

The native suite skips without an explicit device identifier. It creates
uniquely named test files and restores its device name, settings and button
mappings. Passive recording can create capture files; newly observed captures
are reported and retained.

## Known unsupported behavior

- Binary transmission command `0x06` currently returns success without an
  implementation (`include/TransmitterCommands.h`).
- Button action `ToggleRecording` only logs and blinks
  (`include/ButtonCommands.h`); the macOS UI disables this choice.
- Serial `set_gain` is an acknowledged compatibility operation. CC1101 gain
  remains controlled by AGC.
- Serial spectrum commands do not return RSSI points; those use BLE. The Python
  `spectrum_scan()` helper therefore returns an empty result list.
- CC1101 FIFO bytes are demodulated data. Synthetic URH/GNU sample conversion
  cannot supply true IQ measurements.
- Battery charging status is a voltage-based heuristic, not a dedicated
  charging-status input.

## Remaining coverage

| Function | What is still needed |
| --- | --- |
| Known-signal capture and protocol decoding | An owned transmitter with known frequency, modulation, and payload |
| Physical buttons | Press each user button and verify the selected action; test RESET separately |
| URH / GNU Radio desktop integration | Run the external application or runtime flowgraph; local adapter transport has passed |
| BLE OTA | Complete transfer, validation, reboot, and version check with a fresh backup |
| Replay, emulation, brute-force, nRF HID/string/Ducky, jamming | Scoped owned receiver and isolated RF setup appropriate to the operation |
| Factory reset and SD format | Explicit destructive test with a verified backup and restoration plan |
| Battery measurement, RF sensitivity, throughput limits | Appropriate measurement equipment and known inputs |

Passive control commands, such as scan/record start and stop, can be checked
without proving payload reception. Test one radio owner at a time: stop USB
capture before initiating another radio operation from the controller.

See [the macOS guide](macos.md) for setup and firmware commands.
