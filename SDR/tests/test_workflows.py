"""CLI workflows using serial and flash models instead of physical hardware."""

import contextlib
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import firmware_tool as fw
import sdr_launcher
from serial_fixture import SimulatedSerial
from test_firmware_tool import ENTRIES, application_bytes, ota_record, table_bytes


class FirmwareModel:
    def __init__(self):
        self.flash = bytearray(b'\xa5' * fw.FLASH_SIZE)
        self.flash[0x8000:0x9000] = table_bytes(ENTRIES)
        self.flash[0xE000:0x10000] = ota_record(1) + b'\xff' * 4096
        self.flash[0x10000:0x1E0000] = b'\xff' * 0x1D0000
        app = application_bytes(1)
        self.flash[0x10000:0x10000 + len(app)] = app
        self.devices = []
        self.reads = []
        self.writes = []
        self.fail_read_once = None
        self.bad_digest = False

    def connect(self, port, baud):
        model = self

        class Device:
            FLASH_SECTOR_SIZE = 4096
            resets = 0
            stub_used = False

            def __init__(self):
                self._port = SimpleNamespace(closed=False)
                self._port.close = lambda: setattr(self._port, 'closed', True)

            def connect(self):
                pass

            def run_stub(self):
                self.stub_used = True
                return self

            def flash_set_parameters(self, size):
                assert size == len(model.flash)

            def flash_id(self):
                return 0x1640C8

            def read_flash(self, offset, size):
                assert self.FLASH_SECTOR_SIZE == 256
                model.reads.append((offset, size))
                if offset == model.fail_read_once:
                    model.fail_read_once = None
                    raise RuntimeError('Simulated dropped USB packet')
                return bytes(model.flash[offset:offset + size])

            def flash_md5sum(self, offset, size):
                if model.bad_digest:
                    return '0' * 32
                return hashlib.md5(model.flash[offset:offset + size]).hexdigest()

            def hard_reset(self):
                self.resets += 1

            def get_chip_description(self):
                return 'ESP32-PICO-D4 (simulated)'

        device = Device()
        self.devices.append(device)
        return device

    def write_flash(self, device, images, *, compress, no_progress):
        assert device.FLASH_SECTOR_SIZE == 4096
        for offset, image in images:
            self.writes.append((offset, image))
            end = (offset + len(image) + 4095) // 4096 * 4096
            self.flash[offset:end] = b'\xff' * (end - offset)
            self.flash[offset:offset + len(image)] = image


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def cli(self, main, *arguments):
        with patch.object(sys, 'argv', ['test-cli', '--port', '/dev/simulated', *arguments]):
            main()

    def test_launcher_status_is_read_only_and_library_demo_cleans_up(self):
        status_serial = SimulatedSerial()
        with patch('evilcrow_sdr.open_serial', return_value=(status_serial, '')):
            self.cli(sdr_launcher.run_cli, '--tool', 'status')
        self.assertEqual(status_serial.commands, ['board_id_read', 'board_id_read', 'sdr_status'])
        self.assertFalse(status_serial.is_open)
        receive_serial = SimulatedSerial(payload=b'\x00\x80\xff' * 400)
        with patch('evilcrow_sdr.open_serial', return_value=(receive_serial, '')), \
                patch.object(sdr_launcher, 'time', SimpleNamespace(sleep=lambda _: None)):
            self.cli(sdr_launcher.run_cli, '--tool', 'library')
        self.assertIn('Received 1024 bytes', self.output.getvalue())
        self.assertIn('rx_stop', receive_serial.commands)
        self.assertFalse(receive_serial.streaming)
        self.assertFalse(receive_serial.active)
        self.assertFalse(receive_serial.is_open)

    def test_bootloader_and_reboot_cli_preserve_flash_and_release_ports(self):
        model = FirmwareModel()
        original = bytes(model.flash)
        boot_serial = SimulatedSerial()
        with patch.object(fw, 'ESP32ROM', model.connect), \
                patch.object(fw, 'open_serial', return_value=(boot_serial, 'SD card mounted')) as drain:
            self.cli(fw.main, 'bootloader')
            self.assertEqual(model.devices[0].resets, 0)
            self.assertFalse(model.devices[0].stub_used)
            self.assertTrue(model.devices[0]._port.closed)
            drain.assert_not_called()
            self.cli(fw.main, 'reboot')
        self.assertEqual(model.devices[1].resets, 1)
        self.assertTrue(model.devices[1]._port.closed)
        self.assertFalse(boot_serial.is_open)
        self.assertEqual(bytes(model.flash), original)

    def test_full_backup_retry_preview_and_application_update_cli(self):
        model = FirmwareModel()
        model.fail_read_once = 16384
        original = bytes(model.flash)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(fw, 'ESP32ROM', model.connect), \
                patch.object(fw, 'write_flash', model.write_flash), \
                patch.object(fw, 'time', SimpleNamespace(sleep=lambda _: None)):
            backup = Path(directory) / 'backup.bin'
            firmware = Path(directory) / 'firmware.bin'
            replacement = application_bytes(2)
            firmware.write_bytes(replacement)
            self.cli(fw.main, 'backup', '--output', str(backup))
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(fw.inspect_backup(backup)[0], original)
            self.assertEqual(len(model.reads), 257)
            self.assertEqual(len(model.devices), 2)
            self.assertTrue(all(device._port.closed for device in model.devices))
            self.cli(fw.main, 'flash', '--firmware', str(firmware), '--backup', str(backup))
            self.assertEqual(len(model.devices), 2)  # Preview opens no serial connection.
            self.assertEqual(model.writes, [])
            self.cli(fw.main, 'flash', '--firmware', str(firmware), '--backup', str(backup), '--yes')
        self.assertEqual(model.writes, [(0x10000, replacement)])
        self.assertEqual(model.flash[:0x10000], original[:0x10000])
        self.assertEqual(model.flash[0x1E0000:], original[0x1E0000:])
        self.assertEqual(model.flash[0x10000:0x10000 + len(replacement)], replacement)
        self.assertEqual(model.devices[-1].resets, 1)
        self.assertTrue(model.devices[-1]._port.closed)

    def test_backup_digest_failure_leaves_no_verified_artifact(self):
        model = FirmwareModel()
        model.bad_digest = True
        with tempfile.TemporaryDirectory() as directory, patch.object(fw, 'ESP32ROM', model.connect):
            backup = Path(directory) / 'backup.bin'
            with self.assertRaisesRegex(RuntimeError, 'Full flash digest mismatch'):
                fw.backup_flash('/dev/simulated', backup)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(model.devices[-1].resets, 1)
        self.assertTrue(model.devices[-1]._port.closed)

    def test_standalone_backup_cli_rejects_unreadable_or_wrong_size_flash(self):
        script = '''
import runpy
import sys
from pathlib import Path
from unittest.mock import patch
from test_workflows import FirmwareModel
model = FirmwareModel()
script, flash_id, output = sys.argv[1:]
def connect(*arguments, **kwargs):
    device = model.connect(*arguments, **kwargs)
    device.change_baud = lambda baud: None
    device.flash_id = lambda: int(flash_id, 16)
    return device
sys.argv = [script, '--port', '/dev/simulated', '--output', output]
try:
    with patch('esptool.targets.esp32.ESP32ROM', connect), patch('time.sleep'):
        runpy.run_path(script, run_name='__main__')
finally:
    assert len(model.devices) == 3 and all(device._port.closed for device in model.devices)
    assert not model.reads
    assert not Path(output).exists() and not Path(output + '.sha256').exists()
'''
        environment = os.environ.copy()
        environment['PYTHONPATH'] = os.pathsep.join((str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)))
        command = str(Path(__file__).resolve().parents[2] / 'tools' / 'backup_flash.py')
        for flash_id in ('000000', 'ffffff', '1740c8'):
            with self.subTest(flash_id=flash_id), tempfile.TemporaryDirectory() as directory:
                output = str(Path(directory) / 'backup.bin')
                completed = subprocess.run([sys.executable, '-c', script, command, flash_id, output],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env=environment, timeout=10)
                self.assertEqual(completed.returncode, 1)
                self.assertIn(f'0x{flash_id}', completed.stderr.decode())
                self.assertIn('No backup was saved', completed.stderr.decode())
                self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
