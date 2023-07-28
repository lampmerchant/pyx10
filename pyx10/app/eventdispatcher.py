"""Dispatcher for X10 event-based event handlers."""


import logging
from queue import Empty
from threading import Thread, Event

from ..common import X10_FN_ALL_OFF, X10_FN_ALL_LIGHTS_ON, X10_FN_ALL_LIGHTS_OFF, X10_FN_HAIL_REQ
from ..common import X10_FN_ON, X10_FN_OFF, X10_FN_STATUS_REQ, X10_HOUSE_CODES_REV, X10_UNIT_CODES_REV
from ..common import X10AddressEvent, X10FunctionEvent, X10RelativeDimEvent, X10AbsoluteDimEvent, X10ExtendedCodeEvent


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


class EventDispatcher(Thread):
  """Thread to pull events off the queue and fire off handlers in the app."""
  
  def __init__(self, module, intf):
    super().__init__()
    self._module = module
    self._intf = intf
    self._shutdown = False
    self._stopped_event = Event()
  
  def run(self):
    logging.info('starting event dispatcher, module: %s', str(self._module))
    while not self._shutdown:
      try:
        event = self._intf.get(timeout=QUEUE_TIMEOUT)
      except Empty:
        continue
      logging.info('inbound event: %s', event)
      event.do_app_event(self._module, self._intf)
    self._stopped_event.set()
  
  def stop(self):
    logging.info('stopping event dispatcher')
    self._shutdown = True
    self._stopped_event.wait()
    logging.info('event dispatcher stopped')
