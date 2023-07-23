"""Common functionality for working with X10 devices and interfaces in Python."""


from collections import deque
from dataclasses import dataclass
from queue import Queue
from threading import Thread, Event


X10_CODES = (0x6, 0xE, 0x2, 0xA, 0x1, 0x9, 0x5, 0xD, 0x7, 0xF, 0x3, 0xB, 0x0, 0x8, 0x4, 0xC)
X10_HOUSE_CODES = {letter: code for letter, code in zip('ABCDEFGHIJKLMNOP', X10_CODES)}
X10_HOUSE_CODES_REV = {code: letter for letter, code in zip('ABCDEFGHIJKLMNOP', X10_CODES)}
X10_UNIT_CODES = {number: code for number, code in zip(range(1, 17), X10_CODES)}
X10_UNIT_CODES_REV = {code: number for number, code in zip(range(1, 17), X10_CODES)}

X10_FN_CODE_REV = {
  (X10_FN_ALL_OFF        := 0x0): 'All Off',
  (X10_FN_ALL_LIGHTS_ON  := 0x1): 'All Lights On',
  (X10_FN_ON             := 0x2): 'On',
  (X10_FN_OFF            := 0x3): 'Off',
  (X10_FN_DIM            := 0x4): 'Dim',
  (X10_FN_BRIGHT         := 0x5): 'Bright',
  (X10_FN_ALL_LIGHTS_OFF := 0x6): 'All Lights Off',
  (X10_FN_EXT_CODE       := 0x7): 'Extended Code',
  (X10_FN_HAIL_REQ       := 0x8): 'Hail Request',
  (X10_FN_HAIL_ACK       := 0x9): 'Hail Acknowledgement',
  (X10_FN_PRESET_DIM_0   := 0xA): 'Preset Dim 0',
  (X10_FN_PRESET_DIM_1   := 0xB): 'Preset Dim 1',
  (X10_FN_EXT_DATA       := 0xC): 'Extended Data',
  (X10_FN_STATUS_ON      := 0xD): 'Status is On',
  (X10_FN_STATUS_OFF     := 0xE): 'Status is Off',
  (X10_FN_STATUS_REQ     := 0xF): 'Status Request',
}

# xtdcode.pdf calls extended data "extended code 2", preset dim 0 "extended code 3", and preset dim 1 "unused", but it seems like
# these were part of an intended standard which never actually made it into products.

RELATIVE_DIM_STEPS = 22  # Number of relative steps that separate a dim level of 0% from a dim level of 100%


@dataclass
class X10AddressEvent:
  """Event through which an X10 unit is addressed for a function to follow."""
  
  house_code: int  # encoded
  unit_code: int  # encoded
  
  def __str__(self):
    return '<X10AddressEvent: address of house %s (0x%X), unit %s (0x%X)>' % (
      X10_HOUSE_CODES_REV[self.house_code], self.house_code, X10_UNIT_CODES_REV[self.unit_code], self.unit_code)


@dataclass
class X10FunctionEvent:
  """Event through which one or more X10 units are told to perform a function."""
  
  house_code: int  # encoded
  function: int
  
  def __str__(self):
    return '<X10FunctionEvent: function %s (0x%X) at house %s (0x%X)>' % (
      X10_FN_CODE_REV[self.function], self.function, X10_HOUSE_CODES_REV[self.house_code], self.house_code)


@dataclass
class X10RelativeDimEvent:
  """Event through which one or more X10 units are told to adjust their dim level by a relative parameter."""
  
  house_code: int  # encoded
  dim: float  # relative dim value, -1 - 1 inclusive
  
  def __str__(self):
    return '<X10RelativeDimEvent: dim %d%% at house %s (0x%X)>' % (
      int(self.dim * 100),
      X10_HOUSE_CODES_REV[self.house_code], self.house_code,
    )


@dataclass
class X10AbsoluteDimEvent:
  """Event through which one or more X10 units are told to set their dim level to an absolute parameter."""
  
  dim: float  # absolute dim value, 0 - 1 inclusive
  
  def __str__(self):
    return '<X10AbsoluteDimEvent: dim %d%%>' % int(self.dim * 100)


@dataclass
class X10ExtendedCodeEvent:
  """Extended Code X10 event."""
  
  house_code: int  # encoded
  unit_code: int  # encoded
  data_byte: int
  cmd_byte: int
  
  def __str__(self):
    return '<X10ExtendedCodeEvent: house %s (0x%X), unit %s (0x%X), data 0x%02X, cmd 0x%02X>' % (
      X10_HOUSE_CODES_REV[self.house_code], self.house_code,
      X10_UNIT_CODES_REV[self.unit_code], self.unit_code,
      self.data_byte, self.cmd_byte,
    )


class MultiQueueGetter:
  """A thread-queue-like that monitors and aggregates multiple queues into one."""
  
  def __init__(self, *queues):
    self._queues = queues
    self._queue = Queue()
    self._shutdown_token = object()
    self._stopped_events = None
  
  def get(self, block=True, timeout=None):
    """Get a tuple (queue, item) from the common queue."""
    
    return self._queue.get(block, timeout)
  
  def _getter_thread(self, queue, started_event, stopped_event):
    """Getter thread.  Monitors a given queue and puts items read from it onto a common queue."""
    
    started_event.set()
    while True:
      item = queue.get()
      if item is self._shutdown_token: break
      self._queue.put((queue, item))
    queue.task_done()
    stopped_event.set()
  
  def start(self):
    """Start getter threads.  Blocks until all threads are started."""
    
    started_events = deque()
    self._stopped_events = deque()
    for queue in self._queues:
      started_event = Event()
      stopped_event = Event()
      started_events.append(started_event)
      self._stopped_events.append(stopped_event)
      Thread(target=self._getter_thread, args=(queue, started_event, stopped_event)).start()
    for started_event in started_events:
      started_event.wait()
  
  def stop(self):
    """Stop getter threads.  Blocks until all threads are stopped."""
    for queue in self._queues:
      queue.put(self._shutdown_token)
    for stopped_event in self._stopped_events:
      stopped_event.wait()


class X10Controller:
  """A simple X10 controller for a given house code."""
  
  def __init__(self, house_letter, put_batch_function):
    house_letter = house_letter.strip().upper()
    try:
      self._house_code = X10_HOUSE_CODES[house_letter]
    except KeyError as e:
      raise KeyError('invalid house letter "%s"' % house_letter) from e
    self._put_batch_function = put_batch_function
    self._batch = deque()
  
  def send(self):
    self._put_batch_function(self._batch)
  
  # Whole-House Functions
  
  def all_off(self): self._batch.append(X10FunctionEvent(house_code=self._house_code, function=X10_FN_ALL_OFF))
  def all_lights_on(self): self._batch.append(X10FunctionEvent(house_code=self._house_code, function=X10_FN_ALL_LIGHTS_ON))
  def all_lights_off(self): self._batch.append(X10FunctionEvent(house_code=self._house_code, function=X10_FN_ALL_LIGHTS_OFF))
  
  # Simple Unit Functions
  
  def unit(self, unit_number=None):
    if unit_number is None: return
    if not 1 <= unit_number <= 16: raise ValueError('unit number must be between 1 and 16, inclusive')
    self._batch.append(X10AddressEvent(house_code=self._house_code, unit_code=X10_UNIT_CODES[unit_number]))
  
  def on(self, unit_number=None):
    self.unit(unit_number)
    self._batch.append(X10FunctionEvent(house_code=self._house_code, function=X10_FN_ON))
  
  def off(self, unit_number=None):
    self.unit(unit_number)
    self._batch.append(X10FunctionEvent(house_code=self._house_code, function=X10_FN_OFF))
  
  def dim(self, unit_number=None):
    self.unit(unit_number)
    self._batch.append(X10FunctionEvent(house_code=self._house_code, function=X10_FN_DIM))
  
  def bright(self, unit_number=None):
    self.unit(unit_number)
    self._batch.append(X10FunctionEvent(house_code=self._house_code, function=X10_FN_BRIGHT))
  
  # Dim Unit Functions
  
  def rel_dim(self, dim, unit_number=None):
    self.unit(unit_number)
    self._batch.append(X10RelativeDimEvent(house_code=self._house_code, dim=dim))
  
  def abs_dim(self, dim, unit_number):
    self.unit(unit_number)
    self._batch.append(X10AbsoluteDimEvent(dim=dim))
  
  # Extended Functions
  
  def ext_code(self, unit_number, data_byte, cmd_byte):
    if not 1 <= unit_number <= 16: raise ValueError('unit number must be between 1 and 16, inclusive')
    if not 0 <= data_byte < 256: raise ValueError('data byte must be between 0 and 255, inclusive')
    if not 0 <= cmd_byte < 256: raise ValueError('command byte must be between 0 and 255, inclusive')
    self._batch.append(X10ExtendedCodeEvent(
      house_code=self._house_code,
      unit_code=X10_UNIT_CODES[unit_number],
      data_byte=data_byte, cmd_byte=cmd_byte,
    ))
  
  def hail_req(self):
    self._batch.append(X10FunctionEvent(house_code=self._house_code, function=X10_FN_HAIL_REQ))
  
  def status_req(self, unit_number=None):
    self.unit(unit_number)
    self._batch.append(X10FunctionEvent(house_code=self._house_code, function=X10_FN_STATUS_REQ))


class X10Interface(Thread):
  """Represents an X10 interface of some kind."""
  
  def __init__(self):
    super().__init__()
    self._events_in = Queue()
    self._event_batches_out = Queue()
  
  # Thread Functions
  
  def stop(self):
    """Stop the interface thread."""
    
    raise NotImplementedError('subclass must override stop method')
  
  # Queue Functions
  
  def get(self, block=True, timeout=None):
    """Get the next incoming event received by the interface, optionally blocking until one is received."""
    
    return self._events_in.get(block, timeout)
  
  def put(self, event, block=True):
    """Queue a single event for the interface to send, optionally blocking until it is sent."""
    
    self._event_batches_out.put((event,))
    if block: self._event_batches_out.join()
  
  def put_batch(self, event_batch, block=True):
    """Queue a batch of events for the interface to send, optionally blocking until they are sent."""
    
    self._event_batches_out.put(event_batch)
    if block: self._event_batches_out.join()
  
  # Control Functions
  
  def get_controller(self, house_letter):
    """Create and return an X10Controller for this interface and the given house letter."""
    
    return X10Controller(house_letter=house_letter, put_batch_function=self.put_batch)
