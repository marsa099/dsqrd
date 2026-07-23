import QtQuick
import "."
import QsLib

// Link chooser: `o` on a message with several URLs opens this instead of
// silently opening only the first one. Vimium-hint spirit (mlqs's `f`) adapted
// to a list: every row carries a number cap, 1-9 opens it directly; j/k +
// enter and clicking work too. shell.qml's routeKey owns the keys (q/esc
// close), same pattern as the other takeovers.
Item {
    id: root
    anchors.fill: parent
    visible: opacity > 0
    opacity: open ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 120 } }

    property bool open: false
    property int cursor: 0
    property var links: []

    function show(list) { links = list || []; cursor = 0; open = true }
    function close() { open = false }
    function move(d) {
        if (links.length === 0) return
        cursor = Math.max(0, Math.min(links.length - 1, cursor + d))
    }
    function openAt(i) {
        if (links[i]) { Backend.openUrl(links[i]); close() }
    }
    function openCurrent() { openAt(cursor) }

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

            Text { renderType: Text.NativeRendering; text: "Open which link?"
                   color: Theme.fg; font.family: Theme.fontFamily
                   font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 15; font.weight: 600 }

            Rectangle { width: parent.width; height: 1; color: Theme.hairline }

            Column {
                width: parent.width
                spacing: 2
                Repeater {
                    model: root.links
                    delegate: Rectangle {
                        required property string modelData
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
                        Text {
                            renderType: Text.NativeRendering; text: modelData
                            anchors.left: numCap.right; anchors.leftMargin: 10
                            anchors.right: parent.right; anchors.rightMargin: 10
                            anchors.verticalCenter: parent.verticalCenter
                            // middle-elide keeps both the domain and the tail visible
                            elide: Text.ElideMiddle
                            color: Theme.sky; font.family: Theme.fontFamily
                            font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 13
                        }
                        HoverHandler { cursorShape: Qt.PointingHandCursor }
                        TapHandler { onTapped: root.openAt(index) }
                    }
                }
            }

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
