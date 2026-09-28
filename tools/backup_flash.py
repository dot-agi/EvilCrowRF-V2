#!/usr/bin/env python3
"""Read and verify a 4 MiB ESP32 flash backup over a buffering-sensitive UART.

Requires esptool 5.x. This performs no flash writes or erases. Small read
requests and bounded retries avoid restarting a whole backup after a dropped
USB packet. Each read and the complete image are checked against device MD5.
"""

import argparse
import hashlib
import time
from pathlib import Path

from esptool.targets.esp32 import ESP32ROM


def connect(port, baud):
    device = ESP32ROM(port, baud=115200)
    try:
        device.connect()
        device = device.run_stub()
        device.change_baud(baud)
        device.flash_set_parameters(4 * 1024 * 1024)
        flash_id = device.flash_id()
        if flash_id in (0x000000, 0xFFFFFF):
            raise ValueError(
                f'Unreadable flash identification (ID 0x{flash_id:06x}). '
                'Disconnect USB and any other device power for 10 seconds, '
                'reconnect, and retry. No backup was saved.')
        if (flash_id >> 16) & 0xFF != 0x16:
            raise ValueError(
                f'Unsupported flash ID 0x{flash_id:06x}; this tool supports '
                'the EvilCrow model with 4 MiB flash. No backup was saved.')
        # This controls read packet size here, not the flash erase geometry.
        device.FLASH_SECTOR_SIZE = 256
        return device
    except BaseException:
        device._port.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--baud', type=int, default=115200)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sidecar = args.output.with_suffix(args.output.suffix + '.sha256')
    if args.output.exists() or sidecar.exists():
        parser.error('Backup or checksum already exists; choose a new backup filename')
    flash_size = 4 * 1024 * 1024
    chunk_size = 16 * 1024
    image = bytearray()
    device = None
    try:
        for offset in range(0, flash_size, chunk_size):
            for attempt in range(3):
                try:
                    if device is None:
                        device = connect(args.port, args.baud)
                    chunk = device.read_flash(offset, chunk_size)
                    if len(chunk) != chunk_size:
                        raise RuntimeError('Incomplete read')
                    image.extend(chunk)
                    break
                except Exception as error:
                    if device is not None:
                        device._port.close()
                        device = None
                    if attempt == 2:
                        raise
                    print(f'Retrying 0x{offset:06x}: {error}', flush=True)
                    time.sleep(0.5)
            if len(image) % (256 * 1024) == 0:
                print(f'Read {len(image) // 1024} / {flash_size // 1024} KiB', flush=True)
        expected = device.flash_md5sum(0, flash_size).lower()
        actual = hashlib.md5(image).hexdigest()
        if actual != expected:
            raise RuntimeError(f'Full flash digest mismatch: {actual} != {expected}')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Do not leave a partial file that could be mistaken for a backup.
        with args.output.open('xb') as output:
            output.write(image)
        digest = hashlib.sha256(image).hexdigest()
        with sidecar.open('x') as checksum:
            checksum.write(f'{digest}  {args.output.name}\n')
        print(f'Verified {len(image)} bytes; SHA-256 {digest}', flush=True)
    finally:
        if device is not None:
            try:
                device.hard_reset()
            finally:
                device._port.close()


if __name__ == '__main__':
    main()
