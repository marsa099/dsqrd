import QtQuick
import Quickshell
import Quickshell.Io
import "."
import QsLib

// Microsoft Copilot catch-up overlay. Fired from the composer's Copilot button:
// it summarizes everything posted in the open channel since I last read it and
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
    // for free (no re-prompt). English is generated first and is the default
    // view, so streamed text is readable immediately. Session-sticky: the
    // chosen language survives closing and reopening the panel.
    property string lang: "en"      // "en" | "sv"
    property string resultSv: ""
    property string resultEn: ""
    // While streaming, the not-yet-generated language falls back to the other
    // one instead of showing an empty card.
    readonly property string result: lang === "en" ? (resultEn || resultSv) : (resultSv || resultEn)
    property int sourceCount: 0
    readonly property string stamp: Qt.formatDateTime(new Date(), "HH:mm")

    // Per-channel summary cache: channelId → {ts, en, sv}. An entry is fresh
    // while the channel's newest message ts still matches — so reopening `c`
    // with nothing new, or a completed prefetch, costs no prompt.
    property var _cache: ({})
    // `c` pressed while a job for a different channel/ts was mid-flight: let it
    // finish (its result still lands in _cache), then re-run show().
    property bool _pendingShow: false

    // Copilot's summary prompt — kept in sync with dsqrd-cli's `summarize()`.
    // The transcript is wrapped in CHAT LOG markers in show(); the guardrails
    // ("however short", "never ask me to paste") stop the model from punting on
    // a one-line log with "I don't see a chat log, paste it".
    readonly property string _instr:
        "Catch me up on a Discord channel. Everything between the CHAT LOG markers below is "
      + "the last 50 messages for context. Summarize ONLY messages below the "
      + "'LAST READ — SUMMARIZE MESSAGES BELOW' line; use earlier messages only "
      + "to understand references and never include them as catch-up items. Summarize the unread part as "
      + "markdown bullets: each bullet starts with '- ' and is ONE short line, "
      + "with a blank line between bullets — max 5 bullets, fewer when the log "
      + "is small; no filler, no preamble. Be strictly factual: state only what "
      + "the log explicitly says — never infer that something happened (a merge, "
      + "a fix, a decision) unless it's stated outright; if ambiguous, say it's "
      + "unclear instead of asserting. Bold the key part of each bullet "
      + "(speaker or topic) with **...**. I go by marsan, marzan, kottis, "
      + "köttis, kottsamlaren, köttsamlaren or martin (any spelling variant): "
      + "anything directed at me or that I should act on goes in its own bullet "
      + "FIRST, starting with '- @me ' — never use @me on other bullets. Always "
      + "refer to me in the second person — 'you' in English, 'du/dig/din' in "
      + "Swedish — never by those names in the third person. The "
      + "log may be very short — even a single line — just summarize whatever "
      + "is there; never ask me to paste anything, the log is already below. "
      + "Write the summary twice, first in English then in Swedish, in exactly "
      + "this format with nothing outside the markers:\n"
      + "===EN===\n<summary in English>\n===SV===\n<summary in Swedish>\n\n"

    // Bullets the model tagged "@me" (directed at me) get the same yellow
    // highlight treatment as a real @-mention in chat: wrap the line in the
    // private-use runes richify styles as a mention-of-you.
    function _decorated(t) {
        return t.replace(/(^|\n)-[ \t]*@me:?[ \t]*([^\n]*)/gi, (m, pre, rest) =>
            pre + "- \ue001\ud83d\udccc " + rest + "\ue002")
    }

    // ── feedback → standing rules ─────────────────────────────────────────
    // `f` opens an inline feedback field (typed via shell.routeKey, the same
    // keep-focus-in-the-shell pattern as the cheat sheet's search). Submitted
    // feedback goes to pi, which curates a standing-rules list stored in
    // ~/.local/share/dsqrd/copilot-feedback.md; the list is injected into every
    // future summary prompt. Pi decides whether the feedback warrants a rule
    // at all, so one-off gripes don't pollute the list.
    property bool feedbackMode: false
    property string feedbackText: ""
    property bool feedbackBusy: false
    property string rules: ""       // current standing rules, loaded from disk

    function startFeedback() {
        if (phase !== "ready" && phase !== "error") return
        feedbackMode = true
        feedbackText = ""
    }
    function cancelFeedback() { feedbackMode = false; feedbackText = "" }
    function submitFeedback() {
        const fb = feedbackText.trim()
        feedbackMode = false
        feedbackText = ""
        if (fb.length === 0) return
        feedbackBusy = true
        fbProc.b64 = Qt.btoa(
            "You maintain a short list of standing instructions that get appended to "
          + "an LLM prompt which summarizes chat logs (a 'catch me up' feature). "
          + "Decide whether the user's feedback below warrants changing the list. If "
          + "it does, output the COMPLETE updated list: terse markdown bullets, "
          + "deduplicated, merged where overlapping, generally applicable (not tied "
          + "to one specific chat), max 10 bullets, in English. If it warrants no "
          + "change, output the current list unchanged. Output ONLY the bullet list — "
          + "no headers, no commentary. An empty current list is normal.\n\n"
          + "===== CURRENT LIST =====\n" + rules + "\n===== END LIST =====\n\n"
          + "===== USER FEEDBACK =====\n" + fb + "\n===== END FEEDBACK =====\n\n"
          + "For context, the summary the user was looking at when giving feedback:\n"
          + "===== SUMMARY =====\n" + resultEn + "\n===== END SUMMARY =====\n")
        fbProc.running = true
    }
    Process {
        id: fbProc
        property string b64: ""
        command: ["sh", "-c",
            "d=\"$HOME/.local/share/dsqrd\"; mkdir -p \"$d\"; "
          + "printf %s '" + b64 + "' | base64 -d | PI_SKIP_VERSION_CHECK=1 pi -p "
          + "--no-session --no-tools --no-extensions --no-skills --no-prompt-templates "
          + "--no-context-files --thinking off > \"$d/copilot-feedback.md.tmp\" "
          + "2>/tmp/dsqrd-copilot-fb.err "
          + "&& [ -s \"$d/copilot-feedback.md.tmp\" ] "
          + "&& mv \"$d/copilot-feedback.md.tmp\" \"$d/copilot-feedback.md\""]
        onExited: (code, status) => {
            root.feedbackBusy = false
            if (code === 0) {
                rulesLoader.running = true
                root._cache = ({})   // stale: summaries predate the new rules
                Backend.toast("Copilot: feedback inarbetad ✓")
            } else Backend.toast("Copilot: kunde inte bearbeta feedback")
        }
    }
    Process {
        id: rulesLoader
        command: ["sh", "-c", "cat \"$HOME/.local/share/dsqrd/copilot-feedback.md\" 2>/dev/null"]
        stdout: StdioCollector { onStreamFinished: root.rules = (this.text || "").trim() }
    }
    Component.onCompleted: rulesLoader.running = true

    function toggleLang() { lang = lang === "en" ? "sv" : "en" }
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
        const hit = _cache[Backend.currentChannelId]
        if (hit && hit.ts === c.lastTs) {
            resultEn = hit.en; resultSv = hit.sv
            phase = "ready"
            return
        }
        phase = "loading"
        if (proc.running) {
            // A prefetch for exactly this channel+ts is mid-flight: attach to
            // its stream. Anything else: wait for it to finish (killing it
            // wastes the already-spent prompt), then show() re-runs.
            if (proc.jobChannel === Backend.currentChannelId && proc.jobTs === c.lastTs) {
                if (proc.acc.length > 0) { _applyPartial(proc.acc); if (result.length > 0) phase = "ready" }
            } else _pendingShow = true
            return
        }
        _startJob(Backend.currentChannelId, c.lastTs, c.text)
    }
    function close() {
        // Deliberately leaves a running job alive: it finishes into _cache, so
        // the prompt isn't wasted and the next `c` is instant.
        open = false
        phase = "idle"
        _pendingShow = false
        feedbackMode = false
        feedbackText = ""
    }

    // Speculative prefetch — the debounce below arms this on channel switch and
    // on the window regaining focus. Only spends a prompt when ≥5 unread messages
    // arrived since the last read boundary and neither the cache nor a running job already
    // covers them; the result lands in _cache so pressing `c` is instant.
    function maybePrefetch() {
        if (open || proc.running) return
        const c = Backend.catchupSince()
        if (c.sinceCount < 5 || !c.lastTs) return
        const hit = _cache[Backend.currentChannelId]
        if (hit && hit.ts === c.lastTs) return
        _startJob(Backend.currentChannelId, c.lastTs, c.text)
    }
    Timer {
        id: prefetchTimer
        interval: 1500
        onTriggered: root.maybePrefetch()
    }
    Connections {
        target: Backend
        // Channel switch (also fires on startup when the first channel loads).
        function onCurrentChannelIdChanged() { prefetchTimer.restart() }
        // Window refocused after being away — the daemon watches niri and
        // broadcasts the flip, so this also covers "open dsqrd after hours".
        function onAppActiveChanged() { if (Backend.appActive) prefetchTimer.restart() }
    }
    Connections {
        // Messages still pouring in (fresh history after a switch, or a live
        // burst): extend an armed debounce so we summarize the settled view.
        // Only extends — arrivals alone never arm a prefetch, or every busy
        // channel would burn prompts continuously.
        target: Backend.messages
        function onCountChanged() { if (prefetchTimer.running) prefetchTimer.restart() }
    }

    function _startJob(channel, ts, text) {
        proc.jobChannel = channel
        proc.jobTs = ts
        proc.acc = ""
        // base64 the prompt so the transcript (åäö, emoji, quotes) survives the
        // shell untouched, then decode straight into `pi`. Qt.btoa already
        // UTF-8-encodes the string, so pass it raw — wrapping it in
        // unescape(encodeURIComponent()) would double-encode and mojibake it.
        proc.b64 = Qt.btoa(_instr
            + (rules ? "Standing user rules — follow them strictly:\n" + rules + "\n\n" : "")
            + "===== CHAT LOG =====\n" + text + "\n===== END OF CHAT LOG =====")
        proc.running = true
    }
    // Split the accumulated stream into the two language blocks. English comes
    // first, so it fills progressively; Swedish stays empty until its marker
    // arrives. If the model ignored the markers, everything counts as English.
    function _applyPartial(t) {
        const sv = t.split("===SV===")
        const en = sv[0].split("===EN===")
        resultEn = (en.length > 1 ? en[en.length - 1] : sv[0]).trim()
        resultSv = (sv.length > 1 ? sv[1] : "").trim()
    }

    Process {
        id: proc
        property string b64: ""
        property string jobChannel: ""
        property string jobTs: ""
        property string acc: ""
        // pi's JSON event mode exposes text deltas, preserving the streaming UI
        // while using pi's configured provider/model. This one-shot has no tools,
        // session, extensions, skills, project context, or startup update check.
        command: ["sh", "-c", "printf %s '" + b64 + "' | base64 -d | PI_SKIP_VERSION_CHECK=1 pi --mode json --no-session --no-tools --no-extensions --no-skills --no-prompt-templates --no-context-files --thinking off 2>/tmp/dsqrd-copilot.err"]
        stdout: SplitParser {
            onRead: (line) => {
                let ev
                try { ev = JSON.parse(line) } catch (x) { return }
                const d = ev.assistantMessageEvent
                if (ev.type === "message_update" && d && d.type === "text_delta" && d.delta) {
                    proc.acc += d.delta
                    if (root.open && !root._pendingShow && proc.jobChannel === Backend.currentChannelId) {
                        root._applyPartial(proc.acc)
                        if (root.phase === "loading" && root.result.length > 0) root.phase = "ready"
                    }
                }
            }
        }
        onExited: (code, status) => {
            const t = proc.acc.trim()
            const ok = code === 0 && t.length > 0
            if (ok) {
                const sv = t.split("===SV===")
                const en = sv[0].split("===EN===")
                const enTxt = (en.length > 1 ? en[en.length - 1] : sv[0]).trim()
                const svTxt = ((sv.length > 1 ? sv[1] : "").trim()) || enTxt
                root._cache[proc.jobChannel] = { ts: proc.jobTs, en: enTxt, sv: svTxt }
            }
            if (root._pendingShow) {
                root._pendingShow = false
                if (root.open) root.show()   // re-check: likely a cache hit now, else start the right job
                return
            }
            if (root.open && proc.jobChannel === Backend.currentChannelId) {
                if (ok) { root._applyPartial(t); if (root.resultSv === "") root.resultSv = root.resultEn; root.phase = "ready" }
                else if (root.phase === "loading") {
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
                    text: Backend.richify(root._decorated(root.result), 18)
                    wrapMode: Text.Wrap
                    color: root.phase === "error" ? Theme.fg_muted : Theme.fg
                    font.family: Theme.fontFamily; font.hintingPreference: Font.PreferNoHinting
                    font.pixelSize: 14
                    onLinkActivated: (url) => Backend.openUrl(url)
                }
            }

            // ── feedback: inline input (f) or processing pulse ─────────────
            Column {
                visible: root.feedbackMode || root.feedbackBusy
                width: parent.width
                spacing: 10
                Rectangle { width: parent.width; height: 1; color: Theme.hairline }
                Row {
                    visible: root.feedbackMode
                    width: parent.width
                    spacing: 8
                    Text { renderType: Text.NativeRendering; text: "Feedback:"
                           color: Theme.sky; font.family: Theme.fontFamily
                           font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 13; font.weight: 600 }
                    Text { renderType: Text.NativeRendering
                           text: root.feedbackText + "▏"
                           width: parent.width - 90
                           wrapMode: Text.Wrap
                           color: Theme.fg; font.family: Theme.fontFamily
                           font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 13 }
                }
                Row {
                    visible: root.feedbackBusy
                    spacing: 10
                    Rectangle {
                        width: 8; height: 8; radius: 4; color: Theme.sky
                        anchors.verticalCenter: parent.verticalCenter
                        SequentialAnimation on opacity {
                            running: root.feedbackBusy; loops: Animation.Infinite
                            NumberAnimation { from: 1; to: 0.25; duration: 550 }
                            NumberAnimation { from: 0.25; to: 1; duration: 550 }
                        }
                    }
                    Text { renderType: Text.NativeRendering; text: "Copilot arbetar in din feedback…"
                           anchors.verticalCenter: parent.verticalCenter
                           color: Theme.fg_muted; font.family: Theme.fontFamily
                           font.hintingPreference: Font.PreferNoHinting; font.pixelSize: 13 }
                }
            }
        }

        // dismiss hint
        Row {
            anchors.right: parent.right; anchors.rightMargin: 16
            anchors.bottom: parent.bottom; anchors.bottomMargin: 12
            spacing: 6
            visible: root.phase !== "loading"
            KeyCap { visible: !root.feedbackMode; text: "l"; anchors.verticalCenter: parent.verticalCenter }
            CapLabel { visible: !root.feedbackMode; text: root.lang === "sv" ? "English" : "svenska"
                       anchors.verticalCenter: parent.verticalCenter }
            KeyCap { visible: !root.feedbackMode; text: "f"; anchors.verticalCenter: parent.verticalCenter }
            CapLabel { visible: !root.feedbackMode; text: "feedback"; anchors.verticalCenter: parent.verticalCenter }
            KeyCap { visible: !root.feedbackMode; text: "q"; anchors.verticalCenter: parent.verticalCenter }
            CapLabel { visible: !root.feedbackMode; text: "stäng"; anchors.verticalCenter: parent.verticalCenter }
            KeyCap { visible: root.feedbackMode; text: "enter"; anchors.verticalCenter: parent.verticalCenter }
            CapLabel { visible: root.feedbackMode; text: "skicka"; anchors.verticalCenter: parent.verticalCenter }
            KeyCap { visible: root.feedbackMode; text: "esc"; anchors.verticalCenter: parent.verticalCenter }
            CapLabel { visible: root.feedbackMode; text: "avbryt"; anchors.verticalCenter: parent.verticalCenter }
        }
    }
}
