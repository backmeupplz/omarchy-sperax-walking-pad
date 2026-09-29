import QtQuick
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.backmeupplz.sperax-walking-pad"
  manageIpc: false

  readonly property var pad: bar && bar.shell ? bar.shell.serviceFor("io.github.backmeupplz.sperax-walking-pad") : null
  readonly property bool connected: pad ? pad.connected : false
  readonly property bool walking: pad ? pad.running : false
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color track: Style.selectedFillFor(foreground, Color.accent)
  readonly property var today: pad ? pad.today : ({ steps: 0, distanceKm: 0, seconds: 0 })
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string glyph: String.fromCodePoint(0xF0583) // nf-md-walk

  function clock(s) {
    var h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = s % 60
    var mm = (h && m < 10 ? "0" : "") + m
    return (h ? h + ":" : "") + mm + ":" + (sec < 10 ? "0" : "") + sec
  }

  function count(n) {
    return Number(n).toLocaleString(Qt.locale(), "f", 0)
  }

  function dayName(iso) {
    return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][new Date(iso + "T00:00:00").getDay()]
  }

  function nudge(delta) {
    if (pad) pad.setSpeed(pad.targetKmh + delta)
  }

  function toggleBelt() {
    if (pad && connected) pad.toggle()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) Qt.callLater(function() { keyCatcher.forceActiveFocus() })

  // WidgetButton sizes to its label; BarIconButton's fixed icon slot let the
  // step count spill into the next widget.
  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    dimmed: !root.connected
    text: root.today.steps > 0 ? root.glyph + " " + root.count(root.today.steps) : root.glyph
    tooltipText: ""
    onPressed: function(b) {
      if (b === Qt.MiddleButton) root.toggleBelt()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(700))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { root.nudge((dx - dy) * 0.1) }
      onActivateRequested: root.toggleBelt()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === " " || t === "s" || t === "S") root.toggleBelt()
        else if (t === "+" || t === "=") root.nudge(0.1)
        else if (t === "-") root.nudge(-0.1)
      }

      Column {
        id: column
        width: parent.width
        spacing: Style.space(12)

        PanelHero {
          id: hero
          width: parent.width
          title: "Walking pad"
          meta: !root.connected ? "Looking for the pad…"
            : pad.padState === "countdown" ? "Starting in " + pad.countdown + "…"
            : pad.padState === "running" ? "Walking · " + pad.speedKmh.toFixed(1) + " km/h"
            : pad.padState === "stopping" || pad.pending === "stop" ? "Stopping…"
            : pad.pending === "start" ? "Starting…"
            : "Ready"
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconOpacity: root.walking ? 1.0 : 0.5
          iconComponent: Component {
            Text {
              text: root.glyph
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
            }
          }
          trailingControl: Component {
            ToggleSwitch {
              visible: root.connected
              // Throw the knob on click; the pad takes a few seconds to agree.
              checked: pad && pad.pending ? pad.pending === "start" : root.walking
              busy: !!pad && pad.pending !== ""
              foreground: root.foreground
              onToggled: root.toggleBelt()
            }
          }
        }

        RowLayout {
          width: parent.width
          spacing: Style.space(8)
          Stat { label: "STEPS TODAY"; value: root.count(root.today.steps) }
          Stat { label: "KM"; value: root.today.distanceKm.toFixed(2) }
          Stat { label: "TIME"; value: root.clock(root.today.seconds) }
        }

        PanelSeparator { foreground: root.foreground }

        Column {
          id: week
          width: parent.width
          spacing: Style.spacing.md
          readonly property var days: pad ? pad.history : []
          readonly property real peak: Math.max(1, ...days.map(d => d.steps))

          PanelSectionHeader {
            text: "STEPS BY DAY"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            model: week.days
            DayRow {
              required property var modelData
              required property int index
              width: week.width
              label: index === week.days.length - 1 ? "Today" : root.dayName(modelData.date)
              value: root.count(modelData.steps)
              ratio: modelData.steps / week.peak
              today: index === week.days.length - 1
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        PanelSectionHeader {
          text: "SPEED  " + (pad ? pad.targetKmh.toFixed(1) : "–") + " km/h"
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        RowLayout {
          width: parent.width
          spacing: Style.space(8)
          enabled: root.connected

          PanelActionButton {
            iconText: "−"
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.nudge(-0.1)
          }

          PanelSlider {
            Layout.fillWidth: true
            bar: root.bar
            minimum: pad ? pad.minKmh : 0.2
            maximum: pad ? pad.maxKmh : 4
            step: 0.1
            value: pad ? pad.targetKmh : 1
            onReleased: function(v) { if (root.pad) root.pad.setSpeed(v) }
          }

          PanelActionButton {
            iconText: "+"
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.nudge(0.1)
          }
        }
      }
    }
  }

  // Same row as the Agents panel's tokens-by-day chart.
  component DayRow: Item {
    id: dayRow
    property string label: ""
    property string value: ""
    property real ratio: 0
    property bool today: false

    implicitHeight: Math.max(dayLabel.implicitHeight, dayValue.implicitHeight) + Style.spacing.sm

    Text {
      id: dayLabel
      text: dayRow.label
      color: dayRow.today ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: dayRow.today
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(52)
    }

    Rectangle {
      anchors.left: dayLabel.right
      anchors.right: dayValue.left
      anchors.leftMargin: Style.space(8)
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      height: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))
      radius: height / 2
      color: root.track

      Rectangle {
        height: parent.height
        radius: parent.radius
        width: parent.width * Math.max(0, Math.min(1, dayRow.ratio))
        color: dayRow.today ? root.foreground : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.55)
        Behavior on width { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
      }
    }

    Text {
      id: dayValue
      text: dayRow.value
      color: dayRow.today ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      horizontalAlignment: Text.AlignRight
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(52)
    }
  }

  component Stat: Column {
    property string label: ""
    property string value: ""
    Layout.fillWidth: true
    spacing: Style.space(2)

    Text {
      text: parent.value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.heading
    }
    Text {
      text: parent.label
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}
