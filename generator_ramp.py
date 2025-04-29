#!/usr/bin/env python3
import os
import sys
from os.path import join, dirname
from pprint import pformat
from time import time, sleep

import dbus
from dbus.mainloop.glib import DBusGMainLoop

# our own packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'velib_python'))

from vedbus import VeDbusItemImport
from ve_utils import unwrap_dbus_value

INV_SWITCH_OFF = 4
INV_SWITCH_ON = 3
INV_SWITCH_INVERT_ONLY = 2
INV_SWITCH_CHARGE_ONLY = 1


TIMESTEP = 0.25

EXCEPTION_THRESHOLD = 10


# TODO Update the ramp function to look for the AC input 1 voltage to stabilise before beginning the timer.
# Parameters for generator ramp function
GENSET_INITIAL_RAMP_TIME = 5
GENSET_WARMUP_TIME = 30
GENSET_FULLPOWER_RAMP_TIME = 10
GENSET_WARMUP_CURRENT_LIMIT = 20
GENSET_FULLPOWER_CURRENT_LIMIT = 35

PROFILE_MEMORY = True

if PROFILE_MEMORY:
    import tracemalloc


class GeneratorRampController:
    def __init__(self):
        DBusGMainLoop(set_as_default=True)
        self.dbusConn = dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()
        self.Battery_Charge_Limit = 0
        self.Battery_Discharge_Limit = 0
        self.AC_InputCurrentLimit = None
        self.Inverter_Switch_Mode = 0
        self.Inverter_Connected = False
        self.BMS_Connected = False
        self.inverter_delay = 0
        self.generator_ramp_timer = 0
        self.ac_input_current_limit_ramp_target = 0
        self.relay_states = {}

        self._last_log = {}
        self.duplicate_log_counter = {}

        self.logged_vars = {}

        self.outputs_str = ""

        if PROFILE_MEMORY:
            self._initial_snapshot = None
            self._current_snapshot = None

        self.dbus_items_spec = {
            "battery_charge_limit": {"service": "com.victronenergy.battery.socketcan_vecan0",
                                     "path": "/Info/MaxChargeCurrent"},
            "battery_discharge_limit": {"service": "com.victronenergy.battery.socketcan_vecan0",
                                        "path": "/Info/MaxDischargeCurrent"},
            "ac_input_current_limit":    {"service": "com.victronenergy.vebus.ttyS2",
                                          "path": "/Ac/In/1/CurrentLimit"},
            "inverter_switch_mode": {"service": "com.victronenergy.vebus.ttyS2", "path": "/Mode"},
            "relay_0": {"service": "com.victronenergy.system", "path": "/Relay/0/State"},
            "GenSS-Type": {"service": "com.victronenergy.generator.startstop0", "path": "Type"},
            "GenSS-Connected": {"service": "com.victronenergy.generator.startstop0", "path": "Connected"},
            "ac_input1_V": {"service": "com.victronenergy.vebus.ttyS2", "path": "Ac/ActiveIn/L1/V"},
            "ac_input1_f": {"service": "com.victronenergy.vebus.ttyS2", "path": "Ac/ActiveIn/L1/I"},
            "ac_input1_I": {"service": "com.victronenergy.vebus.ttyS2", "path": "Ac/ActiveIn/L1/F"},

                                       }

        self.dbus_items = {}

        self.check_and_create_connections()

    @property
    def Fault_Detected(self):
        if not self.BMS_Connected:
            print("BMS Fault", flush=True)
            return True
        if (self.Inverter_Switch_Mode != INV_SWITCH_OFF) and (not self.Inverter_Connected):
            print("Inverter Fault", flush=True)
            return True
        return False
    #
    # @property
    # def Service_Restart_Requested(self):
    #     # If all 3 buttons held down then trigger script reset
    #     return ((self.Off_Button_Pressed) and (self.On_Button_Pressed) and (self.Charge_Button_Pressed))

    @property
    def Generator_Start_Requested(self):
        return self.relay_states[0]

    @property
    def Battery_Contactors_Closed(self):
        val = (self.Battery_Charge_Limit) and (self.Battery_Discharge_Limit) # Non-zero current limits means that 48V system is online
        if val is False:
            self.inverter_delay = 0
        return val

    def get_dbus_value(self, dbus_item_name: str):
        if (dbus_item := self.dbus_items.get(dbus_item_name)) is not None:
            # print(f"Get DBus Value () : {dbus_item.serviceName} - {dbus_item.path}", flush=True)
            t0 = time()
            try:
                return unwrap_dbus_value(dbus_item._proxy.GetValue())
            except dbus.exceptions.DBusException as e:
                print(f"Could not get DBUS Item : {dbus_item.serviceName} - {dbus_item.path}", flush=True)
                print(e, flush=True)
                self.clear_dbus_item(dbus_item_name)
                duration = time() - t0
                timeout = 10
                if duration > timeout:
                    print(f"Call took more than {timeout}s, potentially unrecoverable situation! raising exception!")
                    raise

    def set_dbus_value(self, dbus_item_name: str, value):
        if (dbus_item := self.dbus_items.get(dbus_item_name)) is not None:
            # print(f"Set DBus Value () : {dbus_item.serviceName} - {dbus_item.path} : {Value}", flush=True))
            t0 = time()
            try:
                dbus_item.set_value(value)
                return True
            except dbus.exceptions.DBusException as e:
                print(f"Could not set DBUS Item : {dbus_item.serviceName} - {dbus_item.path} : {value}", flush=True)
                print(e, flush=True)
                self.clear_dbus_item(dbus_item_name)
                duration = time() - t0
                timeout = 10
                if duration > timeout:
                    print(f"Call took more than {timeout}s, potentially unrecoverable situation! raising exception!")
                    raise
                return False
        print(f"Dbus Item has been cleared so cannot be set until it is reconnected : {dbus_item_name} ")
        return False

    def clear_dbus_item(self, dbus_item_name):
        print(f"Removing dbus item : {dbus_item_name}", flush=True)
        try: # Try to remove the offending dbus item
            dbus_item = self.dbus_items.pop(dbus_item_name)
            del dbus_item
        except KeyError:
            print("Could not find dbus item to remove", flush=True)

    def update_battery_limits(self):
        charge_lim = self.get_dbus_value("battery_charge_limit")
        discharge_lim = self.get_dbus_value("battery_discharge_limit")
        if (charge_lim is not None) and (discharge_lim is not None):
            self.BMS_Connected = True
            self.Battery_Charge_Limit = round(charge_lim, 1)
            self.Battery_Discharge_Limit = round(discharge_lim, 1)
        else:
            self.BMS_Connected = False
            print("Did not receive data from battery about current limits", flush=True)

    def update_ac_input_current_limit(self):
        val = self.get_dbus_value("ac_input_current_limit")
        if val is not None:
            self.Inverter_Connected = True
            self.AC_InputCurrentLimit = round(val, 1)
        else:
            self.Inverter_Connected = False
            print("Did not receive data from inverter", flush=True)
            self.AC_InputCurrentLimit = None
            self.clear_dbus_item("ac_input_current_limit")

    def update_inverter_switch_mode(self):
        val = self.get_dbus_value("inverter_switch_mode")
        if val is not None:
            self.Inverter_Connected = True
            self.Inverter_Switch_Mode = val
        else:
            self.Inverter_Connected = False
            print("Did not receive switch mode from inverter", flush=True)
            self.Inverter_Switch_Mode = 0

    def update_relay_states(self):
        self.relay_states = {
            0: self.get_dbus_value(f"relay_0")
        }

    def update_logged_vars(self):
        for k, v in self.dbus_items_spec.items():
            self.logged_vars[k] = self.get_dbus_value(k)

    def update_generator_ramp_timer(self):
        if self.relay_states[0]: # relay[0] is the generator remote start signal
            self.generator_ramp_timer += 1
        else:
            self.generator_ramp_timer = 0

    def update_ac_input_current_limit_ramp_target(self):
        if self.generator_ramp_timer <= GENSET_INITIAL_RAMP_TIME:
            self.ac_input_current_limit_ramp_target = max(1.0, (self.generator_ramp_timer / GENSET_INITIAL_RAMP_TIME) * GENSET_WARMUP_CURRENT_LIMIT)
        elif self.generator_ramp_timer <= (GENSET_INITIAL_RAMP_TIME + GENSET_WARMUP_TIME):
            self.ac_input_current_limit_ramp_target = GENSET_WARMUP_CURRENT_LIMIT
        elif self.generator_ramp_timer <= (GENSET_INITIAL_RAMP_TIME + GENSET_WARMUP_TIME + GENSET_FULLPOWER_RAMP_TIME):
            self.ac_input_current_limit_ramp_target = ((self.generator_ramp_timer - (GENSET_INITIAL_RAMP_TIME + GENSET_WARMUP_TIME)) / GENSET_FULLPOWER_RAMP_TIME) * (GENSET_FULLPOWER_CURRENT_LIMIT - GENSET_WARMUP_CURRENT_LIMIT) + GENSET_WARMUP_CURRENT_LIMIT
        else:
            self.ac_input_current_limit_ramp_target = GENSET_FULLPOWER_CURRENT_LIMIT

    def set_ac_input_current_limit(self):
        if self.AC_InputCurrentLimit != self.ac_input_current_limit_ramp_target:  # Only Update the current limit when target changes.
            if (self.Battery_Contactors_Closed):  # Only attempt to contol the inverter if the 48V system has become live already
                if self.inverter_delay == 0:
                    self.set_dbus_value("ac_input_current_limit", self.ac_input_current_limit_ramp_target)
                    print(f"Updating AC Current Limit from {self.AC_InputCurrentLimit} to {self.ac_input_current_limit_ramp_target}.", flush=True)
                else:
                    print(f"Waiting {self.inverter_delay}s before updating ac input current limit")
                    # inverter_delay is decremented elsewhere.

    def run(self):
        self.snapshot_memory()

        counter = 0
        while True:
            t0 = time()
            self.check_and_create_connections()

            self.update_logged_vars()
            self.log_dbus_vals()
            # self.update_inverter_switch_mode()
            # if self.Inverter_Switch_Mode == INV_SWITCH_ON or self.Inverter_Switch_Mode == INV_SWITCH_CHARGE_ONLY:
            #     self.update_ac_input_current_limit()
            # self.update_relay_states()
            # self.update_generator_ramp_timer()
            # self.update_ac_input_current_limit_ramp_target()
            # self.set_ac_input_current_limit()

            # if (self.Service_Restart_Requested):
            #     print("Service Restart Requested, Going Down in 5s!", flush=True)
            #     self.store_state()
            #     sleep(5)
            #     exit()
            # print(f"{datetime.isoformat(datetime.now())} : {self}", flush=True))
            # self.log_state()

            counter += 1
            if counter % 60 == 0:
                self.snapshot_memory()
            # if counter % 30 == 0:
            #     self.store_state()

            sleep(max(0.0, TIMESTEP - (time() - t0)))

    def log_dbus_vals(self):
        print(f"DBUS: {pformat(self.logged_vars, width=200)}")
        sys.stdout.flush()

    def log_state(self):
        log = {"Relays": self.relay_states, "State": str(self)}
        for log_type in log.keys():
            if log[log_type] == self._last_log.get(log_type):
                self.duplicate_log_counter[log_type] = self.duplicate_log_counter.get(log_type, 0) + 1
            else:
                self.duplicate_log_counter[log_type] = 0
            if (log[log_type] != self._last_log.get(log_type)) or ((self.duplicate_log_counter[log_type] % 10) == 0):
                if isinstance(log[log_type], dict):
                    print(f"{log_type}: {pformat(log[log_type], width=200)}")
                else:
                    print(f"{log_type}: {log[log_type]}".expandtabs(4))
            else:
                # print(f"{log_type}: No Change")
                pass
            self._last_log[log_type] = log[log_type]
        sys.stdout.flush()

    def snapshot_memory(self):
        if PROFILE_MEMORY:
            self._current_snapshot = tracemalloc.take_snapshot()
            if self._initial_snapshot is None:
                self._initial_snapshot = self._current_snapshot
            top_stats = self._current_snapshot.compare_to(self._initial_snapshot, 'lineno')

            print("\n*************** Memory Snapshot Top 20 ***************\n")
            count = 0
            for stat in top_stats:
                if "tracemalloc.py" not in str(stat.traceback[0]):
                    count += 1
                    print(stat)
                if count >= 20:
                    break

            print("\n******************************************************\n")

    def __repr__(self):
        return ',\t'.join([
            f"Inv Mode {self.Inverter_Switch_Mode}",
            f"Lims {self.Battery_Charge_Limit}A/{self.Battery_Discharge_Limit}A",
            f"AC In Curr Lim {self.AC_InputCurrentLimit}A",
            f"Target {self.ac_input_current_limit_ramp_target}A",
            f"Gen Ramp {self.generator_ramp_timer}s",
            f"Inv Del {self.inverter_delay}s",
            f"Fault {self.Fault_Detected}",
        ]
        )

    def check_and_create_connections(self):
        for k, v in self.dbus_items_spec.items():
            if self.dbus_items.get(k) is None:
                try:
                    print(f"Creating DBUS Item - {v['service']} : {v['path']}")
                    self.dbus_items[k] = VeDbusItemImport(self.dbusConn, v['service'], v['path'])
                except Exception as e:
                    self.dbus_items[k] = None
                    print(f"Could not find DBUS Item - {v['service']} : {v['path']}")
                    print(e, flush=True)

    # def system_uptime(self):
    #     with open("/proc/uptime") as f:
    #         return float(f.read().split()[0])

    # def store_state(self):
    #     state = {"Mode": self.Mode, "Time": time()}
    #     print("Storing State now ", flush=True)
    #     print(state)
    #     with open("state_dump.json", 'w') as f:
    #         json.dump(state, f)
    # def check_stored_state(self):
    #     print("Checking stored state")
    #     if exists("state_dump.json"):
    #         with open("state_dump.json") as f:
    #             state = json.load(f)
    #             print("Stored state : ", flush=True)
    #             pprint(state)
    #
    #         age = (time() - state.get("Time", 0))
    #         if age < 120:
    #             print(f"Found a stored state dump which is less than 60s old ({age}s)", flush=True)
    #             if self.system_uptime() > state.get("Time", 0):
    #                 print("System reboot detected more recently than stored state, ignoring stored state.",flush=True)
    #             else:
    #                 mode = state.get("Mode", "Err")
    #                 if mode not in ["Off", "On", "ChargeOnly"]:
    #                     print(f"Unknown Mode detected : {mode}", flush=True)
    #                 else:
    #                     print(f"Restoring Mode : {mode}", flush=True)
    #                     self.Mode = mode
    #     else:
    #         print("No state_dump.json file detected", flush=True)


if __name__ == "__main__":
    if PROFILE_MEMORY:
        tracemalloc.start()
    try:
        with open(join(dirname(__file__), "version")) as f_version:
            version = f_version.readline()
        print("\n\n****************************************\n")
        print(f"Running generator_ramp.py \t{version}", flush=True)
        print("\n****************************************\n\n")
        print("Waiting 5s for system to startup", flush=True)
        sleep(5)
        print("Running now!", flush=True)
        g = GeneratorRampController()
        print(g)
        g.run()  # global dbusObjects  #  # print(__file__ + " starting up")
    except Exception as ex:
        print("Exception Raised", flush=True)
        print(ex, flush=True)
        print("Restart Required, Going Down in 5s!", flush=True)
        sleep(5)
        raise
# # Have a mainloop, so we can send/receive asynchronous calls to and from dbus  # DBusGMainLoop(set_as_default=True)
