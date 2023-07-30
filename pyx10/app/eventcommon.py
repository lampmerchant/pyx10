"""Common functionality for event handling."""


import logging
from threading import Thread


def _call_module_function_thread(module, func_name, args):
  """Thread for calling a module function."""
  
  func_str = '%s(%s)' % (func_name, ', '.join(str(arg) for arg in args))
  logging.debug('starting thread for %s', func_str)
  getattr(module, func_name)(*args)
  logging.debug('finishing thread for %s', func_str)


def call_module_function(module, func_name, args):
  """Start a thread to call a function in a module if it exists."""
  
  if hasattr(module, func_name):
    Thread(target=_call_module_function_thread, args=(module, func_name, args)).start()
  else:
    logging.debug('no function %s exists in module', func_name)
