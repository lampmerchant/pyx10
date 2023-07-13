"""Functions and classes for working with TashTenHat (I2C interface to TW523 and similar devices)."""


from collections import deque
from enum import Enum
from fcntl import ioctl
import logging
import os
from queue import Queue, Empty
from threading import Thread, Event

import pyx10
import tw523


MAX_FAILURES = 5  # Maximum number of times a transmission can fail to be echoed properly
EVENT_TIMEOUT = 5  # Maximum length of time we should wait for a transmission to be echoed
QUEUE_TIMEOUT = 0.25  # Interval at which main thread should check if it's stopped

I2C_BASE_ADDR = 0x58  # 'X' in ASCII

IOCTL_I2C_TARGET = 0x0703  # From linux/i2c-dev.h


InterfaceType = Enum('InterfaceType', ('PL513', 'TW523', 'XTB523', 'XTB523_ALLBITS'))


# Functions


def bit_str_to_bytes(s):
  """Convert a string consisting of '1's and '0's into packed bytes, left-justified."""
  
  b = bytearray((len(s) + 7) // 8)
  bit_val = 128
  byte_pos = 0
  for bit in s:
    if bit == '1':
      b[byte_pos] |= bit_val
    elif bit == '0':
      pass
    else:
      raise ValueError("invalid character '%s' in binary string" % bit)
    bit_val >>= 1
    if bit_val == 0:
      bit_val = 128
      byte_pos += 1
  return bytes(b)


# Classes


class I2cAdapter(Thread):
  
  def __init__(self, i2c_device, bit_event_processor):
    super().__init__()
    self._i2c_handle = os.open(i2c_device, os.O_RDWR)
    ioctl(self._i2c_handle, IOCTL_I2C_TARGET, I2C_BASE_ADDR)
    self._bit_event_processor = bit_event_processor
    self._zero_flag = False
    self._shutdown = False
    self._stopped_event = Event()
  
  def write(self, data):
    """Write X10 data to the device."""
    
    os.write(self._i2c_handle, data)
  
  def run(self):
    """Thread.  Polls I2C device and feeds X10 bytes read from it into the BitEventProcessor."""
    
    while not self._shutdown:
      data = os.read(self._i2c_handle, 1)  # TODO what happens when this errors?
      data = data[0]
      if data == 0:
        if not self._zero_flag: self._bit_event_processor.feed_byte(data)
        self._zero_flag = True
      else:
        self._bit_event_processor.feed_byte(data)
        self._zero_flag = False
    self._stopped_event.set()
  
  def start(self):
    """Start thread."""
    
    self._shutdown = False
    self._stopped_event.clear()
    super().start()
  
  def stop(self):
    """Stop thread.  Blocks until thread is stopped."""
    
    self._shutdown = True
    self._stopped_event.wait()
    os.close(self._i2c_handle)


class TashTenHat(pyx10.X10Interface):
  """Represents the TashTenHat accessed over i2c-dev.  Linux-specific."""
  
  def __init__(self, i2c_device, interface_type):
    super().__init__()
    self._interface_type = interface_type
    self._bep = tw523.BitEventProcessor(
      event_func=self._handle_event_in,
      return_all_bits=True if self._interface_type == InterfaceType.XTB523_ALLBITS else False,
    )
    self._i2c = I2cAdapter(i2c_device, self._bep)
    self._events_echo = None
    self._shutdown = False
    self._stopped_event = Event()
  
  # Event Handling
  
  def _handle_event_out(self, event):
    """Send an outbound event to the TashTenHat."""
    
    for _ in range(MAX_FAILURES):
      try:
        bit_str = event.as_bit_str()
      except AttributeError:
        raise ValueError('%s is not an event type that can be serialized for the TashTenHat' % type(event).__name__)
      self._events_echo = Queue()
      self._i2c.write(bit_str_to_bytes(bit_str) + b'\x00')
      if self._interface_type == InterfaceType.PL513:
        # PL513 is transmit-only, make no attempt to verify that events are echoed
        expected_events = deque()
      elif self._interface_type == InterfaceType.TW523:
        # TW523 and PSC05 mangle/truncate certain events so we may have to expect different ones than we transmit to be echoed
        expected_events = deque(event.tw523ify())
      elif self._interface_type == InterfaceType.XTB523:
        # XTB-523 and XTB-IIR in normal mode treat relative dim events as doublets so we may have to expect different ones than we
        # transmit to be echoed
        expected_events = deque(event.xtb523ify())
      elif self._interface_type == InterfaceType.XTB523_ALLBITS:
        # XTB-523 and XTB-IIR in "return all bits" mode faithfully echo all bits as transmitted
        expected_events = deque((event,))
      else:
        raise ValueError('unrecognized interface type %s' % self._interface_type)
      while expected_events:
        try:
          next_event = self._events_echo.get(timeout=EVENT_TIMEOUT)
        except Empty:
          break
        if next_event.as_bit_str() == expected_events[0].as_bit_str(): expected_events.popleft()
      if not expected_events: break
    else:
      logging.error('failed to send %s after %d attempts', event, MAX_FAILURES)
    self._events_echo = None
  
  def _handle_event_in(self, event):
    """Receive an inbound event from the TashTenHat."""
    
    self._events_in.put(event)
    try:
      self._events_echo.put(event)
    except AttributeError:  # if self._events_echo is None
      pass
  
  # Threading
  
  def run(self):
    """Main thread.  Handle outbound events for the TashTenHat."""
    
    while not self._shutdown:
      try:
        event = self._events_out.get(timeout=QUEUE_TIMEOUT)
      except Empty:
        continue
      self._handle_event_out(event)
      self._events_out.task_done()
    self._stopped_event.set()
  
  def start(self):
    """Start the main thread."""
    
    self._shutdown = False
    self._stopped_event.clear()
    self._i2c.start()
    super().start()
  
  def stop(self):
    """Stop the main thread.  Blocks until the thread has been stopped."""
    
    self._shutdown = True
    self._i2c.stop()
    self._stopped_event.wait()
