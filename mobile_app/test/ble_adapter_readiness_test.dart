import 'dart:async';

import 'package:evilcrow_rf2_controller/providers/ble_provider.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _Provider extends BleProvider {
  _Provider(this.states);
  final Stream<BluetoothAdapterState> states;
  @override
  Stream<BluetoothAdapterState> get adapterStates => states;
}

class _Device extends BluetoothDevice {
  _Device() : super.fromId('test-device');
  int connectCalls = 0;
  @override
  Future<void> connect(
      {Duration timeout = const Duration(seconds: 35),
      int? mtu = 512,
      bool autoConnect = false}) async {
    connectCalls++;
    // Stop at the transport boundary: these tests never access real BLE.
    throw StateError('test transport reached');
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late StreamController<BluetoothAdapterState> states;
  late _Provider ble;
  late _Device device;
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    states = StreamController<BluetoothAdapterState>.broadcast();
    ble = _Provider(states.stream);
    device = _Device();
  });
  tearDown(() async {
    ble.dispose();
    await states.close();
  });

  test('cached connect waits for a resolved on state without scanning',
      () async {
    await Future<void>.delayed(Duration.zero);
    states.add(BluetoothAdapterState.unknown);
    await Future<void>.delayed(Duration.zero);
    expect(ble.statusMessage, 'Bluetooth initializing...');

    final connecting = ble.connectToDevice(device);
    await Future<void>.delayed(Duration.zero);
    expect(device.connectCalls, 0);
    // A repeated click must not queue another native connection.
    await ble.connectToDevice(device);
    states.add(BluetoothAdapterState.turningOn);
    await Future<void>.delayed(Duration.zero);
    expect(device.connectCalls, 0);
    states.add(BluetoothAdapterState.on);
    await connecting;
    expect(device.connectCalls, 1);
  });

  test('permission denial is distinct from powered off and blocks connect',
      () async {
    await Future<void>.delayed(Duration.zero);
    final connecting = ble.connectToDevice(device);
    states.add(BluetoothAdapterState.unauthorized);
    await connecting;
    expect(device.connectCalls, 0);
    expect(ble.statusMessage, contains('Bluetooth permission denied'));
    expect(ble.statusMessage, isNot(contains('Bluetooth disabled')));
  });

  test('unresolved adapter times out before native connect', () async {
    await Future<void>.delayed(Duration.zero);
    final connecting = ble.connectToDevice(device);
    states.add(BluetoothAdapterState.unknown);
    await connecting;
    expect(device.connectCalls, 0);
    expect(ble.statusMessage, contains('Bluetooth initialization timed out'));
  });

  test('a resolved powered-off adapter does not attempt connect', () async {
    await Future<void>.delayed(Duration.zero);
    final connecting = ble.connectToDevice(device);
    states.add(BluetoothAdapterState.off);
    await connecting;
    expect(device.connectCalls, 0);
    expect(ble.statusMessage, contains('Bluetooth disabled'));
  });
}
