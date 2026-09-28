import Cocoa
import FlutterMacOS

/// Launch exported receive-only integrations through Launch Services. External
/// applications retain their own process and permissions; the USB helper stays
/// owned by Flutter and exposes only its localhost receive stream.
final class ExternalRadioApplications {
  private let channel: FlutterMethodChannel

  init(messenger: FlutterBinaryMessenger) {
    channel = FlutterMethodChannel(name: "evilcrow/radio-apps", binaryMessenger: messenger)
    channel.setMethodCallHandler { call, result in
      guard let path = call.arguments as? String, path.hasPrefix("/") else {
        result(FlutterError(code: "invalid_path", message: "Choose an exported integration folder.", details: nil))
        return
      }
      let url = URL(fileURLWithPath: path)
      if call.method == "reveal" {
        NSWorkspace.shared.activateFileViewerSelecting([url])
        result(nil)
        return
      }
      guard call.method == "openLauncher" else {
        result(FlutterMethodNotImplemented)
        return
      }
      do {
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
        guard url.pathExtension == "command", values.isRegularFile == true,
              values.isSymbolicLink != true else {
          throw NSError(domain: "EvilCrow", code: 1,
              userInfo: [NSLocalizedDescriptionKey: "The exported launcher must be a regular .command file."])
        }
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: path)
      } catch {
        result(FlutterError(code: "launcher_unavailable", message: error.localizedDescription, details: nil))
        return
      }
      guard let terminal = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.Terminal") else {
        result(FlutterError(code: "terminal_unavailable", message: "Terminal could not be located.", details: nil))
        return
      }
      let configuration = NSWorkspace.OpenConfiguration()
      configuration.activates = true
      NSWorkspace.shared.open([url], withApplicationAt: terminal, configuration: configuration) { _, error in
        DispatchQueue.main.async {
          if let error = error {
            result(FlutterError(code: "launch_failed", message: error.localizedDescription, details: nil))
          } else {
            result(nil)
          }
        }
      }
    }
  }
}
