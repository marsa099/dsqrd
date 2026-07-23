import QtQuick
import "."
import QsLib

// Upstream issue tracker takeover (`!`, or clicking the statusbar line): the
// feature requests filed on daphen's repo, one row per issue. Vimium-style
// number caps open a row's GitHub page directly; j/k + enter work too, and
// rows are clickable. shell.qml's routeKey owns the keys (q/esc close), the
// same keep-focus-in-the-shell pattern as the Copilot takeover.
Item {
    id: root
    anchors.fill: parent
    visible: opacity > 0
    opacity: open ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 120 } }

    property bool open: false
    property int cursor: 0
    readonly property var issues: Backend.trackedIssues

    function show() { cursor = 0; open = true }
    function close() { open = false }
    function move(d) {
        if (issues.length === 0) return
        cursor = Math.max(0, Math.min(issues.length - 1, cursor + d))
    }
    function openAt(i) {
        const it = issues[i]
        if (it && it.url) { Qt.openUrlExternally(it.url); close() }
    }
    function openCurrent() { openAt(cursor) }

    // Dim + swallow input from the app behind, like the other takeovers.
    Rectangle {
        anchors.fill: parent
        color: Theme.mode === "light" ? Qt.rgba(0, 0, 0, 0.30) : Qt.rgba(0, 0, 0, 0.58)
        MouseArea { anchors.fill: parent; hoverEnabled: true; onWheel: (w) => w.accepted = true }
    }

    Rectangle {
        id: card
        anchors.centerIn: parent
        width: Math.min(640, parent.width - 80)
        height: Math.min(parent.height - 100, col.implicitHeight + 40)
        radius: Theme.radiusInner
        color: Theme.surface
        border.width: 1; border.color: Theme.hairline

        Column {
            id: col
            anchors.fill: parent
            anchors.margins: 20
            spacing: 12

            Column {
                spacing: 2
                Text { renderType: Text.NativeRendering; text: "Upstream issues"
                       color: Theme.fg; font.family: Theme.fontFamily
                       font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 15; font.weight: 600 }
                Text { renderType: Text.NativeRendering; text: "feature requests filed on daphen/dsqrd"
                       color: Theme.fg_muted; font.family: Theme.fontFamily
                       font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 12 }
            }

            Rectangle { width: parent.width; height: 1; color: Theme.hairline }

            Text {
                visible: root.issues.length === 0
                renderType: Text.NativeRendering; text: "No filed issues (yet)."
                color: Theme.fg_muted; font.family: Theme.fontFamily
                font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 13
            }

            Column {
                width: parent.width
                spacing: 2
                Repeater {
                    model: root.issues
                    delegate: Rectangle {
                        required property var modelData
                        required property int index
                        width: parent.width
                        height: 34
                        radius: 7
                        color: index === root.cursor ? Theme.selection : "transparent"
                        KeyCap {
                            id: numCap
                            visible: index < 9
                            text: String(index + 1)
                            anchors.left: parent.left; anchors.leftMargin: 8
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        // GitHub-style state dot: green = open, muted = closed.
                        Rectangle {
                            id: dot
                            width: 8; height: 8; radius: 4
                            anchors.left: numCap.right; anchors.leftMargin: 10
                            anchors.verticalCenter: parent.verticalCenter
                            color: modelData.state === "open" ? Theme.green : Theme.fg_muted
                        }
                        Text {
                            id: num
                            renderType: Text.NativeRendering; text: "#" + modelData.number
                            anchors.left: dot.right; anchors.leftMargin: 8
                            anchors.verticalCenter: parent.verticalCenter
                            color: Theme.fg_muted; font.family: Theme.fontFamily
                            font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 13
                        }
                        Text {
                            renderType: Text.NativeRendering; text: modelData.title
                            anchors.left: num.right; anchors.leftMargin: 8
                            anchors.right: stateLbl.left; anchors.rightMargin: 8
                            anchors.verticalCenter: parent.verticalCenter
                            elide: Text.ElideRight
                            color: modelData.state === "open" ? Theme.fg : Theme.fg_muted
                            font.family: Theme.fontFamily
                            font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 13
                        }
                        Text {
                            id: stateLbl
                            renderType: Text.NativeRendering; text: modelData.state
                            anchors.right: parent.right; anchors.rightMargin: 10
                            anchors.verticalCenter: parent.verticalCenter
                            color: modelData.state === "open" ? Theme.green : Theme.fg_muted
                            font.family: Theme.fontFamily
                            font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 11
                        }
                        HoverHandler { cursorShape: Qt.PointingHandCursor }
                        TapHandler { onTapped: root.openAt(index) }
                    }
                }
            }

            // breathing room so the rows clear the dismiss hints
            Item { width: 1; height: 6 }
        }

        Row {
            anchors.right: parent.right; anchors.rightMargin: 16
            anchors.bottom: parent.bottom; anchors.bottomMargin: 12
            spacing: 6
            KeyCap { text: "1-9"; anchors.verticalCenter: parent.verticalCenter }
            CapLabel { text: "open"; anchors.verticalCenter: parent.verticalCenter }
            KeyCap { text: "j"; anchors.verticalCenter: parent.verticalCenter }
            KeyCap { text: "k"; anchors.verticalCenter: parent.verticalCenter }
            CapLabel { text: "move"; anchors.verticalCenter: parent.verticalCenter }
            KeyCap { text: "↵"; anchors.verticalCenter: parent.verticalCenter }
            CapLabel { text: "open"; anchors.verticalCenter: parent.verticalCenter }
            KeyCap { text: "q"; anchors.verticalCenter: parent.verticalCenter }
            CapLabel { text: "close"; anchors.verticalCenter: parent.verticalCenter }
        }
    }
}
