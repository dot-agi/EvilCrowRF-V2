#!/usr/bin/env python3
"""
EvilCrow RF v2 — GNU Radio Source Block

Provides a gr.sync_block that reads demodulated data from the EvilCrow
RF v2 and outputs it as complex float samples for GNU Radio flowgraphs.

IMPORTANT: The CC1101 outputs demodulated bytes, not raw IQ. The block
converts each byte to a complex sample (amplitude-modulated). This works
well for OOK/ASK signal visualization and basic analysis but does NOT
provide true quadrature data.

Usage in GNU Radio Companion:
    1. Copy this file to your GRC blocks directory or Python path.
    2. In GRC, add a "Python Block" or import directly.
    3. Connect the output to analysis blocks (FFT, waterfall, file sink).

Standalone test:
    python gnuradio_source.py --port COM8 --freq 433.92e6

Requirements:
    pip install pyserial numpy
    GNU Radio 3.8+ (only for GRC integration)
"""

# Module version
VERSION = "1.0.1"

import logging
import numpy as np

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    raise ImportError(
        "pyserial is required: pip install pyserial"
    )

import threading
import time
import argparse
import sys
from pathlib import Path
from serial_utils import detect_evilcrow_port
from evilcrow_sdr import EvilCrowSDR

log = logging.getLogger(__name__)

# Try to import GNU Radio (optional — standalone mode works without it)
try:
    from gnuradio import gr
    import pmt
    HAS_GNURADIO = True
except ImportError:
    HAS_GNURADIO = False


def find_serial_port() -> str:
    port = detect_evilcrow_port()
    if port:
        return port
    raise RuntimeError('Specify --port; a single USB UART could not be identified')


class EvilCrowSource:
    """
    Core SDR source that reads from EvilCrow serial.

    Can be used standalone or wrapped in a GNU Radio block.
    """

    def __init__(self, port: str, frequency: float = 433.92e6,
                 modulation: int = 2, baudrate: int = 115200):
        self.port = port
        self.frequency = frequency
        self.modulation = modulation
        self.baudrate = baudrate

        self.ser: serial.Serial = None
        self.connected = False
        self.device = None
        self.streaming = False
        self._lock = threading.RLock()

    def connect(self) -> bool:
        """Connect to device, auto-enable SDR mode, and configure."""
        try:
            self.device = EvilCrowSDR(self.port, self.baudrate)
            self.ser = self.device.ser
            self.connected = True
            print(f'[OK] Connected to EvilCrow SDR on {self.port}')

            # Apply settings
            if not (self.set_frequency(self.frequency) and self.set_modulation(self.modulation) and
                    self.set_bandwidth(650) and self.device.set_data_rate(3793.72)):
                raise RuntimeError('Receiver configuration was not acknowledged')

            return True
        except Exception as e:
            print(f'[ERR] Connect failed: {e}')
            self.close()
            return False

    def _configure(self, operation) -> bool:
        with self._lock:
            if not self.connected:
                return False
            resume = self.streaming
            if resume:
                self.stop_streaming()
            try:
                result = operation()
            except Exception:
                if resume:
                    try:
                        if not self.start_streaming():
                            log.error('Receiver did not acknowledge RX restart')
                    except Exception:
                        log.exception('Receiver restart also failed')
                raise  # Preserve the original configuration failure.
            if resume and not self.start_streaming():
                raise RuntimeError('Receiver did not acknowledge RX restart')
            return result

    def set_frequency(self, freq_hz: float) -> bool:
        """Set center frequency."""
        if self._configure(lambda: self.device.set_frequency(freq_hz)):
            self.frequency = freq_hz
            return True
        return False

    def set_modulation(self, mod: int) -> bool:
        """Set modulation (0=2FSK, 2=ASK/OOK, etc)."""
        name = {0: '2FSK', 1: 'GFSK', 2: 'ASK', 3: '4FSK', 4: 'MSK'}.get(mod)
        if name is not None and self._configure(lambda: self.device.set_modulation(name)):
            self.modulation = mod
            return True
        return False

    def set_bandwidth(self, bw_khz: float) -> bool:
        """Set RX bandwidth."""
        return self._configure(lambda: self.device.set_bandwidth(bw_khz))

    def start_streaming(self) -> bool:
        """Start RX through the shared library's single serial reader."""
        with self._lock:
            if not self.connected:
                return False
            if not self.streaming:
                self.streaming = self.device.start_rx()
            return self.streaming

    def stop_streaming(self) -> bool:
        """Stop RX."""
        with self._lock:
            if self.streaming:
                acknowledged = self.device.stop_rx()
                self.streaming = False
                if not acknowledged:
                    raise RuntimeError('RX stop was not acknowledged; restart the device')
            return True

    def read_samples(self, count: int, timeout: float = 2.0) -> np.ndarray:
        """Read up to N received bytes and convert to complex samples."""
        with self._lock:
            if not self.connected or not self.streaming:
                return np.empty(0, dtype=np.complex64)
            data = self.device.read_raw(count, timeout=timeout)
        amplitudes = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128) / 128
        return amplitudes.astype(np.complex64)

    def close(self):
        """Clean up."""
        with self._lock:
            try:
                if self.streaming:
                    self.stop_streaming()
            finally:
                if self.device:
                    self.device.close()
                self.streaming = False
                self.connected = False


# ── GNU Radio Block ────────────────────────────────────────────

if HAS_GNURADIO:
    class EvilCrowGRSource(gr.sync_block):
        """
        GNU Radio source block for EvilCrow RF v2 SDR.

        Parameters (set via GRC):
            port: Serial port (e.g. COM8, /dev/ttyUSB0)
            frequency: Center frequency in Hz
            modulation: 0=2FSK, 2=ASK/OOK
        """

        def __init__(self, port: str = 'COM8', frequency: float = 433.92e6,
                     modulation: int = 2):
            gr.sync_block.__init__(
                self,
                name='EvilCrow SDR Source',
                in_sig=None,
                out_sig=[np.complex64],
            )

            self.source = EvilCrowSource(port, frequency, modulation)

            # Message ports for runtime control
            self.message_port_register_in(pmt.intern('freq'))
            self.set_msg_handler(pmt.intern('freq'), self._handle_freq)

        def start(self):
            if self.source.connect():
                if self.source.start_streaming():
                    return True
                self.source.close()
            return False

        def stop(self):
            self.source.close()
            return True

        def work(self, input_items, output_items):
            out = output_items[0]
            n = len(out)
            samples = self.source.read_samples(n)
            out[:len(samples)] = samples
            return len(samples)

        def _handle_freq(self, msg):
            if pmt.is_number(msg):
                try:
                    if not self.source.set_frequency(pmt.to_double(msg)):
                        log.error('Receiver rejected the frequency update')
                except Exception:
                    log.exception('Frequency update failed; check receiver state before restarting')


# ── GRC XML block definition ──────────────────────────────────

GRC_BLOCK_XML = """<?xml version="1.0"?>
<block>
  <name>EvilCrow SDR Source</name>
  <key>evilcrow_sdr_source</key>
  <category>[EvilCrow RF]</category>
  <import>from gnuradio_source import EvilCrowGRSource</import>
  <make>EvilCrowGRSource($port, $frequency, $modulation)</make>
  <param>
    <name>Serial Port</name>
    <key>port</key>
    <type>string</type>
    <value>COM8</value>
  </param>
  <param>
    <name>Frequency (Hz)</name>
    <key>frequency</key>
    <type>real</type>
    <value>433.92e6</value>
  </param>
  <param>
    <name>Modulation</name>
    <key>modulation</key>
    <type>int</type>
    <value>2</value>
  </param>
  <source>
    <name>out</name>
    <type>complex</type>
  </source>
  <sink>
    <name>freq</name>
    <type>message</type>
    <optional>1</optional>
  </sink>
  <doc>
EvilCrow RF v2 SDR Source Block.

Reads demodulated data from the CC1101 transceiver via USB serial
and outputs complex float samples. Best for OOK/ASK signals.

Parameters:
  - Serial Port: USB-UART port (e.g. COM8 or /dev/ttyUSB0)
  - Frequency: Center frequency in Hz (CC1101 bands: 300-348, 387-464, 779-928 MHz)
  - Modulation: 0=2FSK, 2=ASK/OOK, 1=GFSK, 3=4FSK, 4=MSK
  </doc>
</block>
"""


# ── Standalone test ────────────────────────────────────────────

def standalone_test(port: str, freq: float, duration: float, output=None, cancel_event=None):
    """Run a standalone capture test (no GNU Radio needed)."""
    print(f'\n=== EvilCrow SDR Standalone Test ===')
    print(f'Port: {port}  Freq: {freq/1e6:.2f} MHz  Duration: {duration}s\n')

    outfile = Path(output) if output is not None else Path(f'capture_{freq/1e6:.0f}MHz.raw')
    if outfile.exists():
        raise FileExistsError('Capture output already exists; choose a new filename')
    source = EvilCrowSource(port, frequency=freq, modulation=2)
    chunks = []
    try:
        if not source.connect():
            raise RuntimeError('Could not connect and configure the receiver')
        if cancel_event is not None and cancel_event.is_set():
            return {'captured_samples': 0, 'output': None}
        if not source.start_streaming():
            raise RuntimeError('Receiver did not acknowledge start RX')
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline and (cancel_event is None or not cancel_event.is_set()):
            samples = source.read_samples(2048, timeout=min(0.1, deadline - time.monotonic()))
            if len(samples):
                chunks.append(samples)
        source.stop_streaming()
    finally:
        source.close()
    if not chunks:
        if cancel_event is not None and cancel_event.is_set():
            return {'captured_samples': 0, 'output': None}
        raise RuntimeError('No samples received; capture was not saved')
    samples = np.concatenate(chunks)

    print(f'\nCaptured {len(samples)} samples')
    if len(samples) > 0:
        power = np.mean(np.abs(samples) ** 2)
        peak = np.max(np.abs(samples))
        print(f'Average power: {power:.6f}')
        print(f'Peak amplitude: {peak:.4f}')

        # Save to file
        with outfile.open('xb') as destination:
            samples.tofile(destination)
        print(f'Saved to {outfile} (complex64 format)')
    return {'captured_samples': len(samples), 'output': str(outfile)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='EvilCrow RF v2 — GNU Radio SDR Source')
    parser.add_argument('--port', type=str, default=None,
                        help='Serial port (auto-detect if omitted)')
    parser.add_argument('--freq', type=float, default=433.92e6,
                        help='Center frequency in Hz (default: 433.92 MHz)')
    parser.add_argument('--duration', type=float, default=5.0,
                        help='Capture duration in seconds (default: 5)')
    args = parser.parse_args()

    port = args.port or find_serial_port()
    standalone_test(port, args.freq, args.duration)
