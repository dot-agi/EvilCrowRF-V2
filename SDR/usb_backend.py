"""Headless JSON-lines USB backend for the Flutter desktop controller."""

import argparse
import contextlib
import hashlib
import json
import os
import select
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import serial.tools.list_ports

from evilcrow_sdr import EvilCrowSDR, is_valid_frequency
from serial_utils import detect_evilcrow_port


class Events:
    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()
        self.closed = False

    def emit(self, event, **fields):
        with self.lock:
            if self.closed:
                return
            try:
                self.stream.write(json.dumps({'event': event, **fields}) + '\n')
                self.stream.flush()
            except (OSError, ValueError):
                # Losing the UI's pipe must not interrupt a firmware write.
                self.closed = True


class ProgressWriter:
    def __init__(self, events):
        self.events = events
        self.pending = ''
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            self.pending += text
            while '\n' in self.pending:
                line, self.pending = self.pending.split('\n', 1)
                if line.strip():
                    self.events.emit('log', message=line.rstrip('\r'))
        return len(text)

    def flush(self):
        with self.lock:
            if self.pending.strip():
                self.events.emit('log', message=self.pending)
            self.pending = ''

    def isatty(self):
        return False


class Cancellation:
    def __init__(self):
        self.event = threading.Event()
        self.callback = None
        self.watch_finished = threading.Event()
        self.watcher = None

    def request(self, *args):
        self.event.set()
        if self.callback:
            self.callback()

    def bind(self, callback):
        self.callback = callback
        if self.event.is_set():
            callback()

    def watch_stdin(self, stream):
        def watch():
            try:
                descriptor = stream.fileno()
                while not self.watch_finished.is_set():
                    ready, _, _ = select.select([descriptor], [], [], 0.2)
                    if ready and not os.read(descriptor, 1):
                        self.request()
                        return
            except (OSError, ValueError):
                self.request()
        self.watcher = threading.Thread(target=watch, daemon=True)
        self.watcher.start()

    def close(self):
        self.watch_finished.set()
        if self.watcher:
            self.watcher.join(timeout=0.5)


def duration(value):
    seconds = float(value)
    if not 0.1 <= seconds <= 60:
        raise argparse.ArgumentTypeError('duration must be between 0.1 and 60 seconds')
    return seconds


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--port', help='USB UART; auto-detect one supported adapter')
    result.add_argument('--watch-stdin', action='store_true',
                        help='Stop receive tools when the parent closes stdin')
    commands = result.add_subparsers(dest='command', required=True)
    for name in ('list-ports', 'status', 'bootloader', 'reboot'):
        commands.add_parser(name)
    for name in ('rx', 'gnuradio', 'urh'):
        command = commands.add_parser(name)
        command.add_argument('--frequency', type=float, default=433.92e6)
        if name == 'urh':
            command.add_argument('--tcp-port', type=int, default=1234)
        else:
            command.add_argument('--duration', type=duration, default=5.0)
            command.add_argument('--output', type=Path, required=name == 'gnuradio')
    commands.add_parser('backup').add_argument('--output', type=Path, required=True)
    for name in ('flash-preview', 'flash'):
        command = commands.add_parser(name)
        command.add_argument('--firmware', type=Path, required=True)
        command.add_argument('--backup', type=Path, required=True)
        if name == 'flash':
            command.add_argument('--expected-sha256', required=True)
            command.add_argument('--yes', action='store_true', required=True)
    return result


def receive(port, args, cancel):
    data = bytearray()
    if args.output and args.output.exists():
        raise FileExistsError('Capture output already exists; choose a new filename')
    with EvilCrowSDR(port) as device:
        if not cancel.event.is_set():
            if not (device.set_frequency(args.frequency) and
                    device.set_modulation('ASK') and device.set_bandwidth(650) and
                    device.set_data_rate(3793.72)):
                raise RuntimeError('Receive configuration was not acknowledged')
            if not cancel.event.is_set():
                if not device.start_rx():
                    raise RuntimeError('RX start was not acknowledged')
                deadline = time.monotonic() + args.duration
                try:
                    while not cancel.event.is_set() and time.monotonic() < deadline:
                        data.extend(device.read_raw(4096, timeout=0.1))
                finally:
                    if not device.stop_rx():
                        raise RuntimeError('RX stop was not acknowledged; restart the device')
        if not device.disable_sdr():
            raise RuntimeError('SDR disable was not acknowledged')
        status = device.get_status()
        if status.get('Active') != 'NO' or status.get('Streaming') != 'NO':
            raise RuntimeError('Device did not return to idle')
    if args.output:
        with args.output.open('xb') as output:
            output.write(data)
    return {'received_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'output': str(args.output) if args.output else None, 'final_status': status}


def bridge(port, args, cancel, events):
    from urh_bridge import URHBridge
    receiver = URHBridge(port, args.tcp_port)
    cancel.bind(receiver.request_stop)
    try:
        if cancel.event.is_set():
            return {}
        if not receiver.connect_device():
            raise RuntimeError('Could not connect and configure the receiver')
        if not receiver.device.set_frequency(args.frequency):
            raise RuntimeError('Receiver frequency was not acknowledged')
        if cancel.event.is_set():
            return {}
        if not receiver.start_server():
            raise RuntimeError('Could not start the local RTL-TCP server')
        events.emit('ready', operation='urh', host='127.0.0.1', tcp_port=receiver.server.getsockname()[1])
        while not cancel.event.is_set():
            try:
                client, address = receiver.server.accept()
            except socket.timeout:
                continue
            receiver.handle_client(client, address)
            if not receiver.ser.is_open:
                raise RuntimeError('Receiver connection closed after a failed command')
        return {}
    finally:
        receiver.cleanup()


def run(args, cancel, events):
    import firmware_tool
    operation = args.command
    if operation == 'list-ports':
        ports = [{'path': port.device, 'description': port.description or '',
                  'vid': port.vid, 'pid': port.pid}
                 for port in serial.tools.list_ports.comports()]
        return {'ports': sorted(ports, key=lambda port: port['path']),
                'suggested_port': detect_evilcrow_port()}
    if operation == 'flash-preview':
        plan, _, _ = firmware_tool.prepare_flash(args.firmware, args.backup)
        events.emit('plan', plan=plan)
        return {'plan': plan}
    if hasattr(args, 'frequency') and not is_valid_frequency(args.frequency):
        raise ValueError('Frequency must be within a CC1101 band')
    if operation == 'urh' and not 1 <= args.tcp_port <= 65535:
        raise ValueError('TCP port must be between 1 and 65535')
    if cancel.event.is_set():
        return {}
    port = args.port or detect_evilcrow_port()
    if not port:
        raise ValueError('Select the EvilCrow USB serial port; automatic discovery was ambiguous')
    if operation == 'status':
        with EvilCrowSDR(port, auto_enable=False) as device:
            return {'identity': device.get_device_info(), 'status': device.get_status()}
    if operation == 'rx':
        return receive(port, args, cancel)
    if operation == 'urh':
        return bridge(port, args, cancel, events)
    if operation == 'gnuradio':
        from gnuradio_source import standalone_test
        return standalone_test(port, args.frequency, args.duration,
                               output=args.output, cancel_event=cancel.event)
    if operation == 'bootloader':
        firmware_tool.enter_bootloader(port)
    elif operation == 'reboot':
        firmware_tool.restart_device(port)
    elif operation == 'backup':
        firmware_tool.backup_flash(port, args.output)
        return {'output': str(args.output), 'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest()}
    elif operation == 'flash':
        plan = firmware_tool.flash_firmware(port, args.firmware, args.backup,
                                           confirmed=args.yes, expected_sha256=args.expected_sha256)
        return {'plan': plan}
    return {}


def main(argv=None):
    args = parser().parse_args(argv)
    events = Events(sys.stdout)
    progress = ProgressWriter(events)
    cancel = Cancellation()
    cancellable = args.command in ('rx', 'gnuradio', 'urh')
    previous_sigint = None
    if cancellable:
        previous_sigint = signal.signal(signal.SIGINT, cancel.request)
        if args.watch_stdin:
            cancel.watch_stdin(sys.stdin)
    elif args.command in ('flash', 'backup', 'bootloader', 'reboot'):
        # A terminal can signal the whole process group even when Flutter's
        # Stop and Quit controls are disabled. Finish critical USB operations.
        previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with contextlib.redirect_stdout(progress):
            result = run(args, cancel, events)
        progress.flush()
        events.emit('result', operation=args.command, ok=True,
                    cancelled=cancel.event.is_set(), **result)
        return 130 if cancel.event.is_set() else 0
    except Exception as error:
        progress.flush()
        events.emit('error', operation=args.command, message=str(error))
        return 1
    finally:
        cancel.close()
        if previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)


if __name__ == '__main__':
    raise SystemExit(main())
