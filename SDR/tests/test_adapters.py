"""Real localhost TCP and capture-file checks against simulated USB firmware."""

import contextlib
import importlib.util
import io
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from gnuradio_source import EvilCrowSource, standalone_test
from serial_fixture import SimulatedSerial
from sdr_launcher import SDRLauncherGUI
from urh_bridge import URHBridge


def receive_exact(connection, count):
    result = bytearray()
    while len(result) < count:
        packet = connection.recv(count - len(result))
        if not packet:
            raise EOFError('Bridge disconnected before sending samples')
        result.extend(packet)
    return bytes(result)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def serial_device(self, **kwargs):
        serial = SimulatedSerial(**kwargs)
        connection = patch('evilcrow_sdr.open_serial', return_value=(serial, 'SD card mounted'))
        connection.start()
        self.addCleanup(connection.stop)
        return serial

    def test_gnuradio_retune_preserves_original_error_when_restart_also_fails(self):
        serial = self.serial_device()
        source = EvilCrowSource('/dev/simulated')
        self.addCleanup(source.close)
        self.assertTrue(source.connect())
        self.assertTrue(source.start_streaming())
        serial.reject.add('rx_start')
        with patch.object(source.device, 'set_frequency', side_effect=ValueError('Tuning failed')), \
                self.assertLogs('gnuradio_source', level='ERROR'):
            with self.assertRaisesRegex(ValueError, 'Tuning failed'):
                source.set_frequency(434000000)
        self.assertFalse(source.streaming)

    def test_gnuradio_message_handler_reports_restart_failure(self):
        # Load the optional wrapper against a minimal GNU runtime interface.
        # The real receiver and UART model still exercise stop/config/restart.
        spec = importlib.util.spec_from_file_location(
            'test_gr_wrapper', Path(__file__).parents[1] / 'gnuradio_source.py')
        wrapper = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            'gnuradio': SimpleNamespace(gr=SimpleNamespace(sync_block=object)),
            'pmt': SimpleNamespace(is_number=lambda value: True, to_double=float),
        }):
            spec.loader.exec_module(wrapper)
        serial = self.serial_device()
        source = EvilCrowSource('/dev/simulated')
        self.addCleanup(source.close)
        self.assertTrue(source.connect())
        self.assertTrue(source.start_streaming())
        serial.reject.add('rx_start')
        block = wrapper.EvilCrowGRSource.__new__(wrapper.EvilCrowGRSource)
        block.source = source
        with self.assertLogs('test_gr_wrapper', level='ERROR') as logs:
            block._handle_freq(434000000)
        self.assertIn('Frequency update failed', '\n'.join(logs.output))
        self.assertFalse(source.streaming)

    def test_gnuradio_initial_configuration_rejection_closes_port(self):
        serial = self.serial_device(reject={'set_modulation'})
        source = EvilCrowSource('/dev/simulated')
        self.addCleanup(source.close)
        self.assertFalse(source.connect())
        self.assertFalse(serial.is_open)
        self.assertFalse(serial.active)

    def test_gnuradio_retunes_between_receive_sessions_without_text_samples(self):
        serial = self.serial_device()
        source = EvilCrowSource('/dev/simulated')
        self.addCleanup(source.close)
        self.assertTrue(source.connect())
        self.assertTrue(source.start_streaming())
        expected = np.array([-1, 0, 127 / 128] * 32, dtype=np.complex64)
        np.testing.assert_array_equal(source.read_samples(len(expected)), expected)
        self.assertTrue(source.set_frequency(434000000))
        self.assertEqual(serial.frequency, 434000000)
        self.assertTrue(source.streaming)
        np.testing.assert_array_equal(source.read_samples(len(expected)), expected)
        self.assertTrue(source.set_modulation(1))
        self.assertEqual(serial.modulation, 1)
        self.assertTrue(source.set_bandwidth(203))
        self.assertEqual(serial.bandwidth, 203)
        self.assertFalse(serial.reconfiguration_during_rx)
        self.assertEqual(serial.commands.count('rx_start'), 4)
        self.assertTrue(source.stop_streaming())
        source.close()
        self.assertFalse(serial.streaming)
        self.assertFalse(serial.active)
        self.assertFalse(serial.is_open)

    def test_gnuradio_rejected_stop_never_saves_a_successful_capture(self):
        serial = self.serial_device(reject={'rx_stop'})
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                with self.assertRaisesRegex(RuntimeError, 'stop was not acknowledged'):
                    standalone_test('/dev/simulated', 433920000, 0.01)
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                os.chdir(previous)
        self.assertFalse(serial.is_open)
        self.assertFalse(serial.active)

    def test_gnuradio_empty_receive_never_fabricates_samples(self):
        serial = self.serial_device(payload=b'')
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                with self.assertRaisesRegex(RuntimeError, 'No samples received'):
                    standalone_test('/dev/simulated', 433920000, 0.01)
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                os.chdir(previous)
        self.assertFalse(serial.is_open)

    def test_rtl_tcp_stop_rejection_during_retune_releases_receiver(self):
        serial = self.serial_device()
        bridge = URHBridge('/dev/simulated', tcp_port=0)
        self.addCleanup(bridge.cleanup)
        self.assertTrue(bridge.connect_device())
        self.assertTrue(bridge.start_server())
        self.assertEqual(serial.data_rate, 3793)
        self.assertEqual(serial.modulation, 2)
        self.assertEqual(serial.bandwidth, 650)
        client = socket.create_connection(bridge.server.getsockname(), timeout=3)
        self.addCleanup(client.close)
        accepted, address = bridge.server.accept()
        worker = threading.Thread(target=bridge.handle_client, args=(accepted, address), daemon=True)
        worker.start()
        try:
            receive_exact(client, 12 + len(serial.payload) * 2)
            serial.reject.add('rx_stop')
            client.sendall(struct.pack('>BI', 1, 434000000))
            self.assertTrue(serial.wait_for('sdr_disable'))
        finally:
            client.close()
            worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertFalse(serial.is_open)
        self.assertFalse(serial.active)
        self.assertFalse(serial.streaming)
        self.assertNotIn('set_freq 434000000', serial.commands)
        self.assertIn('stop was not acknowledged', self.output.getvalue())

    def test_rtl_tcp_disconnect_and_reconnect_start_fresh_receive_sessions(self):
        serial = self.serial_device()
        bridge = URHBridge('/dev/simulated', tcp_port=0)
        self.addCleanup(bridge.cleanup)
        self.assertTrue(bridge.connect_device())
        self.assertTrue(bridge.start_server())
        for payload in (b'\x00\x01\xff', b'\x80\x81\xfe'):
            serial.payload = payload
            client = socket.create_connection(bridge.server.getsockname(), timeout=3)
            accepted, address = bridge.server.accept()
            worker = threading.Thread(target=bridge.handle_client, args=(accepted, address), daemon=True)
            worker.start()
            try:
                self.assertEqual(receive_exact(client, 12), b'RTL0' + struct.pack('>II', 1, 1))
                expected = b''.join(bytes((byte, 127)) for byte in payload)
                self.assertEqual(receive_exact(client, len(expected)), expected)
            finally:
                client.close()
                worker.join(timeout=4)
            self.assertFalse(worker.is_alive())
            self.assertFalse(serial.streaming)
            self.assertTrue(serial.is_open)
        self.assertEqual(serial.commands.count('rx_start'), 2)
        self.assertEqual(serial.commands.count('rx_stop'), 2)

    def test_gnuradio_standalone_saves_exact_received_complex_samples(self):
        serial = self.serial_device()
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                standalone_test('/dev/simulated', 433920000, 0.05)
                samples = np.fromfile(Path(directory) / 'capture_434MHz.raw', dtype=np.complex64)
            finally:
                os.chdir(previous)
        expected = np.array([-1, 0, 127 / 128] * 32, dtype=np.complex64)
        np.testing.assert_array_equal(samples, expected)
        self.assertFalse(serial.is_open)
        self.assertFalse(serial.streaming)
        self.assertFalse(serial.active)

    def test_gnuradio_rejected_start_never_saves_a_zero_filled_capture(self):
        serial = self.serial_device(reject={'rx_start'})
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                with self.assertRaisesRegex(RuntimeError, 'start RX'):
                    standalone_test('/dev/simulated', 433920000, 0.01)
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                os.chdir(previous)
        self.assertFalse(serial.is_open)

    def test_rtl_tcp_fragmented_and_batched_commands_keep_samples_clean(self):
        serial = self.serial_device()
        bridge = URHBridge('/dev/simulated', tcp_port=0)
        self.addCleanup(bridge.cleanup)
        self.assertTrue(bridge.connect_device())
        self.assertTrue(bridge.start_server())
        client = socket.create_connection(bridge.server.getsockname(), timeout=3)
        self.addCleanup(client.close)
        accepted, address = bridge.server.accept()
        errors = []

        def serve():
            try:
                bridge.handle_client(accepted, address)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            self.assertEqual(receive_exact(client, 12), b'RTL0' + struct.pack('>II', 1, 1))
            expected = b''.join(bytes((byte, 127)) for byte in serial.payload)
            self.assertEqual(receive_exact(client, len(expected)), expected)
            frequency = struct.pack('>BI', 1, 434000000)
            client.sendall(frequency[:2])
            # Confirm the connection keeps making progress while only a command
            # prefix is available; the later recv must retain those two bytes.
            receive_exact(client, 128)
            client.sendall(frequency[2:] + struct.pack('>BI', 2, 10000) +
                           struct.pack('>Bi', 4, -30))
            self.assertTrue(serial.wait_for('set_freq 434000000'))
            self.assertTrue(serial.wait_for('set_sample_rate 10000'))
            self.assertTrue(serial.wait_for('set_gain -3'))
            self.assertFalse(serial.reconfiguration_during_rx)
            network = receive_exact(client, 1024)
            self.assertTrue(all(value in (0, 127, 128, 255) for value in network[::2]))
            self.assertEqual(set(network[1::2]), {127})
            bridge.handle_rtl_command(struct.pack('>BI', 2, 2400000))
            self.assertNotIn('set_sample_rate 2400000', serial.commands)
            self.assertIn('600–500000 Baud', self.output.getvalue())
        finally:
            client.close()
            worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(serial.streaming)
        bridge.cleanup()
        self.assertFalse(serial.active)
        self.assertFalse(serial.is_open)

    def test_rtl_tcp_header_send_failure_closes_client_without_unbound_worker(self):
        serial = self.serial_device()
        bridge = URHBridge('/dev/simulated', tcp_port=0)
        self.addCleanup(bridge.cleanup)
        self.assertTrue(bridge.connect_device())

        class FailedClient:
            closed = False

            def setsockopt(self, *args):
                pass

            def settimeout(self, *args):
                pass

            def sendall(self, data):
                raise ConnectionResetError('Disconnected before header')

            def close(self):
                self.closed = True

        client = FailedClient()
        bridge.handle_client(client, ('127.0.0.1', 1234))
        self.assertTrue(client.closed)
        self.assertFalse(serial.streaming)

    def test_rtl_tcp_stop_requested_during_header_prevents_rx_start(self):
        serial = self.serial_device()
        bridge = URHBridge('/dev/simulated')
        self.addCleanup(bridge.cleanup)
        self.assertTrue(bridge.connect_device())
        launcher = SDRLauncherGUI.__new__(SDRLauncherGUI)
        launcher._bridge = bridge
        launcher._log = lambda message: None
        launcher.stop_btn = SimpleNamespace(config=lambda **options: None)

        class CancelledClient:
            closed = False

            def setsockopt(self, *args):
                pass

            def settimeout(self, *args):
                pass

            def sendall(self, data):
                launcher._stop()

            def shutdown(self, *args):
                pass

            def close(self):
                self.closed = True

        client = CancelledClient()
        bridge.handle_client(client, ('127.0.0.1', 1234))
        self.assertTrue(client.closed)
        self.assertTrue(launcher._stop_flag)
        self.assertNotIn('rx_start', serial.commands)
        self.assertFalse(serial.streaming)


if __name__ == '__main__':
    unittest.main()
