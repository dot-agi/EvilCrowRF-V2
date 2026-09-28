"""USB serial discovery and startup shared by the desktop tools."""

import os
import time

import serial
import serial.tools.list_ports


USB_UART_IDS = {(0x1A86, 0x7523), (0x1A86, 0x55D4), (0x10C4, 0xEA60),
                (0x0403, 0x6001)}


def detect_evilcrow_port():
    """Suggest a single USB UART; the protocol handshake verifies the board."""
    candidates = []
    for port in serial.tools.list_ports.comports():
        description = (port.description or '').lower()
        if ((port.vid, port.pid) in USB_UART_IDS or
                any(name in description for name in ('cp210', 'ch340', 'ch9102', 'ftdi'))):
            candidates.append(port.device)
    return candidates[0] if len(candidates) == 1 else None


def open_serial(port, baudrate=115200, timeout=0.1):
    """Open without asserting ESP32 reset lines; drain boot output actively.

    Some USB UART drivers still pulse the lines on open. Read during the boot
    wait so a full driver buffer cannot stall the firmware's startup logging.
    """
    connection = serial.Serial(port=None, baudrate=baudrate, timeout=0.05,
                               write_timeout=2,
                               exclusive=True if os.name == 'posix' else None)
    connection.dtr = False
    connection.rts = False
    connection.port = port
    try:
        connection.open()
        boot = bytearray()
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            boot.extend(connection.read(connection.in_waiting or 1))
        connection.timeout = timeout
        return connection, boot.decode('utf-8', errors='replace')
    except BaseException:
        connection.close()
        raise
