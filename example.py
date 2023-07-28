"""Example for pyx10 event-based app."""


import pyx10


def at_0700_mtwrf(intf):
  intf.get_controller('D').on(9)


def at_sunrise(intf):
  intf.get_controller('E').off(1)


def at_sunset(intf):
  intf.get_controller('E').on(1)


pyx10.run()
