"""Example for pyx10 event-based app."""


import pyx10


def x10_d3_on(intf):
  print('D3 is now on')


def x10_d3_off(intf):
  print('D3 is now off')


def x10_d4_on(intf):
  intf.get_controller('D').on(2)


def x10_d4_off(intf):
  intf.get_controller('D').off(2)


pyx10.run()
