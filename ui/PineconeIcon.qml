import QtQuick
import QsLib

Item {
    id: root
    property bool running: false
    property color color: Theme.fg_muted
    property color activeColor: Theme.electric
    readonly property var phases: [0, 1, 2, 4, 3, 5, 6, 8, 7, 9, 13, 10, 12, 11, 14]
    implicitWidth: 18
    implicitHeight: 18

    Repeater {
        model: root.phases.length
        delegate: Icon {
            id: scale
            required property int index
            property color tone: root.color
            readonly property int phase: root.phases[index]
            name: "pinecone-part-" + (index < 9 ? "0" : "") + (index + 1)
            anchors.fill: parent
            color: tone

            SequentialAnimation {
                running: root.running
                loops: Animation.Infinite
                PauseAnimation { duration: scale.phase * 55 }
                ColorAnimation { target: scale; property: "tone"; from: root.color; to: root.activeColor; duration: 180; easing.type: Easing.OutCubic }
                ColorAnimation { target: scale; property: "tone"; from: root.activeColor; to: root.color; duration: 180; easing.type: Easing.InCubic }
                PauseAnimation { duration: (root.phases.length - scale.phase - 1) * 55 }
            }

            Connections {
                target: root
                function onRunningChanged() {
                    if (!root.running) scale.tone = root.color
                }
            }
        }
    }
}
