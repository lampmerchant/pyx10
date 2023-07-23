"""Functions to run an event-based X10 app."""


from collections import deque
from configparser import ConfigParser
import inspect
import logging
import os
import re
from queue import Empty
from threading import Thread, Event
import time

from ..common import X10_FN_ALL_OFF, X10_FN_ALL_LIGHTS_ON, X10_FN_ALL_LIGHTS_OFF, X10_FN_HAIL_REQ
from ..common import X10_FN_ON, X10_FN_OFF, X10_FN_STATUS_REQ, X10_HOUSE_CODES_REV, X10_UNIT_CODES_REV
from ..common import X10_FN_DIM, X10_FN_BRIGHT, X10_HOUSE_CODES, X10_UNIT_CODES
from ..common import X10AddressEvent, X10FunctionEvent, X10RelativeDimEvent, X10AbsoluteDimEvent, X10ExtendedCodeEvent
from ..interface import get_interface


QUEUE_TIMEOUT = 0.25  # Interval at which app thread should check if it's stopped
POSSIBLE_CONFIG_LOCATIONS = ('~/.pyx10.ini', '~/pyx10.ini', '/etc/pyx10.ini')


# Monkey patches for X10 event classes


def X10AddressEvent_do_app_event(self, module, _):
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
  REO_EXT_CODE = re.compile(r'^EXT[_-]?CODE\((0*1[0-6]|0*[1-9]),(?:0X)?([0-9A-F]{1,2})H?(?:,(?:0[Xx])?([0-9A-F]{1,2})H?)\)$')
  
  def __init__(self, intf):
    self._intf = intf
    self._house_code = X10_HOUSE_CODES['A']
  
  def process_command(self, cmd_str):
    """Parse a command string."""
    
    cmds = deque(cmd.strip().upper() for cmd in cmd_str.strip().split())
    logging.debug('parsing command string: %s', ' '.join(cmds))
    batch = deque()
    for cmd in cmds:
      if not cmd: continue
      if m := self.REO_TARGET.match(cmd):
        house, unit = m.groups()
        if house: self._house_code = X10_HOUSE_CODES[house]
        if unit: batch.append(X10AddressEvent(house_code=self._house_code, unit_code=X10_UNIT_CODES[int(unit)]))
      elif (s := cmd.replace('-', '').replace('_', '')) in self.SIMPLE_COMMANDS:
        batch.append(X10FunctionEvent(house_code=self._house_code, function=self.SIMPLE_COMMANDS[s]))
      elif m := self.REO_REL_DIM.match(cmd):
        batch.append(X10RelativeDimEvent(house_code=self._house_code, dim=int(m.group(1)) / 100))
      elif m := self.REO_ABS_DIM.match(cmd):
        batch.append(X10AbsoluteDimEvent(dim=int(m.group(1)) / 100))
      elif m := self.REO_EXT_CODE.match(cmd):
        unit_num, first, second = m.groups()
        first = int(first, 16)
        second = int(second, 16) if second is not None else None
        batch.append(X10ExtendedCodeEvent(
          house_code=self._house_code,
          unit_code=X10_UNIT_CODES[int(unit_num)],
          data_byte=0 if second is None else first,
          cmd_byte=first if second is None else second,
        ))
      else:
        logging.warning('invalid command, not executing batch: %s', cmd)
        break
    else:
      logging.debug('sending batch from command string: %s', ', '.join(str(event) for event in batch))
      self._intf.put_batch(batch)


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


def run(module=None):
  """Run an event-based X10 app out of the given module."""
  
  # This is a bit unholy, but it allows us to get a reference to the calling module
  if module is None: module = inspect.getmodule(inspect.currentframe().f_back)
  
  # Find and read config file
  source_file_location = inspect.getsourcefile(module)
  if source_file_location is not None: source_file_location = os.path.dirname(source_file_location)
  possible_config_locations = []
  for possible_config_location in POSSIBLE_CONFIG_LOCATIONS:
    if possible_config_location.startswith('~/'):
      if source_file_location is not None:
        possible_config_locations.append(os.path.join(source_file_location, possible_config_location[2:]))
      possible_config_locations.append(os.path.expanduser(possible_config_location))
    else:
      possible_config_locations.append(possible_config_location)
  config = ConfigParser()
  for config_location in possible_config_locations:
    if os.path.exists(config_location):
      config.read(config_location)
      break
  else:
    raise FileNotFoundError('config file not found at any expected location: %s' % ', '.join(possible_config_locations))
  
  # interface section
  if 'interface' not in config: raise ValueError('config file at %s is missing "interface" section' % config_location)
  intf = get_interface(config['interface'])
  
  # log section
  log_level_mapping = logging.getLevelNamesMapping()
  log_level = logging.WARNING
  if 'log' in config:
    if 'level' in config['log']:
      if config['log']['level'] not in log_level_mapping:
        raise ValueError('log level "%s" is not one of: %s' % (config['log']['level'], ', '.join(log_level_mapping)))
      log_level = log_level_mapping[config['log']['level']]
  logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=log_level)
  
  # fifo_server section
  fifo_path = None
  if 'fifo_server' in config:
    if 'path' in config['fifo_server']:
      fifo_path = config['fifo_server']['path']
      if not os.path.exists(fifo_path): raise FileNotFoundError('FIFO at "%s" does not exist' % fifo_path)
  
  # Run the app
  dispatcher = EventDispatcher(module, intf)
  intf.start()
  dispatcher.start()
  command_processor = CommandProcessor(intf)
  try:
    while True:
      if fifo_path:
        with open(fifo_path, 'r') as fifo:
          while line := fifo.readline():
            line = line.strip()
            logging.info('inbound line from FIFO: %s', line)
            command_processor.process_command(line)
      else:
        time.sleep(1)
  except KeyboardInterrupt:
    pass
  dispatcher.stop()
  intf.stop()
