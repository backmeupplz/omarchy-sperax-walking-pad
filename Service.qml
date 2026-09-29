import QtQuick
import Quickshell
import Quickshell.Io

// Owns the one BLE link to the pad. The bar is built per monitor, and the pad
// takes a single connection, so the widgets all read this singleton instead of
// each spawning a helper.
Item {
  id: root

  property var shell: null

  property bool connected: false
  // idle | countdown | running | stopping, from the pad's own telemetry.
  property string padState: ""
  property int countdown: 0
  property int steps: 0
  property real distanceKm: 0
  property int seconds: 0
  property real speedKmh: 0
  // Last 7 days oldest first, today last: [{date, steps, distanceKm, seconds}].
  // Kept by pad.py so today survives stopping, restarts and the pad being off.
  property var history: []
  readonly property var today: history.length ? history[history.length - 1] : ({ steps: 0, distanceKm: 0, seconds: 0 })
  // What start/+/- ask for; the pad's own report lands in speedKmh.
  property real targetKmh: 1.0

  readonly property real minKmh: 0.2
  readonly property real maxKmh: 4.0
  readonly property bool running: padState === "running" || padState === "countdown"
  // A start or stop sent but not yet reflected by the pad (the belt counts down
  // 3-2-1 first), so the switch can show it's working rather than look stuck.
  property string pending: ""

  function send(line) {
    if (helper.running) helper.write(line + "\n")
  }

  function start() { pending = "start"; pendingTimeout.restart(); send("start " + targetKmh.toFixed(1)) }
  function stop() { pending = "stop"; pendingTimeout.restart(); send("stop") }
  function toggle() { running ? stop() : start() }

  function setSpeed(kmh) {
    targetKmh = Math.round(Math.max(minKmh, Math.min(maxKmh, kmh)) * 10) / 10
    if (running) send("speed " + targetKmh.toFixed(1))
  }

  function apply(s) {
    connected = s.connected === true
    padState = s.state || ""
    countdown = s.countdown || 0
    if ((pending === "start" && running) || (pending === "stop" && !running)) pending = ""
    steps = s.steps || 0
    distanceKm = s.distanceKm || 0
    seconds = s.seconds || 0
    speedKmh = s.speedKmh || 0
    if (s.history) history = s.history
  }

  Process {
    id: helper
    running: true
    stdinEnabled: true
    command: ["python3", decodeURIComponent(Qt.resolvedUrl("pad.py").toString().replace(/^file:\/\//, ""))]
    stdout: SplitParser {
      onRead: function(line) {
        try { root.apply(JSON.parse(line)) } catch (e) {}
      }
    }
    stderr: SplitParser { onRead: function(line) { console.warn("walking-pad:", line) } }
    onExited: {
      root.connected = false
      root.pending = ""
      respawn.restart()
    }
  }

  Timer {
    id: pendingTimeout
    interval: 10000
    onTriggered: root.pending = ""
  }

  // pad.py only exits on a crash (e.g. bleak missing); don't spin on it.
  Timer {
    id: respawn
    interval: 10000
    onTriggered: helper.running = true
  }
}
