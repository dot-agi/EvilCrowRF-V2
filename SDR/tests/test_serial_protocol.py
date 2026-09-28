"""Regressions for replies captured from Senape3000's serial firmware."""

import unittest
import threading
import io
from types import SimpleNamespace
from unittest.mock import Mock, patch

from evilcrow_sdr import EvilCrowSDR
from serial_utils import detect_evilcrow_port


class ReplaySerial:
    def __init__(self, responses):
        self.responses = responses
        self.pending = b''
        self.is_open = True
        self.commands = []
        self.payload_drained = threading.Event()

    @property
    def in_waiting(self):
        return len(self.pending)

    def write(self, command):
        self.commands.append(command)
        if command == b'rx_start\n':
            self.payload_drained.clear()
        self.pending += self.responses[command]

    def flush(self):
        pass

    def readline(self):
        line, separator, self.pending = self.pending.partition(b'\n')
        return line + separator

    def read(self, size):
        data, self.pending = self.pending[:size], self.pending[size:]
        if not self.pending:
            self.payload_drained.set()
        return data

    def close(self):
        self.is_open = False


def client(serial):
    device = EvilCrowSDR.__new__(EvilCrowSDR)
    device.ser = serial
    device.timeout = 0.4
    device._streaming = False
    device._stream_thread = None
    return device


class SimulatedClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ContinuousSerial(io.RawIOBase):
    """Supply no-LF RX bytes for one simulated second, without real delays."""
    is_open = True

    def __init__(self, clock):
        self.clock = clock

    @property
    def in_waiting(self):
        return int(self.clock.now < 1.0)

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    def read(self, size=1):
        self.clock.sleep(0.005)
        return b'\x00' * size if self.clock.now < 1.0 else b''


class DelayedSerial(ReplaySerial):
    def __init__(self, clock):
        super().__init__({b'board_id_read\n': b''})
        self.clock = clock
        self.chunks = [
            (0.04, b'HACKRF_SU'),
            (0.14, b'CCESS\r\n'),
            (0.18, b'Board ID: EvilCrow_RF_v2_SDR\r\n'),
        ]

    @property
    def in_waiting(self):
        while self.chunks and self.chunks[0][0] <= self.clock.now:
            self.pending += self.chunks.pop(0)[1]
        return len(self.pending)


class SerialProtocolTests(unittest.TestCase):
    def test_spectrum_waits_for_completion_after_a_quiet_interval(self):
        clock = SimulatedClock()
        serial = DelayedSerial(clock)
        command = b'spectrum_scan 420 440 100\n'
        serial.responses[command] = b'HACKRF_SUCCESS\nScanning...\n'
        serial.chunks = [(0.3, b'Scan complete: 200 points\n')]
        device = client(serial)
        with patch('evilcrow_sdr.time.monotonic', clock.monotonic), \
                patch('evilcrow_sdr.time.sleep', clock.sleep):
            reply = device.send_command(command.decode().strip())
        self.assertIn('Scan complete: 200 points', reply)
        self.assertGreaterEqual(clock.now, 0.3)
        self.assertEqual(serial.pending, b'')
        self.assertEqual(serial.chunks, [])

    def test_spectrum_without_completion_times_out(self):
        clock = SimulatedClock()
        serial = ReplaySerial({b'spectrum_scan\n': b'HACKRF_SUCCESS\nScanning...\n'})
        device = client(serial)
        with patch('evilcrow_sdr.time.monotonic', clock.monotonic), \
                patch('evilcrow_sdr.time.sleep', clock.sleep):
            with self.assertRaisesRegex(TimeoutError, 'Spectrum scan did not finish'):
                device.send_command('spectrum_scan')
        self.assertAlmostEqual(clock.now, device.timeout)

    def test_close_cancels_worker_read_before_commands_or_port_close(self):
        serial = ReplaySerial({b'rx_stop\n': b'HACKRF_SUCCESS\n',
                               b'sdr_disable\n': b'HACKRF_SUCCESS\n'})
        device = client(serial)
        device._manage_sdr = True
        release_worker = threading.Event()
        worker = threading.Thread(target=release_worker.wait, daemon=True)
        serial.cancel_read = release_worker.set
        original_close = serial.close
        original_write = serial.write

        def write(command):
            self.assertFalse(worker.is_alive())
            original_write(command)

        def close():
            self.assertFalse(worker.is_alive())
            original_close()

        serial.write, serial.close = write, close
        device._streaming, device._stream_thread = True, worker
        worker.start()
        try:
            device.close()
        finally:
            release_worker.set()
            worker.join(timeout=1)
        self.assertEqual(serial.commands, [b'rx_stop\n', b'sdr_disable\n'])
        self.assertFalse(serial.is_open)

    def test_close_reports_stuck_worker_without_closing_owned_port(self):
        serial = ReplaySerial({})
        device = client(serial)
        device._manage_sdr = True
        device._streaming = True
        device._stream_thread = Mock()
        device._stream_thread.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, 'RX worker did not stop'):
            device.close()
        self.assertTrue(serial.is_open)
        self.assertEqual(serial.commands, [])

    def test_close_reports_stop_failure_after_attempting_disable(self):
        serial = ReplaySerial({b'rx_stop\n': b'HACKRF_ERROR\n',
                               b'sdr_disable\n': b'HACKRF_SUCCESS\n'})
        device = client(serial)
        device._manage_sdr = True
        device._streaming = True
        with self.assertRaisesRegex(RuntimeError, 'RX stop was not acknowledged'):
            device.close()
        self.assertEqual(serial.commands, [b'rx_stop\n', b'sdr_disable\n'])
        self.assertFalse(serial.is_open)

    def test_continuous_bytes_without_lf_respect_command_deadline(self):
        clock = SimulatedClock()
        device = client(ContinuousSerial(clock))
        device.timeout = 0.05
        with patch('evilcrow_sdr.time.monotonic', clock.monotonic), \
                patch('evilcrow_sdr.time.sleep', clock.sleep):
            reply = device.send_command('rx_stop')
        self.assertTrue(reply)
        self.assertLessEqual(clock.now, 0.06)

    def test_delayed_fragmented_ack_and_body_are_reassembled(self):
        clock = SimulatedClock()
        device = client(DelayedSerial(clock))
        with patch('evilcrow_sdr.time.monotonic', clock.monotonic), \
                patch('evilcrow_sdr.time.sleep', clock.sleep):
            reply = device.send_command('board_id_read')
        self.assertEqual(reply, 'HACKRF_SUCCESS\nBoard ID: EvilCrow_RF_v2_SDR')
        self.assertLessEqual(clock.now, device.timeout)

    def test_worker_that_cannot_stop_keeps_sole_serial_read_ownership(self):
        serial = ReplaySerial({})
        device = client(serial)
        release_worker = threading.Event()
        worker = threading.Thread(target=release_worker.wait, daemon=True)
        device._streaming = True
        device._stream_thread = worker
        worker.start()
        try:
            with self.assertRaisesRegex(RuntimeError, 'RX worker did not stop'):
                device.stop_rx()
            with self.assertRaisesRegex(RuntimeError, 'Stop RX'):
                device.send_command('sdr_status')
            self.assertEqual(serial.commands, [])
        finally:
            release_worker.set()
            worker.join(timeout=1)

    def test_status_body_after_ack_is_consumed_before_next_command(self):
        serial = ReplaySerial({
            b'sdr_status\n': b'HACKRF_SUCCESS\r\nActive: YES\nFrequency: 433.920 MHz\nStreaming: NO\nBytes streamed: 0\n',
            b'board_id_read\n': b'HACKRF_SUCCESS\r\nBoard ID: EvilCrow_RF_v2_SDR\r\nSDR Active: YES\n',
        })
        device = client(serial)
        self.assertEqual(device.get_status()['Frequency'], '433.920 MHz')
        identity = device.get_device_info()
        self.assertIn('Board ID: EvilCrow_RF_v2_SDR', identity)
        self.assertNotIn('Bytes streamed', identity)
        self.assertEqual(serial.pending, b'')

    def test_rx_start_consumes_trailer_but_preserves_binary_payload(self):
        serial = ReplaySerial({b'rx_start\n': b'HACKRF_SUCCESS\r\nRX streaming started\r\n\x00\xff\x01'})
        self.assertIn('SUCCESS', client(serial).send_command('rx_start'))
        self.assertEqual(serial.pending, b'\x00\xff\x01')

    def test_new_rx_session_returns_only_its_own_payload(self):
        trailer = b'HACKRF_SUCCESS\r\nRX streaming started\r\n'
        serial = ReplaySerial({
            b'board_id_read\n': b'HACKRF_SUCCESS\r\nBoard ID: EvilCrow_RF_v2_SDR\r\n',
            b'sdr_enable\n': b'HACKRF_SUCCESS\r\n',
            b'rx_start\n': trailer + b'A' * 300,
            b'rx_stop\n': b'HACKRF_SUCCESS\r\n',
            b'sdr_disable\n': b'HACKRF_SUCCESS\r\n',
        })
        with patch('evilcrow_sdr.open_serial', return_value=(serial, '')):
            with EvilCrowSDR('/dev/cu.fake', timeout=0.4) as device:
                self.assertTrue(device.start_rx())
                self.assertTrue(serial.payload_drained.wait(timeout=1))
                self.assertEqual(device.read_raw(255, timeout=0.2), b'A' * 255)
                self.assertTrue(device.stop_rx())
                # Leave part of the first capture unread, then receive a new one.
                serial.responses[b'rx_start\n'] = trailer + b'\x00\xff\x01'
                self.assertTrue(device.start_rx())
                self.assertEqual(device.read_raw(3, timeout=0.2), b'\x00\xff\x01')

    def test_partial_reads_preserve_all_rx_payload_bytes(self):
        serial = ReplaySerial({
            b'board_id_read\n': b'HACKRF_SUCCESS\r\nBoard ID: EvilCrow_RF_v2_SDR\r\n',
            b'sdr_enable\n': b'HACKRF_SUCCESS\r\n',
            b'rx_start\n': b'HACKRF_SUCCESS\r\nRX streaming started\r\n\x00\xff\x01\x02\x03',
            b'rx_stop\n': b'HACKRF_SUCCESS\r\n',
            b'sdr_disable\n': b'HACKRF_SUCCESS\r\n',
        })
        with patch('evilcrow_sdr.open_serial', return_value=(serial, '')):
            with EvilCrowSDR('/dev/cu.fake', timeout=0.4) as device:
                self.assertTrue(device.start_rx())
                self.assertEqual(device.read_raw(2, timeout=0.2), b'\x00\xff')
                self.assertEqual(device.read_raw(3, timeout=0.2), b'\x01\x02\x03')

    def test_error_is_not_a_successful_board_identity_and_releases_port(self):
        serial = ReplaySerial({b'board_id_read\n': b'HACKRF_ERROR\r\nUnknown command\r\n'})
        with patch('evilcrow_sdr.open_serial', return_value=(serial, '')):
            with self.assertRaises(ConnectionError):
                EvilCrowSDR('/dev/cu.fake', timeout=0.4)
        self.assertFalse(serial.is_open)
        self.assertEqual(serial.commands, [b'board_id_read\n'])

    def test_busy_device_does_not_get_reported_as_connected(self):
        serial = ReplaySerial({
            b'board_id_read\n': b'HACKRF_SUCCESS\r\nBoard ID: EvilCrow_RF_v2_SDR\r\n',
            b'sdr_enable\n': b'HACKRF_ERROR\r\nCC1101 busy\r\n',
        })
        with patch('evilcrow_sdr.open_serial', return_value=(serial, '')):
            with self.assertRaises(ConnectionError):
                EvilCrowSDR('/dev/cu.fake', timeout=0.4)
        self.assertFalse(serial.is_open)

    def test_read_only_connection_never_enables_or_disables_sdr(self):
        serial = ReplaySerial({
            b'board_id_read\n': b'HACKRF_SUCCESS\r\nBoard ID: EvilCrow_RF_v2_SDR\r\n',
        })
        with patch('evilcrow_sdr.open_serial', return_value=(serial, '')):
            with EvilCrowSDR('/dev/cu.fake', timeout=0.4, auto_enable=False):
                pass
        self.assertEqual(serial.commands, [b'board_id_read\n'])
        self.assertFalse(serial.is_open)

    def test_mac_usb_uart_is_detected_but_ambiguous_ports_are_not_selected(self):
        uart = SimpleNamespace(device='/dev/cu.usbserial-1130', description='USB Serial', vid=0x1A86, pid=0x7523)
        bluetooth = SimpleNamespace(device='/dev/cu.Bluetooth-Incoming-Port', description='n/a', vid=None, pid=None)
        with patch('serial.tools.list_ports.comports', return_value=[bluetooth, uart]):
            self.assertEqual(detect_evilcrow_port(), uart.device)
        with patch('serial.tools.list_ports.comports', return_value=[uart, uart]):
            self.assertIsNone(detect_evilcrow_port())


if __name__ == '__main__':
    unittest.main()
