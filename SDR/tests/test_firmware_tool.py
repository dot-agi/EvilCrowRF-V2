"""Partition selection and write-boundary checks for USB firmware updates."""

import hashlib
import struct
import tempfile
import unittest
import zlib
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from esptool.bin_image import ELFSection

import firmware_tool as fw


def table_bytes(entries):
    table = b''.join(struct.pack('<HBBII16sI', 0x50AA, kind, subtype, offset,
                                size, name.encode(), 0)
                     for name, kind, subtype, offset, size in entries)
    table += b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(table).digest()
    return table.ljust(fw.SECTOR_SIZE, b'\xff')


ENTRIES = [('nvs', 1, 2, 0x9000, 0x5000),
           ('otadata', 1, 0, 0xE000, 0x2000),
           ('app0', 0, 0x10, 0x10000, 0x1D0000),
           ('app1', 0, 0x11, 0x1E0000, 0x1D0000),
           ('coredump', 1, 3, 0x3B0000, 0x10000),
           ('littlefs', 1, 0x82, 0x3C0000, 0x40000)]


def ota_record(sequence, state=2):
    crc = zlib.crc32(struct.pack('<I', sequence), 0xFFFFFFFF)
    return struct.pack('<I20sII', sequence, b'\xff' * 20, state, crc).ljust(4096, b'\xff')


def application_bytes(fill):
    image = fw.ESP32FirmwareImage()
    descriptor = struct.pack('<I', 0xABCD5432) + bytes([fill]) * 252
    image.segments = [ELFSection(b'.flash.appdesc', 0x3F400020, descriptor, 0)]
    return image.save(None)


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.partitions = fw.partitions_from_bytes(table_bytes(ENTRIES))

    def test_selects_newest_valid_ota_record(self):
        app = fw.application_partition(self.partitions, ota_record(1) + ota_record(2))
        self.assertEqual((app.name, app.offset), ('app1', 0x1E0000))
        app = fw.application_partition(self.partitions, ota_record(1) + ota_record(2, state=3))
        self.assertEqual(app.name, 'app0')

    def test_erased_ota_defaults_to_first_app(self):
        self.assertEqual(fw.application_partition(self.partitions, b'\xff' * 8192).name, 'app0')

    def test_pending_or_corrupted_selection_is_not_guessed(self):
        with self.assertRaisesRegex(ValueError, 'pending validation'):
            fw.application_partition(self.partitions, ota_record(1) + ota_record(2, state=1))
        with self.assertRaisesRegex(ValueError, 'safely determine'):
            fw.application_partition(self.partitions, bytes(8192))

    def test_table_integrity_and_overlapping_partitions(self):
        table = bytearray(table_bytes(ENTRIES))
        table[16] ^= 1
        with self.assertRaisesRegex(ValueError, 'MD5 mismatch'):
            fw.partitions_from_bytes(table)
        with self.assertRaisesRegex(ValueError, 'Overlapping'):
            fw.partitions_from_bytes(table_bytes(ENTRIES + [('bad', 1, 2, 0x9000, 0x1000)]))

    def test_read_packet_size_never_changes_write_erase_geometry(self):
        device = Mock(FLASH_SECTOR_SIZE=4096)
        def read(offset, size):
            self.assertEqual(device.FLASH_SECTOR_SIZE, 256)
            raise RuntimeError('USB dropped packet')
        device.read_flash.side_effect = read
        with self.assertRaises(RuntimeError):
            fw.read_small(device, 0, 4096)
        self.assertEqual(device.FLASH_SECTOR_SIZE, 4096)


class ConnectionTests(unittest.TestCase):
    def test_unreadable_flash_rejects_write_and_closes_port(self):
        for flash_id in (0x000000, 0xFFFFFF):
            for restart in (False, True):
                with self.subTest(flash_id=flash_id, restart=restart):
                    rom = Mock()
                    device = rom.run_stub.return_value
                    device.flash_id.return_value = flash_id
                    with patch.object(fw, 'ESP32ROM', return_value=rom), \
                            patch.object(fw, 'write_flash') as write:
                        with self.assertRaises(ValueError) as raised:
                            with fw.connection('fake', restart=restart) as connected:
                                fw.write_flash(connected, [(0x10000, b'firmware')])
                    message = str(raised.exception)
                    self.assertIn(f'Unreadable flash identification (ID 0x{flash_id:06x})', message)
                    self.assertIn('Disconnect USB', message)
                    self.assertIn('No flash write was performed', message)
                    write.assert_not_called()
                    device._port.close.assert_called_once()
                    self.assertEqual(device.hard_reset.call_count, int(restart))

    def test_non_four_mib_flash_still_rejected_with_actual_id(self):
        rom = Mock()
        device = rom.run_stub.return_value
        device.flash_id.return_value = 0x1740C8
        with patch.object(fw, 'ESP32ROM', return_value=rom):
            with self.assertRaisesRegex(ValueError, '0x1740c8.*4 MiB'):
                with fw.connection('fake'):
                    self.fail('Unsupported flash was accepted')
        device.hard_reset.assert_called_once()
        device._port.close.assert_called_once()

    def test_four_mib_flash_connection_is_accepted(self):
        rom = Mock()
        device = rom.run_stub.return_value
        device.flash_id.return_value = 0x1640C8
        with patch.object(fw, 'ESP32ROM', return_value=rom):
            with fw.connection('fake') as connected:
                self.assertIs(connected, device)
        device.hard_reset.assert_called_once()
        device._port.close.assert_called_once()


class FlashWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backup = Path(self.temp.name) / 'backup.bin'
        self.table = table_bytes(ENTRIES)
        self.ota = ota_record(1) + b'\xff' * 4096
        self.original = bytearray(b'\xff' * fw.FLASH_SIZE)
        self.original[0x8000:0x9000] = self.table
        self.original[0xE000:0x10000] = self.ota
        self.backup.write_bytes(self.original)
        self.backup.with_suffix('.bin.sha256').write_text(hashlib.sha256(self.original).hexdigest())
        self.image = b'replacement application'
        self.read_image = patch.object(fw, 'read_application', return_value=self.image)
        self.read_image.start()
        self.addCleanup(self.read_image.stop)
        self.device = Mock(FLASH_SECTOR_SIZE=4096)
        self.device.flash_md5sum.side_effect = [
            hashlib.md5(self.original[0x10000:0x1E0000]).hexdigest(),
            hashlib.md5(self.image).hexdigest()]
        self.device.read_flash.side_effect = [self.table, self.ota]

    @contextmanager
    def connected(self, *args):
        yield self.device

    def test_preview_does_not_open_usb_or_write(self):
        with patch.object(fw, 'connection') as connect, patch.object(fw, 'write_flash') as write:
            fw.flash_firmware('fake', 'firmware.bin', self.backup)
        connect.assert_not_called()
        write.assert_not_called()

    def test_modified_backup_is_rejected(self):
        self.backup.write_bytes(bytes(fw.FLASH_SIZE))
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            fw.prepare_flash('firmware.bin', self.backup)

    def test_replaced_valid_firmware_is_rejected_before_opening_usb(self):
        self.read_image.stop()  # Exercise real image checksum/digest validation.
        firmware = Path(self.temp.name) / 'firmware.bin'
        firmware.write_bytes(application_bytes(1))
        reviewed_plan, _, _ = fw.prepare_flash(firmware, self.backup)
        replacement = application_bytes(2)
        firmware.write_bytes(replacement)
        self.assertEqual(fw.read_application(firmware), replacement)
        with patch.object(fw, 'connection') as connect, patch.object(fw, 'write_flash') as write:
            with self.assertRaisesRegex(ValueError, 'Firmware changed since confirmation'):
                fw.flash_firmware('fake', firmware, self.backup, confirmed=True,
                                  expected_sha256=reviewed_plan['sha256'])
        connect.assert_not_called()
        write.assert_not_called()

    def test_rejects_stale_installed_application_before_write(self):
        self.device.flash_md5sum.side_effect = ['0' * 32]
        with patch.object(fw, 'connection', self.connected), patch.object(fw, 'write_flash') as write:
            with self.assertRaisesRegex(ValueError, 'Installed application differs'):
                fw.flash_firmware('fake', 'firmware.bin', self.backup, confirmed=True)
        write.assert_not_called()

    def test_rejects_changed_layout_before_write(self):
        self.device.read_flash.side_effect = [bytes(4096)]
        with patch.object(fw, 'connection', self.connected), patch.object(fw, 'write_flash') as write:
            with self.assertRaisesRegex(ValueError, 'layout or OTA'):
                fw.flash_firmware('fake', 'firmware.bin', self.backup, confirmed=True)
        write.assert_not_called()

    def test_writes_only_application_and_verifies_digest(self):
        with patch.object(fw, 'connection', self.connected), patch.object(fw, 'write_flash') as write:
            fw.flash_firmware('fake', 'firmware.bin', self.backup, confirmed=True,
                              expected_sha256=hashlib.sha256(self.image).hexdigest())
        write.assert_called_once_with(self.device, [(0x10000, self.image)], compress=True, no_progress=True)
        self.assertEqual(self.device.FLASH_SECTOR_SIZE, 4096)
        self.assertEqual(self.device.flash_md5sum.call_args.args, (0x10000, len(self.image)))

    def test_verification_failure_is_reported(self):
        self.device.flash_md5sum.side_effect = [
            hashlib.md5(self.original[0x10000:0x1E0000]).hexdigest(), '0' * 32]
        with patch.object(fw, 'connection', self.connected), patch.object(fw, 'write_flash'):
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                fw.flash_firmware('fake', 'firmware.bin', self.backup, confirmed=True)


if __name__ == '__main__':
    unittest.main()
