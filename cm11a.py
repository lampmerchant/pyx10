"""Classes for working with CM11A and XTB-232 powerline RS232 interfaces.

JV Digital Engineering's XTB-232 is similar to CM11A but does not contain a real-time clock or the ability to trigger events
independently of its host (which these classes don't use anyway).
"""


from collections import deque
from queue import Queue, Empty
from threading import Thread, Event
import time

import serial

import pyx10


# CM11A notes:
# - The CM11A appears to have a timeout of some sort that resets the serial interface.  You can't wait any significant length of
#    time between bytes.
# - Polls have to be dealt with before we can send a command.  The time poll will not be interrupted by a receive poll.
# - One push of the dim or bright button on MC10A appears to be interpreted as 0xE (14) out of 210 or 0x3 out of 210, not sure what
#    the pattern is.  Possibly has to do with the transmission starting on the up or down phase?
# - There remain extremely unlikely but not-impossible edge cases if the CM11A starts polling right as we go to send it an event.
#    TODO what are they?
# - CM11A's receive buffer seems to be circular.  I don't know what the heyu code is on about with 'deferred' dim parameters...


MAX_FAILURES = 5  # Maximum number of times the checksum can be wrong when attempting to send a packet to the CM11A
READY_TIMEOUT = 10  # Maximum length of time the CM11A can take to send a transmission
SERIAL_TIMEOUT = 0.25  # Maximum length of time we should wait for the CM11A to respond to a byte
POLL_WAIT_TIME = 1.5  # Maximum length of time we should wait for the CM11A to send a polling byte

CM11A_POLL_RECV = 0x5A
CM11A_POLL_RECV_RESP = 0xC3
CM11A_POLL_TIME = 0xA5
CM11A_POLL_TIME_RESP = 0x9B
CM11A_POLL_BYTES = (CM11A_POLL_RECV, CM11A_POLL_TIME)
CM11A_READY_RESP = 0x55


# Exceptions


class NoResponseError(Exception):
  """Exception raised when we timeout while waiting for an expected response from the CM11A."""


class BadResponseError(Exception):
  """Exception raised when the CM11A gives an unexpected response."""


class InterruptedByPoll(Exception):
  """Exception raised when the CM11A starts polling right when trying to send it an event."""
  
  def __init__(self, poll_byte):
    super().__init__('interrupted by poll byte 0x%02X' % poll_byte)
    self.poll_byte = poll_byte


# Monkey patches for X10 event classes


def X10AddressEvent_as_cm11a_packet(self):
  """Convert this event into a packet for the CM11A."""
  
  return bytes((0x04, (self.house_code & 0xF) << 4 | (self.unit_code & 0xF)))

pyx10.X10AddressEvent.as_cm11a_packet = X10AddressEvent_as_cm11a_packet


def X10FunctionEvent_as_cm11a_packet(self):
  """Convert this event into a packet for the CM11A."""
  
  return bytes((0x06, (self.house_code & 0xF) << 4 | (self.function & 0xF)))

pyx10.X10FunctionEvent.as_cm11a_packet = X10FunctionEvent_as_cm11a_packet


def X10RelativeDimEvent_as_cm11a_packet(self):
  """Convert this event into a packet for the CM11A."""
  
  return bytes((0x06 | (int(self.dim * 22) & 0x1F) << 3, (self.house_code & 0xF) << 4 | (self.function & 0xF)))

pyx10.X10RelativeDimEvent.as_cm11a_packet = X10RelativeDimEvent_as_cm11a_packet


def X10AbsoluteDimEvent_as_cm11a_packet(self):
  """Convert this event into a packet for the CM11A."""
  
  dim = int(self.dim * 31)
  return bytes((0x06, (dim & 0xF) << 4 | (pyx10.X10_FN_PRESET_DIM_1 if dim & 0x10 else pyx10.X10_FN_PRESET_DIM_0)))

pyx10.X10AbsoluteDimEvent.as_cm11a_packet = X10AbsoluteDimEvent_as_cm11a_packet


def X10ExtendedCodeEvent_as_cm11a_packet(self):
  """Convert this event into a packet for the CM11A."""
  
  return bytes((
    0x07,
    (self.house_code & 0xF) << 4 | pyx10.X10_FN_EXT_CODE,
    self.unit_code & 0xF,
    self.data_byte & 0xFF,
    self.cmd_byte & 0xFF,
  ))

pyx10.X10ExtendedCodeEvent.as_cm11a_packet = X10ExtendedCodeEvent_as_cm11a_packet


# Classes


class SerialAdapter(Thread, Queue):
  """A thread-queue which monitors the given serial port, queueing bytes read from it while the thread runs."""
  
  def __init__(self, serial_port):
    Thread.__init__(self)
    Queue.__init__(self)
    self._serial_obj = serial.Serial(serial_port, baudrate=4800, timeout=SERIAL_TIMEOUT)
    self._shutdown = False
    self._stopped_event = Event()
  
  def write(self, data):
    """Write data to the serial port.  Blocks until data is written."""
    
    self._serial_obj.write(data)
    self._serial_obj.flush()
  
  def run(self):
    """Thread.  Monitors serial port and queues bytes read from it."""
    
    while not self._shutdown:
      data = self._serial_obj.read(1)
      if not data: continue
      self.put(data[0])
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


class CM11A(pyx10.X10Interface):
  """Represents the CM11A, queueing events that it detects on the line and allowing it to put events on the line."""
  
  def __init__(self, serial_port):
    super().__init__()
    self._serial = SerialAdapter(serial_port)
    self._shutdown = False
    self._stopped_event = Event()
  
  # Event Handling
  
  def _handle_event(self, event):
    """Send an outbound event to the CM11A."""
    
    try:
      packet = event.as_cm11a_packet()
    except AttributeError:
      raise ValueError('%s is not an event type that can be serialized for the CM11A' % type(event).__name__)
    packet_desc = str(event)
    checksum = sum(packet) & 0xFF
    for _ in range(MAX_FAILURES):
      self._serial.write(packet)
      try:
        response = self._serial.get(timeout=POLL_WAIT_TIME)  # If CM11A is polling, it might not respond immediately
      except Empty:
        raise NoResponseError('no response to %s' % packet_desc)
      if response == checksum: break
      if response in CM11A_POLL_BYTES:
        # If response is not our checksum but is one of the poll bytes, see if it gets sent again
        try:
          response2 = self._serial.get(timeout=POLL_WAIT_TIME)
          if response2 == response:
            # If the same byte did get sent again unprompted, we've been interrupted by a poll
            raise InterruptedByPoll(response)
          else:
            # If we got some other byte unprompted, something very weird is going on
            raise BadResponseError('unprompted responses 0x%02X, 0x%02X sending %s' % (response, response2, packet_desc))
        except Empty:
          # If the byte was not repeated, it was just a bad checksum, so try sending the packet again
          pass
    else:
      raise BadResponseError('too many bad checksum responses to %s' % packet_desc)
    self._serial.write(b'\x00')  # We got a good checksum response, so confirm to the interface to send the packet over X10
    try:
      response = self._serial.get(timeout=READY_TIMEOUT)
    except Empty:
      raise BadResponseError('no ready response to %s after %s seconds' % (packet_desc, READY_TIMEOUT))
    if response in CM11A_POLL_BYTES and response == checksum:
      # If we got the same byte a second time, the first wasn't actually a good checksum, it was a poll
      # This works because CM11A_READY_RESP is not in CM11A_POLL_BYTES
      raise InterruptedByPoll(response)
    if response != CM11A_READY_RESP: raise BadResponseError('bad ready response of 0x%02X after %s' % (response, packet_desc))
    self._events_in.put(event)
  
  def _handle_poll_time(self):
    """Handle the CM11A's poll for the time after a power failure."""
    
    # Get CM11A to shut up about needing the time by sending the response byte and then waiting until the interface resets
    self._serial.write(bytes((CM11A_POLL_TIME_RESP,)))
    time.sleep(1)
    # TODO give it the actual time?  clear EEPROM here, too, maybe?
  
  def _handle_poll_receive(self):
    """Handle the CM11A's poll for incoming events off the line."""
    
    self._serial.write(bytes((CM11A_POLL_RECV_RESP,)))
    try:
      while (size := self._serial.get(timeout=SERIAL_TIMEOUT)) == CM11A_POLL_RECV: pass
    except Empty:
      raise BadResponseError('size byte missing')
    if not 2 <= size <= 9: raise BadResponseError('size byte 0x%02X is not between 2 and 9' % size)
    try:
      func_mask = self._serial.get(timeout=SERIAL_TIMEOUT)
    except Empty:
      raise BadResponseError('address/function mask missing')
    size -= 1
    recv_bytes = deque()
    byte_idx = 0
    while size > 0:
      try:
        byte = self._serial.get(timeout=SERIAL_TIMEOUT)
      except Empty:
        raise BadResponseError('byte %d of received data missing' % byte_idx)
      recv_bytes.append((byte, True if func_mask & 0x1 else False))
      func_mask >>= 1
      byte_idx += 1
      size -= 1
    while recv_bytes:
      byte, is_func = recv_bytes.popleft()
      if is_func:  # function
        try:
          if byte & 0xF == pyx10.X10_FN_DIM:
            dim_byte, _ = recv_bytes.popleft()
            self._events_in.put(pyx10.X10RelativeDimEvent(house_code=byte >> 4, dim=-dim_byte / 210))
          elif byte & 0xF == pyx10.X10_FN_BRIGHT:
            dim_byte, _ = recv_bytes.popleft()
            self._events_in.put(pyx10.X10RelativeDimEvent(house_code=byte >> 4, dim=dim_byte / 210))
          elif byte & 0xF == pyx10.X10_FN_PRESET_DIM_0:
            self._events_in.put(pyx10.X10AbsoluteDimEvent(dim=(byte >> 4) / 31))
          elif byte & 0xF == pyx10.X10_FN_PRESET_DIM_1:
            self._events_in.put(pyx10.X10AbsoluteDimEvent(dim=(16 + (byte >> 4)) / 31))
          elif byte & 0xF == pyx10.X10_FN_EXT_CODE:
            unit_code, _ = recv_bytes.popleft()
            unit_code &= 0x0F
            data_byte, _ = recv_bytes.popleft()
            cmd_byte, _ = recv_bytes.popleft()
            self._events_in.put(pyx10.X10ExtendedCodeEvent(
              house_code=byte >> 4,
              unit_code=unit_code,
              data_byte=data_byte,
              cmd_byte=cmd_byte
            ))
          else:
            self._events_in.put(pyx10.X10FunctionEvent(house_code=byte >> 4, function=byte & 0xF))
        except IndexError as e:
          raise BadResponseError('argument byte missing after function byte 0x%02X' % byte) from e
      else:  # address
        self._events_in.put(pyx10.X10AddressEvent(house_code=byte >> 4, unit_code=byte & 0xF))
  
  def _handle_poll(self, poll_byte):
    """Handle the CM11A polling for service."""
    
    if poll_byte == CM11A_POLL_TIME:
      self._handle_poll_time()
    elif poll_byte == CM11A_POLL_RECV:
      self._handle_poll_receive()
    else:
      raise BadResponseError('unrecognized poll byte 0x%02X' % poll_byte)
  
  # Threading
  
  def run(self):
    """Main thread.  Handle polls from the CM11A and events for the CM11A."""
    
    mqg = pyx10.MultiQueueGetter(self._serial, self._events_out)
    mqg.start()
    while not self._shutdown:
      try:
        queue, event = mqg.get(timeout=SERIAL_TIMEOUT)
      except Empty:
        continue
      mqg.stop()  # Stop the MultiQueueGetter so _handle_poll and _handle_event have access to incoming serial data
      if queue is self._serial:
        self._handle_poll(event)
      elif queue is self._events_out:
        while True:
          try:
            self._handle_event(event)
            break
          except InterruptedByPoll as e:
            self._handle_poll(e.poll_byte)
        self._events_out.task_done()
      mqg.start()
    mqg.stop()
    self._stopped_event.set()
  
  def start(self):
    """Start the main thread."""
    
    self._shutdown = False
    self._stopped_event.clear()
    self._serial.start()
    super().start()
  
  def stop(self):
    """Stop the main thread.  Blocks until the thread has been stopped."""
    
    self._shutdown = True
    self._serial.stop()
    self._stopped_event.wait()
