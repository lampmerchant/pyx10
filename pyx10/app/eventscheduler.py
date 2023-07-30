"""Scheduler for time-based event handlers."""


from datetime import datetime, timedelta
import logging
import re
import sched
from threading import Thread, Event, Lock
import time

try:
  import astral
  import astral.sun
except ImportError:
  astral = None

from .eventcommon import call_module_function


SCHED_TIMEOUT = 0.25  # Interval at which EventScheduler thread should check if it's stopped


class EventScheduler(Thread):
  """Device to schedule and run timed event handlers in the app."""
  
  ASTRAL_TIMES = ('dawn', 'sunrise', 'noon', 'sunset', 'dusk')
  REO_EVENT_FUNC = re.compile(r'^at_(%s|(?:[0-1][0-9]|2[0-3])[0-5][0-9])(?:_(m?t?w?r?f?s?u?))?$' % '|'.join(ASTRAL_TIMES))
  
  def __init__(self, module, intf, astral_observer=None):
    super().__init__()
    self._module = module
    self._intf = intf
    self._astral_observer = astral_observer
    self._scheduler = sched.scheduler(timefunc=time.time, delayfunc=time.sleep)
    self._shutdown = False
    self._lock = Lock()
    self._stopped_event = Event()
  
  def _schedule_events_of_today(self, partial_day=False):
    now_naive = datetime.now()
    logging.debug('scheduling events for %s day at %s', 'partial' if partial_day else 'whole', now_naive.isoformat())
    day_of_week = 'mtwrfsu'[now_naive.weekday()]
    for func_name in dir(self._module):
      if m := self.REO_EVENT_FUNC.match(func_name):
        if m.group(2) is not None and day_of_week not in m.group(2): continue
        if m.group(1) in self.ASTRAL_TIMES:
          if astral is None:
            logging.warning('not scheduling %s to run at %s because astral is not installed', func_name, m.group(1))
            continue
          elif self._astral_observer is None:
            logging.warning('not scheduling %s to run at %s because astral observer is not configured', func_name, m.group(1))
            continue
          else:
            astral_event_time = astral.sun.sun(self._astral_observer, date=now_naive.date(), tzinfo=None)[m.group(1)]
            hour = astral_event_time.hour
            minute = astral_event_time.minute
            second = astral_event_time.second
        else:
          hour = int(m.group(1)[0:2])
          minute = int(m.group(1)[2:4])
          second = 0
        event_dt_naive = now_naive.replace(hour=hour, minute=minute, second=second)
        if partial_day and event_dt_naive < now_naive: continue
        logging.debug('scheduling %s to run at %s', func_name, event_dt_naive.isoformat())
        self._scheduler.enterabs(event_dt_naive.timestamp(), 1, call_module_function, (self._module, func_name, (self._intf,)))
    self._scheduler.enterabs((now_naive.replace(hour=0, minute=0, second=0) + timedelta(days=1)).timestamp(), 1, lambda: None)
  
  def run(self):
    logging.info('starting event scheduler, module: %s', str(self._module))
    with self._lock:
      self._schedule_events_of_today(partial_day=True)
    while not self._shutdown:
      result = self._scheduler.run(blocking=False)
      if result is None:
        with self._lock:
          self._schedule_events_of_today(partial_day=False)
      else:
        time.sleep(SCHED_TIMEOUT)
    self._stopped_event.set()
  
  def stop(self):
    logging.info('stopping event scheduler')
    self._shutdown = True
    with self._lock:
      while self._scheduler.queue: self._scheduler.cancel(self._scheduler.queue[0])
    self._stopped_event.wait()
    logging.info('event scheduler stopped')
