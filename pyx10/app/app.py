"""Functions to run an event-based X10 app."""


from argparse import ArgumentParser
import logging
import re
import sys
from queue import Empty
from threading import Thread, Event
import time

from ..common import X10_FN_ALL_OFF, X10_FN_ALL_LIGHTS_ON, X10_FN_ALL_LIGHTS_OFF, X10_FN_HAIL_REQ
from ..common import X10_FN_ON, X10_FN_OFF, X10_FN_STATUS_REQ, X10_HOUSE_CODES_REV, X10_UNIT_CODES_REV
from ..common import X10_FN_DIM, X10_FN_BRIGHT, X10_HOUSE_CODES, X10_UNIT_CODES
from ..common import X10AddressEvent, X10FunctionEvent, X10RelativeDimEvent, X10AbsoluteDimEvent, X10ExtendedCodeEvent
from ..interface import cm11a, tashtenhat


QUEUE_TIMEOUT = 0.25  # Interval at which app thread should check if it's stopped


# Monkey patches for X10 event classes


def X10AddressEvent_do_app_event(self, module, intf):
  """Invoke an app event function for this event, if one exists."""
  
  module._x10_last_house_letter = X10_HOUSE_CODES_REV[self.house_code]
  if not hasattr(module, '_x10_last_unit_number'): module._x10_last_unit_number = {}
  module._x10_last_unit_number[module._x10_last_house_letter] = X10_UNIT_CODES_REV[self.unit_code]

X10AddressEvent.do_app_event = X10AddressEvent_do_app_event


def X10FunctionEvent_do_app_event(self, module, intf):
  """Invoke an app event function for this event, if one exists."""
  
  house_letter = X10_HOUSE_CODES_REV[self.house_code]
  unit_number = getattr(module, '_x10_last_unit_number', {}).get(house_letter, None)
  if self.function in (X10_FN_ALL_OFF, X10_FN_ALL_LIGHTS_ON, X10_FN_ALL_LIGHTS_OFF, X10_FN_HAIL_REQ):
    func_name = ('x10_%s_%s' % (house_letter, {
      X10_FN_ALL_OFF: 'all_off',
      X10_FN_ALL_LIGHTS_ON: 'all_lights_on',
      X10_FN_ALL_LIGHTS_OFF: 'all_lights_off',
      X10_FN_HAIL_REQ: 'hail_req',
    }[self.function])).lower()
    func = getattr(module, func_name, None)
    if func is None:
      logging.debug('no function %s exists in module', func_name)
    else:
      logging.debug('invoking function %s in module', func_name)
      func(intf)
  elif self.function in (X10_FN_ON, X10_FN_OFF, X10_FN_STATUS_REQ) and unit_number is not None:
    func_name = ('x10_%s%d_%s' % (house_letter, unit_number, {
      X10_FN_ON: 'on',
      X10_FN_OFF: 'off',
      X10_FN_STATUS_REQ: 'status_req',
    }[self.function])).lower()
    func = getattr(module, func_name, None)
    if func is None:
      logging.debug('no function %s exists in module', func_name)
    else:
      logging.debug('invoking function %s in module', func_name)
      func(intf)

X10FunctionEvent.do_app_event = X10FunctionEvent_do_app_event


def X10RelativeDimEvent_do_app_event(self, module, intf):
  """Invoke an app event function for this event, if one exists."""
  
  house_letter = X10_HOUSE_CODES_REV[self.house_code]
  unit_number = getattr(module, '_x10_last_unit_number', {}).get(house_letter, None)
  if unit_number is None: return
  func_name = ('x10_%s%d_rel_dim' % (house_letter, unit_number)).lower()
  func = getattr(module, func_name, None)
  if func is None:
    logging.debug('no function %s exists in module', func_name)
  else:
    logging.debug('invoking function %s in module', func_name)
    func(intf, self.dim)

X10RelativeDimEvent.do_app_event = X10RelativeDimEvent_do_app_event


def X10AbsoluteDimEvent_do_app_event(self, module, intf):
  """Invoke an app event function for this event, if one exists."""
  
  house_letter = getattr(module, '_x10_last_house_letter')
  if house_letter is None: return
  unit_number = getattr(module, '_x10_last_unit_number', {}).get(house_letter, None)
  if unit_number is None: return
  func_name = ('x10_%s%d_abs_dim' % (house_letter, unit_number)).lower()
  func = getattr(module, func_name, None)
  if func is None:
    logging.debug('no function %s exists in module', func_name)
  else:
    logging.debug('invoking function %s in module', func_name)
    func(intf, self.dim)

X10AbsoluteDimEvent.do_app_event = X10AbsoluteDimEvent_do_app_event


def X10ExtendedCodeEvent_do_app_event(self, module, intf):
  """Invoke an app event function for this event, if one exists."""
  
  house_letter = X10_HOUSE_CODES_REV[self.house_code]
  unit_number = X10_UNIT_CODES_REV[self.unit_code]
  func_name = ('x10_%s%d_ext_code' % (house_letter, unit_number)).lower()
  func = getattr(module, func_name, None)
  if func is None:
    logging.debug('no function %s exists in module', func_name)
  else:
    logging.debug('invoking function %s in module', func_name)
    func(intf, self.data_byte, self.command_byte)

X10ExtendedCodeEvent.do_app_event = X10ExtendedCodeEvent_do_app_event


# Classes


class CommandProcessor:
  """Process X10 command strings in a simple language."""
  
  REO_TARGET = re.compile(r'^(?P<house>[A-P])?(?P<unit>0*1[0-6]|0*[1-9])?$')
  SIMPLE_COMMANDS = {
    'ON': X10_FN_ON,
    'OFF': X10_FN_OFF,
    'ALLOFF': X10_FN_ALL_OFF,
    'ALLUNITSOFF': X10_FN_ALL_OFF,
    'ALLLIGHTSON': X10_FN_ALL_LIGHTS_ON,
    'ALLLIGHTSOFF': X10_FN_ALL_LIGHTS_OFF,
    'DIM': X10_FN_DIM,
    'BRIGHT': X10_FN_BRIGHT,
    'HAIL': X10_FN_HAIL_REQ,
    'STATUS': X10_FN_STATUS_REQ,
  }
  REO_REL_DIM = re.compile(r'^DIM\(([+-](?:0*100|0*\d{1,2}))%?\)$')
  REO_ABS_DIM = re.compile(r'^DIM\((0*100|0*\d{1,2})%?\)$')
  
  def __init__(self, intf):
    self._intf = intf
    self._house_code = X10_HOUSE_CODES['A']
  
  def command(self, cmd_str):
    """Parse a command string."""
    
    for cmd in cmd_str.strip().split():
      cmd = cmd.strip().upper()
      if not cmd: continue
      logging.info('inbound FIFO command: %s', cmd)
      if m := self.REO_TARGET.match(cmd):
        house, unit = m.groups()
        if house: self._house_code = X10_HOUSE_CODES[house]
        if unit: self._intf.put(
          X10AddressEvent(house_code=self._house_code, unit_code=X10_UNIT_CODES[int(unit)])
        )
      elif (s := cmd.replace('-', '').replace('_', '')) in self.SIMPLE_COMMANDS:
        self._intf.put(X10FunctionEvent(house_code=self._house_code, function=self.SIMPLE_COMMANDS[s]))
      elif m := self.REO_REL_DIM.match(cmd):
        self._intf.put(X10RelativeDimEvent(house_code=self._house_code, dim=int(m.group(1)) / 100))
      elif m := self.REO_ABS_DIM.match(cmd):
        self._intf.put(X10AbsoluteDimEvent(dim=int(m.group(1)) / 100))
      else:
        logging.warning('invalid command: %s', cmd)


class EventDispatcher(Thread):
  """Thread to pull events off the queue and fire off handlers in the app."""
  
  def __init__(self, module, intf):
    super().__init__()
    self._module = module
    self._intf = intf
    self._shutdown = False
    self._stopped_event = Event()
  
  def run(self):
    while not self._shutdown:
      try:
        event = self._intf.get(timeout=QUEUE_TIMEOUT)
      except Empty:
        continue
      logging.info('inbound event: %s', event)
      event.do_app_event(self._module, self._intf)
    self._stopped_event.set()
  
  def stop(self):
    self._shutdown = True
    self._stopped_event.wait()


# Functions


def run(module, description):
  """Run an event-based X10 app out of the given module."""
  
  parser = ArgumentParser(description=description)
  intf_group = parser.add_mutually_exclusive_group(required=True)
  intf_group.add_argument('--cm11a', metavar='SERIALDEV', help='serial device where CM11A is connected')
  intf_group.add_argument('--pl513', metavar='I2CDEV', help='I2C device where TashTenHat is connected to PL513')
  intf_group.add_argument('--tw523', metavar='I2CDEV', help='I2C device where TashTenHat is connected to TW523/PSC05')
  intf_group.add_argument('--xtb523', metavar='I2CDEV', help='I2C device where TashTenHat is connected to XTB-523/XTB-IIR')
  intf_group.add_argument('--xtb523ab', metavar='I2CDEV', help='I2C device where TashTenHat is connected to XTB-523/XTB-IIR in'
                                                               ' "return all bits" mode')
  parser.add_argument('-v', '--verbose', action='count', default=0, help='verbosity of output, can be given multiple times')
  parser.add_argument('-f', '--fifo_path', help='filesystem path to a named FIFO that should accept commands (must already exist)')
  args = parser.parse_args(sys.argv[1:])
  
  logging.basicConfig(
    format='%(asctime)s %(levelname)s %(message)s',
    level=logging.DEBUG if args.verbose >= 2 else logging.INFO if args.verbose >= 1 else logging.WARNING,
  )
  
  if args.cm11a:
    intf = cm11a.CM11A(args.cm11a)
  elif args.pl513:
    intf = tashtenhat.TashTenHatWithPl513(args.pl513)
  elif args.tw523:
    intf = tashtenhat.TashTenHatWithTw523(args.tw523)
  elif args.xtb523:
    intf = tashtenhat.TashTenHatWithXtb523(args.xtb523)
  elif args.xtb523ab:
    intf = tashtenhat.TashTenHatWithXtb523AllBits(args.xtb523ab)
  else:
    raise ValueError('no valid interface specified')
  
  dispatcher = EventDispatcher(module, intf)
  intf.start()
  dispatcher.start()
  command_processor = CommandProcessor(intf)
  try:
    while True:
      if args.fifo_path:
        with open(args.fifo_path, 'r') as fifo:
          while line := fifo.readline():
            command_processor.command(line.strip())
      else:
        time.sleep(1)
  except KeyboardInterrupt:
    pass
  dispatcher.stop()
  intf.stop()
