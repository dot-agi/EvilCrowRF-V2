"""Launcher must report device failures before showing capture results."""

import io
import hashlib
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import firmware_tool
from sdr_launcher import SDRLauncherGUI, run_library_demo
from test_firmware_tool import application_bytes
from test_workflows import FirmwareModel


class LauncherTests(unittest.TestCase):
    def test_configuration_failure_prevents_rx(self):
        for setting in ('frequency', 'modulation', 'bandwidth'):
            with self.subTest(setting=setting):
                device = MagicMock()
                device.__enter__.return_value = device
                getattr(device, f'set_{setting}').return_value = False
                with patch('evilcrow_sdr.EvilCrowSDR', return_value=device), \
                        redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError, setting):
                        run_library_demo('/dev/cu.fake', 433.92e6)
                device.start_rx.assert_not_called()
                device.__exit__.assert_called_once()

    def test_missing_stop_ack_is_reported_instead_of_capture_success(self):
        device = MagicMock()
        device.__enter__.return_value = device
        device.read_raw.return_value = b'\x01\x02\x03'
        device.stop_rx.return_value = False
        output = io.StringIO()
        with patch('evilcrow_sdr.EvilCrowSDR', return_value=device), \
                patch('sdr_launcher.time.sleep'), redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'RX stop.*restart the device'):
                run_library_demo('/dev/cu.fake', 433.92e6)
        self.assertNotIn('Received ', output.getvalue())
        device.__exit__.assert_called_once()

    def test_gui_all_files_selection_shows_verified_firmware_plan(self):
        gui = SimpleNamespace(_running_threads=[], _get_selected_port=lambda: '/dev/simulated')
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / 'application.any'
            backup = Path(directory) / 'backup.any'
            firmware.write_bytes(application_bytes(2))
            backup.write_bytes(FirmwareModel().flash)
            backup.with_suffix('.any.sha256').write_text(hashlib.sha256(backup.read_bytes()).hexdigest())
            with patch('sdr_launcher.filedialog.askopenfilename', side_effect=[str(firmware), str(backup)]) as picker, \
                    patch('sdr_launcher.messagebox.askyesno', return_value=False) as confirm, \
                    patch('sdr_launcher.messagebox.showerror') as error, \
                    patch.object(firmware_tool, 'connection') as connection:
                SDRLauncherGUI._firmware_action(gui, 'flash')
            self.assertEqual(picker.call_count, 2)
            for call in picker.call_args_list:
                self.assertIn(('All files', '*'), call.kwargs['filetypes'])
            confirm.assert_called_once()
            self.assertIn(hashlib.sha256(firmware.read_bytes()).hexdigest(), confirm.call_args.args[1])
            error.assert_not_called()
            connection.assert_not_called()

    def test_gui_all_files_selection_still_rejects_invalid_image_or_layout(self):
        gui = SimpleNamespace(_running_threads=[], _get_selected_port=lambda: '/dev/simulated')
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / 'application.any'
            backup = Path(directory) / 'backup.any'
            valid_image = application_bytes(2)
            valid_backup = bytes(FirmwareModel().flash)
            for fault in ('image_magic', 'image_digest', 'backup_layout'):
                with self.subTest(fault=fault):
                    firmware.write_bytes(valid_image)
                    backup.write_bytes(valid_backup)
                    if fault == 'image_magic':
                        firmware.write_bytes(b'This is not an ESP32 application')
                    elif fault == 'image_digest':
                        firmware.write_bytes(valid_image[:-1] + bytes([valid_image[-1] ^ 1]))
                    else:
                        backup.write_bytes(bytes(firmware_tool.FLASH_SIZE))
                    backup.with_suffix('.any.sha256').write_text(hashlib.sha256(backup.read_bytes()).hexdigest())
                    with patch('sdr_launcher.filedialog.askopenfilename', side_effect=[str(firmware), str(backup)]), \
                            patch('sdr_launcher.messagebox.askyesno') as confirm, \
                            patch('sdr_launcher.messagebox.showerror') as error, \
                            patch.object(firmware_tool, 'connection') as connection:
                        SDRLauncherGUI._firmware_action(gui, 'flash')
                    error.assert_called_once()
                    confirm.assert_not_called()
                    connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()
