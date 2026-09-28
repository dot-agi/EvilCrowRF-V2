#!/usr/bin/env python3
"""
EvilCrow RF v2 — URH Bridge (RTL-TCP compatible)

Acts as an RTL-TCP server so Universal Radio Hacker (URH) can connect
to the EvilCrow RF v2 as if it were an RTL-SDR dongle.

How it works:
  1. Connects to EvilCrow via USB serial.
  2. Starts a TCP server on localhost:1234.
  3. Sends the RTL-TCP DongleInfo header when URH connects.
  4. Translates RTL-TCP commands (set_freq, set_rate, set_gain) to
     EvilCrow serial commands.
  5. Reads raw demodulated bytes from CC1101 FIFO and streams them
     as 8-bit unsigned IQ samples to URH.

IMPORTANT: The CC1101 does NOT produce raw IQ data. The bytes streamed
are demodulated FIFO data, not true I/Q. URH will display signal
activity but spectral analysis will be limited. This is useful for:
  - Seeing when signals are present
  - Recording demodulated data for protocol analysis
  - Basic signal detection and timing analysis

Usage:
    python urh_bridge.py                  # Auto-detect port
    python urh_bridge.py --port COM8      # Specify serial port
    python urh_bridge.py --tcp-port 1235  # Custom TCP port

Then in URH: File > New Project > "RTL-TCP" source > localhost:1234

Requirements:
    pip install pyserial
"""

# Module version
VERSION = "1.0.1"

import logging
import socket

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    raise ImportError(
        "pyserial is required: pip install pyserial"
    )

import struct
import threading
import time
import sys
import argparse
import select
from serial_utils import detect_evilcrow_port
from evilcrow_sdr import EvilCrowSDR

log = logging.getLogger(__name__)


def find_evilcrow_port() -> str:
    port = detect_evilcrow_port()
    if port:
        return port
    raise RuntimeError('Specify --port; a single USB UART could not be identified')


class URHBridge:
    """RTL-TCP compatible bridge for EvilCrow RF v2."""

    def __init__(self, serial_port: str, tcp_port: int = 1234):
        self.serial_port = serial_port
        self.tcp_port = tcp_port
        self.ser: serial.Serial = None
        self.server: socket.socket = None
        self.client: socket.socket = None
        self.running = False
        self.device = None
        self._device_lock = threading.RLock()
        self._rx_active = False
        self._stop_requested = threading.Event()

    def log(self, msg: str):
        print(f'[{time.strftime("%H:%M:%S")}] {msg}')

    def connect_device(self) -> bool:
        """Open serial connection to EvilCrow and enable SDR mode."""
        try:
            self.log(f'Connecting to {self.serial_port}...')
            self.device = EvilCrowSDR(self.serial_port)
            self.ser = self.device.ser
            if not (self.device.set_frequency(433.92e6) and
                    self.device.set_modulation('ASK') and self.device.set_bandwidth(650) and
                    self.device.set_data_rate(3793.72) and self.device.set_gain(15)):
                raise RuntimeError('Initial receiver configuration failed')
            return True
        except Exception as e:
            self.log(f'Connection failed: {e}')
            self.cleanup()
            return False

    def start_server(self) -> bool:
        """Start TCP server for URH connections."""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind(('127.0.0.1', self.tcp_port))
            self.server.listen(1)
            self.server.settimeout(0.2)
            self.log(f'TCP server listening on 127.0.0.1:{self.tcp_port}')
            self.log('In URH: File > New > "RTL-TCP" source > localhost:{}'.format(
                self.tcp_port))
            return True
        except Exception as e:
            self.log(f'Server start failed: {e}')
            return False

    def handle_rtl_command(self, data: bytes):
        """Handle RTL-TCP 5-byte commands from URH."""
        if len(data) != 5:
            return
        cmd = data[0]
        param = struct.unpack('>I', data[1:5])[0]
        if cmd == 0x01:
            operation = lambda: self.device.set_frequency(param)
            label = f'frequency {param} Hz'
        elif cmd == 0x02:
            if not 600 <= param <= 500000:
                self.log(f'Rejected RTL sample-rate request {param}: CC1101 demodulation '
                         'supports 600–500000 Baud. IQ sample-clock settings do not apply.')
                return
            self.log(f'RTL sample-rate request sets CC1101 demodulation to {param} Baud.')
            operation = lambda: self.device.set_data_rate(param)
            label = f'data rate {param} Baud'
        elif cmd == 0x04:
            gain = int(struct.unpack('>i', data[1:5])[0] / 10)
            operation = lambda: self.device.set_gain(gain)
            label = f'gain {gain} dB'
        elif cmd in (0x03, 0x08):  # RTL tuner gain mode / AGC mode
            self.log('CC1101 uses automatic gain control.')
            return
        else:
            self.log(f'Unsupported RTL command: 0x{cmd:02X} param={param}')
            return

        # Only the shared library reads UART. Pause RX before textual replies
        # so configuration ACKs cannot be forwarded as demodulated samples.
        with self._device_lock:
            resume = self._rx_active
            if resume:
                self._stop_rx()
            try:
                if not operation():
                    self.log(f'Receiver rejected {label}; keeping its previous setting.')
            finally:
                if resume and self.running and not self._stop_requested.is_set():
                    if not self.device.start_rx():
                        raise RuntimeError('Receiver did not acknowledge RX restart')
                    self._rx_active = True

    def _stop_rx(self):
        if self._rx_active:
            if not self.device.stop_rx():
                raise RuntimeError('RX stop was not acknowledged; restart the device')
            self._rx_active = False

    def handle_client(self, client: socket.socket, addr):
        """Handle a URH client connection."""
        self.client = client
        self.log(f'URH connected from {addr[0]}:{addr[1]}')
        sample_count = 0
        try:
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client.settimeout(2.0)  # A stalled reader must not hold RX indefinitely.

            # Send RTL-TCP DongleInfo header (12 bytes)
            # Magic: "RTL0" | Tuner type: uint32 | Gain count: uint32
            header = b'RTL0' + struct.pack('>II', 1, 1)
            client.sendall(header)
            self.log('Sent RTL-TCP header (12 bytes)')

            with self._device_lock:
                if self._stop_requested.is_set():
                    return
                if not self.device.start_rx():
                    raise RuntimeError('Receiver did not acknowledge RX start')
                self._rx_active = True
                self.running = True
            commands = bytearray()
            last_log = time.monotonic()
            while self.running and not self._stop_requested.is_set():
                ready, _, _ = select.select([client], [], [], 0)
                if ready:
                    data = client.recv(1024)
                    if not data:
                        break
                    commands.extend(data)
                    while len(commands) >= 5 and not self._stop_requested.is_set():
                        self.handle_rtl_command(bytes(commands[:5]))
                        del commands[:5]
                with self._device_lock:
                    if not self.running:
                        break
                    raw = self.device.read_raw(512, timeout=0.02)
                if raw:
                    iq = bytearray(len(raw) * 2)
                    iq[::2] = raw
                    iq[1::2] = b'\x7f' * len(raw)
                    client.sendall(iq)
                    sample_count += len(raw)
                else:
                    client.sendall(b'\x7f\x7f' * 64)
                if time.monotonic() - last_log >= 5:
                    self.log(f'  Streamed {sample_count} demodulated samples')
                    last_log = time.monotonic()
        except (OSError, RuntimeError) as error:
            self.log(f'Client session ended: {error}')
        finally:
            self.running = False
            with self._device_lock:
                try:
                    self._stop_rx()
                except Exception as error:
                    self.log(f'Receiver cleanup failed: {error}')
                    self.device.close()
                    self._rx_active = False
            try:
                client.close()
            except Exception:
                pass
            self.client = None
            self.log(f'URH disconnected ({sample_count} demodulated samples).')

    def run(self):
        """Main entry point: connect device, start server, accept clients."""
        if not self.connect_device():
            return False
        if not self.start_server():
            self.cleanup()
            return False

        self.log('Bridge ready. Waiting for URH...')
        try:
            while not self._stop_requested.is_set():
                try:
                    client, addr = self.server.accept()
                except socket.timeout:
                    continue
                self.handle_client(client, addr)
                self.log('Ready for next connection...')
        except KeyboardInterrupt:
            self.log('Shutting down.')
        finally:
            self.cleanup()

    def request_stop(self):
        """Cancel startup or an active client without blocking the GUI thread."""
        self._stop_requested.set()
        self.running = False
        if self.client:
            try:
                self.client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def cleanup(self):
        self.request_stop()
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
        with self._device_lock:
            if self.device:
                self.device.close()
            self._rx_active = False
        self.log('Cleanup done.')


def main():
    parser = argparse.ArgumentParser(
        description='EvilCrow RF v2 — URH Bridge (RTL-TCP compatible)')
    parser.add_argument('--port', type=str, default=None,
                        help='Serial port (auto-detect if omitted)')
    parser.add_argument('--tcp-port', type=int, default=1234,
                        help='TCP server port (default: 1234)')
    args = parser.parse_args()

    port = args.port or find_evilcrow_port()
    bridge = URHBridge(serial_port=port, tcp_port=args.tcp_port)
    bridge.run()


if __name__ == '__main__':
    main()
