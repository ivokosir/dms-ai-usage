import QtQuick
import QtQuick.Shapes
import Quickshell
import qs.Common
import qs.Widgets
import qs.Modules.Plugins

PluginComponent {
    id: root

    layerNamespacePlugin: "ai-usage"

    property var accounts: []
    property string generatedAt: ""
    property int refreshSeconds: 120
    property bool loadFailed: false
    property string collectorPath: Quickshell.env("HOME") + "/.local/bin/dms-ai-usage"

    readonly property var claudeLive: accounts.filter(account => account.provider === "claude" && root.isConnected(account))
    readonly property var codexLive: accounts.filter(account => account.provider === "codex" && root.isConnected(account))
    readonly property var offlineAccounts: accounts.filter(account => !root.isConnected(account))

    function refreshUsage() {
        Proc.runCommand(
            "dmsAiUsage.collect",
            [root.collectorPath],
            (stdout, exitCode) => {
                if (exitCode !== 0 && !stdout.trim()) {
                    root.loadFailed = true
                    return
                }
                try {
                    const payload = JSON.parse(stdout.trim())
                    root.accounts = Array.isArray(payload.accounts) ? payload.accounts : []
                    root.generatedAt = payload.generated_at || ""
                    root.loadFailed = false
                    if (payload.refresh_seconds >= 30)
                        root.refreshSeconds = payload.refresh_seconds
                } catch (error) {
                    root.loadFailed = true
                }
            },
            100
        )
    }

    function isConnected(account) {
        return (account.status === "ok" || account.status === "stale")
            && root.windowsOf(account).length > 0
    }

    // Accounts crossing a Repeater arrive as QVariantMaps: windows becomes a
    // QVariantList, which fails Array.isArray. Iterate by length instead, and
    // drop any Spark window defensively.
    function windowsOf(account) {
        const raw = account ? account.windows : undefined
        if (!raw || typeof raw.length !== "number")
            return []
        const result = []
        for (let i = 0; i < raw.length; i++) {
            const item = raw[i]
            if (!item)
                continue
            const text = (String(item.id || "") + " " + String(item.label || "")).toLowerCase()
            if (text.indexOf("spark") !== -1)
                continue
            result.push(item)
        }
        return result
    }

    function modelWindow(account) {
        const windows = root.windowsOf(account)
        for (const item of windows) {
            if (String(item.id || "").indexOf("model:") === 0)
                return item
        }
        for (const item of windows) {
            if (String(item.label || "").toLowerCase().indexOf("fable") !== -1)
                return item
        }
        return null
    }

    function labeledWindow(account, label) {
        for (const item of root.windowsOf(account)) {
            if (item.label === label)
                return item
        }
        return null
    }

    function firstWindow(account) {
        const windows = root.windowsOf(account)
        return windows.length > 0 ? windows[0] : null
    }

    // remaining_percent may be a QVariant-wrapped number; Number() + isFinite
    // is the only conversion that survives every wrapping.
    function remainingOf(item) {
        if (!item)
            return -1
        const raw = item.remaining_percent
        if (raw === undefined || raw === null || raw === "")
            return -1
        const value = Number(raw)
        return isFinite(value) ? value : -1
    }

    function worstClaudeRemaining(account) {
        const fable = root.remainingOf(root.modelWindow(account))
        const five = root.remainingOf(root.labeledWindow(account, "5h"))
        if (fable < 0)
            return five
        if (five < 0)
            return fable
        return Math.min(fable, five)
    }

    function percentText(item) {
        const remaining = root.remainingOf(item)
        return remaining < 0 ? "–" : Math.round(remaining) + "%"
    }

    function statusColor(remaining) {
        if (remaining < 0)
            return Theme.surfaceVariantText
        if (remaining <= 15)
            return Theme.error
        if (remaining <= 35)
            return Theme.warning
        return Theme.primary
    }

    function statusText(account) {
        if (account.status === "ok")
            return "live"
        if (account.status === "stale")
            return "stale"
        return (account.error || "not connected").replace(/_/g, " ")
    }

    function providerName(account) {
        return account && account.provider === "claude" ? "Claude" : "Codex"
    }

    // Language-neutral countdown ("in 57m", "in 5d 17h"). Reading generatedAt
    // makes bindings recompute on every collector refresh.
    function resetIn(item) {
        const refreshTick = root.generatedAt
        const value = item ? item.resets_at : undefined
        if (value === undefined || value === null || value === "")
            return ""
        const numeric = Number(value)
        const date = isFinite(numeric) && typeof value !== "string"
            ? new Date(numeric * 1000)
            : new Date(value)
        if (isNaN(date.getTime()))
            return ""
        const minutes = Math.floor((date.getTime() - Date.now()) / 60000)
        if (minutes <= 0)
            return "now"
        if (minutes < 60)
            return "in " + minutes + "m"
        const hours = Math.floor(minutes / 60)
        if (hours < 24)
            return "in " + hours + "h " + (minutes % 60) + "m"
        return "in " + Math.floor(hours / 24) + "d " + (hours % 24) + "h"
    }

    component CapacityRing: Item {
        id: ringRoot

        property real value: -1
        property real thickness: 3
        property color ringColor: Theme.primary

        Shape {
            anchors.fill: parent
            preferredRendererType: Shape.CurveRenderer

            ShapePath {
                strokeWidth: ringRoot.thickness
                strokeColor: Theme.withAlpha(ringRoot.ringColor, 0.18)
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap

                PathAngleArc {
                    centerX: ringRoot.width / 2
                    centerY: ringRoot.height / 2
                    radiusX: ringRoot.width / 2 - ringRoot.thickness / 2
                    radiusY: ringRoot.height / 2 - ringRoot.thickness / 2
                    startAngle: 0
                    sweepAngle: 360
                }
            }

            ShapePath {
                strokeWidth: ringRoot.thickness
                strokeColor: ringRoot.value >= 0 ? ringRoot.ringColor : "transparent"
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap

                PathAngleArc {
                    centerX: ringRoot.width / 2
                    centerY: ringRoot.height / 2
                    radiusX: ringRoot.width / 2 - ringRoot.thickness / 2
                    radiusY: ringRoot.height / 2 - ringRoot.thickness / 2
                    startAngle: -90
                    sweepAngle: 360 * Math.max(0, Math.min(100, ringRoot.value)) / 100
                }
            }
        }
    }

    component CapacityMeter: Rectangle {
        id: meterRoot

        property real value: -1
        property color meterColor: Theme.primary

        height: 6
        radius: height / 2
        color: Theme.withAlpha(meterColor, 0.18)

        Rectangle {
            width: meterRoot.value >= 0
                ? meterRoot.width * Math.max(0, Math.min(100, meterRoot.value)) / 100
                : 0
            height: parent.height
            radius: parent.radius
            color: meterRoot.meterColor
        }
    }

    // Labeled numeral with a thin status meter underneath: "Fable 66%" over a 3px bar.
    component BarMetric: Column {
        id: metricRoot

        property string label
        property var windowData

        spacing: 3

        Row {
            id: metricRow

            spacing: 4

            StyledText {
                text: metricRoot.label
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.surfaceVariantText
                anchors.verticalCenter: parent.verticalCenter
            }

            StyledText {
                text: root.percentText(metricRoot.windowData)
                font.pixelSize: Theme.fontSizeSmall
                font.weight: Font.DemiBold
                color: Theme.surfaceText
                anchors.verticalCenter: parent.verticalCenter
            }
        }

        CapacityMeter {
            width: metricRow.width
            height: 3
            value: root.remainingOf(metricRoot.windowData)
            meterColor: root.statusColor(root.remainingOf(metricRoot.windowData))
        }
    }

    component RingStat: Column {
        id: statRoot

        property var windowData
        property string caption

        spacing: Theme.spacingXS

        Item {
            width: 64
            height: 64
            anchors.horizontalCenter: parent.horizontalCenter

            CapacityRing {
                anchors.fill: parent
                thickness: 5
                value: root.remainingOf(statRoot.windowData)
                ringColor: root.statusColor(root.remainingOf(statRoot.windowData))
            }

            StyledText {
                anchors.centerIn: parent
                text: root.percentText(statRoot.windowData)
                font.pixelSize: Theme.fontSizeLarge
                font.weight: Font.DemiBold
                color: Theme.surfaceText
            }
        }

        StyledText {
            anchors.horizontalCenter: parent.horizontalCenter
            text: {
                const reset = root.resetIn(statRoot.windowData)
                return reset ? statRoot.caption + " · " + reset : statRoot.caption
            }
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.surfaceVariantText
        }
    }

    component CompactStat: Column {
        id: compactStatRoot

        property var windowData
        property string caption

        spacing: 3

        Row {
            width: parent.width

            StyledText {
                id: compactCaption

                text: compactStatRoot.caption
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.surfaceVariantText
            }

            Item {
                width: Math.max(0, parent.width - compactCaption.implicitWidth - compactValue.implicitWidth)
                height: 1
            }

            StyledText {
                id: compactValue

                text: root.percentText(compactStatRoot.windowData)
                font.pixelSize: Theme.fontSizeSmall
                font.weight: Font.DemiBold
                color: Theme.surfaceText
            }
        }

        CapacityMeter {
            width: parent.width
            height: 3
            value: root.remainingOf(compactStatRoot.windowData)
            meterColor: root.statusColor(root.remainingOf(compactStatRoot.windowData))
        }
    }

    Timer {
        interval: root.refreshSeconds * 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refreshUsage()
    }

    horizontalBarPill: Component {
        Row {
            spacing: Theme.spacingS

            DankIcon {
                visible: root.claudeLive.length === 0 && root.codexLive.length === 0
                name: "data_usage"
                size: Theme.iconSize - 6
                color: root.loadFailed ? Theme.error : Theme.surfaceVariantText
                anchors.verticalCenter: parent.verticalCenter
            }

            Repeater {
                model: root.claudeLive

                delegate: Row {
                    required property var modelData
                    required property int index

                    spacing: Theme.spacingXS + 2
                    anchors.verticalCenter: parent.verticalCenter
                    opacity: modelData.status === "stale" ? 0.6 : 1

                    Rectangle {
                        visible: index > 0
                        width: 1
                        height: 12
                        color: Theme.outlineLight
                        anchors.verticalCenter: parent.verticalCenter
                    }

                    Rectangle {
                        visible: root.claudeLive.length > 1
                        width: 13
                        height: 13
                        radius: 6.5
                        color: Theme.withAlpha(Theme.surfaceVariantText, 0.2)
                        anchors.verticalCenter: parent.verticalCenter

                        StyledText {
                            anchors.centerIn: parent
                            text: String(index + 1)
                            font.pixelSize: Theme.fontSizeSmall - 3
                            font.weight: Font.DemiBold
                            color: Theme.surfaceText
                        }
                    }

                    BarMetric {
                        // Short "F" only when two Claude accounts compete for bar width.
                        label: {
                            if (root.claudeLive.length > 1)
                                return "F"
                            const item = root.modelWindow(modelData)
                            return item && item.label ? String(item.label) : "Fable"
                        }
                        windowData: root.modelWindow(modelData)
                        anchors.verticalCenter: parent.verticalCenter
                    }

                    BarMetric {
                        label: "5h"
                        windowData: root.labeledWindow(modelData, "5h")
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }
            }

            Rectangle {
                visible: root.claudeLive.length > 0 && root.codexLive.length > 0
                width: 1
                height: 16
                color: Theme.outlineMedium
                anchors.verticalCenter: parent.verticalCenter
            }

            Repeater {
                model: root.codexLive

                delegate: Row {
                    required property var modelData

                    spacing: Theme.spacingXS
                    anchors.verticalCenter: parent.verticalCenter
                    opacity: modelData.status === "stale" ? 0.6 : 1

                    CapacityRing {
                        width: 14
                        height: 14
                        thickness: 2
                        value: root.remainingOf(root.firstWindow(modelData))
                        ringColor: root.statusColor(root.remainingOf(root.firstWindow(modelData)))
                        anchors.verticalCenter: parent.verticalCenter
                    }

                    StyledText {
                        text: {
                            const item = root.firstWindow(modelData)
                            return item && item.label ? String(item.label) : "–"
                        }
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.surfaceVariantText
                        anchors.verticalCenter: parent.verticalCenter
                    }

                    StyledText {
                        text: root.percentText(root.firstWindow(modelData))
                        font.pixelSize: Theme.fontSizeSmall
                        font.weight: Font.DemiBold
                        color: Theme.surfaceText
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }
            }
        }
    }

    verticalBarPill: Component {
        Column {
            spacing: Theme.spacingXS

            DankIcon {
                visible: root.claudeLive.length === 0 && root.codexLive.length === 0
                name: "data_usage"
                size: Theme.iconSize - 6
                color: root.loadFailed ? Theme.error : Theme.surfaceVariantText
                anchors.horizontalCenter: parent.horizontalCenter
            }

            Repeater {
                model: root.claudeLive

                delegate: Column {
                    required property var modelData

                    spacing: 1
                    opacity: modelData.status === "stale" ? 0.6 : 1
                    anchors.horizontalCenter: parent.horizontalCenter

                    CapacityRing {
                        width: 18
                        height: 18
                        thickness: 2.5
                        value: root.worstClaudeRemaining(modelData)
                        ringColor: root.statusColor(root.worstClaudeRemaining(modelData))
                        anchors.horizontalCenter: parent.horizontalCenter
                    }

                    StyledText {
                        text: {
                            const remaining = root.worstClaudeRemaining(modelData)
                            return remaining < 0 ? "–" : String(Math.round(remaining))
                        }
                        font.pixelSize: Theme.fontSizeSmall - 2
                        font.weight: Font.DemiBold
                        color: Theme.surfaceText
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                }
            }

            Repeater {
                model: root.codexLive

                delegate: CapacityRing {
                    required property var modelData

                    width: 14
                    height: 14
                    thickness: 2
                    value: root.remainingOf(root.firstWindow(modelData))
                    ringColor: root.statusColor(root.remainingOf(root.firstWindow(modelData)))
                    opacity: modelData.status === "stale" ? 0.6 : 1
                    anchors.horizontalCenter: parent.horizontalCenter
                }
            }
        }
    }

    popoutContent: Component {
        PopoutComponent {
            id: popout

            headerText: "AI usage"
            detailsText: "Remaining capacity"
            showCloseButton: true

            Item {
                width: parent.width
                implicitHeight: root.popoutHeight - popout.headerHeight - popout.detailsHeight - Theme.spacingXL
                clip: true

                Column {
                    id: contentColumn

                    width: parent.width
                    spacing: Theme.spacingS

                    Repeater {
                        model: root.claudeLive

                        delegate: StyledRect {
                            id: claudeCard

                            required property var modelData

                            width: parent.width
                            radius: Theme.cornerRadius
                            color: Theme.nestedSurface
                            implicitHeight: claudeColumn.implicitHeight + Theme.spacingS * 2
                            opacity: modelData.status === "stale" ? 0.75 : 1

                            Column {
                                id: claudeColumn

                                anchors.top: parent.top
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.margins: Theme.spacingS
                                spacing: Theme.spacingXS

                                Item {
                                    width: parent.width
                                    height: Theme.iconSizeSmall

                                    DankIcon {
                                        id: claudeIcon

                                        name: "auto_awesome"
                                        size: Theme.iconSizeSmall
                                        color: Theme.surfaceVariantText
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.left: parent.left
                                    }

                                    StyledText {
                                        id: claudeProvider

                                        text: "Claude"
                                        font.pixelSize: Theme.fontSizeSmall
                                        color: Theme.surfaceVariantText
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.left: claudeIcon.right
                                        anchors.leftMargin: Theme.spacingXS
                                    }

                                    StyledText {
                                        text: "· " + claudeCard.modelData.label
                                        font.pixelSize: Theme.fontSizeSmall
                                        font.weight: Font.DemiBold
                                        color: Theme.surfaceText
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.left: claudeProvider.right
                                        anchors.leftMargin: 3
                                    }

                                    Rectangle {
                                        width: 7
                                        height: 7
                                        radius: 3.5
                                        color: claudeCard.modelData.status === "ok" ? Theme.success : Theme.warning
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.right: claudeSecondary.left
                                        anchors.rightMargin: Theme.spacingXS
                                    }

                                    StyledText {
                                        id: claudeSecondary

                                        text: {
                                            if (claudeCard.modelData.status !== "ok")
                                                return root.statusText(claudeCard.modelData)
                                            const seven = root.labeledWindow(claudeCard.modelData, "7d")
                                            return seven ? "7d " + root.percentText(seven) : ""
                                        }
                                        font.pixelSize: Theme.fontSizeSmall
                                        color: Theme.surfaceVariantText
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.right: parent.right
                                    }
                                }

                                Row {
                                    width: parent.width
                                    spacing: Theme.spacingM

                                    CompactStat {
                                        width: (parent.width - parent.spacing) / 2
                                        windowData: root.modelWindow(claudeCard.modelData)
                                        caption: windowData && windowData.label ? String(windowData.label) : "Fable"
                                    }

                                    CompactStat {
                                        width: (parent.width - parent.spacing) / 2
                                        windowData: root.labeledWindow(claudeCard.modelData, "5h")
                                        caption: "5h"
                                    }
                                }
                            }
                        }
                    }

                    Repeater {
                        model: root.codexLive

                        delegate: StyledRect {
                            id: codexCard

                            required property var modelData

                            width: parent.width
                            radius: Theme.cornerRadius
                            color: Theme.nestedSurface
                            implicitHeight: codexColumn.implicitHeight + Theme.spacingS * 2
                            opacity: modelData.status === "stale" ? 0.75 : 1

                            Column {
                                id: codexColumn

                                anchors.top: parent.top
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.margins: Theme.spacingS
                                spacing: Theme.spacingXS

                                Item {
                                    width: parent.width
                                    height: Theme.iconSizeSmall

                                    DankIcon {
                                        id: codexIcon

                                        name: "terminal"
                                        size: Theme.iconSizeSmall
                                        color: Theme.surfaceVariantText
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.left: parent.left
                                    }

                                    StyledText {
                                        id: codexProvider

                                        text: "Codex"
                                        font.pixelSize: Theme.fontSizeSmall
                                        color: Theme.surfaceVariantText
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.left: codexIcon.right
                                        anchors.leftMargin: Theme.spacingXS
                                    }

                                    StyledText {
                                        text: "· " + codexCard.modelData.label
                                        font.pixelSize: Theme.fontSizeSmall
                                        font.weight: Font.DemiBold
                                        color: Theme.surfaceText
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.left: codexProvider.right
                                        anchors.leftMargin: 3
                                    }

                                    Rectangle {
                                        width: 7
                                        height: 7
                                        radius: 3.5
                                        color: codexCard.modelData.status === "ok" ? Theme.success : Theme.warning
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.right: codexState.visible ? codexState.left : parent.right
                                        anchors.rightMargin: codexState.visible ? Theme.spacingXS : 0
                                    }

                                    StyledText {
                                        id: codexState

                                        visible: codexCard.modelData.status !== "ok"
                                        text: root.statusText(codexCard.modelData)
                                        font.pixelSize: Theme.fontSizeSmall
                                        color: Theme.surfaceVariantText
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.right: parent.right
                                    }
                                }

                                Repeater {
                                    model: root.windowsOf(codexCard.modelData)

                                    delegate: Row {
                                        required property var modelData

                                        width: parent.width
                                        spacing: Theme.spacingS

                                        StyledText {
                                        width: 32
                                            text: modelData.label
                                            font.pixelSize: Theme.fontSizeSmall
                                            color: Theme.surfaceVariantText
                                            elide: Text.ElideRight
                                            anchors.verticalCenter: parent.verticalCenter
                                        }

                                        CapacityMeter {
                                            width: parent.width - 32 - 44 - 88 - Theme.spacingS * 3
                                            height: 3
                                            value: root.remainingOf(modelData)
                                            meterColor: root.statusColor(root.remainingOf(modelData))
                                            anchors.verticalCenter: parent.verticalCenter
                                        }

                                        StyledText {
                                            width: 44
                                            text: root.percentText(modelData)
                                            font.pixelSize: Theme.fontSizeSmall
                                            font.weight: Font.DemiBold
                                            color: Theme.surfaceText
                                            horizontalAlignment: Text.AlignRight
                                            anchors.verticalCenter: parent.verticalCenter
                                        }

                                        StyledText {
                                            width: 88
                                            text: root.resetIn(modelData)
                                            font.pixelSize: Theme.fontSizeSmall
                                            color: Theme.surfaceVariantText
                                            elide: Text.ElideRight
                                            anchors.verticalCenter: parent.verticalCenter
                                        }
                                    }
                                }
                            }
                        }
                    }

                    Repeater {
                        model: root.offlineAccounts

                        delegate: StyledRect {
                            id: offlineRow

                            required property var modelData

                            width: parent.width
                            height: 34
                            radius: Theme.cornerRadius
                            color: Theme.nestedSurface

                            Row {
                                anchors.left: parent.left
                                anchors.leftMargin: Theme.spacingM
                                anchors.verticalCenter: parent.verticalCenter
                                spacing: Theme.spacingS

                                Rectangle {
                                    width: 8
                                    height: 8
                                    radius: 4
                                    color: "transparent"
                                    border.width: 1
                                    border.color: Theme.outlineStrong
                                    anchors.verticalCenter: parent.verticalCenter
                                }

                                StyledText {
                                    text: root.providerName(offlineRow.modelData) + " · " + offlineRow.modelData.label
                                    font.pixelSize: Theme.fontSizeSmall
                                    font.weight: Font.DemiBold
                                    color: Theme.surfaceText
                                    anchors.verticalCenter: parent.verticalCenter
                                }

                                StyledText {
                                    text: root.statusText(offlineRow.modelData)
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: Theme.surfaceVariantText
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }
                        }
                    }

                    StyledText {
                        visible: root.accounts.length === 0
                        text: root.loadFailed ? "Collector failed" : "No accounts configured"
                        font.pixelSize: Theme.fontSizeMedium
                        color: Theme.surfaceVariantText
                    }
                }
            }
        }
    }

    popoutWidth: 380
    popoutHeight: 350
}
