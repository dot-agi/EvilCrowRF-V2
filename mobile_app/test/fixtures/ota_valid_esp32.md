# ESP32 OTA parser fixture

`ota_valid_esp32.bin` is a synthetic image generated with esptool's
`ESP32FirmwareImage` and two `ELFSection` objects. It contains an ESP32
application descriptor magic plus byte ramps, an image XOR checksum and SHA-256
footer. It is parser test data, not executable device firmware.

The first segment at `0x3f400020` contains little-endian `0xabcd5432` followed by
`bytes(range(252))`. The second at `0x40080000` contains
`bytes(range(256)) * 2`. The image is saved with `image.save(None)`.
