#!/usr/bin/env python3
"""Check USB identity/status and optionally run a bounded receive-only test."""

import argparse
import json
import time
from pathlib import Path

from evilcrow_sdr import EvilCrowSDR, is_valid_frequency
from serial_utils import detect_evilcrow_port


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default=None)
    parser.add_argument('--rx-seconds', type=float, default=0)
    parser.add_argument('--frequency', type=float, default=433.92e6, help='Hz')
    parser.add_argument('--output', type=Path, help='Save diagnostic JSON')
    args = parser.parse_args()
    if not 0 <= args.rx_seconds <= 60:
        parser.error('--rx-seconds must be between 0 and 60')
    if not is_valid_frequency(args.frequency):
        parser.error('frequency must be within a CC1101 band')
    port = args.port or detect_evilcrow_port()
    if not port:
        parser.error('Specify --port; a single USB UART could not be identified')

    report = {'port': port, 'rx_seconds': args.rx_seconds}
    with EvilCrowSDR(port, auto_enable=bool(args.rx_seconds)) as device:
        report['boot_log'] = device.boot_log
        report['identity'] = device.get_device_info()
        report['status'] = device.get_status()
        if 'Active' not in report['status']:
            raise RuntimeError(f'Unexpected status: {report["status"]}')
        if args.rx_seconds:
            if not all((device.set_frequency(args.frequency),
                        device.set_modulation('ASK'), device.set_bandwidth(650))):
                raise RuntimeError('Receive configuration failed')
            if not device.start_rx():
                raise RuntimeError('RX start failed')
            count = 0
            deadline = time.monotonic() + args.rx_seconds
            while time.monotonic() < deadline:
                count += len(device.read_raw(65536, timeout=min(0.1, deadline - time.monotonic())))
            if not device.stop_rx():
                raise RuntimeError('RX stop failed')
            report['received_bytes'] = count
            report['after_rx'] = device.get_status()
        if args.rx_seconds and not device.disable_sdr():
            raise RuntimeError('SDR disable failed')
        report['final_status'] = device.get_status()
        if args.rx_seconds and (report['final_status'].get('Active') != 'NO' or report['final_status'].get('Streaming') != 'NO'):
            raise RuntimeError('Device did not return to idle')

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'boot_log'}, indent=2))
    print('PASS: USB commands and cleanup completed. RX byte counts do not prove a decoded signal.')


if __name__ == '__main__':
    main()
