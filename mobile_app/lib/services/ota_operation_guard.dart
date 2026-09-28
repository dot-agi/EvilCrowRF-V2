import 'dart:io';

import 'package:flutter/services.dart';

Future<void> setOtaOperationGuard(bool enabled) async {
  if (!Platform.isMacOS) return;
  await const MethodChannel('evilcrow/usb')
      .invokeMethod<void>('setOtaOperation', enabled);
}
