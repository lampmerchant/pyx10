"""Example for pyx10 event-based app."""


import pyx10


def x10_d3_on(intf):
  print('D3 is now on')


def x10_d3_off(intf):
  print('D3 is now off')


def x10_d4_on(intf):
  x10 = intf.get_controller('D')
  x10.on(2)
  x10.send()


def x10_d4_off(intf):
  x10 = intf.get_controller('D')
  x10.off(2)
  x10.send()


pyx10.run()
