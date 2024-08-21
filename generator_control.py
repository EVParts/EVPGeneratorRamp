#!/usr/bin/env python3
import datetime
import os
import sys
from time import time, sleep

import dbus
from dbus.mainloop.glib import DBusGMainLoop

# our own packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'velib_python'))

from vedbus import VeDbusItemImport

from logger import setup_logging
#logging.basicConfig( level=logging.DEBUG )
logger=setup_logging(name="generator_control")

softwareVersion = '1.0'

INV_SWITCH_OFF = 4
INV_SWITCH_ON = 3
INV_SWITCH_INVERT_ONLY = 2
INV_SWITCH_CHARGE_ONLY = 1

REVERSE_POWER_THRESHOLD = -100  # Watts

TIMESTEP = 0.25
REVERSE_POWER_COUNTER_THRESHOLD = 10 / TIMESTEP  # 10s


class GeneratorController():
    def __init__(self):
        DBusGMainLoop(set_as_default=True)
        self.dbusConn = dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()
        self.Mode = ""
        self._Toggle_State = False
        self.Battery_SOC = 0
        self.AC_Output_Power = 0
        self.Reverse_Power_Counter = 0
        self.Reverse_Power_Alarm = False

        self.Inverter_Connected = True
        self.BMS_Connected = True

        self._dbus_battery_soc = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Dc/Battery/Soc")
        self._dbus_ac_output_power = VeDbusItemImport(self.dbusConn, "com.victronenergy.vebus.ttyS2", "/Ac/Out/L1/P")
        self._dbus_inverter_switch = VeDbusItemImport(self.dbusConn, "com.victronenergy.vebus.ttyS2", "/Mode")

        self._dbus_relay_2 = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/2/State")
        self._dbus_relay_3 = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/3/State")
        self._dbus_relay_4 = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/4/State")
        self._dbus_relay_5 = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/5/State")
        self._dbus_relay_6 = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/6/State")
        self._dbus_relay_7 = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/7/State")
        self._dbus_relay_8 = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/8/State")
        self._dbus_relay_9 = VeDbusItemImport(self.dbusConn, "com.victronenergy.system", "/Relay/9/State")

    @property
    def Fault_Detected(self):
        if self.Mode not in ["Off", "On", "ChargeOnly"]:
            return True
        if self.BMS_Connected == False:
            return True
        if self.Inverter_Connected == False:
            return True
        if self.Reverse_Power_Alarm == False:
            return True

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
    def RCD_Reset_Switch(self):
        # If on and off buttons held down then trigger RCD reset relay
        return ((self.Off_Button_Pressed) and (self.On_Button_Pressed))

    @property
    def Service_Restart_Requested(self):
        # If all 3 buttons held down then trigger script reset
        return ((self.Off_Button_Pressed) and (self.On_Button_Pressed) and (self.Charge_Button_Pressed))


    @property
    def Inverter_Switch_Mode(self):
        if self.Reverse_Power_Alarm: # Disable the inverter if the Reverse Power Alarm is set
            return INV_SWITCH_OFF
        else:
            modes = {"Off": INV_SWITCH_OFF, "On": INV_SWITCH_ON, "InvertOnly": INV_SWITCH_INVERT_ONLY, "ChargeOnly": INV_SWITCH_CHARGE_ONLY}
            return modes.get(self.Mode)

    @property
    def Reverse_Power_Detected(self):
        return self.AC_Output_Power < REVERSE_POWER_THRESHOLD

    def update_mode(self):
        _last_mode = self.Mode
        if self.Off_Button_Pressed:
            self.Mode = "Off"
        elif self.On_Button_Pressed:
            self.Mode = "On"
        elif self.Charge_Button_Pressed:
            self.Mode = "ChargeOnly"
        else:
            pass  # Leave mode unchanged

        if (_last_mode != "Off") and (self.Mode != "Off"):
            print(self, flush=True)

    def get_dbus_value(self, dbus_item : VeDbusItemImport):
        # print(f"Get DBus Value () : {dbus_item.serviceName} - {dbus_item.path}", flush=True)
        try:
            return dbus_item.get_value()
        except dbus.exceptions.DBusException as e:
            print(f"Could not get DBUS Item : {dbus_item.serviceName} - {dbus_item.path}", flush=True)
            print(e, flush=True))
            return None

    def set_dbus_value(self, dbus_item : VeDbusItemImport, value):
        # print(f"Set DBus Value () : {dbus_item.serviceName} - {dbus_item.path} : {Value}", flush=True))
        try:
            dbus_item.set_value(value)
        except dbus.exceptions.DBusException as e:
            print(f"Could not set DBUS Item : {dbus_item.serviceName} - {dbus_item.path} : {value}", flush=True)
            print(e, flush=True))
            return None

    def update_battery_soc(self):
        val = self.get_dbus_value(self._dbus_battery_soc)
        if val:
            self.BMS_Connected = True
            self.Battery_SOC = val
        else:
            self.BMS_Connected = False
            print("Did not receive data from battery", flush=True)
            self.Battery_SOC = 0

    def update_ac_output_power(self):
        val = self.get_dbus_value(self._dbus_ac_output_power)
        if val:
            self.Inverter_Connected = True
            self.AC_Output_Power = val
        else:
            self.Inverter_Connected = False
            print("Did not receive data from inverter", flush=True)
            self.AC_Output_Power = 0

    def set_outputs(self):
        if (self.Fault_Detected):
            self.set_dbus_value(self._dbus_relay_2, self._Toggle_State)
            self._Toggle_State = not self._Toggle_State
        else:
            self.set_dbus_value(self._dbus_relay_2, self.Off_LED)
        self.set_dbus_value(self._dbus_relay_3, self.On_LED)
        self.set_dbus_value(self._dbus_relay_4, self.Charge_LED)
        self.set_dbus_value(self._dbus_relay_5, self.BMS_Wake)
        self.set_dbus_value(self._dbus_relay_6, self.DSE_Remote_Start)
        self.set_dbus_value(self._dbus_relay_7, self.DSE_Mode_Request)
        self.set_dbus_value(self._dbus_relay_8, self.RCD_Reset_Switch)
        self.set_dbus_value(self._dbus_relay_9, self.Reverse_Power_Alarm)


    def set_inverter_switch_mode(self):
        self.set_dbus_value( self._dbus_inverter_switch, self.Inverter_Switch_Mode)

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

        while True:
            self.update_mode()
            self.update_battery_soc()
            self.update_ac_output_power()
            self.check_reverse_power()
            self.set_outputs()
            self.set_inverter_switch_mode()
            if (self.Service_Restart_Requested):
                print("Service Restart Requested, Going Down in 5s!", flush=True)
                sleep(5)
                exit()
            # print(f"{datetime.isoformat(datetime.now())} : {self}", flush=True))
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
                         f"Charge LED {self.Charge_LED}",
                         f"BMS Wake {self.BMS_Wake}",
                         f"DSE Start {self.DSE_Remote_Start}",
                         f"DSE Mode {self.DSE_Mode_Request}",
                         f"Fault {self.Fault_Detected}",
                         ]
                        )



if __name__ == "__main__":
    try:
        print("Running generator_control.py", flush=True)
        print("Waiting 10s for system to startup", flush=True)
        sleep(10)
        print("Running now!", flush=True)
        g = GeneratorController()
        print(g)
        g.run()  # global dbusObjects  #  # print(__file__ + " starting up")
    except Exception as e:
        print("Exception Raised", flush=True)
        print(e, flush=True)
        raise
    # # Have a mainloop, so we can send/receive asynchronous calls to and from dbus  # DBusGMainLoop(set_as_default=True)
