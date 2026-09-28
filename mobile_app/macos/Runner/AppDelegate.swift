import Cocoa
import FlutterMacOS

@main
class AppDelegate: FlutterAppDelegate, NSWindowDelegate {
  var usbCriticalOperation = false
  var otaCriticalOperation = false

  private func allowClosing() -> Bool {
    if !usbCriticalOperation && !otaCriticalOperation { return true }
    let alert = NSAlert()
    alert.messageText = otaCriticalOperation ? "Firmware update in progress" : "USB operation in progress"
    alert.informativeText = "Keep the device connected and wait for backup or firmware and restart verification to finish before closing."
    alert.addButton(withTitle: "Keep Open")
    alert.runModal()
    return false
  }

  override func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
    return allowClosing() ? .terminateNow : .terminateCancel
  }

  func windowShouldClose(_ sender: NSWindow) -> Bool {
    return allowClosing()
  }

  override func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
    return true
  }

  override func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool {
    return true
  }
}
