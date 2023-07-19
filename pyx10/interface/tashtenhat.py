"""Functions and classes for working with TashTenHat (I2C interface to TW523 and similar devices)."""


from collections import deque
from enum import Enum
from fcntl import ioctl
import logging
import os
from queue import Queue, Empty
from threading import Thread, Event, Lock

from . import tw523
from ..common import X10Interface
from .registry import register_interface


MAX_FAILURES = 5  # Maximum number of times a transmission can fail to be echoed properly
ECHO_TIMEOUT = 5  # Maximum length of time we should wait for a transmission to be echoed
QUEUE_TIMEOUT = 0.25  # Interval at which main thread should check if it's stopped
INTERFRAME_ZEROES = 6  # Nominal number of zeroes that separate frames

I2C_BASE_ADDR = 0x58  # 'X' in ASCII

IOCTL_I2C_TARGET = 0x0703  # From linux/i2c-dev.h


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


class BitStringMatcher:
  """A device to attempt to match a stream of incoming bits from TashTenHat with an expected stream."""
  
  def __init__(self, expected_bit_str, expected_qty, passthrough_feed_bit):
    self._expected_bits = deque((1 if i == '1' else 0)
                                for i in (INTERFRAME_ZEROES * '0').join(expected_bit_str.strip('0') for j in range(expected_qty)))
    self._matching_bits = deque()
    self._held_bits = deque()
    self._zeroes = 0
    self._passthrough_feed_bit = passthrough_feed_bit
    self._event = Event()
    self._lock = Lock()
  
  def feed_byte(self, byte):
    """Feed a byte into the matcher."""
    self.feed_bit(1 if byte & 0x80 else 0)
    self.feed_bit(1 if byte & 0x40 else 0)
    self.feed_bit(1 if byte & 0x20 else 0)
    self.feed_bit(1 if byte & 0x10 else 0)
    self.feed_bit(1 if byte & 0x08 else 0)
    self.feed_bit(1 if byte & 0x04 else 0)
    self.feed_bit(1 if byte & 0x02 else 0)
    self.feed_bit(1 if byte & 0x01 else 0)
  
  def feed_bit(self, bit):
    """Feed a bit into the matcher."""
    with self._lock:
      if self._event.is_set():
        self._passthrough_feed_bit(bit)
      else:
        self._held_bits.append(bit)
        if bit:
          self._zeroes = 0
        else:
          self._zeroes += 1
          if self._zeroes > INTERFRAME_ZEROES: return
        self._matching_bits.append(bit)
        if len(self._matching_bits) == len(self._expected_bits):
          if self._matching_bits == self._expected_bits: self._event.set()
  
  def wait(self, timeout):
    """Wait for a match.  Return True if there was a match within the timeout, else False and pass the held bits through."""
    
    result = self._event.wait(timeout)
    if result: return True
    with self._lock:
      while self._held_bits: self._passthrough_feed_bit(self._held_bits.popleft())
      self._event.set()
    return False


class I2cAdapter(Thread):
  
  def __init__(self, i2c_device, feed_byte_func):
    super().__init__()
    self._i2c_handle = os.open(i2c_device, os.O_RDWR)
    ioctl(self._i2c_handle, IOCTL_I2C_TARGET, I2C_BASE_ADDR)
    self._feed_byte_func = feed_byte_func
    self._zero_flag = False
    self._shutdown = False
    self._stopped_event = Event()
  
  def write(self, data):
    """Write X10 data to the device."""
    
    os.write(self._i2c_handle, data)
  
  def run(self):
    """Thread.  Polls I2C device and feeds X10 bytes read from it to the given function."""
    
    while not self._shutdown:
      data = os.read(self._i2c_handle, 1)  # TODO what happens when this errors?
      data = data[0]
      if data == 0:
        if not self._zero_flag: self._feed_byte_func(data)
        self._zero_flag = True
      else:
        self._feed_byte_func(data)
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


class TashTenHat(X10Interface):
  """Represents the TashTenHat accessed over i2c-dev.  Linux-specific."""
  
  def __init__(self, i2c_device):
    super().__init__()
    self._i2c = None
    self._shutdown = False
    self._stopped_event = Event()
  
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


@register_interface('tashtenhat_pl513', ('*i2c_device',))
class TashTenHatWithPl513(TashTenHat):
  """Represents the TashTenHat, connected to a PL513, accessed over i2c-dev."""
  
  def __init__(self, i2c_device):
    super().__init__(i2c_device)
    self._i2c = I2cAdapter(i2c_device, lambda byte: None)
  
  def _handle_event_out(self, event):
    """Send an outbound event to the PL513 through the TashTenHat."""
    
    try:
      bit_str = event.as_bit_str()
    except AttributeError:
      raise ValueError('%s is not an event type that can be serialized for the TashTenHat' % type(event).__name__)
    self._i2c.write(bit_str_to_bytes(bit_str) + b'\x00')
    self._events_in.put(event)  # Local echo is the only source of inbound events from the PL513


@register_interface('tashtenhat_tw523', ('*i2c_device',))
class TashTenHatWithTw523(TashTenHat):
  """Represents the TashTenHat, connected to a TW523, accessed over i2c-dev."""
  
  def __init__(self, i2c_device):
    super().__init__(i2c_device)
    self._bep = tw523.BitEventProcessor(
      event_func=self._events_in.put,
      dim_func=lambda dim_quantity: dim_quantity * 3 - 1,
      return_all_bits=False,
    )
    self._bsm = BitStringMatcher('0', 1, self._bep.feed_bit)
    self._i2c = I2cAdapter(i2c_device, lambda byte: self._bsm.feed_byte(byte))
  
  def _handle_event_out(self, event):
    """Send an outbound event to the TashTenHat."""
    
    for _ in range(MAX_FAILURES):
      try:
        bit_str = event.as_bit_str()
      except AttributeError:
        raise ValueError('%s is not an event type that can be serialized for the TashTenHat' % type(event).__name__)
      # Expect different echoed bits because TW523 and PSC05 mangle/truncate certain events
      echo_bit_str, echo_qty = event.as_tw523_echo_bit_str_and_qty()
      self._bsm = BitStringMatcher(echo_bit_str, echo_qty, self._bep.feed_bit)
      self._i2c.write(bit_str_to_bytes(bit_str) + b'\x00')
      if self._bsm.wait(ECHO_TIMEOUT):
        self._events_in.put(event)
        break
    else:
      logging.error('failed to send %s after %d attempts', event, MAX_FAILURES)


@register_interface('tashtenhat_xtb523', ('*i2c_device',))
class TashTenHatWithXtb523(TashTenHat):
  """Represents the TashTenHat, connected to an XTB-523 in normal mode, accessed over i2c-dev."""
  
  def __init__(self, i2c_device):
    super().__init__(i2c_device)
    self._bep = tw523.BitEventProcessor(
      event_func=self._events_in.put,
      dim_func=lambda dim_quantity: dim_quantity * 2,
      return_all_bits=False,
    )
    self._bsm = BitStringMatcher('0', 1, self._bep.feed_bit)
    self._i2c = I2cAdapter(i2c_device, lambda byte: self._bsm.feed_byte(byte))
  
  def _handle_event_out(self, event):
    """Send an outbound event to the TashTenHat."""
    
    for _ in range(MAX_FAILURES):
      try:
        bit_str = event.as_bit_str()
      except AttributeError:
        raise ValueError('%s is not an event type that can be serialized for the TashTenHat' % type(event).__name__)
      # Expect different echoed bits because XTB-523 in normal mode treats all events as doublets and returns only one half
      echo_bit_str, echo_qty = event.as_xtb523_echo_bit_str_and_qty()
      self._bsm = BitStringMatcher(echo_bit_str, echo_qty, self._bep.feed_bit)
      self._i2c.write(bit_str_to_bytes(bit_str) + b'\x00')
      if self._bsm.wait(ECHO_TIMEOUT):
        self._events_in.put(event)
        break
    else:
      logging.error('failed to send %s after %d attempts', event, MAX_FAILURES)


@register_interface('tashtenhat_xtb523allbits', ('*i2c_device',))
class TashTenHatWithXtb523AllBits(TashTenHat):
  """Represents the TashTenHat, connected to an XTB-523 in Return All Bits mode, accessed over i2c-dev."""
  
  def __init__(self, i2c_device):
    super().__init__(i2c_device)
    self._bep = tw523.BitEventProcessor(event_func=self._events_in.put, dim_func=None, return_all_bits=True)
    self._bsm = BitStringMatcher('0', 1, self._bep.feed_bit)
    self._i2c = I2cAdapter(i2c_device, lambda byte: self._bsm.feed_byte(byte))
  
  def _handle_event_out(self, event):
    """Send an outbound event to the TashTenHat."""
    
    for _ in range(MAX_FAILURES):
      try:
        bit_str = event.as_bit_str()
      except AttributeError:
        raise ValueError('%s is not an event type that can be serialized for the TashTenHat' % type(event).__name__)
      # Match almost the exact bit string we send because XTB-523 in Return All Bits mode does just that... almost
      echo_bit_str, echo_qty = event.as_xtb523allbits_echo_bit_str_and_qty()
      self._bsm = BitStringMatcher(echo_bit_str, echo_qty, self._bep.feed_bit)
      self._i2c.write(bit_str_to_bytes(bit_str) + b'\x00')
      if self._bsm.wait(ECHO_TIMEOUT):
        self._events_in.put(event)
        break
    else:
      logging.error('failed to send %s after %d attempts', event, MAX_FAILURES)
