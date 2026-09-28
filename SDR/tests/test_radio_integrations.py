"""Exported application projects plus optional real URH/GNU Radio runtime checks.

Set EVILCROW_URH_PYTHON and EVILCROW_GNURADIO_PYTHON to installed interpreters
to exercise the actual applications. All radio traffic uses simulated UART.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

from radio_integrations import export_integration
from serial_fixture import SimulatedSerial
from urh_bridge import URHBridge
import usb_backend


def exact(connection, size):
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise EOFError('incomplete stream')
        result.extend(chunk)
    return bytes(result)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def bridge(self, payload=b'\x81\x5a', sample_format='bits'):
        serial = SimulatedSerial(payload=payload)
        opener = patch('evilcrow_sdr.open_serial', return_value=(serial, 'SD card mounted'))
        opener.start()
        self.addCleanup(opener.stop)
        receiver = URHBridge('/dev/simulated', 0, sample_format=sample_format)
        self.addCleanup(receiver.cleanup)
        self.assertTrue(receiver.connect_device())
        self.assertTrue(receiver.start_server())
        errors = []

        def serve():
            try:
                while not receiver._stop_requested.is_set():
                    try:
                        client, address = receiver.server.accept()
                    except socket.timeout:
                        continue
                    receiver.handle_client(client, address)
            except OSError:
                if not receiver._stop_requested.is_set():
                    raise
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()

        def close():
            receiver.request_stop()
            worker.join(timeout=4)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
        self.addCleanup(close)
        return receiver, serial

    def test_export_backend_does_not_open_usb_and_reports_portable_paths(self):
        output = self.directory / 'URH project'
        stream = io.StringIO()
        with patch('usb_backend.detect_evilcrow_port') as discover, \
                patch('evilcrow_sdr.open_serial') as connect, contextlib.redirect_stdout(stream):
            result = usb_backend.main(['integration-export', '--target', 'urh',
                                       '--output', str(output), '--tcp-port', '4321'])
        discover.assert_not_called()
        connect.assert_not_called()
        self.assertEqual(result, 0)
        event = json.loads(stream.getvalue().splitlines()[-1])
        self.assertEqual(event['launch_file'], 'Launch URH.command')
        self.assertEqual(event['entry_file'], 'URHProject.xml')
        self.assertTrue(all(not Path(name).is_absolute() for name in event['files']))
        self.assertTrue(os.access(output / event['launch_file'], os.X_OK))
        self.assertEqual(json.loads((output / 'integration.json').read_text())['tcp_port'], 4321)

    def test_capture_export_unpacks_bits_and_preserves_portable_project(self):
        raw = self.directory / 'raw capture.bin'
        raw.write_bytes(b'\x81\x5a')
        output = self.directory / 'export'
        export_integration('urh', output, input_file=raw)
        expected = np.unpackbits(np.array([0x81, 0x5a], dtype=np.uint8)).astype(np.complex64)
        np.testing.assert_array_equal(np.fromfile(output / 'capture.complex', dtype='<c8'), expected)
        text = (output / 'URHProject.xml').read_text()
        self.assertIn('name="capture.complex"', text)
        self.assertNotIn(str(self.directory), text)

    def test_existing_output_is_preserved_and_invalid_inputs_create_nothing(self):
        output = self.directory / 'export'
        output.mkdir()
        (output / 'keep').write_text('keep')
        with self.assertRaises(FileExistsError):
            export_integration('gnuradio', output)
        self.assertEqual((output / 'keep').read_text(), 'keep')
        for kwargs in ({'tcp_port': 0}, {'frequency': float('nan')}, {'frequency': 2400e6}):
            with self.assertRaises(ValueError):
                export_integration('urh', self.directory / 'invalid', **kwargs)
            self.assertFalse((self.directory / 'invalid').exists())

    def test_selected_executable_is_literal_shell_data(self):
        executable = self.directory / "python$(touch PWNED)'quoted"
        executable.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\nprintf "%s\\n" "$@"\n')
        executable.chmod(0o755)
        output = self.directory / 'export'
        export_integration('gnuradio', output, executable=executable)
        result = subprocess.run([str(output / 'Launch GNU Radio.command'), '--help'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['evilcrow_gnuradio.py', '--help'])
        self.assertFalse((output / 'PWNED').exists())

    def test_urh_launcher_follows_relative_symlink_to_its_own_interpreter(self):
        installed = self.directory / 'libexec' / 'bin'
        installed.mkdir(parents=True)
        urh = installed / 'urh'
        urh.write_text('#!/bin/sh\nexit 99\n')
        urh.chmod(0o755)
        python = installed / 'python3'
        python.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\nprintf "%s\\n" "$@"\n')
        python.chmod(0o755)
        bin_dir = self.directory / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'urh').symlink_to('../libexec/bin/urh')
        output = self.directory / 'project'
        export_integration('urh', output, executable=bin_dir / 'urh')
        result = subprocess.run([str(output / 'Launch URH.command'), '--check'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['evilcrow_urh.py', '--check'])

    def test_bit_bridge_keeps_header_separate_and_never_fills_idle_gaps(self):
        receiver, serial = self.bridge()
        with socket.create_connection(receiver.server.getsockname(), timeout=3) as client:
            self.assertEqual(exact(client, 12)[:4], b'RTL0')
            client.settimeout(0.1)
            with self.assertRaises(socket.timeout):
                client.recv(1)
            self.assertNotIn('rx_start', serial.commands)
            setting = struct.pack('>BI', 1, 434000000)
            client.sendall(setting[:2])
            with self.assertRaises(socket.timeout):
                client.recv(1)
            client.sendall(setting[2:])
            client.settimeout(3)
            expected = bytes(value for bit in np.unpackbits(np.frombuffer(serial.payload, dtype=np.uint8))
                             for value in (254 if bit else 127, 127))
            self.assertEqual(exact(client, len(expected)), expected)
            client.settimeout(0.1)
            with self.assertRaises(socket.timeout):
                client.recv(1)
        self.assertTrue(serial.wait_for('rx_stop'))

    def test_bit_bridge_cancel_during_client_settings_does_not_start_receive(self):
        receiver, serial = self.bridge()
        with socket.create_connection(receiver.server.getsockname(), timeout=3) as client:
            exact(client, 12)
            receiver.request_stop()
        self.assertNotIn('rx_start', serial.commands)

    @unittest.skipUnless(os.environ.get('EVILCROW_URH_PYTHON'), 'URH runtime interpreter not selected')
    def test_real_urh_loads_project_capture_and_configures_receive_dialog(self):
        raw = self.directory / 'capture.bin'
        raw.write_bytes(b'\x81\x5a' * 16)
        output = self.directory / 'project'
        export_integration('urh', output, input_file=raw, tcp_port=24321, frequency=434e6,
                           executable=os.environ['EVILCROW_URH_PYTHON'])
        # A venv's python is usually a symlink. The launcher must preserve the
        # selected path so URH comes from that environment, not the base Python.
        result = subprocess.run([str(output / 'Launch URH.command'), '--check'],
                                env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{'))
        self.assertEqual(report['receiver'], 'RTL-TCP')
        self.assertEqual(report['tcp_port'], 24321)
        self.assertEqual(report['frequency'], 434e6)
        self.assertEqual(report['sample_rate'], 3794)
        self.assertEqual(report['open_signals'], 1)

    @unittest.skipUnless(os.environ.get('EVILCROW_URH_PYTHON'), 'URH runtime interpreter not selected')
    def test_real_urh_receiver_consumes_bit_bridge_without_header_coalescing(self):
        receiver, serial = self.bridge()
        script = '''
import json, sys, time
from urh.dev.native.RTLSDRTCP import RTLSDRTCP
class Control:
    def send(self, value): pass
control = Control()
device = RTLSDRTCP(433920000, 15, 3794, 650000, 0)
device.open(control, '127.0.0.1', int(sys.argv[1]))
assert device.socket_is_open, 'URH rejected the RTL-TCP header'
device.set_parameter('centerFreq', 433920000, control)
deadline = time.monotonic() + 3
data = b''
while len(data) < 32 and time.monotonic() < deadline:
    data += device.read_sync()
device.close()
print(json.dumps({'received': list(data)}))
'''
        result = subprocess.run([os.environ['EVILCROW_URH_PYTHON'], '-c', script,
                                 str(receiver.server.getsockname()[1])],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.splitlines()[-1])
        expected = [value for bit in np.unpackbits(np.frombuffer(serial.payload, dtype=np.uint8))
                    for value in (254 if bit else 127, 127)]
        self.assertEqual(report['received'], expected)
        self.assertTrue(serial.wait_for('rx_stop'))

    @unittest.skipUnless(os.environ.get('EVILCROW_GNURADIO_PYTHON'), 'GNU Radio runtime interpreter not selected')
    def test_real_gnuradio_flowgraph_receives_exact_bits_and_stops(self):
        receiver, serial = self.bridge()
        output = self.directory / 'project'
        export_integration('gnuradio', output, tcp_port=receiver.server.getsockname()[1])
        capture = output / 'received.complex'
        result = subprocess.run([os.environ['EVILCROW_GNURADIO_PYTHON'],
                                 str(output / 'evilcrow_gnuradio.py'), '--duration', '3',
                                 '--max-samples', '16', '--output', str(capture)],
                                capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = np.unpackbits(np.frombuffer(serial.payload, dtype=np.uint8)).astype(np.complex64)
        np.testing.assert_array_equal(np.fromfile(capture, dtype=np.complex64), expected)
        self.assertTrue(serial.wait_for('rx_stop'))

    @unittest.skipUnless(os.environ.get('EVILCROW_GNURADIO_PYTHON'), 'GNU Radio runtime interpreter not selected')
    def test_real_gnuradio_qt_plot_opens_saved_capture_without_radio(self):
        raw = self.directory / 'capture.bin'
        raw.write_bytes(b'\x55' * 256)
        output = self.directory / 'project'
        export_integration('gnuradio', output, input_file=raw)
        result = subprocess.run([os.environ['EVILCROW_GNURADIO_PYTHON'],
                                 str(output / 'evilcrow_gnuradio.py'), '--gui', '--duration', '0.25',
                                 '--input', str(output / 'capture.complex')],
                                env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"ok": true', result.stdout)


if __name__ == '__main__':
    unittest.main()
