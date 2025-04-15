# EVP 15KVA Generator Control
A victron Ekrano package for controlling the 15Kva aquafuel generator.

## Overview

The package installs a service which runs the python script generator_control.py on the target victron device (Only tested against Ekrano)

The service will monitor the input buttons on the front of the generator and control the state of the generator by activating relays via a DTWonder 8-relay module.

### Usage

Press the Off button to turn off the system.
Press the On button to turn on the system.
Press the Charge Only button to turn on the system in such a way that only charging of the battery is allowed.

## Installation

### Prerequisites

Install 
- SetupHelper 
- GUIMods
- RemoteGPIO

copy `venus-data.tar.gz` (from https://github.com/kwindrem/SetupHelper) onto a usb stick or sd card and insert into the Ekrano then reboot.
The package manager should appear at the bottom of the settings menu.
In package manager select Inactive Packages and choose GuiMods and RemoteGPIO, press Add to add them to the active packages.

Go back to package manager and select Active Packages and install GuiMods then Remote GPIO (follow the instructions onscreen)

### Setup RemoteGPIO
Remote GPIO needs to be configured to work with the DTWonder relay module.
#### Ethernet
Set the IP Address for the Victron to match the IP/subnet of the DTWonder.

###
Add EVP15KVAGeneratorControl Package Via Github
In package manager, select Inactive Packages. The choose new and fill in the details.
EVP15KVAGeneratorControl
EVParts
release


# TODO 
Need to get second Ekrano working and then complete the documentation.
