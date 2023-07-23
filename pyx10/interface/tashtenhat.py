"""Functions and classes for working with TashTenHat (I2C interface to TW523 and similar devices)."""


from collections import deque
from fcntl import ioctl
import logging
import os
from queue import Empty
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
  
  def __init__(self, expected_bit_str, passthrough_feed_bit):
    self._expected_bits = deque()
    zeroes = 0
    for bit in expected_bit_str:
      if bit == '1':
        self._expected_bits.append(1)
        zeroes = 0
      elif zeroes < INTERFRAME_ZEROES:
        self._expected_bits.append(0)
        zeroes += 1
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
        if bit:
          self._held_bits.append(1)
          self._zeroes = 0
        elif self._zeroes < INTERFRAME_ZEROES:
          self._held_bits.append(0)
          self._zeroes += 1
        while len(self._held_bits) > len(self._expected_bits): self._passthrough_feed_bit(self._held_bits.popleft())
        if len(self._held_bits) == len(self._expected_bits):
          if self._held_bits == self._expected_bits: self._event.set()
  
  def wait(self, timeout):
    """Wait for a match.  Return True if there was a match within the timeout, else False and pass the held bits through."""
    
    result = self._event.wait(timeout)
    if result: return True
    with self._lock:
      while self._held_bits: self._passthrough_feed_bit(self._held_bits.popleft())
      self._event.set()
    return False


class I2cAdapter(Thread):
  """A device to repeatedly poll an I2C device and feed bytes read from it to a function."""
  
  def __init__(self, i2c_device, feed_byte_func):
    super().__init__()
    self._i2c_handle = os.open(i2c_device, os.O_RDWR)
    ioctl(self._i2c_handle, IOCTL_I2C_TARGET, I2C_BASE_ADDR)  # TODO target address should be configurable?
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
  
  def __init__(self):
    super().__init__()
    self._i2c = None
    self._shutdown = False
    self._stopped_event = Event()
    self._bep = None  # subclass must provide this if it uses _handle_event_batch_out_with_echo
    self._bsm = None  # subclass must provide this if it uses _handle_event_batch_out_with_echo
  
  def _handle_event_batch_out(self, event_batch):
    raise NotImplementedError('subclass must override _handle_event_batch_out method')
  
  def _handle_event_batch_out_with_echo(self, event_batch, echo_bit_str_and_qty_fn_name):
    """Send a batch of outbound events to the TashTenHat, expecting them to be echoed back in some form."""
    
    for event in event_batch:
      if not hasattr(event, 'as_output_bit_str'):
        raise ValueError('%s is not an event type that can be serialized for the TashTenHat' % type(event).__name__)
    output_bit_str = (INTERFRAME_ZEROES * '0').join(event.as_output_bit_str() for event in event_batch)
    echo_bit_str = (INTERFRAME_ZEROES * '0').join(
      event_bit_str
      for event_bit_str, event_bit_qty in (getattr(event, echo_bit_str_and_qty_fn_name)() for event in event_batch)
      for i in range(event_bit_qty)
    )
    for attempt in range(MAX_FAILURES):
      self._bsm = BitStringMatcher(echo_bit_str, self._bep.feed_bit)
      self._i2c.write(bit_str_to_bytes(output_bit_str) + b'\x00')
      if self._bsm.wait(ECHO_TIMEOUT):
        # TODO This echoing does not take into account retries that were partially successful, is this good enough?
        for event in event_batch: self._events_in.put(event)
        break
      if attempt + 1 < MAX_FAILURES:
        logging.warning('failed to send batch, retrying: %s', ', '.join(str(event) for event in event_batch))
      else:
        logging.warning('failed to send batch, giving up: %s', ', '.join(str(event) for event in event_batch))
    else:
      logging.error('failed to send batch after %d attempts: %s', MAX_FAILURES, ', '.join(str(event) for event in event_batch))
  
  def run(self):
    """Main thread.  Handle outbound events for the TashTenHat."""
    
    while not self._shutdown:
      try:
        event_batch = self._event_batches_out.get(timeout=QUEUE_TIMEOUT)
      except Empty:
        continue
      self._handle_event_batch_out(event_batch)
      self._event_batches_out.task_done()
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
    super().__init__()
    self._i2c = I2cAdapter(i2c_device, lambda byte: None)
  
  def _handle_event_batch_out(self, event_batch):
    """Send a batch of outbound events to the PL513 through the TashTenHat."""
    
    for event in event_batch:
      if not hasattr(event, 'as_output_bit_str'):
        raise ValueError('%s is not an event type that can be serialized for the TashTenHat' % type(event).__name__)
    output_bit_str = ('0' * INTERFRAME_ZEROES).join(event.as_output_bit_str() for event in event_batch)
    self._i2c.write(bit_str_to_bytes(output_bit_str) + b'\x00')
    for event in event_batch: self._events_in.put(event)  # Local echo is the only source of inbound events from the PL513


@register_interface('tashtenhat_tw523', ('*i2c_device',))
class TashTenHatWithTw523(TashTenHat):
  """Represents the TashTenHat, connected to a TW523, accessed over i2c-dev."""
  
  def __init__(self, i2c_device):
    super().__init__()
    self._bep = tw523.BitEventProcessor(
      event_func=self._events_in.put,
      dim_func=lambda dim_quantity: dim_quantity * 3 - 1,
      return_all_bits=False,
    )
    self._bsm = BitStringMatcher('0', self._bep.feed_bit)
    self._i2c = I2cAdapter(i2c_device, lambda byte: self._bsm.feed_byte(byte))
  
  def _handle_event_batch_out(self, event_batch):
    """Send a batch of outbound events to the TashTenHat."""
    
    # Expect different echoed bits because TW523 and PSC05 mangle/truncate certain events
    self._handle_event_batch_out_with_echo(event_batch, 'as_tw523_echo_bit_str_and_qty')


@register_interface('tashtenhat_xtb523', ('*i2c_device',))
class TashTenHatWithXtb523(TashTenHat):
  """Represents the TashTenHat, connected to an XTB-523 in normal mode, accessed over i2c-dev."""
  
  def __init__(self, i2c_device):
    super().__init__()
    self._bep = tw523.BitEventProcessor(
      event_func=self._events_in.put,
      dim_func=lambda dim_quantity: dim_quantity * 2,
      return_all_bits=False,
    )
    self._bsm = BitStringMatcher('0', self._bep.feed_bit)
    self._i2c = I2cAdapter(i2c_device, lambda byte: self._bsm.feed_byte(byte))
  
  def _handle_event_batch_out(self, event_batch):
    """Send a batch of outbound events to the TashTenHat."""
    
    # Expect different echoed bits because XTB-523 in normal mode treats all events as doublets and returns only one half
    self._handle_event_batch_out_with_echo(event_batch, 'as_xtb523_echo_bit_str_and_qty')


@register_interface('tashtenhat_xtb523allbits', ('*i2c_device',))
class TashTenHatWithXtb523AllBits(TashTenHat):
  """Represents the TashTenHat, connected to an XTB-523 in Return All Bits mode, accessed over i2c-dev."""
  
  def __init__(self, i2c_device):
    super().__init__()
    self._bep = tw523.BitEventProcessor(event_func=self._events_in.put, dim_func=None, return_all_bits=True)
    self._bsm = BitStringMatcher('0', self._bep.feed_bit)
    self._i2c = I2cAdapter(i2c_device, lambda byte: self._bsm.feed_byte(byte))
  
  def _handle_event_batch_out(self, event_batch):
    """Send a batch of outbound events to the TashTenHat."""
    
    # Match almost the exact bit string we send because XTB-523 in Return All Bits mode does just that... almost
    self._handle_event_batch_out_with_echo(event_batch, 'as_xtb523allbits_echo_bit_str_and_qty')
