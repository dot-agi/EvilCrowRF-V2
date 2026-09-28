import 'package:evilcrow_rf2_controller/providers/ble_provider.dart';
import 'package:evilcrow_rf2_controller/providers/log_provider.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('BLE teardown does not call a disposed UI provider', () async {
    SharedPreferences.setMockInitialValues({});
    final ble = BleProvider();
    final log = LogProvider();
    ble.setLogCallback((level, message, {details}) => log.addInfoLog(message));
    ble.setNotificationCallback((level, message) => log.addInfoLog(message));

    // MultiProvider can dispose these UI callbacks before the BLE provider.
    log.dispose();
    ble.dispose();
    await Future<void>.delayed(Duration.zero);
    await ble.disconnect();
    ble.notifyListeners();
  });
}
