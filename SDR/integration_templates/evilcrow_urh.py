#!/usr/bin/env python3
"""Open the real URH application and its receive dialog for an exported project.

Tested with URH 2.10.0. URH's GUI entry point does not accept project paths;
this small bootstrap calls the same application controllers directly.
"""

import argparse
import json
import multiprocessing
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true',
                        help='Load the real application and report configuration without receiving')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    config = json.loads((root / 'integration.json').read_text())
    try:
        from PyQt6.QtCore import QSettings
        from PyQt6.QtWidgets import QApplication
        # Keep exported-project settings separate from the user's main URH app.
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                          str(root / 'settings'))
        from urh import settings
        from urh.controller.MainController import MainController
        from urh.controller.dialogs.ReceiveDialog import ReceiveDialog
        from urh.version import VERSION
        import urh.ui.urh_rc  # noqa: F401 - application icons
    except ImportError as error:
        raise RuntimeError('Install URH into this Python environment: python3 -m pip install urh') from error

    # These settings apply only to this exported project's private preferences.
    settings.write('RTL-TCP_is_enabled', True)
    settings.write('RTL-TCP_selected_backend', 'native')
    settings.write('lock_bandwidth_sample_rate', False)
    settings.write('auto_detect_new_signals', False)
    application = QApplication(['EvilCrow URH'])
    window = MainController()
    window.project_manager.set_project_folder(str(root), ask_for_new_project=False)
    receiver = ReceiveDialog(window.project_manager, parent=window)
    ui = receiver.device_settings_widget.ui
    # URH saves receiver parameters in a project, but IP/port are not restored
    # by DeviceSettingsWidget.bootstrap. Apply them explicitly to the real UI.
    ui.lineEditIP.setText(config['host'])
    ui.lineEditIP.editingFinished.emit()
    ui.spinBoxPort.setValue(config['tcp_port'])
    ui.spinBoxPort.editingFinished.emit()
    receiver.device_parameters_changed.connect(window.project_manager.set_device_parameters)
    receiver.files_recorded.connect(window.on_signals_recorded)
    if args.check:
        result = dict(version=VERSION, receiver=ui.cbDevice.currentText(),
                      host=ui.lineEditIP.text(), tcp_port=ui.spinBoxPort.value(),
                      frequency=ui.spinBoxFreq.value(), sample_rate=ui.spinBoxSampleRate.value(),
                      open_signals=len(window.signal_tab_controller.signal_frames))
        print(json.dumps(result), flush=True)
        receiver.close()
        window.close()
        application.processEvents()
        return 0
    window.setWindowTitle('EvilCrow demodulated bits — Universal Radio Hacker')
    window.show()
    receiver.show()  # Receiving starts only when the user presses its Start button.
    return application.exec()


if __name__ == '__main__':
    multiprocessing.set_start_method('spawn', force=True)
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'URH integration: {error}', file=sys.stderr)
        raise SystemExit(1)
