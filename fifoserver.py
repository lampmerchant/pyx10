"""FIFO-based X10 server."""


from argparse import ArgumentParser
import logging
import re
import sys

import pyx10


class CommandProcessor:
  """Process X10 command strings in a simple language."""
  
  REO_TARGET = re.compile(r'^(?P<house>[A-P])?(?P<unit>0*1[0-6]|0*[1-9])?$')
  SIMPLE_COMMANDS = {
    'ON': pyx10.X10_FN_ON,
    'OFF': pyx10.X10_FN_OFF,
    'ALLOFF': pyx10.X10_FN_ALL_OFF,
    'ALLUNITSOFF': pyx10.X10_FN_ALL_OFF,
    'ALLLIGHTSON': pyx10.X10_FN_ALL_LIGHTS_ON,
    'ALLLIGHTSOFF': pyx10.X10_FN_ALL_LIGHTS_OFF,
    'DIM': pyx10.X10_FN_DIM,
    'BRIGHT': pyx10.X10_FN_BRIGHT,
    'HAIL': pyx10.X10_FN_HAIL_REQ,
    'STATUS': pyx10.X10_FN_STATUS_REQ,
  }
  REO_REL_DIM = re.compile(r'^DIM\(([+-](?:0*100|0*\d{1,2}))%?\)$')
  REO_ABS_DIM = re.compile(r'^DIM\((0*100|0*\d{1,2})%?\)$')
  
  def __init__(self, intf):
    self._intf = intf
    self._house_code = pyx10.X10_HOUSE_CODES['A']
  
  def command(self, cmd_str):
    """Parse a command string."""
    
    for cmd in cmd_str.strip().split():
      cmd = cmd.strip().upper()
      if not cmd: continue
      logging.info('inbound FIFO command: %s', cmd)
      if m := self.REO_TARGET.match(cmd):
        house, unit = m.groups()
        if house: self._house_code = pyx10.X10_HOUSE_CODES[house]
        if unit: self._intf.put(
          pyx10.X10AddressEvent(house_code=self._house_code, unit_code=pyx10.X10_UNIT_CODES[int(unit)])
        )
      elif (s := cmd.replace('-', '').replace('_', '')) in self.SIMPLE_COMMANDS:
        self._intf.put(pyx10.X10FunctionEvent(house_code=self._house_code, function=self.SIMPLE_COMMANDS[s]))
      elif m := self.REO_REL_DIM.match(cmd):
        self._intf.put(pyx10.X10RelativeDimEvent(house_code=self._house_code, dim=int(m.group(1)) / 100))
      elif m := self.REO_ABS_DIM.match(cmd):
        self._intf.put(pyx10.X10AbsoluteDimEvent(dim=int(m.group(1)) / 100))
      else:
        logging.warning('invalid command: %s', cmd)


def main(argv):
  parser = ArgumentParser(description='Run a FIFO-based X10 server.')
  intf_group = parser.add_mutually_exclusive_group(required=True)
  intf_group.add_argument('--cm11a', metavar='SERIALDEV', help='serial device where CM11A is connected')
  intf_group.add_argument('--pl513', metavar='I2CDEV', help='I2C device where TashTenHat is connected to PL513')
  intf_group.add_argument('--tw523', metavar='I2CDEV', help='I2C device where TashTenHat is connected to TW523/PSC05')
  intf_group.add_argument('--xtb523', metavar='I2CDEV', help='I2C device where TashTenHat is connected to XTB-523/XTB-IIR')
  intf_group.add_argument('--xtb523ab', metavar='I2CDEV', help='I2C device where TashTenHat is connected to XTB-523/XTB-IIR in'
                                                               ' "return all bits" mode')
  parser.add_argument('-v', '--verbose', action='count', default=0, help='verbosity of output, can be given multiple times')
  parser.add_argument('fifo_path', help='filesystem path to the named FIFO that accepts commands (must already exist)')
  args = parser.parse_args(argv[1:])
  
  logging.basicConfig(
    format='%(asctime)s %(levelname)s %(message)s',
    level=logging.DEBUG if args.verbose >= 2 else logging.INFO if args.verbose >= 1 else logging.WARNING,
  )
  
  if args.cm11a:
    intf = pyx10.interface.cm11a.CM11A(args.cm11a)
  elif args.pl513:
    intf = pyx10.interface.tashtenhat.TashTenHatWithPl513(args.pl513)
  elif args.tw523:
    intf = pyx10.interface.tashtenhat.TashTenHatWithTw523(args.tw523)
  elif args.xtb523:
    intf = pyx10.interface.tashtenhat.TashTenHatWithXtb523(args.xtb523)
  elif args.xtb523ab:
    intf = pyx10.interface.tashtenhat.TashTenHatWithXtb523AllBits(args.xtb523ab)
  else:
    raise ValueError('no valid interface specified')
  
  ert = pyx10.EventReaderThread(intf)
  intf.start()
  ert.start()
  command_processor = CommandProcessor(intf)
  try:
    while True:
      with open(args.fifo_path, 'r') as fifo:
        while line := fifo.readline():
          command_processor.command(line.strip())
  except KeyboardInterrupt:
    pass
  ert.stop()
  intf.stop()


if __name__ == '__main__': sys.exit(main(sys.argv))
