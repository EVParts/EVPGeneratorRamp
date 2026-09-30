import QtQuick 1.1

MbPage {
    title: qsTr("Generator Ramp")

    model: VisualItemModel {

        MbSpinBox {
            description: qsTr("Current Limit")
            bind: "com.victronenergy.settings/Settings/GeneratorRamp/CurrentLimit"
            unit: "A"
            numOfDecimals: 1
            min: 0
            max: 100
            stepSize: 0.1
            writeAccessLevel: User.AccessUser
        }
    }
}