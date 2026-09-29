import QtQuick
import Quickshell
import qs.Common
import qs.Widgets
import qs.Modules.Plugins

PluginComponent {
    id: root

    layerNamespacePlugin: "ai-usage"

    property var accounts: []
    property string generatedAt: ""
    property int refreshSeconds: 600
    property bool loadFailed: false
    property bool refreshInProgress: false
    property string collectorPath: Quickshell.env("HOME") + "/.local/bin/dms-ai-usage"

    readonly property var claudeLive: root.liveAccounts("claude")
    readonly property var codexLive: root.liveAccounts("codex")
    readonly property var offlineAccounts: accounts.filter(account => !root.isConnected(account))

    function refreshUsage() {
        if (root.refreshInProgress)
            return
        root.refreshInProgress = true
        Proc.runCommand(
            null,
            [root.collectorPath, "--cached"],
            (stdout, exitCode) => {
                root.refreshInProgress = false
                if (exitCode !== 0 && !stdout.trim()) {
                    root.loadFailed = true
                    return
                }
                try {
                    const payload = JSON.parse(stdout.trim())
                    root.accounts = Array.isArray(payload.accounts) ? payload.accounts : []
                    root.generatedAt = payload.generated_at || ""
                    root.loadFailed = exitCode !== 0 || Boolean(payload.error)
                    if (payload.refresh_seconds >= 30)
                        root.refreshSeconds = payload.refresh_seconds
                } catch (error) {
                    root.loadFailed = true
                }
            },
            100,
            -1, // The collector bounds provider requests and SSH itself.
            root
        )
    }

    function isConnected(account) {
        return (account.status === "ok" || account.status === "stale")
            && root.windowsOf(account).length > 0
    }

    function accountOrder(account) {
        const label = String(account && account.label || "").toLowerCase()
        if (label.indexOf("gmail") !== -1)
            return 0
        if (label.indexOf("ffe") !== -1)
            return 1
        return 2
    }

    function liveAccounts(provider) {
        return accounts
            .filter(account => account.provider === provider && root.isConnected(account))
            .sort((left, right) => root.accountOrder(left) - root.accountOrder(right))
    }

    function accountInitial(account) {
        const label = String(account && account.label || "").trim()
        const lower = label.toLowerCase()
        if (lower.indexOf("gmail") !== -1)
            return "G"
        if (lower.indexOf("ffe") !== -1)
            return "F"
        return label.length > 0 ? label.charAt(0).toUpperCase() : "–"
    }

    // Accounts crossing a Repeater arrive as QVariantMaps: windows becomes a
    // QVariantList, which fails Array.isArray. Iterate by length instead, and
    // drop hidden quotas defensively, including from an older cache.
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
            if (account.provider === "codex"
                    && (text.indexOf("gpt-reserve") !== -1 || text.indexOf("base_model_inference") !== -1))
                continue
            if (account.provider === "codex" && item.label !== "7d")
                continue
            if (account.provider === "claude" && item.id !== "five_hour" && item.id !== "seven_day")
                continue
            result.push(item)
        }
        return result
    }

    function labeledWindow(account, label) {
        for (const item of root.windowsOf(account)) {
            if (item.label === label)
                return item
        }
        return null
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
            return ""
        if (account.status === "stale")
            return "stale"
        return (account.error || "not connected").replace(/_/g, " ")
    }

    function providerName(account) {
        return account && account.provider === "claude" ? "Claude" : "Codex"
    }

    // Shortest window first, named the way Claude's own /usage names them.
    function limitsOf(account) {
        const result = []
        const session = root.labeledWindow(account, "5h")
        const week = root.labeledWindow(account, "7d")
        if (session)
            result.push({ name: "Session", data: session })
        if (week)
            result.push({ name: "Week", data: week })
        return result
    }

    // Language-neutral countdown ("57m", "5d 17h"). Reading generatedAt
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
            return minutes + "m"
        const hours = Math.floor(minutes / 60)
        if (hours < 24)
            return hours + "h " + (minutes % 60) + "m"
        return Math.floor(hours / 24) + "d " + (hours % 24) + "h"
    }

    component CapacityMeter: Rectangle {
        id: meterRoot

        property real value: -1
        property color meterColor: Theme.primary
        property color trackColor: Theme.withAlpha(meterColor, 0.18)

        height: 6
        radius: height / 2
        color: trackColor

        Rectangle {
            width: meterRoot.value >= 0
                ? meterRoot.width * Math.max(0, Math.min(100, meterRoot.value)) / 100
                : 0
            height: parent.height
            radius: parent.radius
            color: meterRoot.meterColor
        }
    }

    component ProviderIcon: Item {
        id: providerIconRoot

        property string provider

        width: Theme.iconSizeSmall
        height: Theme.iconSizeSmall

        Image {
            anchors.fill: parent
            source: Quickshell.iconPath(providerIconRoot.provider === "claude" ? "claude-desktop" : "chatgpt", true)
            sourceSize.width: 32
            sourceSize.height: 32
            fillMode: Image.PreserveAspectFit
            smooth: true
            mipmap: true
        }
    }

    // Keep each account compact: provider + initial above its weekly meter.
    component BarMetric: Column {
        id: metricRoot

        property var account
        readonly property var windowData: root.labeledWindow(account, "7d")

        spacing: 3

        Row {
            id: metricRow

            spacing: 3

            ProviderIcon {
                provider: metricRoot.account.provider
                anchors.verticalCenter: parent.verticalCenter
            }

            StyledText {
                text: root.accountInitial(metricRoot.account)
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

    // One quota per line, laid out as a table: name, meter, percent, reset.
    component LimitRow: Row {
        id: limitRoot

        property string name
        property var windowData
        readonly property real remaining: root.remainingOf(windowData)

        spacing: Theme.spacingM

        StyledText {
            width: 48
            text: limitRoot.name
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.surfaceVariantText
            anchors.verticalCenter: parent.verticalCenter
        }

        CapacityMeter {
            width: limitRoot.width - 48 - 36 - 52 - limitRoot.spacing * 3
            height: 4
            value: limitRoot.remaining
            meterColor: root.statusColor(limitRoot.remaining)
            trackColor: Theme.withAlpha(Theme.surfaceText, 0.08)
            anchors.verticalCenter: parent.verticalCenter
        }

        StyledText {
            width: 36
            text: root.percentText(limitRoot.windowData)
            font.pixelSize: Theme.fontSizeSmall
            font.features: { "tnum": 1 }
            color: Theme.surfaceText
            horizontalAlignment: Text.AlignRight
            anchors.verticalCenter: parent.verticalCenter
        }

        StyledText {
            width: 52
            text: root.resetIn(limitRoot.windowData)
            font.pixelSize: Theme.fontSizeSmall
            font.features: { "tnum": 1 }
            color: Theme.surfaceVariantText
            horizontalAlignment: Text.AlignRight
            anchors.verticalCenter: parent.verticalCenter
        }
    }

    component AccountSection: Column {
        id: sectionRoot

        property var account
        readonly property bool connected: root.isConnected(account)
        readonly property string status: root.statusText(account)

        spacing: Theme.spacingS

        Item {
            width: parent.width
            height: Math.max(Theme.iconSizeSmall, providerText.implicitHeight)
            opacity: sectionRoot.connected ? 1 : 0.6

            ProviderIcon {
                id: sectionIcon

                provider: sectionRoot.account.provider
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
            }

            StyledText {
                id: providerText

                text: root.providerName(sectionRoot.account)
                font.pixelSize: Theme.fontSizeMedium
                font.weight: Font.Medium
                color: Theme.surfaceText
                anchors.left: sectionIcon.right
                anchors.leftMargin: Theme.spacingS
                anchors.verticalCenter: parent.verticalCenter
            }

            StyledText {
                text: sectionRoot.account.label
                font.pixelSize: Theme.fontSizeMedium
                color: Theme.surfaceVariantText
                elide: Text.ElideRight
                anchors.left: providerText.right
                anchors.leftMargin: Theme.spacingS
                anchors.right: statusLabel.left
                anchors.rightMargin: Theme.spacingS
                anchors.verticalCenter: parent.verticalCenter
            }

            StyledText {
                id: statusLabel

                text: sectionRoot.status
                font.pixelSize: Theme.fontSizeSmall
                color: sectionRoot.account.status === "stale" ? Theme.warning : Theme.surfaceVariantText
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
            }
        }

        // Indented so quota names line up under the provider name.
        Column {
            visible: sectionRoot.connected
            x: Theme.iconSizeSmall + Theme.spacingS
            width: parent.width - x
            spacing: Theme.spacingXS + 2

            Repeater {
                model: sectionRoot.connected ? root.limitsOf(sectionRoot.account) : []

                delegate: LimitRow {
                    required property var modelData

                    width: parent.width
                    name: modelData.name
                    windowData: modelData.data
                }
            }
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
                model: root.claudeLive.concat(root.codexLive)

                delegate: BarMetric {
                    required property var modelData

                    account: modelData
                    anchors.verticalCenter: parent.verticalCenter
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
                model: root.claudeLive.concat(root.codexLive)

                delegate: BarMetric {
                    required property var modelData

                    account: modelData
                    anchors.horizontalCenter: parent.horizontalCenter
                }
            }
        }
    }

    popoutContent: Component {
        PopoutComponent {
            id: popout

            Item {
                width: parent.width
                implicitHeight: contentColumn.implicitHeight + Theme.spacingS * 2

                Column {
                    id: contentColumn

                    readonly property var sections: root.claudeLive.concat(root.codexLive, root.offlineAccounts)

                    x: Theme.spacingS
                    y: Theme.spacingS
                    width: parent.width - Theme.spacingS * 2

                    Repeater {
                        model: contentColumn.sections

                        delegate: Column {
                            required property var modelData
                            required property int index

                            width: parent.width

                            Rectangle {
                                visible: index > 0
                                width: parent.width
                                height: 1
                                color: Theme.withAlpha(Theme.outline, 0.2)
                            }

                            AccountSection {
                                width: parent.width
                                account: modelData
                                topPadding: index > 0 ? Theme.spacingM : 0
                                bottomPadding: index < contentColumn.sections.length - 1 ? Theme.spacingM : 0
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

                MouseArea {
                    anchors.fill: parent
                    anchors.margins: -Theme.spacingS
                    acceptedButtons: Qt.AllButtons
                    onClicked: root.closePopout()
                }
            }
        }
    }

    popoutWidth: 360
}
