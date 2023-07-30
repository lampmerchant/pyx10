"""Functions to run an event-based X10 app."""


from configparser import ConfigParser
import inspect
import logging
import logging.config
import os
import signal
from threading import Event
import time

from ..common import PROGRAM_NAME, PROGRAM_VERSION
from ..interface import get_interface
from .eventdispatcher import EventDispatcher
from .eventscheduler import EventScheduler
from .fifoserver import FifoServer

try:
  import astral
  import astral.geocoder
except ImportError:
  astral = None


POSSIBLE_CONFIG_LOCATIONS = ('~/.pyx10.ini', '~/pyx10.ini', '/etc/pyx10.ini')


def ensure_clock_stability(period, tolerance=0.1):
  """Ensure that the system clock has not changed drastically within the given number of seconds."""
  
  logging.debug('ensuring stability of system clock')
  initial_time = time.time()
  start_time = initial_time
  retries_required = 0
  while True:
    expected_time_min = start_time + (period * (1 - tolerance))
    expected_time_max = start_time + (period * (1 + tolerance))
    time.sleep(period)
    end_time = time.time()
    if expected_time_min <= end_time <= expected_time_max: break
    logging.debug('system clock not stable; expected it to change by %d seconds but changed by %d seconds instead so retrying',
                  period, end_time - start_time)
    retries_required += 1
    start_time = time.time()
  logging.info('system clock appears to %s after %d seconds', 'have stabilized' if retries_required else 'be stable',
               period * (retries_required + 1))


class SignalHandler:
  """Handler for terminal signals."""
  
  def __init__(self):
    self._signal_event = Event()
  
  def _handle_SIGBREAK(self, unused_signum, unused_frame):
    logging.warning('SIGBREAK received')
    self._signal_event.set()
  
  def _handle_SIGINT(self, unused_signum, unused_frame):
    logging.warning('SIGINT received')
    self._signal_event.set()
  
  def _handle_SIGTERM(self, unused_signum, unused_frame):
    logging.warning('SIGTERM received')
    self._signal_event.set()
  
  def _setup_signals(self):
    if hasattr(signal, 'SIGBREAK'): signal.signal(getattr(signal, 'SIGBREAK'), self._handle_SIGBREAK)
    signal.signal(signal.SIGINT, self._handle_SIGINT)
    signal.signal(signal.SIGTERM, self._handle_SIGTERM)
  
  def _teardown_signals(self):
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    if hasattr(signal, 'SIGBREAK'): signal.signal(getattr(signal, 'SIGBREAK'), signal.SIG_DFL)
  
  def wait(self):
    self._setup_signals()
    self._signal_event.wait()
    self._teardown_signals()


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
  
  # logging sections
  if 'loggers' in config and 'handlers' in config and 'formatters' in config:
    logging.config.fileConfig(config)
    basic_log_warning = False
  else:
    logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
    basic_log_warning = True
  logging.info('configuration read from %s', config_location)
  if basic_log_warning:
    logging.warning('config does not contain "loggers", "handlers", and "formatters" sections, using basic logging to stderr only')
  
  # interface section
  if 'interface' not in config: raise ValueError('config file at %s is missing "interface" section' % config_location)
  intf = get_interface(config['interface'])
  dispatcher = EventDispatcher(module, intf)
  
  # location section
  if astral is None:
    logging.warning('astral is not installed, astral events cannot be configured')
    astral_observer = None
  elif 'location' not in config:
    logging.warning('config does not contain "location" section, astral events cannot be configured')
    astral_observer = None
  elif 'latitude' in config['location'] and 'longitude' in config['location']:
    astral_observer = astral.LocationInfo('here', 'here', None,
                                          float(config['location']['latitude']), float(config['location']['longitude'])).observer
  elif 'city' in config['location']:
    try:
      astral_observer = astral.geocoder.lookup(config['location']['city'], astral.geocoder.database()).observer
    except KeyError:
      logging.warning('city "%s" is not recognized, astral events cannot be configured', config['location']['city'])
      astral_observer = None
  else:
    logging.warning('config "location" section must define "city" or "latitude"/"longitude", astral events cannot be configured')
    astral_observer = None
  scheduler = EventScheduler(module, intf, astral_observer)
  
  # fifo_server section
  fifo_server = None
  if 'fifo_server' in config:
    if 'path' in config['fifo_server']:
      fifo_path = config['fifo_server']['path']
      if not os.path.exists(fifo_path): raise FileNotFoundError('FIFO at "%s" does not exist' % fifo_path)
      fifo_server = FifoServer(fifo_path, intf)
  
  # scheduler section
  clock_stability_delay = 0
  if 'scheduler' in config and 'clock_stability_delay' in config['scheduler']:
    try:
      clock_stability_delay = int(config['scheduler']['clock_stability_delay'])
    except ValueError:
      logging.warning('value "%s" for clock_stability_delay is not a valid integer', config['scheduler']['clock_stability_delay'])
  
  # Run the app
  logging.info('starting %s %s', PROGRAM_NAME, PROGRAM_VERSION)
  intf.start()
  if fifo_server: fifo_server.start()
  if clock_stability_delay: ensure_clock_stability(clock_stability_delay)
  dispatcher.start()
  scheduler.start()
  SignalHandler().wait()
  scheduler.stop()
  dispatcher.stop()
  if fifo_server: fifo_server.stop()
  intf.stop()
  logging.info('stopping %s %s', PROGRAM_NAME, PROGRAM_VERSION)
