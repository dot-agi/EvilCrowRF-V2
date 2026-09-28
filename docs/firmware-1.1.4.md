# Firmware 1.1.4

Version 1.1.4 fixes SDR receive startup, continuous FIFO reception, and stop
handling on EvilCrow RF v2. It also reports the active nRF scan and spectrum
states correctly. The version is defined in `include/config.h` and returned by
the BLE version response.

## Changes

- SDR configuration uses the CC1101 wrappers' existing SPI locks, avoiding
  nested acquisition of the non-recursive mutex.
- Raw RX temporarily selects continuous FIFO reception. It restores the
  receiver's asynchronous-mode registers on stop, handles FIFO overflow, and
  uses stable byte-count reads while leaving one byte in the FIFO.
- A lifecycle/output mutex keeps RX payload and start/stop responses in order.
  The main loop yields after releasing it so the serial command task can run
  while BLE notifications are active.
- Arduino UART0 debug logging is muted during binary RX and restored on stop.
  Explicit payload and command responses continue to use the UART.
- State requests report nRF scanning, attacking, and spectrum activity using
  the existing status codes. This change does not start those operations.

The FIFO behavior follows sections 15.2, 20, and 27.1 of the
[TI CC1101 datasheet](https://www.ti.com/lit/ds/symlink/cc1101.pdf).

## Build

Install Python 3.10 or newer, then run from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install platformio
tools/build_firmware_macos.sh
```

The build helper keeps PlatformIO and uv caches in the checkout unless
`PLATFORMIO_CORE_DIR` or `UV_CACHE_DIR` overrides them. The pinned platform is
PIOArduino `55.03.36-1`, providing Arduino 3.3.6 and ESP-IDF 5.5.2; the battery
module requires the ESP-IDF 5 ADC API. The build produces
`.pio/build/esp32dev/firmware.bin` without flashing the device.

The firmware build requires no Flutter SDK or desktop USB application.
Esptool's image descriptor can contain the prebuilt Arduino core's project
version; the firmware's BLE version response reports `1.1.4`.

The independent firmware checkout built successfully in 42.18 seconds for
`esp32dev`. The 1,205,888-byte ESP32 image passed esptool's checksum and digest
checks and uses DIO mode, 40 MHz flash, and a 4 MiB flash layout. Its SHA-256 is:

```text
194d315494693d957b6d440093d9adfc14adb36c926e270546e7b034413db671
```

The firmware/build source files match those used for the device validation
below. The clean rebuild has a different artifact fingerprint; the hardware
receipts apply to the installed image identified in the next section.

## Device validation

Validation used an owned EvilCrow RF v2 with 4 MiB flash and a microSD card.
The installed application was 1,205,888 bytes with SHA-256:

```text
92f78f00bc80b70e59cf821cf051318858238133a3850c3c6d429ae822423fc6
```

A full flash backup was verified before the application write, and the written
application's device MD5 matched. On that image:

- BLE reported version 1.1.4, mounted SD storage, and the expected radio state.
- Fourteen passive/local controller workflows and three final cleanup checks
  passed: connection/reconnection, scanner start and navigation cleanup,
  internal/SD file operations, settings and button mapping restoration,
  recording start/stop, ProtoPirate file decoding and passive receive, and nRF
  passive scan/spectrum start/stop.
- Local RTL-TCP clients and the GNU source received data across retuning and
  clean stop. The packaged controller also captured and exported USB data.

Combined USB RX with BLE connected was previously verified on the same 1.1.4
SDR implementation before the nRF status addition: both bounded captures
acknowledged stop and ended inactive, with no firmware log lines found in the
captured binary. The earlier image SHA-256 was
`80c4cac4dd35804880a1af5c73fd3b6cbefdbb13372b303096dad41baf991024`.

These are device observations obtained with separate host tools. Captured
demodulated bytes can be noise; the tests do not establish known-signal RF
decoding, sensitivity, or lossless operation at every data rate. The CC1101
does not provide true IQ samples.

## Limits and recovery

Use one radio controller at a time. Stop USB reception before issuing radio
operations from another client. Physical button presses, BLE OTA, transmission,
replay, emulation, jamming, HID injection, factory reset, and SD formatting
were not part of the passive validation. The existing binary-transmit command
`0x06` and button recording action remain unimplemented.

The board's USB UART auto-reset circuit entered the bootloader without a button
press. Repeated resets sometimes left flash identification unreadable
(`0xffffff` or `0x000000`), and the validation tools stopped before writing.
Removing USB and every other power source for ten seconds restored access in
the observed cases. The electrical cause remains unconfirmed; firmware update
tools must reject unreadable identification rather than write through it.
See [Espressif's boot-mode documentation](https://docs.espressif.com/projects/esptool/en/latest/esp32/advanced-topics/boot-mode-selection.html)
for the auto-reset and strapping-pin behavior.
