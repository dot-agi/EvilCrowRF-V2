import Cocoa
import FlutterMacOS

class MainFlutterWindow: NSWindow {
  private var usbChannel: FlutterMethodChannel?

  override func awakeFromNib() {
    let flutterViewController = FlutterViewController()
    let windowFrame = self.frame
    self.contentViewController = flutterViewController
    self.setFrame(windowFrame, display: true)
    self.minSize = NSSize(width: 900, height: 640)
    self.setContentSize(NSSize(width: 1180, height: 820))
    self.center()

    RegisterGeneratedPlugins(registry: flutterViewController)

    self.delegate = NSApplication.shared.delegate as? AppDelegate
    let channel = FlutterMethodChannel(name: "evilcrow/usb",
        binaryMessenger: flutterViewController.engine.binaryMessenger)
    channel.setMethodCallHandler { call, result in
      guard call.method == "setCriticalOperation", let enabled = call.arguments as? Bool,
            let appDelegate = NSApplication.shared.delegate as? AppDelegate else {
        result(FlutterMethodNotImplemented)
        return
      }
      appDelegate.usbCriticalOperation = enabled
      result(nil)
    }
    usbChannel = channel

    super.awakeFromNib()
  }
}
