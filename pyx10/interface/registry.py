"""Registry of X10 interfaces."""


from collections import namedtuple, deque


InterfaceRegistryEntry = namedtuple('InterfaceRegistryEntry', ('cls', 'params'))


_REGISTRY = {}


def register_interface(name, params):
  """Decorator to register an interface."""
  
  def _inner(cls):
    _REGISTRY[name] = InterfaceRegistryEntry(cls=cls, params=params)
    return cls
  return _inner


def get_interface(params):
  """Function to get an instance of an interface from the registry based on a mapping of params."""
  
  if 'interface' not in params: raise KeyError('"interface" parameter must be defined')
  intf_name = params['interface'].lower()
  if intf_name not in _REGISTRY:
    raise ValueError('interface "%s" is unknown; try one of these: %s' % (intf_name, ', '.join(_REGISTRY.keys())))
  intf_class, intf_params = _REGISTRY[intf_name]
  
  possible_params = deque(('interface',))
  for intf_param in intf_params:
    required = True if intf_param.startswith('*') else False
    if required:
      if intf_param[1:] not in params:
        raise KeyError('required parameter "%s" for interface %s is missing' % (intf_param[1:], intf_name))
      possible_params.append(intf_param[1:])
    else:
      possible_params.append(intf_param)
  for param in params:
    if param not in possible_params: raise KeyError('"%s" is not a recognized parameter for interface %s' % (param, intf_name))
  
  params_without_interface = dict(params)
  params_without_interface.pop('interface')
  return intf_class(**params_without_interface)
