"""Stateful receive-only serial fixture; never opens a physical port."""

import threading
import time


class SimulatedSerial:
    def __init__(self, payload=b'\x00\x80\xff' * 32, reject=()):
        self.payload = payload
        self.reject = set(reject)
        self.commands = []
        self.pending = bytearray()
        self.is_open = True
        self.active = False
        self.streaming = False
        self.frequency = 433920000
        self.modulation = 2
        self.bandwidth = 650.0
        self.data_rate = 3793
        self.reconfiguration_during_rx = []
        self._condition = threading.Condition()

    @property
    def in_waiting(self):
        with self._condition:
            return len(self.pending)

    def write(self, data):
        command = data.decode('ascii').strip()
        name, _, argument = command.partition(' ')
        with self._condition:
            self.commands.append(command)
            reply = b'HACKRF_SUCCESS\r\n'
            if name in self.reject:
                reply = b'HACKRF_ERROR\r\nSimulated rejection\r\n'
            elif name.startswith('set_') and self.streaming:
                self.reconfiguration_during_rx.append(command)
                reply = b'HACKRF_ERROR\r\nStop RX before reconfiguration\r\n'
            elif name == 'board_id_read':
                reply += b'Board ID: EvilCrow_RF_v2_SDR\r\n'
            elif name == 'sdr_enable':
                self.active = True
            elif name == 'sdr_disable':
                self.active = self.streaming = False
            elif name == 'sdr_status':
                reply += (
                    f'Active: {"YES" if self.active else "NO"}\r\n'
                    f'Frequency: {self.frequency / 1e6:.3f} MHz\r\n'
                    f'Streaming: {"YES" if self.streaming else "NO"}\r\n'
                    f'Bytes streamed: {len(self.payload)}\r\n').encode()
            elif name == 'set_freq':
                self.frequency = int(argument)
            elif name == 'set_modulation':
                self.modulation = int(argument)
            elif name == 'set_bandwidth':
                self.bandwidth = float(argument)
            elif name == 'set_sample_rate':
                self.data_rate = int(argument)
            elif name == 'set_gain':
                pass
            elif name == 'rx_start' and self.active:
                self.streaming = True
                reply += b'RX streaming started\r\n' + self.payload
            elif name == 'rx_stop':
                self.streaming = False
            else:
                reply = b'HACKRF_ERROR\r\nUnknown command or inactive receiver\r\n'
            self.pending.extend(reply)
            self._condition.notify_all()
        return len(data)

    def read(self, size=1):
        with self._condition:
            result = bytes(self.pending[:size])
            del self.pending[:size]
            return result

    def reset_input_buffer(self):
        with self._condition:
            self.pending.clear()

    def close(self):
        self.is_open = False

    def wait_for(self, command, timeout=3):
        deadline = time.monotonic() + timeout
        with self._condition:
            while command not in self.commands:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True
