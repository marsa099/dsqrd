import QtQuick
import QtQuick.Layouts
import QsLib

Item {
    id: root

    Rectangle {
        anchors.fill: parent
        color: Theme.bgDim
    }

    ColumnLayout {
        anchors.centerIn: parent
        width: Math.min(420, parent.width - 48)
        spacing: 18

        Text {
            Layout.alignment: Qt.AlignHCenter
            text: "Sign in to Discord"
            color: Theme.fg
            font.family: Theme.fontFamily
            font.pixelSize: 28
            font.weight: 600
        }

        Text {
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            text: Backend.authState === "confirming"
                ? "Confirm " + (Backend.authAccount || "this account") + " in the Discord mobile app."
                : Backend.authState === "qr" || Backend.authState === "waiting"
                    ? "Scan this code from the Discord mobile app, then approve the login."
                    : Backend.authState === "connecting" || Backend.authState === "approved"
                        ? "Connecting securely…"
                        : Backend.authMessage || "Use the official Discord mobile app to approve this device."
            color: Backend.authState === "error" ? Theme.red : Theme.fg_muted
            font.family: Theme.fontFamily
            font.pixelSize: 15
        }

        Rectangle {
            Layout.alignment: Qt.AlignHCenter
            visible: Backend.authQrPath.length > 0
                     && (Backend.authState === "qr" || Backend.authState === "waiting" || Backend.authState === "confirming")
            width: 260
            height: 260
            radius: 20
            color: "white"

            Image {
                anchors.fill: parent
                anchors.margins: 14
                source: Backend.authQrPath.length ? "file://" + Backend.authQrPath : ""
                fillMode: Image.PreserveAspectFit
                cache: false
            }
        }

        Text {
            Layout.alignment: Qt.AlignHCenter
            visible: Backend.authRemaining > 0
            text: "Expires in " + Math.floor(Backend.authRemaining / 60) + ":"
                  + String(Backend.authRemaining % 60).padStart(2, "0")
            color: Theme.fg_muted
            font.family: Theme.fontFamily
            font.pixelSize: 13
        }

        Rectangle {
            Layout.alignment: Qt.AlignHCenter
            visible: Backend.authState === "signedOut" || Backend.authState === "error"
            width: 180
            height: 44
            radius: 14
            color: startHover.hovered ? Theme.surface3 : Theme.surface2

            Text {
                anchors.centerIn: parent
                text: Backend.authState === "error" ? "Try again" : "Show QR code"
                color: Theme.fg
                font.family: Theme.fontFamily
                font.pixelSize: 15
                font.weight: 600
            }
            HoverHandler { id: startHover; cursorShape: Qt.PointingHandCursor }
            TapHandler { onTapped: Backend.startAuth() }
        }

        Rectangle {
            Layout.alignment: Qt.AlignHCenter
            visible: Backend.authState === "qr" || Backend.authState === "waiting" || Backend.authState === "confirming"
            width: 100
            height: 36
            radius: 12
            color: cancelHover.hovered ? Theme.surface2 : "transparent"

            Text {
                anchors.centerIn: parent
                text: "Cancel"
                color: Theme.fg_muted
                font.family: Theme.fontFamily
                font.pixelSize: 14
            }
            HoverHandler { id: cancelHover; cursorShape: Qt.PointingHandCursor }
            TapHandler { onTapped: Backend.cancelAuth() }
        }

        Text {
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            text: "Dsqrd never displays or stores your password. The approved session is saved in your system keyring."
            color: Theme.fg_muted
            opacity: 0.7
            font.family: Theme.fontFamily
            font.pixelSize: 12
        }
    }
}
