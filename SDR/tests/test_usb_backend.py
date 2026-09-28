"""Headless Flutter backend contracts without a physical USB device."""

import contextlib
import hashlib
import io
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import firmware_tool
import usb_backend
from serial_fixture import SimulatedSerial
from test_firmware_tool import application_bytes
from test_workflows import FirmwareModel


class BackendTests(unittest.TestCase):
    def invoke(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = usb_backend.main(['--port', '/dev/simulated', *arguments])
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_port_discovery_and_status_are_structured_and_read_only(self):
        port = SimpleNamespace(device='/dev/simulated', description='USB UART', vid=0x1A86, pid=0x7523)
        with patch('serial.tools.list_ports.comports', return_value=[port]):
            code, events = self.invoke('list-ports')
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]['ports'][0]['path'], '/dev/simulated')
        self.assertEqual(events[-1]['suggested_port'], '/dev/simulated')
        serial = SimulatedSerial()
        with patch('evilcrow_sdr.open_serial', return_value=(serial, '')):
            code, events = self.invoke('status')
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]['status']['Active'], 'NO')
        self.assertEqual(serial.commands, ['board_id_read', 'board_id_read', 'sdr_status'])
        self.assertFalse(serial.is_open)

    def test_rx_and_gnu_capture_output_match_received_samples(self):
        for operation in ('rx', 'gnuradio'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                serial = SimulatedSerial()
                output = Path(directory) / 'capture.raw'
                with patch('evilcrow_sdr.open_serial', return_value=(serial, '')):
                    code, events = self.invoke(operation, '--duration', '0.1', '--output', str(output))
                result = events[-1]
                self.assertEqual(code, 0)
                self.assertEqual(result['event'], 'result')
                self.assertEqual(result['output'], str(output))
                if operation == 'rx':
                    self.assertEqual(output.read_bytes(), serial.payload)
                    self.assertEqual(result['received_bytes'], len(serial.payload))
                    self.assertEqual(result['final_status']['Streaming'], 'NO')
                else:
                    expected = np.array([-1, 0, 127 / 128] * 32, dtype=np.complex64)
                    np.testing.assert_array_equal(np.fromfile(output, dtype=np.complex64), expected)
                    self.assertEqual(result['captured_samples'], len(expected))
                self.assertFalse(serial.active)
                self.assertFalse(serial.streaming)
                self.assertFalse(serial.is_open)

    def test_preview_hash_binds_flash_before_usb_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / 'firmware.bin'
            backup = Path(directory) / 'backup.bin'
            firmware.write_bytes(application_bytes(1))
            backup.write_bytes(FirmwareModel().flash)
            backup.with_suffix('.bin.sha256').write_text(hashlib.sha256(backup.read_bytes()).hexdigest())
            common = ['--firmware', str(firmware), '--backup', str(backup)]
            with patch.object(firmware_tool, 'connection') as connection:
                code, events = self.invoke('flash-preview', *common)
                self.assertEqual(code, 0)
                plan = next(event['plan'] for event in events if event['event'] == 'plan')
                firmware.write_bytes(application_bytes(2))
                code, events = self.invoke('flash', *common, '--expected-sha256', plan['sha256'], '--yes')
            self.assertEqual(code, 1)
            self.assertEqual(events[-1]['event'], 'error')
            self.assertIn('Firmware changed since confirmation', events[-1]['message'])
            connection.assert_not_called()

    def test_flash_failure_preserves_exact_device_error(self):
        message = 'Unreadable flash identification (ID 0xffffff). Power-cycle USB. No flash write was performed.'
        with patch.object(firmware_tool, 'flash_firmware', side_effect=ValueError(message)):
            code, events = self.invoke('flash', '--firmware', 'image.bin', '--backup', 'backup.bin',
                                       '--expected-sha256', 'a' * 64, '--yes')
        self.assertEqual(code, 1)
        self.assertEqual(events[-1], {'event': 'error', 'operation': 'flash', 'message': message})

    def test_bootloader_reboot_and_backup_emit_valid_events(self):
        model = FirmwareModel()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(firmware_tool, 'ESP32ROM', model.connect), \
                patch.object(firmware_tool, 'open_serial', return_value=(SimulatedSerial(), 'SD card mounted')):
            for command in ('bootloader', 'reboot'):
                code, events = self.invoke(command)
                self.assertEqual(code, 0)
                self.assertEqual(events[-1]['operation'], command)
            backup = Path(directory) / 'backup.bin'
            code, events = self.invoke('backup', '--output', str(backup))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]['sha256'], hashlib.sha256(model.flash).hexdigest())
        self.assertTrue(all(device._port.closed for device in model.devices))

    def test_closed_ui_pipe_does_not_interrupt_operation(self):
        class ClosedPipe:
            def write(self, data):
                raise BrokenPipeError('Parent exited')

        events = usb_backend.Events(ClosedPipe())
        events.emit('log', message='Writing application')
        self.assertTrue(events.closed)
        events.emit('result', ok=True)

    def test_rejected_rx_stop_emits_error_and_closes_the_port(self):
        serial = SimulatedSerial(reject={'rx_stop'})
        with patch('evilcrow_sdr.open_serial', return_value=(serial, '')):
            code, events = self.invoke('rx', '--duration', '0.1')
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]['event'], 'error')
        self.assertIn('RX stop was not acknowledged', events[-1]['message'])
        self.assertFalse(serial.is_open)
        self.assertFalse(serial.active)

    def test_cancel_before_urh_callback_binding_never_starts_rx(self):
        cancel = usb_backend.Cancellation()
        cancel.request()
        args = usb_backend.parser().parse_args(['--port', '/dev/simulated', 'urh'])
        with patch('evilcrow_sdr.open_serial') as connect, \
                contextlib.redirect_stdout(io.StringIO()):
            usb_backend.bridge('/dev/simulated', args, cancel, usb_backend.Events(io.StringIO()))
        connect.assert_not_called()

    def test_critical_operations_finish_after_sigint_and_restore_handler(self):
        script = '''
import os
import signal
from unittest.mock import patch
import firmware_tool
import usb_backend
from serial_fixture import SimulatedSerial
from test_workflows import FirmwareModel
model = FirmwareModel()
args = usb_backend.parser().parse_args()
previous = signal.getsignal(signal.SIGINT)
sent = False
def interrupt_once():
    global sent
    if not sent:
        sent = True
        os.kill(os.getpid(), signal.SIGINT)
def connect(*arguments, **kwargs):
    device = model.connect(*arguments, **kwargs)
    if args.command in ('bootloader', 'reboot'):
        device.connect = interrupt_once
    if args.command == 'backup':
        read = device.read_flash
        def read_with_signal(offset, size):
            interrupt_once()
            return read(offset, size)
        device.read_flash = read_with_signal
    return device
def write_with_signal(device, images, **kwargs):
    offset, image = images[0]
    model.flash[offset:offset + 16] = image[:16]
    interrupt_once()  # Signal after a simulated partial write, before completion.
    model.write_flash(device, images, **kwargs)
with patch.object(firmware_tool, 'ESP32ROM', connect), \\
     patch.object(firmware_tool, 'write_flash', write_with_signal), \\
     patch.object(firmware_tool, 'open_serial', return_value=(SimulatedSerial(), '')):
    result = usb_backend.main()
assert sent and result == 0
assert signal.getsignal(signal.SIGINT) == previous
assert all(device._port.closed for device in model.devices)
if args.command == 'flash':
    assert len(model.writes) == 1
if args.command == 'backup':
    assert args.output.read_bytes() == model.flash
raise SystemExit(result)
'''
        environment = os.environ.copy()
        environment['PYTHONPATH'] = os.pathsep.join((str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)))
        for command in ('flash', 'backup', 'bootloader', 'reboot'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                arguments = ['--port', '/dev/simulated', '--watch-stdin', command]
                if command == 'flash':
                    firmware = Path(directory) / 'firmware.bin'
                    backup = Path(directory) / 'backup.bin'
                    firmware.write_bytes(application_bytes(2))
                    backup.write_bytes(FirmwareModel().flash)
                    backup.with_suffix('.bin.sha256').write_text(hashlib.sha256(backup.read_bytes()).hexdigest())
                    arguments += ['--firmware', str(firmware), '--backup', str(backup),
                                  '--expected-sha256', hashlib.sha256(firmware.read_bytes()).hexdigest(), '--yes']
                elif command == 'backup':
                    arguments += ['--output', str(Path(directory) / 'backup.bin')]
                completed = subprocess.run([sys.executable, '-c', script, *arguments],
                                           input=b'', stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env=environment, timeout=10)
                self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                events = [json.loads(line) for line in completed.stdout.splitlines()]
                self.assertEqual(events[-1]['event'], 'result')
                self.assertTrue(events[-1]['ok'])
                self.assertFalse(events[-1]['cancelled'])

    def test_receive_sigint_and_parent_eof_cleanup_in_real_subprocess(self):
        script = '''
import sys
from unittest.mock import patch
import usb_backend
from serial_fixture import SimulatedSerial
serial = SimulatedSerial()
with patch('evilcrow_sdr.open_serial', return_value=(serial, '')):
    result = usb_backend.main()
assert not serial.is_open and not serial.active and not serial.streaming
raise SystemExit(result)
'''
        environment = os.environ.copy()
        environment['PYTHONPATH'] = os.pathsep.join((str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)))
        for command, cancellation in (('rx', 'sigint'), ('urh', 'eof'), ('urh', 'active_sigint')):
            with self.subTest(command=command, cancellation=cancellation):
                arguments = ['--port', '/dev/simulated', '--watch-stdin', command]
                if command == 'rx':
                    arguments += ['--duration', '60']
                else:
                    with socket.socket() as listener:
                        listener.bind(('127.0.0.1', 0))
                        port = listener.getsockname()[1]
                    arguments += ['--tcp-port', str(port)]
                process = subprocess.Popen([sys.executable, '-c', script, *arguments],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, bufsize=0, env=environment)
                observed = []
                pending = b''
                client = None
                try:
                    deadline = time.monotonic() + 8
                    ready = False
                    while time.monotonic() < deadline:
                        readable, _, _ = select.select([process.stdout], [], [], 0.1)
                        if not readable:
                            continue
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk:
                            break
                        lines = (pending + chunk).split(b'\n')
                        pending = lines.pop()
                        for line in lines:
                            event = json.loads(line)
                            observed.append(event)
                            ready |= event['event'] == 'ready' or 'RX streaming started' in event.get('message', '')
                        if ready:
                            break
                    self.assertTrue(ready, observed)
                    if cancellation == 'active_sigint':
                        client = socket.create_connection(('127.0.0.1', observed[-1]['tcp_port']), timeout=3)
                        header = b''
                        while len(header) < 4:
                            part = client.recv(4 - len(header))
                            if not part:
                                break
                            header += part
                        self.assertEqual(header, b'RTL0')
                        # Wait for receive startup so Stop must clean up an
                        # active TCP stream, not just the accepting listener.
                        deadline = time.monotonic() + 3
                        client.settimeout(3)
                        payload = b''
                        while len(payload) < 200 and time.monotonic() < deadline:
                            payload += client.recv(256)
                        self.assertGreaterEqual(len(payload), 200)
                    if cancellation in ('sigint', 'active_sigint'):
                        process.send_signal(signal.SIGINT)
                        # Keep stdin open to prove SIGINT itself is sufficient
                        # and the stdin watcher cannot hold shutdown open.
                        process.wait(timeout=8)
                        process.stdin.close()
                        process.stdin = None
                    else:
                        process.stdin.close()
                        process.stdin = None
                    remainder, error = process.communicate(timeout=8)
                    observed.extend(json.loads(line) for line in (pending + remainder).splitlines())
                    self.assertEqual(process.returncode, 130, error.decode())
                    self.assertEqual(observed[-1]['event'], 'result')
                    self.assertTrue(observed[-1]['cancelled'])
                finally:
                    if client:
                        client.close()
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main()
