#!/usr/bin/env python3
"""USB bootloader, backup and application updates for a 4 MiB EvilCrow RF v2.

Uses the CH340 DTR/RTS auto-reset circuit. Flashing preserves the bootloader,
partition table, OTA selection and data partitions. A verified full backup is
required, and its application bytes must match the connected device.
"""

import argparse
import hashlib
import io
import json
import struct
import time
import zlib
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from esptool.bin_image import ESP32FirmwareImage
from esptool.cmds import write_flash
from esptool.targets.esp32 import ESP32ROM

from serial_utils import detect_evilcrow_port, open_serial


FLASH_SIZE = 4 * 1024 * 1024
TABLE_OFFSET = 0x8000
SECTOR_SIZE = 0x1000


@dataclass(frozen=True)
class Partition:
    name: str
    type: int
    subtype: int
    offset: int
    size: int


def partitions_from_bytes(table):
    """Parse ESP-IDF entries, checking the table digest and flash boundaries."""
    if len(table) != SECTOR_SIZE:
        raise ValueError('Incomplete partition table')
    partitions = []
    for pos in range(0, SECTOR_SIZE, 32):
        entry = table[pos:pos + 32]
        if entry[:2] == b'\xeb\xeb':
            if entry[16:] != hashlib.md5(table[:pos]).digest():
                raise ValueError('Partition table MD5 mismatch')
            break
        if entry == b'\xff' * 32:
            break
        magic, kind, subtype, offset, size, name, flags = struct.unpack('<HBBII16sI', entry)
        if magic != 0x50AA or not size or flags & 1:
            raise ValueError('Invalid or encrypted partition table entry')
        if offset < TABLE_OFFSET + SECTOR_SIZE or offset % SECTOR_SIZE or size % SECTOR_SIZE or offset + size > FLASH_SIZE:
            raise ValueError('Partition outside supported 4 MiB flash boundaries')
        partitions.append(Partition(name.rstrip(b'\0').decode('ascii'), kind, subtype, offset, size))
    if not partitions:
        raise ValueError('No partitions found')
    ordered = sorted(partitions, key=lambda item: item.offset)
    if any(a.offset + a.size > b.offset for a, b in zip(ordered, ordered[1:])):
        raise ValueError('Overlapping partitions')
    return partitions


def application_partition(partitions, otadata):
    """Follow ESP-IDF's two OTA sequence records; reject ambiguous states."""
    apps = sorted((p for p in partitions if p.type == 0 and 0x10 <= p.subtype < 0x20), key=lambda p: p.subtype)
    factory = next((p for p in partitions if p.type == 0 and p.subtype == 0), None)
    if not apps or [p.subtype for p in apps] != list(range(0x10, 0x10 + len(apps))):
        raise ValueError('Expected contiguous OTA application partitions')
    if len(otadata) != 2 * SECTOR_SIZE:
        raise ValueError('Expected two OTA selection sectors')
    records = []
    for pos in (0, SECTOR_SIZE):
        seq = struct.unpack_from('<I', otadata, pos)[0]
        state, crc = struct.unpack_from('<II', otadata, pos + 24)
        if seq not in (0, 0xFFFFFFFF) and state not in (3, 4) and crc == zlib.crc32(otadata[pos:pos + 4], 0xFFFFFFFF):
            records.append((seq, state))
    if records:
        seq, state = max(records, key=lambda record: record[0])
        if state in (0, 1):
            raise ValueError('OTA image is pending validation; boot and validate it before a USB update')
        return apps[(seq - 1) % len(apps)]
    if otadata == b'\xff' * len(otadata):
        return factory or apps[0]
    raise ValueError('Cannot safely determine the boot application from OTA data')


def inspect_backup(path):
    path = Path(path)
    data = path.read_bytes()
    if len(data) != FLASH_SIZE:
        raise ValueError('Backup must contain exactly 4 MiB of flash')
    sidecar = path.with_suffix(path.suffix + '.sha256')
    digest = hashlib.sha256(data).hexdigest()
    if sidecar.read_text().split()[0] != digest:
        raise ValueError('Backup SHA-256 does not match its sidecar')
    table = data[TABLE_OFFSET:TABLE_OFFSET + SECTOR_SIZE]
    partitions = partitions_from_bytes(table)
    ota = next((p for p in partitions if p.type == 1 and p.subtype == 0), None)
    if ota is None or ota.size != 2 * SECTOR_SIZE:
        raise ValueError('Unsupported OTA selection partition')
    otadata = data[ota.offset:ota.offset + ota.size]
    app = application_partition(partitions, otadata)
    return data, table, ota, otadata, app


def read_application(path):
    data = Path(path).read_bytes()
    if len(data) < 288 or len(data) > FLASH_SIZE or data[32:36] != b'\x32\x54\xcd\xab':
        raise ValueError('Choose an ESP32 application firmware.bin, not a bootloader or merged flash image')
    image = ESP32FirmwareImage(io.BytesIO(data))
    if image.chip_id != 0 or image.checksum != image.calculate_checksum():
        raise ValueError('Invalid ESP32 application image or checksum')
    if not image.append_digest or image.stored_digest != image.calc_digest:
        raise ValueError('Application SHA-256 digest is missing or invalid')
    return data


def prepare_flash(firmware, backup):
    image = read_application(firmware)
    original, _, _, _, app = inspect_backup(backup)
    if len(image) > app.size:
        raise ValueError('Firmware does not fit the selected application partition')
    plan = {'firmware': str(Path(firmware).resolve()),
            'firmware_bytes': len(image), 'sha256': hashlib.sha256(image).hexdigest(),
            'backup': str(Path(backup).resolve()), 'partition': asdict(app),
            'preserves': ['bootloader', 'partition table', 'OTA selection', 'NVS', 'LittleFS', 'microSD']}
    return plan, image, original


@contextmanager
def connection(port, *, stub=True, restart=True):
    device = ESP32ROM(port, baud=115200)
    try:
        device.connect()  # DTR/RTS automatically selects the ROM bootloader.
        if stub:
            device = device.run_stub()
            device.flash_set_parameters(FLASH_SIZE)
            flash_id = device.flash_id()
            if flash_id in (0x000000, 0xFFFFFF):
                raise ValueError(
                    f'Unreadable flash identification (ID 0x{flash_id:06x}). '
                    'Disconnect USB and any other device power for 10 seconds, '
                    'reconnect, and retry. No flash write was performed.')
            if (flash_id >> 16) & 0xFF != 22:
                raise ValueError(
                    f'Unsupported flash ID 0x{flash_id:06x}; this tool supports '
                    'the EvilCrow model with 4 MiB flash. No flash write was performed.')
        yield device
    finally:
        try:
            if restart:
                device.hard_reset()
        finally:
            device._port.close()


def read_small(device, offset, size):
    # The stub uses this attribute for read packet size. Restore the real erase
    # sector size before write_flash ever sees the device.
    previous = device.FLASH_SECTOR_SIZE
    try:
        device.FLASH_SECTOR_SIZE = 256
        result = device.read_flash(offset, size)
        if len(result) != size:
            raise RuntimeError('Incomplete flash read')
        return result
    finally:
        device.FLASH_SECTOR_SIZE = previous


def enter_bootloader(port):
    with connection(port, stub=False, restart=False) as device:
        print(f'{device.get_chip_description()}: ROM bootloader ready on {port}.')
    print('Bluetooth is unavailable in this mode. Use Restart device to resume.')


def restart_device(port):
    with connection(port, stub=False):
        pass
    # Actively drain startup output; some CH340 drivers stall on a full buffer.
    serial, boot = open_serial(port)
    serial.close()
    for line in boot.splitlines():
        if 'SD card' in line:
            print(line)
    print('Device restarted. Reconnect from the Bluetooth controller.')


def backup_flash(port, output):
    output = Path(output)
    if output.exists() or output.with_suffix(output.suffix + '.sha256').exists():
        raise ValueError('Backup output already exists; choose a new filename')
    image = bytearray()
    # Reconnect after a failed read, preserving completed chunks in memory.
    session = None
    device = None
    try:
        for offset in range(0, FLASH_SIZE, 16 * 1024):
            for attempt in range(3):
                try:
                    if device is None:
                        session = connection(port, restart=False)
                        device = session.__enter__()
                    image.extend(read_small(device, offset, 16 * 1024))
                    break
                except Exception as error:
                    if device is not None:
                        session.__exit__(None, None, None)
                        device = None
                    if attempt == 2:
                        raise
                    print(f'Retrying 0x{offset:06x}: {error}', flush=True)
                    time.sleep(0.5)
            if len(image) % (256 * 1024) == 0:
                print(f'Read {len(image) // 1024} / 4096 KiB', flush=True)
        if device.flash_md5sum(0, FLASH_SIZE).lower() != hashlib.md5(image).hexdigest():
            raise RuntimeError('Full flash digest mismatch')
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('xb') as stream:
            stream.write(image)
        digest = hashlib.sha256(image).hexdigest()
        with output.with_suffix(output.suffix + '.sha256').open('x') as stream:
            stream.write(f'{digest}  {output.name}\n')
        print(f'Verified backup: {output} (SHA-256 {digest})')
    finally:
        if device is not None:
            try:
                device.hard_reset()
            finally:
                session.__exit__(None, None, None)


def flash_firmware(port, firmware, backup, *, confirmed=False, expected_sha256=None):
    plan, image, original = prepare_flash(firmware, backup)
    if expected_sha256 is not None and plan['sha256'] != expected_sha256:
        raise ValueError(
            'Firmware changed since confirmation: '
            f'expected SHA-256 {expected_sha256}, found {plan["sha256"]}. '
            'Review the firmware again. No flash write was performed.')
    print(json.dumps(plan, indent=2))
    if not confirmed:
        print('Preview only. Supply --yes to write this application image.')
        return plan
    _, table, ota, otadata, app = inspect_backup(backup)
    with connection(port) as device:
        if read_small(device, TABLE_OFFSET, SECTOR_SIZE) != table or read_small(device, ota.offset, ota.size) != otadata:
            raise ValueError('Device partition layout or OTA selection differs from the backup; make a new backup')
        app_digest = hashlib.md5(original[app.offset:app.offset + app.size]).hexdigest()
        if device.flash_md5sum(app.offset, app.size).lower() != app_digest:
            raise ValueError('Installed application differs from the backup; make a new backup')
        print(f'Writing {app.name} at 0x{app.offset:06x}; keep USB connected.')
        write_flash(device, [(app.offset, image)], compress=True, no_progress=True)
        if device.flash_md5sum(app.offset, len(image)).lower() != hashlib.md5(image).hexdigest():
            raise RuntimeError('Application verification failed; keep the backup for recovery')
        print('Application write verified. Restarting device.')
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', help='USB UART; auto-detects a single supported adapter')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('bootloader', help='Enter ROM flashing mode without pressing a button')
    commands.add_parser('reboot', help='Restart into the installed firmware')
    backup = commands.add_parser('backup', help='Read and verify all 4 MiB of flash')
    backup.add_argument('--output', type=Path, required=True)
    flash = commands.add_parser('flash', help='Preview or install an application image')
    flash.add_argument('--firmware', type=Path, required=True)
    flash.add_argument('--backup', type=Path, required=True)
    flash.add_argument('--yes', action='store_true', help='Perform the reviewed application write')
    flash.add_argument('--expected-sha256', help='Reject an image changed since its reviewed preview')
    args = parser.parse_args()
    port = args.port or detect_evilcrow_port()
    if not port and not (args.command == 'flash' and not args.yes):
        parser.error('Specify --port; a single USB UART could not be identified')
    try:
        if args.command == 'bootloader':
            enter_bootloader(port)
        elif args.command == 'reboot':
            restart_device(port)
        elif args.command == 'backup':
            backup_flash(port, args.output)
        else:
            flash_firmware(port, args.firmware, args.backup, confirmed=args.yes,
                           expected_sha256=args.expected_sha256)
    except Exception as error:
        parser.exit(1, f'ERROR: {error}\n')


if __name__ == '__main__':
    main()
