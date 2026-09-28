#!/usr/bin/env python3
"""GNU Radio 3.10 receive flowgraph for the EvilCrow demodulated-bit bridge.

Live source: localhost RTL-TCP, amplitude 0/1 with zero Q (not RF I/Q).
No serial module, transmit block, or external GNU Radio extension is needed.
"""

import argparse
import json
from pathlib import Path
import signal
import socket
import struct
import sys
import threading

import numpy as np
from gnuradio import blocks, gr


class EvilCrowTCPSource(gr.sync_block):
    def __init__(self, host='127.0.0.1', port=1234, frequency=433920000, sample_rate=3794):
        super().__init__(name='EvilCrow demodulated bits', in_sig=None, out_sig=[np.complex64])
        self.address = (host, port)
        self.frequency = int(frequency)
        self.sample_rate = int(sample_rate)
        self.connection = None
        self.pending = bytearray()
        self.stopping = threading.Event()
        self.error = None

    def start(self):
        self.stopping.clear()
        try:
            self.connection = socket.create_connection(self.address, timeout=3)
            header = bytearray()
            while len(header) < 12:
                chunk = self.connection.recv(12 - len(header))
                if not chunk:
                    raise ConnectionError('RTL-TCP closed before its header completed')
                header.extend(chunk)
            if header[:4] != b'RTL0':
                raise ValueError('Endpoint did not return an RTL-TCP header')
            self.connection.sendall(struct.pack('>BI', 1, self.frequency) +
                                    struct.pack('>BI', 2, self.sample_rate))
            self.connection.settimeout(0.2)
            return True
        except Exception:
            self.stop()
            raise

    def stop(self):
        self.stopping.set()
        connection, self.connection = self.connection, None
        if connection:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        return True

    def work(self, input_items, output_items):
        connection = self.connection
        if self.stopping.is_set() or connection is None:
            return -1
        out = output_items[0]
        try:
            chunk = connection.recv(max(2, 2 * len(out) - len(self.pending)))
            if not chunk:
                if self.pending:
                    self.error = 'RTL-TCP ended with an incomplete I/Q pair'
                return -1
            self.pending.extend(chunk)
        except socket.timeout:
            return 0
        except OSError as error:
            if not self.stopping.is_set():
                self.error = str(error)
            return -1
        count = min(len(out), len(self.pending) // 2)
        if count:
            values = np.frombuffer(bytes(self.pending[:count * 2]), dtype=np.uint8).reshape(-1, 2)
            out[:count] = ((values[:, 0].astype(np.float32) - 127) / 127 +
                           1j * (values[:, 1].astype(np.float32) - 127) / 127)
            del self.pending[:count * 2]
        return count


def main(argv=None):
    config = json.loads((Path(__file__).resolve().parent / 'integration.json').read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gui', action='store_true', help='Show a Qt time plot')
    parser.add_argument('--host', default=config['host'])
    parser.add_argument('--port', type=int, default=config['tcp_port'])
    parser.add_argument('--frequency', type=float, default=config['frequency'])
    parser.add_argument('--sample-rate', type=int, default=config['sample_rate'])
    parser.add_argument('--duration', type=float, help='Seconds to run (headless default: 5)')
    parser.add_argument('--max-samples', type=int, help='Stop after this many samples')
    parser.add_argument('--output', type=Path, help='Save native complex64 samples')
    parser.add_argument('--input', type=Path, help='Read a saved complex64 file instead of live TCP')
    args = parser.parse_args(argv)
    if args.duration is not None and args.duration <= 0:
        parser.error('duration must be positive')
    if args.max_samples is not None and args.max_samples <= 0:
        parser.error('max-samples must be positive')
    if args.output and args.output.exists():
        parser.error('output already exists; choose a new filename')
    if args.input and (args.input.stat().st_size == 0 or args.input.stat().st_size % 8):
        parser.error('input must contain complete complex64 samples')

    application = window = None
    graph = gr.top_block('EvilCrow demodulated bits')
    if args.input:
        source = blocks.file_source(gr.sizeof_gr_complex, str(args.input), False)
    else:
        source = EvilCrowTCPSource(args.host, args.port, args.frequency, args.sample_rate)
    endpoint = source
    if args.max_samples:
        head = blocks.head(gr.sizeof_gr_complex, args.max_samples)
        graph.connect(endpoint, head)
        endpoint = head
    if args.output:
        sink = blocks.file_sink(gr.sizeof_gr_complex, str(args.output), False)
        sink.set_unbuffered(True)
        graph.connect(endpoint, sink)
    if args.gui:
        print('Loading GNU Radio Qt; the first launch may build the system font cache.', flush=True)
        from PyQt5 import Qt, QtCore
        from PyQt5 import sip
        from gnuradio import qtgui
        application = Qt.QApplication(['EvilCrow GNU Radio'])
        window = Qt.QWidget()
        window.setWindowTitle('EvilCrow demodulated bits — GNU Radio')
        layout = Qt.QVBoxLayout(window)
        plot = qtgui.time_sink_c(1024, args.sample_rate, 'Demodulated bits (not RF I/Q)', 1)
        plot.set_y_axis(-0.1, 1.1)
        layout.addWidget(sip.wrapinstance(plot.qwidget(), Qt.QWidget))
        graph.connect(endpoint, plot)
        window.resize(1000, 500)
        window.show()
    elif not args.output:
        graph.connect(endpoint, blocks.null_sink(gr.sizeof_gr_complex))

    finished = threading.Event()
    graph.start()
    waiter = threading.Thread(target=lambda: (graph.wait(), finished.set()), daemon=True)
    waiter.start()
    previous = signal.signal(signal.SIGINT, lambda *unused: graph.stop())
    seconds = args.duration if args.duration is not None else (None if args.gui else 5)
    try:
        if application:
            timer = QtCore.QTimer()
            timer.timeout.connect(lambda: application.quit() if finished.is_set() else None)
            timer.start(100)
            if seconds is not None:
                QtCore.QTimer.singleShot(int(seconds * 1000), application.quit)
            application.exec_()
        else:
            finished.wait(seconds)
    finally:
        graph.stop()
        waiter.join()
        signal.signal(signal.SIGINT, previous)
        if args.output:
            sink.close()
    if getattr(source, 'error', None):
        raise RuntimeError(source.error)
    print(json.dumps({'ok': True, 'output': str(args.output) if args.output else None,
                      'samples': args.output.stat().st_size // 8 if args.output else None,
                      'sample_format': 'demodulated_bits', 'gnuradio': gr.version()}), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'GNU Radio integration: {error}', file=sys.stderr)
        raise SystemExit(1)
