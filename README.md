# pyx10

Python framework for development of event-driven X10 apps.

## Apps

### Example

```python
import pyx10

def x10_d4_on(intf):
  """When D4 is turned on, turn on D2."""
  intf.get_controller('D').on(2).send()

def x10_d4_off(intf):
  """When D4 is turned off, turn off D2."""
  intf.get_controller('D').off(2).send()

def at_0700_mtwrf(intf):
  """At 7:00a, Monday through Friday, turn on D9."""
  intf.get_controller('D').on(9).send()

def at_sunrise(intf):
  """At sunrise every day, turn E1 off."""
  intf.get_controller('E').off(1).send()

def at_sunset(intf):
  """At sunset every day, turn E1 on."""
  intf.get_controller('E').on(1).send()

pyx10.run()
```

### Event Handler Names

* Unit Functions
   * Format: `x10_<unit>_<function>`
   * `<unit>` may be any house code (A-P) and unit number (1-16) combination
   * `<function>` may be one of the following: `on`, `off`, `status`
   * Function parameters:
      * `intf` - reference to X10 interface
* House Functions
   * Format: `x10_<house>_<function>`
   * `<house>` may be any house code (A-P)
   * `<function>` may be one of the following: `all_off`, `all_lights_on`, `all_lights_off`, `hail`
   * Function parameters:
      * `intf` - reference to X10 interface
* Unit Relative Dim
   * Format: `x10_<unit>_rel_dim`
   * `<unit>` may be any house code (A-P) and unit number (1-16) combination
   * Function parameters:
      * `intf` - reference to X10 interface
      * `dim` - relative dim level, -1.0 to 1.0, inclusive
* Unit Absolute (Preset) Dim
   * Format: `x10_<unit>_abs_dim`
   * `<unit>` may be any house code (A-P) and unit number (1-16) combination
   * Function parameters:
      * `intf` - reference to X10 interface
      * `dim` - absolute dim level, 0.0 to 1.0, inclusive
* Extended Code
   * Format: `x10_<unit>_ext_code`
   * `<unit>` may be any house code (A-P) and unit number (1-16) combination
   * Function parameters:
      * `intf` - reference to X10 interface
      * `data_byte` - data byte (0-0xFF)
      * `cmd_byte` - command byte (0-0xFF)
* Timed Event (Every Day)
   * Format: `at_<time>`
   * `<time>` may be a 24-hour clock time (0000 to 2359) or, if [astral](https://pypi.org/project/astral/) is installed and
     location is configured, `sunrise`, `sunset`, `dawn`, `dusk`, `noon` (solar noon)
   * Function parameters:
      * `intf` - reference to X10 interface
* Timed Event (Days of Week)
   * Format: `at_<time>_<days of week>`
   * `<time>` may be a 24-hour clock time (0000 to 2359) or, if [astral](https://pypi.org/project/astral/) is installed and
     location is configured, `sunrise`, `sunset`, `dawn`, `dusk`, `noon` (solar noon)
   * `<days of week>` may be a string of characters corresponding to the days of the week when the function is triggered
      * Example: `mwf` for Monday, Wednesday, and Friday
      * Days of week letters are as follows: **M**onday, **T**uesday, **W**ednesday, Thu**r**sday, **F**riday, **S**aturday,
        S**u**nday
   * Function parameters:
      * `intf` - reference to X10 interface

## Configuration

pyx10 will use the first configuration file it finds in one of the following locations, searched in order:

* (script directory)/.pyx10.ini
* (user's home directory)/.pyx10.ini
* (script directory)/pyx10.ini
* (user's home directory)/pyx10.ini
* /etc/pyx10.ini

### Example

```
[interface]
interface=cm11a
serial_port=/dev/ttyS0

[fifo_server]
path=/root/x10

[location]
city=Denver

[scheduler]
clock_stability_delay=60

# Logging Configuration (see https://docs.python.org/3/library/logging.config.html#logging-config-fileformat for details)

[loggers]
keys=root

[handlers]
keys=hand01

[formatters]
keys=form01

[logger_root]
level=NOTSET
handlers=hand01

[handler_hand01]
class=StreamHandler
level=NOTSET
formatter=form01
args=(sys.stdout,)

[formatter_form01]
format=%(asctime)s %(levelname)s -- %(message)s
```

### `interface` Section

This section is required.  The `interface` parameter may be any interface known to pyx10:

* `cm11a` - CM11A RS-232 interface
   * Parameter `serial_port` must be supplied to name the serial port where the CM11A is connected
* `tashtenhat_pl513` - [TashTenHat](https://github.com/lampmerchant/tashtenhat) connected to PL513 powerline interface
   * Parameter `i2c_device` must be supplied to name the i2c-dev device where the TashTenHat is connected
* `tashtenhat_tw523` - [TashTenHat](https://github.com/lampmerchant/tashtenhat) connected to TW523 powerline interface
   * Parameter `i2c_device` must be supplied to name the i2c-dev device where the TashTenHat is connected
* `tashtenhat_xtb523` - [TashTenHat](https://github.com/lampmerchant/tashtenhat) connected to [XTB-523](https://jvde.us/xtb-523/)
  powerline interface in normal mode
   * Parameter `i2c_device` must be supplied to name the i2c-dev device where the TashTenHat is connected
* `tashtenhat_xtb523allbits` - [TashTenHat](https://github.com/lampmerchant/tashtenhat) connected to
  [XTB-523](https://jvde.us/xtb-523/) powerline interface in "return all bits" mode
   * Parameter `i2c_device` must be supplied to name the i2c-dev device where the TashTenHat is connected

### `fifo_server` Section

This section is optional.  If it is present and the `path` parameter points to a FIFO (created using `mkfifo`) in the filesystem,
pyx10 will listen for X10 commands written to the FIFO and execute them.  See below for more information on the command protocol.

### `location` Section

This section is optional.  If it is present (and the Python [astral](https://pypi.org/project/astral/) package is installed), it
will be possible to schedule timed events at dawn, dusk, sunrise, sunset, and solar noon.  Either the parameter `city` must be
supplied, containing the name of a major city (all world and US state capitals are recognized), or `latitude` and `longitude`
must be supplied.

### `scheduler` Section

This section is optional.  If the `clock_stability_delay` parameter is present, before starting the event scheduler, pyx10 will
sleep for the given number of seconds and check that the same number of seconds elapsed according to the system clock.  If not, the
check is retried until it passes.  This allows pyx10 apps to be used as services on Raspberry Pi systems and others without
real-time clocks where the system clock may be vastly wrong on startup before it is corrected by an NTP service.

### Logging Sections

See [Python's documentation](https://docs.python.org/3/library/logging.config.html#logging-config-fileformat) for more information.

## FIFO Server

If the optional FIFO server is started, a simple command protocol can be used to send X10 commands through the FIFO.  Commands are
case-insensitive and delimited by spaces.  Newlines delimit "batches" of commands (if any command in the batch fails to send, the
entire batch will be retried).  Recognized commands are as follows:

### Addressing a Unit

Format: the house letter (A-P) followed by the unit (1-16).

Example: `echo "D8" > /root/x10`

### Addressing a House

Format: the house letter (A-P).

Example: `echo "D" > /root/x10`

### Simple Command

Format: one of the following: `on`, `off`, `all-off`, `all-units-off`, `all-lights-on`, `all-lights-off`, `dim`, `bright`, `hail`,
`status`.

Example: `echo "D8 on" > /root/x10`

### Relative Dim

Format: `dim(<+/-><0-100>)`

Example: `echo "D8 dim(-50)" > /root/x10`

### Absolute (Preset) Dim

Format: `dim(<0-100>)`

Example: `echo "D8 dim(50)" > /root/x10`

### Extended Code

Format: `ext_code(<unit number>,<data byte in hex>,<command byte in hex>)`

Example: `echo "D ext_code(12,BE,EF)" > /root/x10`
