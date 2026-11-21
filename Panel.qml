import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Topbar tray wallpaper rotator supporting:
// - Start / Stop auto-rotation control
// - Static wallpapers from Wallhaven (with optional per-period color palette)
// - Live video wallpapers from Pixabay
// - Time-of-day tags: Morning (7am-12pm), Noon (12pm-3pm), Evening (3pm-6pm), Night (6pm-7am)
// - Next, Previous, Shuffle actions & Recent 10 wallpapers
Panel {
  id: root
  moduleName: "ric-v.wallrotator"
  ipcTarget: "wallrotator"
  manageIpc: false

  Commons.Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  component PlainText: Text {
    textFormat: Text.PlainText
  }

  readonly property string script: decodeURIComponent(String(Qt.resolvedUrl("wallrotator.py")).replace(/^file:\/\//, ""))
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color accent: Commons.Color.accent
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // Curated official Wallhaven palette hex codes (without '#')
  readonly property var wallhavenPalette: [
    "cc0000", "ff6600", "ff9900", "ffcc33", "ffff00",
    "77cc33", "336600", "66cccc", "0099cc", "0066cc",
    "333399", "663399", "993399", "ea4c88", "424153",
    "000000", "999999", "ffffff"
  ]

  // State synchronized with wallrotator.py
  property string mode: "wallhaven"
  property bool running: false
  property string currentPeriod: "night"
  property string selectedPeriod: "night"
  property bool periodPickedByUser: false
  property var periodTags: ({
    "morning": ["sunrise", "morning mist", "forest", "meadow", "fresh nature"],
    "noon": ["sunny landscape", "ocean", "mountains", "waterfall", "blue sky"],
    "evening": ["sunset", "golden hour", "autumn", "city skyline", "warm clouds"],
    "night": ["night sky", "cyberpunk", "aurora", "galaxy", "neon city"]
  })
  property var periodColors: ({
    "morning": "",
    "noon": "",
    "evening": "",
    "night": ""
  })
  property int scheduleMinutes: 30
  property bool shuffle: true
  property var history: []
  property int historyIndex: 0
  property string currentPath: ""
  property string themeName: ""
  property bool hasPixabayKey: false
  property bool hasWallhavenKey: false
  property bool editingKey: false
  property string keySavedProvider: ""
  property bool busy: false
  property string errorCode: ""
  property string errorDetail: ""
  property var pendingCommands: []
  property string activeCmd: ""
  property int pendingSchedule: -1
  property int pendingRunning: -1

  readonly property bool activeModeHasKey: root.mode === "pixabay" ? root.hasPixabayKey : root.hasWallhavenKey
  readonly property string activeProviderLabel: root.mode === "pixabay" ? "Pixabay" : "Wallhaven"
  readonly property bool showKeyEditor: root.editingKey || (root.mode === "pixabay" && !root.hasPixabayKey)
  readonly property var selectedTags: (root.periodTags && root.periodTags[root.selectedPeriod]) ? root.periodTags[root.selectedPeriod] : []
  readonly property string selectedColor: (root.periodColors && root.periodColors[root.selectedPeriod]) ? String(root.periodColors[root.selectedPeriod]) : ""

  function periodLabel(p) {
    return strings.t("period." + p, p)
  }

  function periodRange(p) {
    return strings.t("period." + p + "Range", "")
  }

  // Neutralize markup before passing any dynamic string to shared sinks
  function neutralizeMarkup(val) {
    return String(val || "").replace(/[<>&"'`\x00-\x1f\x7f]/g, " ").replace(/\s+/g, " ").trim()
  }

  readonly property string errorText: {
    if (root.errorCode === "") return ""
    var t = strings.t("error." + root.errorCode, "")
    if (t === "") t = strings.t("error.generic")
    return t.indexOf("%1") >= 0 ? t.arg(root.neutralizeMarkup(root.errorDetail)) : t
  }

  function hasQueuedCommand(cmdName) {
    for (var i = 0; i < root.pendingCommands.length; i++) {
      if (root.pendingCommands[i] && root.pendingCommands[i][0] === cmdName) return true
    }
    return false
  }

  function applyStatus(r, sourceCmd) {
    if (!r) return
    if (r.ok === false) {
      root.errorCode = String(r.code || "generic").replace(/[^a-z_]/g, "").substring(0, 40)
      root.errorDetail = root.neutralizeMarkup(r.detail || "").substring(0, 200)
      return
    }
    root.errorCode = ""
    root.errorDetail = ""
    if (r.mode === "wallhaven" || r.mode === "pixabay") root.mode = r.mode

    // Only accept running / scheduleMinutes from backend once any user-initiated
    // schedule/running change has been persisted (never let a stale in-flight
    // wallpaper fetch restore an old schedule or restart rotation).
    if (sourceCmd === "set-schedule" && !root.hasQueuedCommand("set-schedule")) {
      root.pendingSchedule = -1
      if (!root.hasQueuedCommand("set-running")) root.pendingRunning = -1
    } else if (sourceCmd === "set-running" && !root.hasQueuedCommand("set-running")) {
      root.pendingRunning = -1
      if (!root.hasQueuedCommand("set-schedule")) root.pendingSchedule = -1
    }

    if (r.running !== undefined && root.pendingRunning < 0) root.running = !!r.running
    if (r.currentPeriod === "morning" || r.currentPeriod === "noon" || r.currentPeriod === "evening" || r.currentPeriod === "night") {
      root.currentPeriod = r.currentPeriod
      if (!root.periodPickedByUser) root.selectedPeriod = r.currentPeriod
    }
    if (r.periodTags && typeof r.periodTags === "object") {
      var pt = {}
      var keys = ["morning", "noon", "evening", "night"]
      for (var i = 0; i < keys.length; i++) {
        var k = keys[i]
        var arr = Array.isArray(r.periodTags[k]) ? r.periodTags[k] : []
        pt[k] = arr.slice(0, 15).map(function(t) { return root.neutralizeMarkup(t).substring(0, 28) })
      }
      root.periodTags = pt
    }
    if (r.periodColors && typeof r.periodColors === "object") {
      var pc = {}
      var ckeys = ["morning", "noon", "evening", "night"]
      for (var j = 0; j < ckeys.length; j++) {
        var ck = ckeys[j]
        var cv = String(r.periodColors[ck] || "").replace(/[^0-9a-fA-F]/g, "").toLowerCase().substring(0, 6)
        pc[ck] = cv.length === 6 ? cv : ""
      }
      root.periodColors = pc
    }
    if (r.scheduleMinutes !== undefined && root.pendingSchedule < 0) root.scheduleMinutes = Number(r.scheduleMinutes) || 0
    if (r.shuffle !== undefined) root.shuffle = !!r.shuffle
    if (Array.isArray(r.history)) {
      root.history = r.history.slice(0, 10).map(function(h) {
        return {
          id: root.neutralizeMarkup(h.id || "").substring(0, 40),
          path: String(h.path || ""),
          thumb: String(h.thumb || ""),
          title: root.neutralizeMarkup(h.title || "").substring(0, 48),
          mode: h.mode === "pixabay" ? "pixabay" : "wallhaven",
          resolution: root.neutralizeMarkup(h.resolution || "").substring(0, 24),
          tag: root.neutralizeMarkup(h.tag || "").substring(0, 28)
        }
      })
    }
    if (r.historyIndex !== undefined) root.historyIndex = Number(r.historyIndex) || 0
    if (r.currentPath !== undefined) root.currentPath = String(r.currentPath || "")
    if (r.theme !== undefined) root.themeName = root.neutralizeMarkup(r.theme || "").substring(0, 40)
    if (r.hasPixabayKey !== undefined) root.hasPixabayKey = !!r.hasPixabayKey
    if (r.hasWallhavenKey !== undefined) root.hasWallhavenKey = !!r.hasWallhavenKey
    if (r.keySaved) {
      root.editingKey = false
      root.keySavedProvider = r.keySaved === "pixabay" ? "Pixabay" : "Wallhaven"
    }
  }

  function startCommand(args) {
    root.busy = true
    root.errorCode = ""
    root.activeCmd = String((args && args[0]) || "")
    cmdProc.command = ["python3", "-I", root.script].concat(args)
    cmdProc.running = true
  }

  function runCommand(args) {
    if (cmdProc.running) return
    root.startCommand(args)
  }

  function queueCommand(args, dedupeCmd) {
    if (!cmdProc.running) {
      root.startCommand(args)
      return
    }
    var nextQueue = []
    for (var i = 0; i < root.pendingCommands.length; i++) {
      var item = root.pendingCommands[i]
      if (dedupeCmd && item && item[0] === dedupeCmd) continue
      nextQueue.push(item)
    }
    nextQueue.push(args)
    root.pendingCommands = nextQueue
  }

  function refresh() {
    if (!statusProc.running && !cmdProc.running) {
      statusProc.command = ["python3", "-I", root.script, "status"]
      statusProc.running = true
    }
  }

  function setMode(m) {
    if (m === root.mode) return
    root.mode = m
    root.keySavedProvider = ""
    root.queueCommand(["set-mode", m], "set-mode")
  }

  function setRunning(want) {
    var w = !!want
    root.running = w
    root.pendingRunning = w ? 1 : 0
    if (w && root.scheduleMinutes === 0) {
      root.scheduleMinutes = 30
      root.pendingSchedule = 30
    }
    root.queueCommand(["set-running", w ? "true" : "false"], "set-running")
  }

  function toggleRunning() {
    root.setRunning(!root.running)
  }

  function setSchedule(mins) {
    var m = Number(mins) || 0
    root.scheduleMinutes = m
    root.running = m > 0
    root.pendingSchedule = m
    root.pendingRunning = m > 0 ? 1 : 0
    root.queueCommand(["set-schedule", String(m)], "set-schedule")
  }

  function next() {
    root.runCommand(["next"])
  }

  function prev() {
    root.runCommand(["prev"])
  }

  function shuffleNow() {
    root.runCommand(["shuffle"])
  }

  function addTag(raw) {
    var clean = root.neutralizeMarkup(raw).toLowerCase().replace(/[^a-z0-9 _+-]/g, "").trim()
    if (clean === "") return
    tagInput.text = ""
    root.runCommand(["add-tag", clean, root.selectedPeriod])
  }

  function removeTag(tag) {
    root.runCommand(["remove-tag", String(tag), root.selectedPeriod])
  }

  function setPeriodColor(hex) {
    root.runCommand(["set-color", root.selectedPeriod, hex === "" ? "none" : hex])
  }

  function applyHistory(path) {
    if (!path) return
    root.runCommand(["apply", String(path)])
  }

  function saveApiKey() {
    var k = keyField.text.trim()
    if (k === "" || keyProc.running) return
    root.busy = true
    root.errorCode = ""
    root.keySavedProvider = ""
    keyProc.command = ["python3", "-I", root.script, "set-key", root.mode]
    keyProc.running = true
    keyProc.write(k + "\n")
    keyField.text = ""
  }

  Component.onCompleted: root.refresh()
  onOpenedChanged: {
    if (opened) {
      root.periodPickedByUser = false
      root.refresh()
    }
  }

  ShellIpc {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function next(): void { root.next() }
    function prev(): void { root.prev() }
    function shuffle(): void { root.shuffleNow() }
    function start(): void { root.setRunning(true) }
    function stop(): void { root.setRunning(false) }
    function toggleRunning(): void { root.toggleRunning() }
  }

  Timer {
    id: scheduleTimer
    interval: Math.max(1, root.scheduleMinutes) * 60 * 1000
    running: root.running && root.scheduleMinutes > 0 && root.pendingRunning !== 0 && root.pendingSchedule !== 0
    repeat: true
    onTriggered: {
      if (root.running && root.scheduleMinutes > 0 && root.pendingRunning !== 0 && root.pendingSchedule !== 0) {
        root.next()
      }
    }
  }

  Timer {
    id: periodClockTimer
    interval: 60000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  Process {
    id: statusProc
    stdout: SplitParser {
      onRead: function(line) {
        try { root.applyStatus(JSON.parse(line), "status") } catch (e) {}
      }
    }
  }

  Process {
    id: cmdProc
    stdout: SplitParser {
      onRead: function(line) {
        try { root.applyStatus(JSON.parse(line), root.activeCmd) } catch (e) {}
      }
    }
    onExited: {
      if (root.pendingCommands.length > 0) {
        var nextArgs = root.pendingCommands[0]
        root.pendingCommands = root.pendingCommands.slice(1)
        root.startCommand(nextArgs)
      } else {
        root.activeCmd = ""
        root.busy = false
      }
    }
  }

  Process {
    id: keyProc
    stdout: SplitParser {
      onRead: function(line) {
        try { root.applyStatus(JSON.parse(line), "set-key") } catch (e) {}
      }
    }
    onExited: {
      root.busy = false
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰸉"
    active: root.busy || root.running
    activeColor: root.accent
    tooltipText: root.busy
      ? strings.t("bar.tooltipBusy")
      : strings.t("bar.tooltip")
          .arg(root.mode === "pixabay" ? strings.t("mode.pixabay") : strings.t("mode.wallhaven"))
          .arg(root.running ? strings.t("status.running") : strings.t("status.stopped"))
    onPressed: function(b) {
      if (b === Qt.RightButton) root.next()
      else if (b === Qt.MiddleButton) root.shuffleNow()
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
    contentWidth: panel.fittedContentWidth(Style.space(396))
    contentHeight: panel.fittedContentHeight(mainColumn.implicitHeight + Style.space(8), Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: tagInput.activeFocus || keyField.activeFocus
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      ScrollView {
        id: scroll
        anchors.fill: parent
        clip: true
        ScrollBar.vertical: ScrollBar {
          policy: ScrollBar.AsNeeded
          active: true
        }

        Column {
          id: mainColumn
          width: scroll.width
          spacing: Style.space(10)

          // Header with Start / Stop button
          RowLayout {
            width: parent.width
            spacing: Style.space(8)

            Column {
              spacing: Style.space(2)
              PlainText {
                text: strings.t("panel.title")
                font.family: root.fontFamily
                font.pixelSize: Style.font.heading
                font.bold: true
                color: root.foreground
              }

              PlainText {
                text: (root.busy ? strings.t("panel.busy") : (root.running ? strings.t("status.running") : strings.t("status.stopped")))
                  + " · " + root.periodLabel(root.currentPeriod) + " (" + root.periodRange(root.currentPeriod) + ")"
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                color: root.busy || root.running ? root.accent : root.dim
              }
            }

            Item { Layout.fillWidth: true }

            Button {
              text: root.running ? strings.t("panel.stop") : strings.t("panel.start")
              iconText: root.running ? "󰓛" : "󰐊"
              tooltipText: root.running ? strings.t("panel.stopTip") : strings.t("panel.startTip")
              bordered: true
              selected: root.running
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onClicked: root.toggleRunning()
            }
          }

          // Error banner (if any)
          PlainText {
            visible: root.errorText !== ""
            width: parent.width
            wrapMode: Text.WordWrap
            text: root.errorText
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          PanelSeparator { foreground: root.foreground }

          // 1. Mode Switcher: Static (Wallhaven) vs Live (Pixabay) + API Key
          Column {
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader {
              text: strings.t("section.mode")
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            ButtonGroup {
              width: parent.width
              options: [
                { value: "wallhaven", label: strings.t("mode.wallhaven"), icon: "󰋩" },
                { value: "pixabay", label: strings.t("mode.pixabay"), icon: "󰕧" }
              ]
              value: root.mode
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onChanged: function(v) { root.setMode(v) }
            }

            // API Key / Token status and editor for both Wallhaven & Pixabay
            RowLayout {
              width: parent.width
              spacing: Style.space(6)

              PlainText {
                Layout.fillWidth: true
                elide: Text.ElideRight
                text: root.keySavedProvider !== ""
                  ? strings.t("key.savedNotice").arg(root.keySavedProvider)
                  : (root.activeModeHasKey
                    ? "󰌆 " + strings.t("key.statusSet").arg(root.activeProviderLabel)
                    : "󰌊 " + (root.mode === "pixabay" ? strings.t("key.statusRequired") : strings.t("key.statusOptional")))
                color: root.keySavedProvider !== "" || root.activeModeHasKey ? root.accent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              Button {
                text: root.showKeyEditor && !(root.mode === "pixabay" && !root.hasPixabayKey)
                  ? strings.t("key.cancelBtn")
                  : (root.activeModeHasKey ? strings.t("key.changeBtn") : strings.t("key.setBtn"))
                iconText: "󰌆"
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                horizontalPadding: Style.space(8)
                verticalPadding: Style.space(3)
                visible: !(root.mode === "pixabay" && !root.hasPixabayKey)
                onClicked: {
                  root.keySavedProvider = ""
                  root.editingKey = !root.editingKey
                  if (root.editingKey) {
                    Qt.callLater(function() { keyField.forceActiveFocus() })
                  } else {
                    keyField.text = ""
                  }
                }
              }
            }

            Column {
              visible: root.showKeyEditor
              width: parent.width
              spacing: Style.space(4)

              RowLayout {
                width: parent.width
                spacing: Style.space(6)

                TextField {
                  id: keyField
                  Layout.fillWidth: true
                  password: true
                  foreground: root.foreground
                  placeholderText: root.mode === "pixabay"
                    ? strings.t("key.placeholderPixabay")
                    : strings.t("key.placeholderWallhaven")
                  maximumLength: 120
                  verticalPadding: Style.space(4)
                  onAccepted: root.saveApiKey()
                  Keys.onEscapePressed: function(event) {
                    root.editingKey = false
                    keyField.text = ""
                    event.accepted = true
                  }
                }

                Button {
                  text: strings.t("key.save")
                  bordered: true
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  enabled: !root.busy && keyField.text.trim() !== ""
                  onClicked: root.saveApiKey()
                }
              }
            }
          }

          PanelSeparator { foreground: root.foreground }

          // 2. Actions: Previous, Next, Shuffle
          Column {
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader {
              text: strings.t("section.actions")
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Row {
              id: actionsRow
              width: parent.width
              spacing: Style.space(6)
              readonly property real btnW: Math.floor((width - spacing * 2) / 3)

              Button {
                width: actionsRow.btnW
                text: strings.t("action.prev")
                iconText: "󰒮"
                tooltipText: strings.t("action.prevTip")
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                enabled: !root.busy && root.history.length > 0
                onClicked: root.prev()
              }

              Button {
                width: actionsRow.btnW
                text: strings.t("action.next")
                iconText: "󰒭"
                tooltipText: strings.t("action.nextTip")
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                enabled: !root.busy
                onClicked: root.next()
              }

              Button {
                width: actionsRow.btnW
                text: strings.t("action.shuffle")
                iconText: "󰒝"
                tooltipText: strings.t("action.shuffleTip").arg(root.shuffle ? strings.t("state.on") : strings.t("state.off"))
                bordered: true
                selected: root.shuffle
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                enabled: !root.busy
                onClicked: root.shuffleNow()
                onRightClicked: root.runCommand(["set-shuffle", root.shuffle ? "false" : "true"])
              }
            }
          }

          PanelSeparator { foreground: root.foreground }

          // 3. Time-of-Day Tags & Optional Wallhaven Color Palette
          Column {
            width: parent.width
            spacing: Style.space(6)

            RowLayout {
              width: parent.width
              PanelSectionHeader {
                text: strings.t("section.tags")
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Item { Layout.fillWidth: true }
              PlainText {
                text: root.periodRange(root.selectedPeriod)
                  + (root.selectedPeriod === root.currentPeriod ? " · " + strings.t("period.activeNow") : "")
                color: root.selectedPeriod === root.currentPeriod ? root.accent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            ButtonGroup {
              width: parent.width
              options: [
                { value: "morning", label: (root.currentPeriod === "morning" ? "● " : "") + strings.t("period.morning"), tooltip: strings.t("period.morningRange") },
                { value: "noon", label: (root.currentPeriod === "noon" ? "● " : "") + strings.t("period.noon"), tooltip: strings.t("period.noonRange") },
                { value: "evening", label: (root.currentPeriod === "evening" ? "● " : "") + strings.t("period.evening"), tooltip: strings.t("period.eveningRange") },
                { value: "night", label: (root.currentPeriod === "night" ? "● " : "") + strings.t("period.night"), tooltip: strings.t("period.nightRange") }
              ]
              value: root.selectedPeriod
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              onChanged: function(v) {
                root.periodPickedByUser = true
                root.selectedPeriod = v
              }
            }

            // Recessed card visually separating the selected period's tags & palette from the period tabs
            Rectangle {
              width: parent.width
              implicitHeight: periodCardCol.implicitHeight + Style.space(18)
              radius: Style.cornerRadius
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.04)
              border.width: 1
              border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.14)

              Column {
                id: periodCardCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: Style.space(9)
                spacing: Style.space(8)

                PlainText {
                  text: strings.t("tags.sublabel").arg(root.periodLabel(root.selectedPeriod))
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }

                Flow {
                  width: parent.width
                  spacing: Style.space(6)

                  Repeater {
                    model: root.selectedTags

                    delegate: Button {
                      required property var modelData
                      text: "#" + root.neutralizeMarkup(modelData) + "  ✕"
                      tooltipText: strings.t("tags.removeTip").arg(root.neutralizeMarkup(modelData))
                      bordered: true
                      foreground: root.foreground
                      fontFamily: root.fontFamily
                      fontSize: Style.font.caption
                      horizontalPadding: Style.space(8)
                      verticalPadding: Style.space(3)
                      enabled: !root.busy
                      onClicked: root.removeTag(modelData)
                    }
                  }
                }

                PlainText {
                  visible: root.selectedTags.length === 0
                  text: strings.t("tags.empty").arg(root.periodLabel(root.selectedPeriod))
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                RowLayout {
                  width: parent.width
                  spacing: Style.space(6)

                  TextField {
                    id: tagInput
                    Layout.fillWidth: true
                    foreground: root.foreground
                    placeholderText: strings.t("tags.placeholder").arg(root.periodLabel(root.selectedPeriod))
                    maximumLength: 28
                    verticalPadding: Style.space(4)
                    onAccepted: root.addTag(text)
                  }

                  Button {
                    text: strings.t("tags.add")
                    iconText: "󰐕"
                    bordered: true
                    foreground: root.foreground
                    fontFamily: root.fontFamily
                    fontSize: Style.font.bodySmall
                    enabled: !root.busy && tagInput.text.trim() !== ""
                    onClicked: root.addTag(tagInput.text)
                  }
                }

                Rectangle {
                  width: parent.width
                  height: 1
                  color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)
                }

                // Optional Color Palette (Static / Wallhaven only)
                Column {
                  visible: root.mode === "wallhaven"
                  width: parent.width
                  spacing: Style.space(6)

                  RowLayout {
                    width: parent.width
                    PlainText {
                      text: strings.t("palette.label")
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: true
                    }
                    Item { Layout.fillWidth: true }
                    PlainText {
                      text: root.selectedColor !== "" ? ("#" + root.selectedColor.toUpperCase()) : strings.t("palette.any")
                      color: root.selectedColor !== "" ? root.accent : root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: root.selectedColor !== ""
                    }
                  }

                  Flow {
                    width: parent.width
                    spacing: Style.space(5)

                    // "Any" (no color filter) button
                    Button {
                      text: strings.t("palette.any")
                      tooltipText: strings.t("palette.anyTip")
                      bordered: true
                      selected: root.selectedColor === ""
                      foreground: root.foreground
                      fontFamily: root.fontFamily
                      fontSize: Math.max(10, Style.font.caption - 1)
                      horizontalPadding: Style.space(6)
                      verticalPadding: Style.space(2)
                      enabled: !root.busy
                      onClicked: root.setPeriodColor("")
                    }

                    Repeater {
                      model: root.wallhavenPalette

                      delegate: Rectangle {
                        id: swatch
                        required property string modelData
                        readonly property bool isSelected: root.selectedColor === modelData

                        width: Style.space(20)
                        height: Style.space(20)
                        radius: Style.cornerRadius
                        color: "#" + modelData
                        border.width: isSelected ? 2 : (swatchMouse.containsMouse ? 1.5 : 1)
                        border.color: isSelected ? root.accent : (swatchMouse.containsMouse ? root.foreground : Qt.rgba(1, 1, 1, 0.25))

                        PlainText {
                          visible: swatch.isSelected
                          anchors.centerIn: parent
                          text: "✓"
                          color: (swatch.modelData === "ffffff" || swatch.modelData === "ffff00" || swatch.modelData === "ffcc33" || swatch.modelData === "cccccc") ? "black" : "white"
                          font.family: root.fontFamily
                          font.pixelSize: Math.max(9, Style.font.caption - 2)
                          font.bold: true
                        }

                        MouseArea {
                          id: swatchMouse
                          anchors.fill: parent
                          hoverEnabled: true
                          cursorShape: Qt.PointingHandCursor
                          enabled: !root.busy
                          onClicked: root.setPeriodColor(swatch.isSelected ? "" : swatch.modelData)
                        }

                        PanelToolTip {
                          visible: swatchMouse.containsMouse
                          text: strings.t("palette.colorTip").arg(swatch.modelData.toUpperCase())
                          fontFamily: root.fontFamily
                        }
                      }
                    }
                  }
                }

                PlainText {
                  visible: root.mode === "pixabay"
                  width: parent.width
                  wrapMode: Text.WordWrap
                  text: strings.t("palette.pixabayNote")
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }
          }

          PanelSeparator { foreground: root.foreground }

          // 4. Schedule (Auto-Rotation)
          Column {
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader {
              text: strings.t("section.schedule")
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            ButtonGroup {
              width: parent.width
              options: [
                { value: "0", label: strings.t("schedule.off") },
                { value: "5", label: strings.t("schedule.5m") },
                { value: "15", label: strings.t("schedule.15m") },
                { value: "30", label: strings.t("schedule.30m") },
                { value: "60", label: strings.t("schedule.1h") },
                { value: "120", label: strings.t("schedule.2h") }
              ]
              value: String(root.scheduleMinutes)
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              onChanged: function(v) { root.setSchedule(Number(v)) }
            }

            PlainText {
              width: parent.width
              wrapMode: Text.WordWrap
              text: strings.t("schedule.disclosure")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          PanelSeparator { foreground: root.foreground }

          // 5. Recent 10 Wallpapers
          Column {
            width: parent.width
            spacing: Style.space(6)

            RowLayout {
              width: parent.width
              PanelSectionHeader {
                text: strings.t("section.recent")
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Item { Layout.fillWidth: true }
              PlainText {
                text: strings.tn("recent.count", root.history.length).arg(Commons.I18n.formatNumber(root.history.length))
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            PlainText {
              visible: root.history.length === 0
              width: parent.width
              wrapMode: Text.WordWrap
              text: strings.t("recent.empty")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Grid {
              id: recentGrid
              visible: root.history.length > 0
              width: parent.width
              columns: 2
              spacing: Style.space(8)
              readonly property real cardWidth: Math.floor((width - spacing) / 2)
              readonly property real cardHeight: Math.round(cardWidth * 0.58)

              Repeater {
                model: root.history

                delegate: BorderSurface {
                  id: card
                  required property var modelData
                  required property int index

                  readonly property bool isCurrent: modelData.path !== "" && modelData.path === root.currentPath
                  readonly property bool isHot: cardMouse.containsMouse

                  width: recentGrid.cardWidth
                  height: recentGrid.cardHeight
                  radius: Style.cornerRadius
                  clip: true

                  color: isHot
                    ? Style.hoverFillFor(root.foreground, root.accent)
                    : (isCurrent ? Style.selectedFillFor(root.foreground, root.accent) : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.06))
                  borderSpec: isCurrent
                    ? Border.controlSpec("selected", root.foreground, root.accent)
                    : (isHot ? Border.controlSpec("hover-cursor", root.foreground, root.accent) : Border.controlSpec("normal", root.foreground, root.accent))

                  Behavior on color { ColorAnimation { duration: Style.duration(120) } }

                  SizedImage {
                    anchors.fill: parent
                    url: card.modelData.thumb ? Util.fileUrl(card.modelData.thumb) : ""
                    crop: true
                  }

                  // Top badges: Mode (STATIC / LIVE) and Active indicator
                  Rectangle {
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.margins: Style.space(4)
                    width: badgeText.implicitWidth + Style.space(8)
                    height: badgeText.implicitHeight + Style.space(2)
                    radius: Style.cornerRadius
                    color: Qt.rgba(0, 0, 0, 0.68)

                    PlainText {
                      id: badgeText
                      anchors.centerIn: parent
                      text: card.modelData.mode === "pixabay" ? strings.t("recent.badgeLive") : strings.t("recent.badgeStatic")
                      color: card.modelData.mode === "pixabay" ? root.accent : "white"
                      font.family: root.fontFamily
                      font.pixelSize: Math.max(9, Style.font.caption - 2)
                      font.bold: true
                    }
                  }

                  Rectangle {
                    visible: card.isCurrent
                    anchors.top: parent.top
                    anchors.right: parent.right
                    anchors.margins: Style.space(4)
                    width: activeLabel.implicitWidth + Style.space(8)
                    height: activeLabel.implicitHeight + Style.space(2)
                    radius: Style.cornerRadius
                    color: root.accent

                    PlainText {
                      id: activeLabel
                      anchors.centerIn: parent
                      text: "● " + strings.t("recent.current")
                      color: "black"
                      font.family: root.fontFamily
                      font.pixelSize: Math.max(9, Style.font.caption - 2)
                      font.bold: true
                    }
                  }

                  // Bottom title bar
                  Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    height: titleRow.implicitHeight + Style.space(6)
                    color: Qt.rgba(0, 0, 0, 0.72)

                    PlainText {
                      id: titleRow
                      anchors.left: parent.left
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      anchors.leftMargin: Style.space(6)
                      anchors.rightMargin: Style.space(6)
                      text: card.modelData.title || ("#" + (card.index + 1))
                      elide: Text.ElideRight
                      color: "white"
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }

                  MouseArea {
                    id: cardMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.applyHistory(card.modelData.path)
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
