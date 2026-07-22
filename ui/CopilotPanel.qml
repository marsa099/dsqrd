import QtQuick
import Quickshell.Io
import "."
import QsLib

// Microsoft Copilot catch-up overlay. Fired from the composer's Copilot button:
// it summarizes everything posted in the open channel since my last message and
// shows it as a single takeover "message" from Microsoft Copilot — the only
// thing on screen until dismissed. shell.qml's routeKey owns dismissal (q / esc),
// exactly like the `?` cheat sheet, so focus handling stays in one place.
Item {
    id: root
    anchors.fill: parent
    visible: opacity > 0
    opacity: open ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 120 } }

    property bool open: false
    property string phase: "idle"   // "idle" | "loading" | "ready" | "error"
    // One prompt returns the summary in both languages; `l` flips between them
    // for free (no re-prompt). Session-sticky: the chosen language survives
    // closing and reopening the panel.
    property string lang: "sv"      // "sv" | "en"
    property string resultSv: ""
    property string resultEn: ""
    readonly property string result: lang === "sv" ? resultSv : resultEn
    property int sourceCount: 0
    readonly property string stamp: Qt.formatDateTime(new Date(), "HH:mm")

    // Copilot's summary prompt — kept in sync with dsqrd-cli's `summarize()`.
    // The transcript is wrapped in CHAT LOG markers in show(); the guardrails
    // ("however short", "never ask me to paste") stop the model from punting on
    // a one-line log with "I don't see a chat log, paste it".
    readonly property string _instr:
        "Catch me up on a Discord channel. Everything between the markers below is "
      + "a chat log of the recent conversation in the channel. Give me the "
      + "shortest possible catch-up: main topics, who said what that matters, and "
      + "anything directed at me or that I should act on — a few terse lines, no "
      + "filler, no preamble. The log may be very short — even a single line — "
      + "just summarize whatever is there; never ask me to paste anything, the log "
      + "is already below. Write the summary twice, first in Swedish then in "
      + "English, in exactly this format with nothing outside the markers:\n"
      + "===SV===\n<summary in Swedish>\n===EN===\n<summary in English>\n\n"

    function toggleLang() { lang = lang === "sv" ? "en" : "sv" }
    function show() {
        open = true
        resultSv = ""
        resultEn = ""
        const c = Backend.catchupSince()
        sourceCount = c.count
        if (c.count === 0) {
            phase = "ready"
            resultSv = "Inget att sammanfatta i den här kanalen än. ✨"
            resultEn = "Nothing to summarize in this channel yet. ✨"
            return
        }
        phase = "loading"
        // base64 the prompt so the transcript (åäö, emoji, quotes) survives the
        // shell untouched, then decode straight into `claude`. Qt.btoa already
        // UTF-8-encodes the string, so pass it raw — wrapping it in
        // unescape(encodeURIComponent()) would double-encode and mojibake it.
        proc.b64 = Qt.btoa(_instr + "===== CHAT LOG =====\n" + c.text + "\n===== END OF CHAT LOG =====")
        proc.running = true
    }
    function close() {
        open = false
        if (proc.running) proc.running = false
        phase = "idle"
    }

    Process {
        id: proc
        property string b64: ""
        command: ["sh", "-c", "printf %s '" + b64 + "' | base64 -d | claude -p --model claude-opus-4-8 2>/tmp/dsqrd-copilot.err"]
        stdout: StdioCollector {
            onStreamFinished: {
                proc.running = false
                const t = (this.text || "").trim()
                if (t.length > 0) {
                    // Split out the two language blocks; if the model ignored the
                    // markers, show the raw text in both languages rather than nothing.
                    const en = t.split("===EN===")
                    const sv = en[0].split("===SV===")
                    root.resultSv = (sv.length > 1 ? sv[sv.length - 1] : en[0]).trim()
                    root.resultEn = (en.length > 1 ? en[1] : root.resultSv).trim()
                    root.phase = "ready"
                } else {
                    root.resultSv = "Copilot kunde inte sammanfatta just nu."
                    root.resultEn = "Copilot couldn't summarize right now."
                    root.phase = "error"
                }
            }
        }
    }

    // Dim the app behind the takeover so the Copilot message is all you see.
    // The MouseArea also swallows clicks/scroll from reaching the app underneath.
    Rectangle {
        anchors.fill: parent
        color: Theme.mode === "light" ? Qt.rgba(0, 0, 0, 0.30) : Qt.rgba(0, 0, 0, 0.58)
        MouseArea { anchors.fill: parent; hoverEnabled: true; onWheel: (w) => w.accepted = true }
    }

    // The single Copilot "message" card.
    Rectangle {
        id: card
        anchors.centerIn: parent
        width: Math.min(660, parent.width - 80)
        height: Math.min(parent.height - 100, contentCol.implicitHeight + 40)
        radius: Theme.radiusInner
        color: Theme.surface
        border.width: 1; border.color: Theme.hairline

        Column {
            id: contentCol
            anchors.fill: parent
            anchors.margins: 20
            spacing: 14

            // ── header: Copilot avatar + name + timestamp ──────────────────
            Row {
                width: parent.width
                spacing: 11
                // Copilot mark — a self-drawn gradient bloom (ui/copilot.svg).
                Image {
                    width: 36; height: 36
                    anchors.verticalCenter: parent.verticalCenter
                    source: Qt.resolvedUrl("copilot.svg")
                    sourceSize.width: 36; sourceSize.height: 36; smooth: true
                }
                Column {
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 1
                    Row {
                        spacing: 7
                        Text { renderType: Text.NativeRendering; text: "Microsoft Copilot"
                               color: Theme.fg; font.family: Theme.fontFamily
                               font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 15; font.weight: 600 }
                        Rectangle {
                            anchors.verticalCenter: parent.verticalCenter
                            width: botLbl.implicitWidth + 10; height: 15; radius: 4
                            color: Theme.tintFill
                            Text { id: botLbl; anchors.centerIn: parent; text: "APP"
                                   renderType: Text.NativeRendering; color: Theme.sky
                                   font.family: Theme.fontFamily; font.pixelSize: 9; font.weight: 700; font.letterSpacing: 0.5 }
                        }
                        Text { renderType: Text.NativeRendering; text: root.stamp
                               anchors.verticalCenter: parent.verticalCenter
                               color: Theme.fg_muted; font.family: Theme.fontFamily
                               font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 12 }
                    }
                    Text { renderType: Text.NativeRendering
                           text: "Sammanfattning · #" + Backend.currentChannel
                                 + (root.sourceCount > 0 ? "  ·  " + root.sourceCount + " meddelanden" : "")
                                 + "  ·  " + (root.lang === "sv" ? "svenska" : "English")
                           color: Theme.fg_muted; font.family: Theme.fontFamily
                           font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 12 }
                }
            }

            Rectangle { width: parent.width; height: 1; color: Theme.hairline }

            // ── body: loading pulse, or the rendered summary ───────────────
            Row {
                visible: root.phase === "loading"
                spacing: 10
                Rectangle {
                    width: 9; height: 9; radius: 4.5; color: Theme.sky
                    anchors.verticalCenter: parent.verticalCenter
                    SequentialAnimation on opacity {
                        running: root.phase === "loading"; loops: Animation.Infinite
                        NumberAnimation { from: 1; to: 0.25; duration: 550 }
                        NumberAnimation { from: 0.25; to: 1; duration: 550 }
                    }
                }
                Text { renderType: Text.NativeRendering; text: "Copilot sammanfattar chatten…"
                       anchors.verticalCenter: parent.verticalCenter
                       color: Theme.fg_muted; font.family: Theme.fontFamily
                       font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 14 }
            }

            Flickable {
                visible: root.phase === "ready" || root.phase === "error"
                width: parent.width
                // Size to the text's natural height (independent of the card, which
                // sizes to us — referencing card.height here would be circular and
                // collapse to zero), capped so a long summary scrolls instead of
                // overflowing the screen.
                height: Math.min(body.implicitHeight, root.height - 220)
                contentHeight: body.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                Text {
                    id: body
                    width: parent.width
                    textFormat: Text.RichText
                    text: Backend.richify(root.result, 18)
                    wrapMode: Text.Wrap
                    color: root.phase === "error" ? Theme.fg_muted : Theme.fg
                    font.family: Theme.fontFamily; font.hintingPreference: Font.PreferNoHinting
                    font.pixelSize: 14
                    onLinkActivated: (url) => Backend.openUrl(url)
                }
            }
        }

        // dismiss hint
        Row {
            anchors.right: parent.right; anchors.rightMargin: 16
            anchors.bottom: parent.bottom; anchors.bottomMargin: 12
            spacing: 6
            visible: root.phase !== "loading"
            KeyCap { text: "l"; anchors.verticalCenter: parent.verticalCenter }
            CapLabel { text: root.lang === "sv" ? "English" : "svenska"
                       anchors.verticalCenter: parent.verticalCenter }
            KeyCap { text: "q"; anchors.verticalCenter: parent.verticalCenter }
            CapLabel { text: "stäng"; anchors.verticalCenter: parent.verticalCenter }
        }
    }
}
