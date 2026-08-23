import QtQuick
import Quickshell
import qs.Common
import qs.Widgets
import qs.Modules.Plugins

PluginComponent {
    id: root

    layerNamespacePlugin: "ai-usage"

    property string displayText: "AI …"
    property var accounts: []
    property string generatedAt: ""
    property int refreshSeconds: 120
    property string collectorPath: Quickshell.env("HOME") + "/.local/bin/dms-ai-usage"

    function refreshUsage() {
        Proc.runCommand(
            "dmsAiUsage.collect",
            [root.collectorPath],
            (stdout, exitCode) => {
                if (exitCode !== 0 && !stdout.trim()) {
                    root.displayText = "AI !"
                    return
                }
                try {
                    const payload = JSON.parse(stdout.trim())
                    root.displayText = payload.bar_text || "AI –"
                    root.accounts = Array.isArray(payload.accounts) ? payload.accounts : []
                    root.generatedAt = payload.generated_at || ""
                    if (payload.refresh_seconds >= 30)
                        root.refreshSeconds = payload.refresh_seconds
                } catch (error) {
                    root.displayText = "AI !"
                }
            },
            100
        )
    }

    function colorForRemaining(value) {
        if (value === undefined || value === null)
            return Theme.surfaceVariantText
        if (value <= 15)
            return Theme.error
        if (value <= 35)
            return Theme.tertiary
        return Theme.primary
    }

    function statusText(account) {
        if (account.status === "ok")
            return "live"
        if (account.status === "stale")
            return "stale"
        return (account.error || "unavailable").replace(/_/g, " ")
    }

    function resetText(value) {
        if (value === undefined || value === null || value === "")
            return ""
        const date = typeof value === "number" ? new Date(value * 1000) : new Date(value)
        if (isNaN(date.getTime()))
            return ""
        return "resets " + date.toLocaleString(Qt.locale(), "ddd HH:mm")
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
                name: "data_usage"
                size: Theme.iconSize
                color: Theme.primary
                anchors.verticalCenter: parent.verticalCenter
            }

            StyledText {
                text: root.displayText
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.surfaceText
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }

    verticalBarPill: Component {
        Column {
            spacing: Theme.spacingXS

            DankIcon {
                name: "data_usage"
                size: Theme.iconSize
                color: Theme.primary
                anchors.horizontalCenter: parent.horizontalCenter
            }

            StyledText {
                text: "AI"
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.surfaceText
                anchors.horizontalCenter: parent.horizontalCenter
            }
        }
    }

    popoutContent: Component {
        PopoutComponent {
            id: popout

            headerText: "AI usage"
            detailsText: "Remaining subscription capacity"
            showCloseButton: true

            Column {
                width: parent.width
                spacing: Theme.spacingM

                Repeater {
                    model: root.accounts

                    delegate: Column {
                        required property var modelData
                        width: parent.width
                        spacing: Theme.spacingXS

                        Row {
                            width: parent.width
                            spacing: Theme.spacingS

                            StyledText {
                                text: modelData.label
                                font.pixelSize: Theme.fontSizeMedium
                                font.weight: Font.DemiBold
                                color: Theme.surfaceText
                            }

                            StyledText {
                                text: root.statusText(modelData)
                                font.pixelSize: Theme.fontSizeSmall
                                color: modelData.status === "ok" ? Theme.primary : Theme.surfaceVariantText
                            }
                        }

                        Repeater {
                            model: modelData.windows || []

                            delegate: Row {
                                required property var modelData
                                width: parent.width
                                spacing: Theme.spacingS

                                StyledText {
                                    width: 150
                                    text: modelData.label
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: Theme.surfaceVariantText
                                }

                                StyledText {
                                    text: Math.round(modelData.remaining_percent) + "% left"
                                    font.pixelSize: Theme.fontSizeSmall
                                    font.weight: Font.DemiBold
                                    color: root.colorForRemaining(modelData.remaining_percent)
                                }

                                StyledText {
                                    text: root.resetText(modelData.resets_at)
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: Theme.surfaceVariantText
                                }
                            }
                        }
                    }
                }

                StyledText {
                    visible: root.accounts.length === 0
                    text: "No accounts configured"
                    font.pixelSize: Theme.fontSizeMedium
                    color: Theme.surfaceVariantText
                }
            }
        }
    }

    popoutWidth: 560
    popoutHeight: 440
}
