#!/usr/bin/env python3

import os
import sys
from time import time, sleep

import dbus
from dbus.mainloop.glib import DBusGMainLoop

# our own packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'velib_python'))

from vedbus import VeDbusItemImport

import logging
#logging.basicConfig( level=logging.DEBUG )
logger=logging.getLogger("generator_control")
logger.setLevel(logging.INFO)

softwareVersion = '1.0'

INV_SWITCH_OFF = 4
INV_SWITCH_ON = 3
INV_SWITCH_INVERT_ONLY = 2
INV_SWITCH_CHARGE_ONLY = 1

REVERSE_POWER_THRESHOLD = -100  # Watts

TIMESTEP = 1.0
REVERSE_POWER_COUNTER_THRESHOLD = 10 / TIMESTEP  # 10s


class GeneratorController():
    def __init__(self):
        self.dbusConn = None
        self.Mode = "Off"
        self.Battery_SOC = 0
        self.AC_Output_Power = 0
        self.Reverse_Power_Counter = 0
        self.Reverse_Power_Alarm = False

    @property
    def Off_Button_Pressed(self):
        path = "/dev/gpio/digital_input_5/value"
        with open(path) as f:
            return (f.read().strip() == '1')

    @property
    def On_Button_Pressed(self):
        path = "/dev/gpio/digital_input_6/value"
        with open(path) as f:
            return (f.read().strip() == '1')

    @property
    def Charge_Button_Pressed(self):
        path = "/dev/gpio/digital_input_7/value"
        with open(path) as f:
            return (f.read().strip() == '1')

    @property
    def Off_LED_Feedback(self):
        path = "/dev/gpio/digital_input_8/value"
        with open(path) as f:
            return (f.read().strip() == '1')

    @property
    def On_LED_Feedback(self):
        path = "/dev/gpio/digital_input_9/value"
        with open(path) as f:
            return (f.read().strip() == '1')

    @property
    def Charge_LED_Feedback(self):
        path = "/dev/gpio/digital_input_a/value"
        with open(path) as f:
            return (f.read().strip() == '1')

    @property
    def BMS_Wake_Feedback(self):
        path = "/dev/gpio/digital_input_b/value"
        with open(path) as f:
            return (f.read().strip() == '1')

    @property
    def Off_LED(self):
        return self.Mode == "Off"

    @property
    def On_LED(self):
        return self.Mode == "On"

    @property
    def Charge_LED(self):
        return self.Mode == "ChargeOnly"

    @property
    def BMS_Wake(self):
        # BMS Wake set in all modes except off with low SOC
        return not ((self.Mode == "Off") and (self.Battery_SOC < 50))

    @property
    def DSE_Remote_Start(self):
        # Remote Start only set in "On" mode
        return (self.Mode == "On")

    @property
    def DSE_Mode_Request(self):
        # Mode Request set in "On" and "ChargeOnly" mode
        return ((self.Mode == "On") or (self.Mode == "ChargeOnly"))

    @property
    def RCD_Test_Switch(self):
        # If all 3 buttons held down then trigger RCD reset relay
        return ((self.Off_Button_Pressed) and (self.On_Button_Pressed) and (self.Charge_Button_Pressed))


    @property
    def Inverter_Switch_Mode(self):
        if self.Reverse_Power_Alarm: # Disable the inverter if the Reverse Power Alarm is set
            return INV_SWITCH_OFF
        else:
            modes = {"Off": INV_SWITCH_OFF, "On": INV_SWITCH_ON, "InvertOnly": INV_SWITCH_INVERT_ONLY, "ChargeOnly": INV_SWITCH_CHARGE_ONLY}
            return modes[self.Mode]

    @property
    def Reverse_Power_Detected(self):
        return self.AC_Output_Power < REVERSE_POWER_THRESHOLD

    def update_mode(self):
        if self.Off_Button_Pressed:
            self.Mode = "Off"
        elif self.On_Button_Pressed:
            self.Mode = "On"
        elif self.Charge_Button_Pressed:
            self.Mode = "ChargeOnly"
        else:
            pass  # Leave mode unchanged

    def update_battery_soc(self):
        dbus_item = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Dc/Battery/Soc")
        val = dbus_item.get_value()
        if val:
            self.Battery_SOC = val
        else:
            print("Did not receive data from battery")
            self.Battery_SOC = 0

    def update_ac_output_power(self):
        dbus_item = VeDbusItemImport(self.dbusConn, "com.victronenergy.vebus.ttyS2", "/Ac/Out/L1/P")
        val = dbus_item.get_value()
        if val:
            self.AC_Output_Power = val
        else:
            print("Did not receive data from inverter")
            self.AC_Output_Power = 0

    def set_outputs(self):
        off_LED_relay = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/2/State")
        normal_LED_relay = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/3/State")
        charge_LED_relay = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/4/State")
        BMSWake_relay = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/5/State")
        DSERemoteStart_relay = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/6/State")
        DSEModeRequest_relay = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/7/State")
        RCD_Reset_Switch_relay = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/8/State")
        ReversePowerAlarm_relay = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/9/State")

        off_LED_relay.set_value(self.Off_LED)
        normal_LED_relay.set_value(self.On_LED)
        charge_LED_relay.set_value(self.Charge_LED)
        BMSWake_relay.set_value(self.BMS_Wake)
        DSERemoteStart_relay.set_value(self.DSE_Remote_Start)
        DSEModeRequest_relay.set_value(self.DSE_Mode_Request)
        RCD_Reset_Switch_relay.set_value(self.RCD_Test_Switch)
        ReversePowerAlarm_relay.set_value(self.Reverse_Power_Alarm)

    def set_inverter_switch_mode(self):
        inverter_switch_mode = VeDbusItemImport(self.dbusConn, "com.victronenergy.vebus.ttyS2", "/Mode")
        inverter_switch_mode.set_value(self.Inverter_Switch_Mode)

    def check_reverse_power(self):
        if self.Reverse_Power_Detected:
            self.Reverse_Power_Counter += 1
            max(REVERSE_POWER_COUNTER_THRESHOLD, self.Reverse_Power_Counter)
        else:
            self.Reverse_Power_Counter -= 1
            self.Reverse_Power_Counter = max(0, self.Reverse_Power_Counter)

        if (self.Reverse_Power_Counter >= REVERSE_POWER_COUNTER_THRESHOLD):
            self.Reverse_Power_Alarm = True
        elif (self.Mode == "Off") and (self.Reverse_Power_Counter == 0): # Only reset if Off and has counted back down to 0
            self.Reverse_Power_Alarm = False

    def run(self):
        DBusGMainLoop(set_as_default=True)
        self.dbusConn = dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()

        t0 = time()
        while True:
            self.update_mode()
            self.update_battery_soc()
            self.update_ac_output_power()
            self.check_reverse_power()
            self.set_outputs()
            self.set_inverter_switch_mode()
            print(f"{time():.2f}s : {self}")
            logger.info(self)
            sleep(TIMESTEP)

    def __repr__(self):
        return ','.join([f"Mode {self.Mode}",
                         f"SOC {self.Battery_SOC}%",
                         f"AC Out {self.AC_Output_Power}W",
                         f"Switch Mode {self.Inverter_Switch_Mode}",
                         f"Reverse Power {self.Reverse_Power_Detected}",
                         f"Reverse Power {self.Reverse_Power_Counter * TIMESTEP}s",
                         f"Off LED {self.Off_LED}",
                         f"On LED {self.On_LED}",
                         f"Charge LED {self.Charge_LED}"])


if __name__ == "__main__":
    g = GeneratorController()
    g.run()  # global dbusObjects  #  # print(__file__ + " starting up")

    # # Have a mainloop, so we can send/receive asynchronous calls to and from dbus  # DBusGMainLoop(set_as_default=True)
