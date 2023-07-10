"""Functions and classes for working with TW523 powerline interface and similar devices.

TW523 and PSC05 (which is the "pro" version of TW523 and functionally identical) are limited in their receiving capabilities; they
receive only the second transmission in a doublet, truncate it to 11 bits (22 half-cycles), and expect 6 half-cycles to pass before
receiving another start sequence.  For this reason, they cannot receive extended code transmissions in their entirety (they drop
the unit code, data byte, and command byte) and cannot receive sequences of dim/bright transmissions (they receive only one
transmission out of every three).  The 'tw523ify' functions are provided to convert X10 event objects into the events that TW523
and PSC05 will echo back so that transmissions using these transceivers can be verified.  The XTB-523 and XTB-IIR from JV Digital
Engineering have no such limitations.

PL513 is TW523/PSC05 without any receive functionality.
"""


from collections import deque

import pyx10


# Functions


def x10_bit_str(n):
  """Convert an X10 code into its line bit representation."""
  
  return ''.join((
    '10' if n & 8 else '01',
    '10' if n & 4 else '01',
    '10' if n & 2 else '01',
    '10' if n & 1 else '01',
  ))


# Monkey patches for X10 event classes


def X10AddressEvent_as_bit_str(self):
  """Convert this event into its line bit representation."""
  return ''.join(('1110', x10_bit_str(self.house_code), x10_bit_str(self.unit_code), '01')) * 2

pyx10.X10AddressEvent.as_bit_str = X10AddressEvent_as_bit_str


def X10FunctionEvent_as_bit_str(self):
  """Convert this event into its line bit representation."""
  return ''.join(('1110', x10_bit_str(self.house_code), x10_bit_str(self.function), '10')) * 2

pyx10.X10FunctionEvent.as_bit_str = X10FunctionEvent_as_bit_str


def X10RelativeDimEvent_as_bit_str(self):
  """Convert this event into its line bit representation."""
  return ''.join((
    '1110', x10_bit_str(self.house_code), x10_bit_str(pyx10.X10_FN_DIM if self.dim < 0 else pyx10.X10_FN_BRIGHT), '10'
  )) * int(pyx10.RELATIVE_DIM_STEPS * abs(self.dim))

pyx10.X10RelativeDimEvent.as_bit_str = X10RelativeDimEvent_as_bit_str


def X10AbsoluteDimEvent_as_bit_str(self):
  """Convert this event into its line bit representation."""
  dim = int(self.dim * 31)
  return ''.join((
    '1110', x10_bit_str(dim & 0xF), x10_bit_str(pyx10.X10_FN_PRESET_DIM_1 if dim & 0x10 else pyx10.X10_FN_PRESET_DIM_0), '10'
  )) * 2

pyx10.X10AbsoluteDimEvent.as_bit_str = X10AbsoluteDimEvent_as_bit_str


def X10ExtendedCodeEvent_as_bit_str(self):
  """Convert this event into its line bit representation."""
  return ''.join((
    '1110', x10_bit_str(self.house_code), x10_bit_str(pyx10.X10_FN_EXT_CODE), '10',
    x10_bit_str(self.unit_code),
    x10_bit_str(self.data_byte >> 4), x10_bit_str(self.data_byte),
    x10_bit_str(self.cmd_byte >> 4), x10_bit_str(self.cmd_byte),
  )) * 2

pyx10.X10ExtendedCodeEvent.as_bit_str = X10ExtendedCodeEvent_as_bit_str


def self_tw523ify(self):
  """Yield the events that will be echoed by a real TW523/PSC05 (not an XTB-523 or XTB-IIR) when sending this event."""
  yield self

pyx10.X10AddressEvent.tw523ify = self_tw523ify
pyx10.X10FunctionEvent.tw523ify = self_tw523ify
pyx10.X10AbsoluteDimEvent.tw523ify = self_tw523ify


def X10RelativeDimEvent_tw523ify(self):
  """Yield the events that will be echoed by a real TW523/PSC05 (not an XTB-523 or XTB-IIR) when sending this event."""
  for _ in range((2 + int(pyx10.RELATIVE_DIM_STEPS * abs(self.dim))) // 3):
    yield type(self)(house_code=self.house_code, dim=1/pyx10.RELATIVE_DIM_STEPS)

pyx10.X10RelativeDimEvent.tw523ify = X10RelativeDimEvent_tw523ify


def X10ExtendedCodeEvent_tw523ify(self):
  """Yield the events that will be echoed by a real TW523/PSC05 (not an XTB-523 or XTB-IIR) when sending this event."""
  for _ in range(2):
    # TW523 will return this twice because we send it twice and it doesn't know about the extra bits
    yield pyx10.X10FunctionEvent(house_code=self.house_code, function=pyx10.X10_FN_EXT_CODE)

pyx10.X10ExtendedCodeEvent.tw523ify = X10ExtendedCodeEvent_tw523ify


# Classes


class BitEventProcessor:
  """A device to process incoming data from the line and output events."""
  
  def __init__(self, event_func):
    self._event_func = event_func
    self._bits = deque()
    self._zeroes = 0
  
  def _get_bit(self):
    bit_true = self._bits.popleft()
    bit_com = self._bits.popleft()
    bits = ''.join((bit_true, bit_com))
    if bits == '10':
      return 1
    elif bits == '01':
      return 0
    else:
      return None
  
  def _get_nibble(self):
    return sum((
      self._get_bit() and 8 or 0,
      self._get_bit() and 4 or 0,
      self._get_bit() and 2 or 0,
      self._get_bit() and 1 or 0
    ))
  
  def _process_frame(self):
    try:
      if len(self._bits) % 2: self._bits.append('0')
      if len(self._bits) < 22:
        # TODO emit a warning of some kind
        return
      if ''.join(self._bits.popleft() for i in range(4)) != '1110': return
      house_code = self._get_nibble()
      key_code = self._get_nibble()
      d16 = self._get_bit()
      if not d16 and not self._bits:  # unit address
        self._event_func(pyx10.X10AddressEvent(house_code=house_code, unit_code=key_code))
      elif not d16:  # unit address with extra bits that we don't understand and will ignore
        # TODO emit a warning of some kind
        self._event_func(pyx10.X10AddressEvent(house_code=house_code, unit_code=key_code))
      elif d16 and key_code in (pyx10.X10_FN_DIM, pyx10.X10_FN_BRIGHT) and len(self._bits) % 22 == 0:  # dim X10 function
        dim_quantity = 1
        for i in range(len(self._bits) // 22):
          if ''.join(self._bits.popleft() for i in range(4)) != '1110': break
          if self._get_nibble() != house_code: break
          if self._get_nibble() != key_code: break
          if self._get_bit() != d16: break
          dim_quantity += 1
        if key_code == pyx10.X10_FN_DIM: dim_quantity = -dim_quantity
        self._event_func(pyx10.X10RelativeDimEvent(house_code=house_code, dim=min(dim_quantity, 22) / 22))
      elif d16 and key_code == pyx10.X10_FN_EXT_CODE and len(self._bits) == 40:  # extended code
        unit_code = self._get_nibble()
        data_byte = self._get_nibble() << 4
        data_byte |= self._get_nibble()
        cmd_byte = self._get_nibble() << 4
        cmd_byte |= self._get_nibble()
        self._event_func(pyx10.X10ExtendedCodeEvent(
          house_code=house_code, unit_code=unit_code, data_byte=data_byte, cmd_byte=cmd_byte
        ))
      elif d16 and key_code in (pyx10.X10_FN_PRESET_DIM_0, pyx10.X10_FN_PRESET_DIM_1) and not self._bits:  # absolute dim
        dim = ((16 if key_code == pyx10.X10_FN_PRESET_DIM_1 else 0) + house_code) / 31
        self._event_func(pyx10.X10AbsoluteDimEvent(dim=dim))
      elif d16 and not self._bits:  # simple X10 function
        self._event_func(pyx10.X10FunctionEvent(house_code=house_code, function=key_code))
      elif d16:  # X10 function with extra bits that we don't understand and will ignore
        # TODO emit a warning of some kind
        self._event_func(pyx10.X10FunctionEvent(house_code=house_code, function=key_code))
    finally:
      self._bits = deque()
      self._zeroes = 0
  
  def feed_bit(self, bit):
    if bit == '0' or not bit:
      if self._bits:
        self._zeroes += 1
        if self._zeroes >= 3: self._process_frame()
    else:
      for _ in range(self._zeroes): self._bits.append('0')
      self._zeroes = 0
      self._bits.append('1')
  
  def feed_byte(self, byte):
    self.feed_bit('1' if byte & 0x80 else '0')
    self.feed_bit('1' if byte & 0x40 else '0')
    self.feed_bit('1' if byte & 0x20 else '0')
    self.feed_bit('1' if byte & 0x10 else '0')
    self.feed_bit('1' if byte & 0x08 else '0')
    self.feed_bit('1' if byte & 0x04 else '0')
    self.feed_bit('1' if byte & 0x02 else '0')
    self.feed_bit('1' if byte & 0x01 else '0')
