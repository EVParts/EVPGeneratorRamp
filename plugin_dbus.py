#!/usr/bin/env python3
"""Shared D-Bus helpers for Venus OS plugins."""

import os
import sys
import threading

import dbus

# Locate Victron's velib_python library.
_VELIB_PATHS = (
    "/opt/victronenergy/velib_python",
    "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "velib_python"),
)
for _path in _VELIB_PATHS:
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)

from vedbus import VeDbusService


# Plugin metadata. The version file is stored alongside the calling plugin.
_PLUGIN_FILE = os.path.abspath(sys.argv[0])
_PLUGIN_NAME = os.path.splitext(os.path.basename(_PLUGIN_FILE))[0]
_VERSION_FILE = os.path.join(os.path.dirname(_PLUGIN_FILE), "version")

with open(_VERSION_FILE, "r", encoding="utf-8") as _version_file:
    VERSION = _version_file.read().strip()

PRODUCT_NAME = _PLUGIN_NAME
DEVICE_INSTANCE = 0
PRODUCT_ID = 0xFFFF
FIRMWARE_VERSION = VERSION
HARDWARE_VERSION = VERSION


_main_loop = None
_main_loop_thread = None
_main_loop_lock = threading.Lock()


def start_dbus():
    """Start a GLib main loop in a background thread, once per process.

    Call this from plugins that do not already run a GLib main loop.
    Plugins with their own GLib loop should not call this function.
    Call it before creating D-Bus services or connections.
    """
    global _main_loop, _main_loop_thread

    with _main_loop_lock:
        if _main_loop_thread is not None and _main_loop_thread.is_alive():
            return

        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib

        DBusGMainLoop(set_as_default=True)
        _main_loop = GLib.MainLoop()
        _main_loop_thread = threading.Thread(
            target=_main_loop.run,
            name="D-Bus GLib main loop",
            daemon=True,
        )
        _main_loop_thread.start()

def _get_device_instance():
    """Reserve and retrieve a unique switch DeviceInstance."""

    bus = dbus.SystemBus()

    # Reserve an instance through Local Settings.
    settings_object = bus.get_object(
        "com.victronenergy.settings",
        "/Settings/Devices",
    )
    settings = dbus.Interface(
        settings_object,
        "com.victronenergy.Settings",
    )

    settings.AddSetting(
        _PLUGIN_NAME,
        "ClassAndVrmInstance",
        "switch:0",
        "s",
        "",
        "",
    )

    # Read back the allocated instance.
    setting_path = (
        "/Settings/Devices/{}/ClassAndVrmInstance".format(_PLUGIN_NAME)
    )

    setting_object = bus.get_object(
        "com.victronenergy.settings",
        setting_path,
    )
    setting = dbus.Interface(
        setting_object,
        "com.victronenergy.BusItem",
    )

    class_and_instance = str(setting.GetValue())
    device_class, instance = class_and_instance.split(":", 1)

    if device_class != "switch":
        raise ValueError(
            "Unexpected device class: {}".format(device_class)
        )

    return int(instance)

def create_service():
    """Create the plugin service and add its mandatory D-Bus paths."""
    device_instance = _get_device_instance()

    service_name = "com.victronenergy.switch.{}".format(_PLUGIN_NAME)
    service = VeDbusService(service_name, register=False)

    service.add_mandatory_paths(
        _PLUGIN_FILE,
        VERSION,
        "Python",
        device_instance,
        PRODUCT_ID,
        PRODUCT_NAME,
        FIRMWARE_VERSION,
        HARDWARE_VERSION,
        1,
    )

    return service


class Input:
    def __init__(self, service, path):
        self.service = service
        self.path = path

    @property
    def value(self):
        return self.service[self.path + "/Value"]

    def update(self, value):
        update_input(self, value)


def update_input(input_handle, value):
    """Update a GenericInput value."""
    input_handle.service[input_handle.path + "/Value"] = int(value)


def create_input(service, index, name, value=0, labels=None, group=None, show_ui=6):
    """Add a GenericInput and return a handle with .value and .update()."""
    path = "/GenericInput/{}".format(index)

    service.add_path(path + "/Name", str(name))
    service.add_path(path + "/Value", int(value))
    service.add_path(path + "/Status", 0)
    service.add_path(path + "/Settings/CustomName", str(name))
    service.add_path(path + "/Settings/Type", 0)
    service.add_path(path + "/Settings/ValidTypes", 1)
    service.add_path(path + "/Settings/ShowUIControl", int(show_ui))

    if labels is not None:
        service.add_path(
            path + "/Settings/Labels",
            dbus.Array([str(label) for label in labels], signature="s", variant_level=1),
        )

    if group is not None:
        service.add_path(path + "/Settings/Group", str(group))

    return Input(service, path)


class Relay:
    def __init__(self, service, path, disable_control=False):
        self.service = service
        self.path = path
        self.disable_control = bool(disable_control)

    @property
    def state(self):
        return self.service[self.path + "/State"]

    @property
    def status(self):
        return self.service[self.path + "/Status"]

    def update_state(self, value):
        update_relay(self, state=value)

    def update_status(self, value):
        update_relay(self, status=value)


def update_relay(relay_handle, state=None, status=None):
    """Update a relay's requested State and/or hardware Status.

    State is the writable control value. Status represents the reported
    hardware state (0x09 when on, 0x00 when off), with the disable-control
    bit preserved when applicable.
    """
    if state is not None:
        relay_handle.service[relay_handle.path + "/State"] = int(bool(state))

    if status is not None:
        value = 0x09 if bool(status) else 0x00
        if relay_handle.disable_control:
            value |= 0x20
        relay_handle.service[relay_handle.path + "/Status"] = value


def create_relay(
    service,
    index,
    name,
    value=0,
    group=None,
    show_ui=6,
    disable_control=False,
    on_change=None,
):
    """Add a SwitchableOutput and return a handle for its state and status."""
    path = "/SwitchableOutput/{}".format(index)
    initial_state = int(bool(value))

    status = 0x09 if initial_state else 0x00
    if disable_control:
        status |= 0x20

    service.add_path(path + "/Name", str(name))
    service.add_path(
        path + "/State",
        initial_state,
        writeable=on_change is not None,
        onchangecallback=on_change,
    )
    service.add_path(path + "/Status", status)
    service.add_path(path + "/Settings/CustomName", str(name))
    service.add_path(path + "/Settings/Type", 1)
    service.add_path(path + "/Settings/ValidTypes", 2)
    service.add_path(path + "/Settings/ShowUIControl", int(show_ui))

    if group is not None:
        service.add_path(path + "/Settings/Group", str(group))

    return Relay(service, path, disable_control)
