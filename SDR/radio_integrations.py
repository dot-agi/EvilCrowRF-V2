"""Portable, receive-only projects for installed URH and GNU Radio applications."""

import json
import math
from pathlib import Path
import shlex
import shutil
import sys
import xml.etree.ElementTree as ET

import numpy as np

from evilcrow_sdr import is_valid_frequency


SAMPLE_RATE = 3794  # RTL-TCP carries integer rates; nearest to the tested 3793.72 Baud.


def _launcher(target, executable):
    """Resolve an installed Python without evaluating paths as shell commands."""
    selected = shlex.quote(str(executable or ''))
    if target == 'urh':
        module = 'urh'
        script = 'evilcrow_urh.py'
        candidates = ('/opt/homebrew/bin/urh', '/usr/local/bin/urh',
                      '/opt/homebrew/opt/urh/libexec/bin/python',
                      '/usr/local/opt/urh/libexec/bin/python')
    else:
        module = 'gnuradio'
        script = 'evilcrow_gnuradio.py'
        candidates = ('/opt/homebrew/bin/python3', '/usr/local/bin/python3')
    paths = ' '.join(shlex.quote(path) for path in candidates)
    return f'''#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
selected={selected}
resolve_python() {{
  local candidate="$1" parent link hops=0
  if [[ ! -x "$candidate" ]]; then return 1; fi
  # Preserve a selected Python path: resolving a venv's interpreter symlink
  # would lose its site-packages. Only resolve the URH entry-point script.
  if [[ "$(basename -- "$candidate")" == urh* ]]; then
    while [[ -L "$candidate" ]]; do
      hops=$((hops + 1))
      if [[ "$hops" -gt 40 ]]; then return 1; fi
      link="$(readlink -- "$candidate")" || return 1
      if [[ "$link" == /* ]]; then
        candidate="$link"
      else
        candidate="$(dirname -- "$candidate")/$link"
      fi
    done
    parent="$(dirname -- "$candidate")"
    candidate="$parent/python3"
  fi
  if "$candidate" -c 'import {module}' >/dev/null 2>&1; then
    printf '%s\\n' "$candidate"
    return 0
  fi
  return 1
}}
python_bin=""
if [[ -n "$selected" ]]; then
  python_bin="$(resolve_python "$selected")" || {{
    echo 'The selected executable needs a Python environment containing {module}.' >&2
    exit 1
  }}
else
  for candidate in {paths} "$(command -v {'urh' if target == 'urh' else 'python3'} || true)"; do
    if python_bin="$(resolve_python "$candidate")"; then break; fi
  done
fi
if [[ -z "$python_bin" ]]; then
  echo 'Install {module}, or export again with its Python executable selected in Flutter.' >&2
  exit 1
fi
if [[ "$#" -eq 0 ]]; then
  exec "$python_bin" {shlex.quote(script)}{' --gui' if target == 'gnuradio' else ''}
fi
exec "$python_bin" {shlex.quote(script)} "$@"
'''


def _urh_project(config, capture_name=None):
    root = ET.Element('UniversalRadioHackerProject', {
        'description': 'EvilCrow CC1101 demodulated bits. Receive only; not RF I/Q.',
        'modulation_was_edited': '1',
    })
    device = ET.SubElement(root, 'device_conf')
    values = dict(name='RTL-TCP', frequency=config['frequency'],
                  sample_rate=config['sample_rate'], bandwidth=650000,
                  rx_gain=15, apply_dc_correction=False,
                  ip='127.0.0.1', port=config['tcp_port'])
    for name, value in values.items():
        ET.SubElement(device, name).text = str(value)
    protocol = ET.SubElement(root, 'protocol')
    decodings = ET.SubElement(protocol, 'decodings')
    ET.SubElement(decodings, 'decoding', {'name': 'NRZ'}).text = "'Non Return To Zero (NRZ)'"
    ET.SubElement(protocol, 'participants')
    ET.SubElement(protocol, 'messages')
    types = ET.SubElement(protocol, 'message_types')
    ET.SubElement(types, 'message_type', {'name': 'default', 'id': 'evilcrow-default'})
    if capture_name:
        ET.SubElement(root, 'open_file', {'name': capture_name, 'position': '0'})
        ET.SubElement(root, 'signal', {
            'filename': capture_name, 'name': 'EvilCrow demodulated bits',
            'modulation_type': 'ASK', 'sample_rate': str(config['sample_rate']),
            'samples_per_symbol': '1', 'center': '0.5', 'noise_threshold': '0',
            'tolerance': '0', 'pause_threshold': '0',
        })
    ET.indent(root)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def export_integration(target, output, frequency=433920000, tcp_port=1234,
                       input_file=None, executable=None):
    """Export into a new directory. Never opens a radio or starts an application."""
    if target not in ('urh', 'gnuradio'):
        raise ValueError('Integration target must be urh or gnuradio')
    if not math.isfinite(frequency) or not is_valid_frequency(frequency):
        raise ValueError('Frequency must be within a CC1101 band')
    if not 1 <= tcp_port <= 65535:
        raise ValueError('TCP port must be between 1 and 65535')
    if executable and ('\0' in str(executable) or '\n' in str(executable)):
        raise ValueError('Executable path must be a single valid filesystem path')
    output = Path(output)
    raw = None
    if input_file:
        source = Path(input_file)
        if source.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Raw capture is too large (maximum 64 MiB)')
        raw = source.read_bytes()
        if not raw:
            raise ValueError('Raw capture is empty')
    output.mkdir(parents=True, exist_ok=False)
    try:
        config = dict(target=target, host='127.0.0.1', tcp_port=tcp_port,
                      frequency=int(frequency), sample_rate=SAMPLE_RATE,
                      sample_format='bits', receive_only=True,
                      capture_file='capture.complex' if raw is not None else None)
        if raw is not None:
            # CC1101 FIFO bytes contain eight demodulated bits, MSB first.
            bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder='big')
            bits.astype('<c8').tofile(output / 'capture.complex')
        (output / 'integration.json').write_text(json.dumps(config, indent=2) + '\n')
        assets = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'integration_templates'
        script = 'evilcrow_urh.py' if target == 'urh' else 'evilcrow_gnuradio.py'
        shutil.copyfile(assets / script, output / script)
        if target == 'urh':
            (output / 'URHProject.xml').write_bytes(_urh_project(config, config['capture_file']))
        launcher = 'Launch URH.command' if target == 'urh' else 'Launch GNU Radio.command'
        (output / launcher).write_text(_launcher(target, executable))
        (output / launcher).chmod(0o755)
        (output / 'README.txt').write_text(
            'EvilCrow receive integration\n\n'
            'Start the Flutter Project receive bridge, then open the launcher.\n'
            f'Endpoint: 127.0.0.1:{tcp_port}; frequency: {int(frequency)} Hz; ASK, 650 kHz bandwidth.\n'
            f'Sample rate: {SAMPLE_RATE} demodulated bits/second. No RF I/Q, RSSI, phase, or RF spectrum is available.\n'
            'FIFO bytes are unpacked MSB-first to real amplitudes 0 and 1; Q is zero.\n'
            'No synthetic idle samples are inserted; gaps and UART loss are not reconstructed.\n'
            'Only one receiver application can use the bridge at a time. Stop it before switching applications.\n\n'
            + ('URH 2.10: the launcher loads URHProject.xml and opens Record Signal, configured for RTL-TCP.\n'
               'Press Start in that dialog to receive. Its project may also be opened manually in URH.\n'
               'Choose a pip-installed urh executable or the Python interpreter containing URH.\n'
               'This bootstrap uses URH application APIs; frozen third-party app bundles are not supported.\n'
               'Application preferences are stored in this project folder. Existing URH preferences are preserved.\n'
               if target == 'urh' else
               'GNU Radio 3.10: the launcher opens a live Qt time plot; it requires GNU Radio and PyQt5.\n'
               'Headless capture: python3 evilcrow_gnuradio.py --duration 5 --output capture.complex\n'
               'Saved-capture playback: python3 evilcrow_gnuradio.py --gui --input capture.complex\n'
               'Stop the plot or press Ctrl-C to disconnect; Flutter owns the USB device and stops RX.\n'))
        files = sorted(path.name for path in output.iterdir())
        return dict(target=target, directory=str(output), files=files,
                    launch_file=launcher,
                    entry_file='URHProject.xml' if target == 'urh' else script,
                    sample_format='bits', sample_rate=SAMPLE_RATE)
    except BaseException:
        shutil.rmtree(output)
        raise
