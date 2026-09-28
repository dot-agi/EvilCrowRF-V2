#!/usr/bin/env python3
"""
EvilCrow RF v2 — SDR Control Library

Python interface for the EvilCrow RF v2 SDR mode.
Communicates via USB serial using HackRF-compatible text commands.

IMPORTANT: The CC1101 is NOT a true SDR. This module provides:
  - Spectrum scanning (real RSSI measurements per frequency step)
  - Raw RX streaming (demodulated bytes from CC1101 FIFO, not raw IQ)
  - Frequency / modulation / bandwidth configuration

SDR mode is now auto-enabled via serial (no app/phone needed).
The library sends 'sdr_enable' on connect.

Usage:
    from evilcrow_sdr import EvilCrowSDR

    sdr = EvilCrowSDR('COM8')          # Windows
    sdr = EvilCrowSDR('/dev/ttyUSB0')  # Linux

    sdr.set_frequency(433.92e6)
    sdr.set_modulation('ASK')
    sdr.set_bandwidth(650)

    # Spectrum scan
    spectrum = sdr.spectrum_scan(300e6, 928e6, step_khz=200)
    for freq, rssi in spectrum:
        print(f'{freq/1e6:.2f} MHz : {rssi} dBm')

    # Raw RX
    sdr.start_rx()
    data = sdr.read_raw(timeout=2.0)
    sdr.stop_rx()

    sdr.close()

Requirements:
    pip install pyserial
"""

# Module version
VERSION = "1.0.1"

import logging

try:
    import serial
except ImportError:
    raise ImportError(
        "pyserial is required: pip install pyserial"
    )

import time
import threading
import queue
from typing import Optional, List, Tuple
from serial_utils import open_serial

log = logging.getLogger(__name__)

# CC1101 valid frequency bands (MHz)
CC1101_BANDS = [
    (300.0, 348.0),
    (387.0, 464.0),
    (779.0, 928.0),
]

# CC1101 hardware parameter limits
CC1101_LIMITS = {
    'freq_bands_mhz': [(300.0, 348.0), (387.0, 464.0), (779.0, 928.0)],
    'bandwidth_khz': [58, 68, 81, 102, 116, 135, 162, 203, 232, 270,
                      325, 406, 464, 541, 650, 812],
    'data_rate_baud': (600, 500_000),  # min, max
    'modulations': {
        0: '2-FSK', 1: 'GFSK', 2: 'ASK/OOK', 3: '4-FSK', 4: 'MSK',
    },
    'fifo_bytes': 64,
    'serial_max_baud': 115200,  # ESP32 USB-UART default
}


def is_valid_frequency(freq_hz: float) -> bool:
    """Check if frequency is within CC1101 supported bands."""
    freq_mhz = freq_hz / 1e6
    return any(lo <= freq_mhz <= hi for lo, hi in CC1101_BANDS)


def print_cc1101_limits():
    """Print CC1101 hardware parameter limits to console."""
    print('\n╔══════════════════════════════════════════════════════╗')
    print('║        CC1101 SDR Parameter Limits                  ║')
    print('╠══════════════════════════════════════════════════════╣')
    print('║ Frequency bands:                                   ║')
    for lo, hi in CC1101_LIMITS['freq_bands_mhz']:
        print(f'║   {lo:7.1f} – {hi:7.1f} MHz{" " * 29}║')
    print('║                                                    ║')
    print('║ Bandwidth (kHz, discrete values):                  ║')
    bws = CC1101_LIMITS['bandwidth_khz']
    line = '  '.join(f'{b}' for b in bws[:8])
    print(f'║   {line:<50}║')
    line = '  '.join(f'{b}' for b in bws[8:])
    print(f'║   {line:<50}║')
    print('║                                                    ║')
    lo, hi = CC1101_LIMITS['data_rate_baud']
    print(f'║ Data rate: {lo:,} – {hi:,} Baud{" " * 21}║')
    print('║                                                    ║')
    print('║ Modulations:                                       ║')
    for k, v in CC1101_LIMITS['modulations'].items():
        print(f'║   {k} = {v:<46}║')
    print('║                                                    ║')
    print(f'║ RX FIFO: {CC1101_LIMITS["fifo_bytes"]} bytes{" " * 36}║')
    print('║ NOTE: NOT a true SDR — no raw IQ output.           ║')
    print('║       Data is demodulated bytes from CC1101 FIFO.  ║')
    print('╚══════════════════════════════════════════════════════╝\n')


class EvilCrowSDR:
    """
    EvilCrow RF v2 USB SDR control interface.

    Communicates via serial text commands with the firmware SDR module.
    The firmware responds with HACKRF_SUCCESS / HACKRF_ERROR lines.
    """

    MODULATIONS = {
        '2FSK': 0, 'GFSK': 1, 'ASK': 2, 'OOK': 2,
        'ASK/OOK': 2, '4FSK': 3, 'MSK': 4,
    }

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0,
                 auto_enable: bool = True):
        """
        Open serial connection to EvilCrow RF v2.

        Args:
            port: Serial port name (e.g. 'COM8' or '/dev/ttyUSB0').
            baudrate: Baud rate (must match firmware: 115200).
            timeout: Read timeout in seconds.
            auto_enable: Enable SDR mode; False allows read-only diagnostics.
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._manage_sdr = auto_enable
        self.ser: Optional[serial.Serial] = None
        self._streaming = False
        self._stream_thread: Optional[threading.Thread] = None
        self._rx_queue: queue.Queue = queue.Queue(maxsize=50000)
        self._rx_remainder = bytearray()

        # Current state (updated after successful commands)
        self.frequency_hz: float = 433.92e6
        self.modulation: int = 2  # ASK/OOK
        self.bandwidth_khz: float = 650.0
        self.data_rate_baud: float = 3793.72

        self._connect()

    def _connect(self):
        """Open serial port, auto-enable SDR mode, and verify device."""
        self.ser, self.boot_log = open_serial(self.port, self.baudrate, 0.05)
        try:
            resp = self.send_command('board_id_read')
            if 'Board ID: EvilCrow_RF_v2_SDR' not in resp or 'HACKRF_ERROR' in resp:
                raise ConnectionError(
                    f'Device on {self.port} did not identify as EvilCrow SDR.\n'
                    f'Response: {resp}')
            if self._manage_sdr:
                print(f'[...] Enabling SDR mode on {self.port}...')
                enable_resp = self.send_command('sdr_enable')
                if 'HACKRF_SUCCESS' not in enable_resp or 'HACKRF_ERROR' in enable_resp:
                    raise ConnectionError(
                        f'Cannot enable SDR mode: {enable_resp or "no response"}. '
                        'If identity works but enable hangs, see docs/macos.md '
                        'for the firmware SPI-lock fix.')
        except BaseException:
            self.ser.close()
            raise
        if 'SD card not mounted' in self.boot_log:
            print('[WARN] SD card not mounted; firmware is using internal LittleFS.')
        if self._manage_sdr:
            print('[OK] SDR mode enabled via serial')
        print(f'[OK] Connected to EvilCrow SDR on {self.port}')

    def send_command(self, command: str) -> str:
        """
        Send a text command and read the response.

        Args:
            command: Command string (without newline).

        Returns:
            Full response string from device.
        """
        if not self.ser or not self.ser.is_open:
            raise RuntimeError('Serial port not open')
        if self._streaming or (self._stream_thread and self._stream_thread.is_alive()):
            raise RuntimeError('Stop RX before sending text commands')

        deadline = time.monotonic() + self.timeout
        self.ser.write((command + '\n').encode('ascii'))
        # write_timeout bounds submission; the reply confirms delivery. POSIX
        # flush() calls tcdrain(), which has no timeout and can stall forever.

        lines: List[str] = []
        pending_line = bytearray()
        last_data = time.monotonic()
        acknowledged = False
        scan_command = command.split(' ', 1)[0].lower() == 'spectrum_scan'
        scan_complete = False
        failed = False
        while time.monotonic() < deadline:
            if self.ser.in_waiting > 0:
                # readline() only times out between bytes: a continuous binary
                # stream without LF can outlive the command deadline. Read one
                # byte so the deadline also bounds that case, and so rx_start
                # leaves every byte after its trailer for the RX worker.
                raw = self.ser.read(1)
                if not raw:
                    continue
                last_data = time.monotonic()
                if raw != b'\n':
                    pending_line.extend(raw)
                    continue
                line = pending_line.decode('ascii', errors='replace').strip()
                pending_line.clear()
                if line:
                    lines.append(line)
                acknowledged |= line in ('HACKRF_SUCCESS', 'HACKRF_ERROR')
                failed |= line == 'HACKRF_ERROR'
                if scan_command and line.startswith('Scan complete:'):
                    scan_complete = True
                    break
                # The ACK precedes the body. For RX, stop at the trailer so
                # subsequent binary bytes stay available to the RX worker.
                if command == 'rx_start' and line == 'RX streaming started':
                    break
            else:
                if acknowledged and (not scan_command or failed) and time.monotonic() - last_data >= 0.15:
                    break
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))

        if pending_line:
            lines.append(pending_line.decode('ascii', errors='replace').strip())
        if scan_command and not (scan_complete or failed):
            raise TimeoutError('Spectrum scan did not finish before the command deadline')
        return '\n'.join(lines)

    # ── Configuration commands ─────────────────────────────────

    def set_frequency(self, freq_hz: float) -> bool:
        """
        Set center frequency in Hz.

        Valid CC1101 ranges: 300-348, 387-464, 779-928 MHz.
        """
        if not is_valid_frequency(freq_hz):
            print(f'[WARN] {freq_hz/1e6:.2f} MHz is outside CC1101 bands')
            return False

        resp = self.send_command(f'set_freq {int(freq_hz)}')
        if 'SUCCESS' in resp:
            self.frequency_hz = freq_hz
            print(f'[OK] Frequency: {freq_hz/1e6:.3f} MHz')
            return True
        print(f'[ERR] set_freq failed: {resp}')
        return False

    def set_modulation(self, mod: str) -> bool:
        """
        Set modulation type.

        Valid values: '2FSK', 'GFSK', 'ASK', 'OOK', 'ASK/OOK', '4FSK', 'MSK'
        """
        mod_upper = mod.upper()
        if mod_upper not in self.MODULATIONS:
            print(f'[ERR] Unknown modulation: {mod}')
            return False

        mod_id = self.MODULATIONS[mod_upper]
        resp = self.send_command(f'set_modulation {mod_id}')
        if 'SUCCESS' in resp:
            self.modulation = mod_id
            print(f'[OK] Modulation: {mod_upper} ({mod_id})')
            return True
        print(f'[ERR] set_modulation failed: {resp}')
        return False

    def set_bandwidth(self, bw_khz: float) -> bool:
        """Set RX filter bandwidth in kHz."""
        resp = self.send_command(f'set_bandwidth {bw_khz}')
        if 'SUCCESS' in resp:
            self.bandwidth_khz = bw_khz
            print(f'[OK] Bandwidth: {bw_khz:.1f} kHz')
            return True
        print(f'[ERR] set_bandwidth failed: {resp}')
        return False

    def set_data_rate(self, rate_hz: float) -> bool:
        """
        Set data rate in Hz (maps to CC1101 data rate in kBaud).

        CC1101 range: 600 - 500000 Baud.
        """
        resp = self.send_command(f'set_sample_rate {int(rate_hz)}')
        if 'SUCCESS' in resp:
            self.data_rate_baud = rate_hz
            print(f'[OK] Data rate: {rate_hz/1000:.2f} kBaud')
            return True
        print(f'[ERR] set_sample_rate failed: {resp}')
        return False

    def set_gain(self, gain_db: int) -> bool:
        """Set gain (CC1101 uses AGC, so this is approximate)."""
        resp = self.send_command(f'set_gain {gain_db}')
        if 'SUCCESS' in resp:
            print(f'[OK] Gain: {gain_db} dB (AGC mode)')
            return True
        return False

    def get_status(self) -> dict:
        """Query current SDR status from device."""
        resp = self.send_command('sdr_status')
        info = {}
        for line in resp.split('\n'):
            if ':' in line:
                key, _, val = line.partition(':')
                info[key.strip()] = val.strip()
        return info

    def enable_sdr(self) -> bool:
        """Enable SDR mode on-device (sends serial command)."""
        resp = self.send_command('sdr_enable')
        ok = 'SUCCESS' in resp.upper()
        if ok:
            print('[OK] SDR mode enabled')
        else:
            print(f'[ERR] sdr_enable failed: {resp}')
        return ok

    def disable_sdr(self) -> bool:
        """Disable SDR mode on-device."""
        resp = self.send_command('sdr_disable')
        ok = 'SUCCESS' in resp.upper()
        if ok:
            print('[OK] SDR mode disabled')
        else:
            print(f'[ERR] sdr_disable failed: {resp}')
        return ok

    def get_device_info(self) -> str:
        """Query device identity string."""
        return self.send_command('board_id_read')

    def get_sdr_info(self) -> str:
        """Query CC1101 parameter limits from firmware."""
        return self.send_command('sdr_info')

    # ── Spectrum scan ──────────────────────────────────────────

    def spectrum_scan(self, start_hz: float, end_hz: float,
                      step_khz: float = 100) -> List[Tuple[float, int]]:
        """
        Perform a spectrum scan (frequency sweep with RSSI readings).

        Args:
            start_hz: Start frequency in Hz.
            end_hz: End frequency in Hz.
            step_khz: Step size in kHz.

        Returns:
            List of (frequency_hz, rssi_dBm) tuples.
        """
        start_mhz = start_hz / 1e6
        end_mhz = end_hz / 1e6
        step_mhz = step_khz / 1000.0

        print(f'[SCAN] {start_mhz:.2f} - {end_mhz:.2f} MHz, step {step_khz:.0f} kHz')
        resp = self.send_command(
            f'spectrum_scan {start_mhz:.2f} {end_mhz:.2f} {step_khz:.0f}')

        if 'HACKRF_ERROR' in resp:
            raise RuntimeError(f'Spectrum scan was rejected: {resp}')

        # Parse spectrum output — firmware prints per-frequency RSSI
        # after "Scanning..." and before "Scan complete"
        results: List[Tuple[float, int]] = []
        for line in resp.split('\n'):
            # Look for lines with frequency and RSSI data
            if 'complete' in line.lower():
                # Parse "Scan complete: N points"
                break

        # The actual RSSI data comes via BLE, not serial text.
        # For serial mode, firmware prints summary.
        # For PC tools, use the raw serial data from pollRawRx.
        print(f'[OK] Spectrum scan requested. Results arrive via BLE.')
        return results

    # ── Raw RX streaming ───────────────────────────────────────

    def start_rx(self) -> bool:
        """
        Start raw RX streaming.

        Demodulated bytes from the CC1101 FIFO are sent via serial.
        Read them with read_raw() or read_raw_continuous().
        """
        if self._streaming:
            return True
        # A fresh capture must not return unread data from an earlier session.
        self._rx_remainder.clear()
        while True:
            try:
                self._rx_queue.get_nowait()
            except queue.Empty:
                break
        resp = self.send_command('rx_start')
        if 'SUCCESS' in resp:
            self._streaming = True
            self._stream_thread = threading.Thread(
                target=self._rx_worker, daemon=True)
            self._stream_thread.start()
            print('[OK] RX streaming started')
            return True
        print(f'[ERR] rx_start failed: {resp}')
        return False

    def stop_rx(self) -> bool:
        """Stop raw RX streaming."""
        self._streaming = False
        if self._stream_thread:
            # Wake a blocked driver read without closing its port beneath the
            # worker. The command reader starts only after that worker exits.
            cancel_read = getattr(self.ser, 'cancel_read', None)
            if self._stream_thread.is_alive() and cancel_read:
                try:
                    cancel_read()
                except (OSError, serial.SerialException):
                    pass  # The bounded join still checks worker ownership.
            self._stream_thread.join(timeout=2.0)
            if self._stream_thread.is_alive():
                raise RuntimeError('RX worker did not stop; refusing a second serial reader')
            self._stream_thread = None
        resp = self.send_command('rx_stop')
        if 'SUCCESS' in resp:
            print('[OK] RX streaming stopped')
            return True
        return False

    def read_raw(self, count: int = 1024, timeout: float = 2.0) -> bytes:
        """
        Read raw demodulated bytes from the RX stream.

        Args:
            count: Maximum number of bytes to read.
            timeout: Timeout in seconds.

        Returns:
            Bytes received from CC1101 FIFO.
        """
        result = bytearray(self._rx_remainder)
        self._rx_remainder.clear()
        deadline = time.time() + timeout
        while len(result) < count and time.time() < deadline:
            try:
                chunk = self._rx_queue.get(timeout=0.1)
                result.extend(chunk)
            except queue.Empty:
                continue
        # A serial packet can be larger than the caller's requested read.
        self._rx_remainder.extend(result[count:])
        return bytes(result[:count])

    def _rx_worker(self):
        """Background thread: read raw bytes from serial during RX."""
        while self._streaming and self.ser and self.ser.is_open:
            try:
                avail = self.ser.in_waiting
                if avail > 0:
                    data = self.ser.read(min(avail, 256))
                    if data:
                        try:
                            self._rx_queue.put_nowait(data)
                        except queue.Full:
                            try:
                                self._rx_queue.get_nowait()
                            except queue.Empty:
                                pass
                            self._rx_queue.put_nowait(data)
                else:
                    time.sleep(0.005)
            except Exception:
                break

    # ── Cleanup ────────────────────────────────────────────────

    def __repr__(self) -> str:
        """Return a human-readable representation of the SDR instance."""
        state = 'streaming' if self._streaming else 'idle'
        return (
            f"<EvilCrowSDR port={self.port} "
            f"freq={self.frequency_hz / 1e6:.3f}MHz "
            f"mod={self.modulation} state={state}>"
        )

    def close(self):
        """Stop the serial reader, disable SDR, and report shutdown failures."""
        if not self.ser or not self.ser.is_open:
            return
        failure = None
        if self._streaming or self._stream_thread:
            try:
                if not self.stop_rx():
                    raise RuntimeError('RX stop was not acknowledged; restart the device')
            except Exception as error:
                # Sending another command or closing the port while the worker
                # still owns reads would race it. Keep ownership and report it.
                if self._stream_thread and self._stream_thread.is_alive():
                    raise
                failure = error
        try:
            if self._manage_sdr and not self.disable_sdr():
                raise RuntimeError('SDR disable was not acknowledged; restart the device')
        except Exception as error:
            if failure is None:
                failure = error
            else:
                log.warning('SDR disable also failed: %s', error)
        finally:
            self.ser.close()
        if failure is not None:
            raise failure
        print('[OK] Disconnected')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ── Quick test ─────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    port = sys.argv[1] if len(sys.argv) > 1 else 'COM8'

    # Show CC1101 limits
    print_cc1101_limits()

    try:
        with EvilCrowSDR(port) as sdr:
            print('\n--- Device Info ---')
            print(sdr.get_device_info())

            print('\n--- SDR Info (CC1101 Limits) ---')
            print(sdr.get_sdr_info())

            print('\n--- Status ---')
            for k, v in sdr.get_status().items():
                print(f'  {k}: {v}')

            sdr.set_frequency(433.92e6)
            sdr.set_modulation('ASK')
            sdr.set_bandwidth(650)

            print('\n--- RX Test (3 seconds) ---')
            sdr.start_rx()
            time.sleep(3)
            data = sdr.read_raw(256, timeout=0.5)
            sdr.stop_rx()
            print(f'Received {len(data)} bytes')
            if data:
                print(f'Hex: {data[:32].hex()}')

    except Exception as e:
        print(f'Error: {e}')
