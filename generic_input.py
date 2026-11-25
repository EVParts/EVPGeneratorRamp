
#!/usr/bin/env python3

import os
import sys
import threading

import dbus

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

# Locate Victron's velib_python library
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "velib_python")
)

from vedbus import VeDbusService


# One service can contain multiple Generic Inputs.
_services = {}

_dbus_lock = threading.RLock()
_dbus_started = False
_mainloop = None


class GenericInput:
    """Handle for one published Generic Input."""

    def __init__(self, service, path):
        self._service = service
        self._path = path

    @property
    def value(self):
        return self._service[f"{self._path}/Value"]

    def update(self, value):
        self._service[f"{self._path}/Value"] = int(value)


def _start_dbus():
    global _dbus_started, _mainloop

    if _dbus_started:
        return

    DBusGMainLoop(set_as_default=True)

    _mainloop = GLib.MainLoop()

    thread = threading.Thread(
        target=_mainloop.run,
        daemon=True
    )
    thread.start()

    _dbus_started = True


def _get_service(service_name):
    """Return an existing service or create a new one."""

    if service_name in _services:
        return _services[service_name]

    service = VeDbusService(service_name, register=False)

    service.add_mandatory_paths(
        processname=__file__,
        processversion="1.0",
        connection="Python",
        deviceinstance=0,
        productid=0xFFFF,
        productname="Generator Ramp Plugin",
        firmwareversion=0,
        hardwareversion=0,
        connected=1
    )

    service.register()

    _services[service_name] = service

    return service


def create_generic_input(
    service_name,
    index,
    name,
    value=0,
    labels=None,
    group=None,
    show_ui=6
):
    """
    Create and publish a Generic Input.

    service_name: D-Bus service name
    index:        Unique identifier within that service
    name:         Input name
    value:        Initial integer value
    labels:       Optional list of labels for discrete values
    group:        Optional UI group
    show_ui:      UI visibility setting (default 6)
    """

    if "/" in str(index):
        raise ValueError("Input index cannot contain '/'")

    if not isinstance(name, str) or not name.strip():
        raise ValueError("Input name must be a non-empty string")

    with _dbus_lock:
        _start_dbus()

        service = _get_service(service_name)

        path = f"/GenericInput/{index}"

        if f"{path}/Value" in service:
            raise ValueError(
                f"Generic Input '{index}' already exists "
                f"in service '{service_name}'"
            )

        service.add_path(f"{path}/Value", value=int(value))
        service.add_path(f"{path}/Name", value=name)
        service.add_path(f"{path}/Status", value=0)

        service.add_path(
            f"{path}/Settings/CustomName",
            value=name
        )
        service.add_path(
            f"{path}/Settings/Type",
            value=0
        )
        service.add_path(
            f"{path}/Settings/ValidTypes",
            value=1
        )
        service.add_path(
            f"{path}/Settings/ShowUIControl",
            value=int(show_ui)
        )

        if labels is not None:
            service.add_path(
                f"{path}/Settings/Labels",
                value=dbus.Array(
                    [dbus.String(label) for label in labels],
                    signature="s",
                    variant_level=1
                )
            )

        if group is not None:
            service.add_path(
                f"{path}/Settings/Group",
                value=group
            )

        return GenericInput(service, path)


def update_generic_input(generic_input, value):
    """Update the value of a previously created Generic Input."""

    if not isinstance(generic_input, GenericInput):
        raise TypeError("Expected a GenericInput handle")

    generic_input.update(value)
