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
logger = setup_logging(name="generator_control")

softwareVersion = '1.0'

INV_SWITCH_OFF = 4
INV_SWITCH_ON = 3
INV_SWITCH_INVERT_ONLY = 2
INV_SWITCH_CHARGE_ONLY = 1

REVERSE_POWER_THRESHOLD = -5  # Amps

DEFAULT_MODE = "Off"

TIMESTEP = 1
REVERSE_POWER_COUNTER_THRESHOLD = 10 / TIMESTEP  # 10s


class GeneratorController():
    def __init__(self):
        DBusGMainLoop(set_as_default=True)
        self.dbusConn = dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()
        self.Mode = ""
        self._Toggle_State = False
        self._inverter_switch_mode_update_time = 0
        self.Battery_SOC = 0
        self.AC_Output_Current = 0
        self.Inverter_Switch_Mode = 0
        self.Reverse_Power_Counter = 0
        self.Reverse_Power_Alarm = False
        self.Inverter_Connected = False
        self.BMS_Connected = False
        self.outputs_str = ""

        self.dbus_items_spec = {
            "battery_soc": {"service": "com.victronenergy.system", "path": "/Dc/Battery/Soc"},
            "ac_output_current": {"service": "com.victronenergy.vebus.ttyS2", "path": "/Ac/Out/L1/I"},
            "inverter_switch_mode": {"service": "com.victronenergy.vebus.ttyS2", "path": "/Mode"},
            "relay_2": {"service": "com.victronenergy.system", "path": "/Relay/2/State"},
            "relay_3": {"service": "com.victronenergy.system", "path": "/Relay/3/State"},
            "relay_4": {"service": "com.victronenergy.system", "path": "/Relay/4/State"},
            "relay_5": {"service": "com.victronenergy.system", "path": "/Relay/5/State"},
            "relay_6": {"service": "com.victronenergy.system", "path": "/Relay/6/State"},
            "relay_7": {"service": "com.victronenergy.system", "path": "/Relay/7/State"},
            "relay_8": {"service": "com.victronenergy.system", "path": "/Relay/8/State"},
            "relay_9": {"service": "com.victronenergy.system", "path": "/Relay/9/State"},
        }

        self.dbus_items = {}

        self.check_and_create_connections()

    @property
    def Fault_Detected(self):
        if self.Mode not in ["Off", "On", "ChargeOnly"]:
            print("Mode Fault", flush=True)
            return True
        if self.BMS_Connected == False:
            print("BMS Fault", flush=True)
            return True
        if (self.Mode != "Off") and (self.Inverter_Connected == False):
            print("Inverter Fault", flush=True)
            return True
        if (self.Mode != "Off") and (self.Reverse_Power_Alarm == True):
            print("Reverse Power Fault", flush=True)
            return True

    # if self.Relays_Connected == False:
    #     return True

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
    def Inverter_Switch_Mode_Target(self):
        if self.Reverse_Power_Alarm:  # Disable the inverter if the Reverse Power Alarm is set
            return INV_SWITCH_OFF
        else:
            modes = {"Off": INV_SWITCH_OFF, "On": INV_SWITCH_ON, "InvertOnly": INV_SWITCH_INVERT_ONLY,
                     "ChargeOnly": INV_SWITCH_CHARGE_ONLY}
            return modes.get(self.Mode)

    @property
    def Reverse_Power_Detected(self):
        return self.AC_Output_Current < REVERSE_POWER_THRESHOLD

    def update_mode(self):
        _last_mode = self.Mode

        if self.Mode == "":
            self.Mode = DEFAULT_MODE

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

    def get_dbus_value(self, dbus_item_name: str):
        if (dbus_item := self.dbus_items.get(dbus_item_name)) is not None:
            # print(f"Get DBus Value () : {dbus_item.serviceName} - {dbus_item.path}", flush=True)
            try:
                return dbus_item.get_value()
            except dbus.exceptions.DBusException as e:
                print(f"Could not get DBUS Item : {dbus_item.serviceName} - {dbus_item.path}", flush=True)
                print(e, flush=True)
                return None

    def set_dbus_value(self, dbus_item_name: str, value):
        if (dbus_item := self.dbus_items.get(dbus_item_name)) is not None:
            # print(f"Set DBus Value () : {dbus_item.serviceName} - {dbus_item.path} : {Value}", flush=True))
            try:
                dbus_item.set_value(value)
            except dbus.exceptions.DBusException as e:
                print(f"Could not set DBUS Item : {dbus_item.serviceName} - {dbus_item.path} : {value}", flush=True)
                print(e, flush=True)
                return None
    #
    # def clear_dbus_item(self, dbus_item_name):
    #     dbus_item = self.dbus_items.pop(dbus_item_name)
    #     if isinstance(dbus_item, VeDbusItemImport):
    #         pass

    def update_battery_soc(self):
        val = self.get_dbus_value("battery_soc")
        if val is not None:
            self.BMS_Connected = True
            self.Battery_SOC = val
        else:
            self.BMS_Connected = False
            print("Did not receive data from battery", flush=True)
            self.Battery_SOC = 0

    def update_ac_output_current(self):
        print("Getting AC Out Curr")
        val = self.get_dbus_value("ac_output_current")
        print(f"Got AC Out Curr : {val}")
        if val is not None:
            self.Inverter_Connected = True
            self.AC_Output_Current = val
        else:
            self.Inverter_Connected = False
            print("Did not receive data from inverter", flush=True)
            self.AC_Output_Current = 0

    def update_inverter_switch_mode(self):
        val = self.get_dbus_value("inverter_switch_mode")
        if val is not None:
            self.Inverter_Connected = True
            self.Inverter_Switch_Mode = val
        else:
            self.Inverter_Connected = False
            print("Did not receive switch mode from inverter", flush=True)
            self.Inverter_Switch_Mode = 0

    def set_outputs(self):
        outs = ""
        if (self.Fault_Detected):
            outs += "1" if self._Toggle_State else "0"
            self.set_dbus_value("relay_2", self._Toggle_State)
            self._Toggle_State = not self._Toggle_State
        else:
            outs += "1" if self.Off_LED else "0"
            self.set_dbus_value("relay_2", self.Off_LED)

        outs += "1" if self.On_LED else "0"
        outs += "1" if self.Charge_LED else "0"
        outs += "-"
        outs += "1" if self.BMS_Wake else "0"
        outs += "-"
        outs += "1" if self.DSE_Remote_Start else "0"
        outs += "1" if self.DSE_Mode_Request else "0"
        outs += "-"
        outs += "1" if self.RCD_Reset_Switch else "0"
        outs += "-"
        outs += "1" if self.Reverse_Power_Alarm else "0"

        self.set_dbus_value("relay_3", self.On_LED)
        self.set_dbus_value("relay_4", self.Charge_LED)
        self.set_dbus_value("relay_5", self.BMS_Wake)
        self.set_dbus_value("relay_6", self.DSE_Remote_Start)
        self.set_dbus_value("relay_7", self.DSE_Mode_Request)
        self.set_dbus_value("relay_8", self.RCD_Reset_Switch)
        self.set_dbus_value("relay_9", self.Reverse_Power_Alarm)
        self.outputs_str = outs

    def set_inverter_switch_mode(self):

        if self.Inverter_Switch_Mode_Target != self.Inverter_Switch_Mode:  # Only Update the switch mode when it changes.
            if (time() - self._inverter_switch_mode_update_time) > 5:
                self._inverter_switch_mode_update_time = time()
                self.set_dbus_value("inverter_switch_mode", self.Inverter_Switch_Mode_Target)
                print(f"Updating switch mode from {self.Inverter_Switch_Mode} to {self.Inverter_Switch_Mode_Target}.", flush=True)
            else:
                print("Not updating the switch mode until 5s have elapsed.", flush=True)

    def check_reverse_power(self):
        if self.Reverse_Power_Detected:
            self.Reverse_Power_Counter += 1
            max(REVERSE_POWER_COUNTER_THRESHOLD, self.Reverse_Power_Counter)
        else:
            self.Reverse_Power_Counter -= 1
            self.Reverse_Power_Counter = max(0, self.Reverse_Power_Counter)

        if (self.Reverse_Power_Counter >= REVERSE_POWER_COUNTER_THRESHOLD):
            self.Reverse_Power_Alarm = True
        elif (self.Mode == "Off") and (
                self.Reverse_Power_Counter == 0):  # Only reset if Off and has counted back down to 0
            self.Reverse_Power_Alarm = False

    def run(self):
        counter = 0
        while True:
            t0 = time()
            self.update_mode()
            self.update_battery_soc()
            self.update_ac_output_current()
            self.check_reverse_power()
            self.set_outputs()
            self.update_inverter_switch_mode()
            self.set_inverter_switch_mode()
            self.check_and_create_connections()
            if (self.Service_Restart_Requested):
                print("Service Restart Requested, Going Down in 5s!", flush=True)
                sleep(5)
                exit()
            # print(f"{datetime.isoformat(datetime.now())} : {self}", flush=True))
            print(self, flush=True)
            sleep(max(0, TIMESTEP - (time() - t0)))

    def __repr__(self):
        return ',\t'.join([
            f"Relays {self.outputs_str}",
            f"Mode {self.Mode}",
            f"SOC {self.Battery_SOC}%",
            f"AC Out {self.AC_Output_Current}A",
            f"Switch Mode {self.Inverter_Switch_Mode_Target}/{self.Inverter_Switch_Mode}",
            f"Rev Pwr {self.Reverse_Power_Detected} - {self.Reverse_Power_Counter * TIMESTEP}s",
            # f"Off LED {self.Off_LED}",
            # f"On LED {self.On_LED}",
            # f"Charge LED {self.Charge_LED}",
            # f"BMS Wake {self.BMS_Wake}",
            # f"DSE Start {self.DSE_Remote_Start}",
            # f"DSE Mode {self.DSE_Mode_Request}",
            f"Fault {self.Fault_Detected}",
        ]
        )

    def check_and_create_connections(self):
        for k, v in self.dbus_items_spec.items():
            if self.dbus_items.get(k) is None:
                try:
                    self.dbus_items[k] = VeDbusItemImport(self.dbusConn, v['service'], v['path'])
                except Exception as e:
                    self.dbus_items[k] = None
                    print(f"Could not find DBUS Item - {v['service']} : {v['path']}")
                    print(e, flush=True)


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
