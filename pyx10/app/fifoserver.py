"""FIFO-based server for sending X10 events.  Available for *nix only."""


from collections import deque
import logging
import os
import re
from select import select
from threading import Thread, Event

from ..common import X10_FN_ALL_OFF, X10_FN_ALL_LIGHTS_ON, X10_FN_ON, X10_FN_OFF, X10_FN_DIM, X10_FN_BRIGHT, X10_FN_ALL_LIGHTS_OFF
from ..common import X10_FN_HAIL_REQ, X10_FN_STATUS_REQ, X10_HOUSE_CODES, X10_UNIT_CODES
from ..common import X10AddressEvent, X10FunctionEvent, X10RelativeDimEvent, X10AbsoluteDimEvent, X10ExtendedCodeEvent


SELECT_TIMEOUT = 0.25  # Interval at which FIFO server thread should check if it's stopped
OPEN_TIMEOUT = 0.25  # Interval at which FIFO server thread should try to open the FIFO
CHUNK_SIZE = 1024  # Number of bytes read from FIFO at a time


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
    
    cmds = deque(cmd.strip().upper() for cmd in cmd_str.decode('utf-8').strip().split())
    logging.info('parsing command string: %s', ' '.join(cmds))
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


class LineBreaker:
  """Class to break incoming data into lines."""
  
  def __init__(self):
    self._current_line = deque()
    self._waiting_lines = deque()
  
  def feed(self, s):
    chunks = s.replace(b'\r', b'').split(b'\n')
    self._current_line.append(chunks[0])
    for chunk in chunks[1:]:
      self._waiting_lines.append(b''.join(self._current_line))
      self._current_line = deque((chunk,))
  
  def lines(self):
    try:
      while True:
        yield self._waiting_lines.popleft()
    except IndexError:
      pass


class FifoServer(Thread):
  """FIFO-based server for sending X10 events."""
  
  def __init__(self, fifo_path, intf):
    super().__init__()
    self._fifo_path = fifo_path
    self._intf = intf
    self._shutdown = False
    self._stopped_event = Event()
  
  def run(self):
    """Repeatedly open the FIFO for reading and execute lines read from it as X10 command strings through a CommandProcessor."""
    
    logging.info('starting FIFO server, fifo path: %s', self._fifo_path)
    command_processor = CommandProcessor(self._intf)
    line_breaker = LineBreaker()
    while not self._shutdown:
      fifo_handle = os.open(self._fifo_path, os.O_RDONLY | os.O_NONBLOCK)
      if fifo_handle < 0:
        time.sleep(OPEN_TIMEOUT)
        continue
      while not self._shutdown:
        rlist, _, _ = select((fifo_handle,), (), (), SELECT_TIMEOUT)
        if fifo_handle not in rlist: continue
        chunk = os.read(fifo_handle, CHUNK_SIZE)
        if not chunk: break
        line_breaker.feed(chunk)
        for line in line_breaker.lines(): command_processor.process_command(line)
    self._stopped_event.set()
  
  def stop(self):
    """Stop the thread.  Blocks until thread is stopped."""
    
    logging.info('stopping FIFO server')
    self._shutdown = True
    self._stopped_event.wait()
    logging.info('FIFO server stopped')
