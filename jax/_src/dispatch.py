# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Primitive dispatch and jit dispatch.

from functools import partial
import itertools
from typing import (
    Any, Callable, Dict, Optional, Sequence, Set, Tuple, Type, Union)
import warnings

from absl import logging
import numpy as np

from jax import core
from jax import linear_util as lu
import jax.interpreters.ad as ad
import jax.interpreters.batching as batching
import jax.interpreters.masking as masking
import jax.interpreters.xla as xla
import jax.interpreters.partial_eval as pe
from jax.errors import UnexpectedTracerError
from jax._src.abstract_arrays import array_types
from jax._src.config import config
from jax._src import device_array
from jax._src import dtypes
from jax._src.lib import xla_bridge as xb
from jax._src.lib import xla_client as xc
import jax._src.util as util
from jax._src import traceback_util

traceback_util.register_exclusion(__file__)


xe = xc._xla

Backend = xe.Client
Device = xc.Device
Buffer = xe.Buffer

XlaExecutable = xc.Executable

map, unsafe_map = util.safe_map, map
zip, unsafe_zip = util.safe_zip, zip

# This flag is set on exit; no logging should be attempted
_on_exit = False

### op-by-op execution

ArgSpec = Tuple[core.AbstractValue, Optional[Device]]

def arg_spec(x: Any) -> ArgSpec:
  aval = xla.abstractify(x)
  try:
    return aval, x._device
  except:
    return aval, None


def apply_primitive(prim, *args, **params):
  """Impl rule that compiles and runs a single primitive 'prim' using XLA."""
  if config.jax_enable_mlir:
    import jax.interpreters.mlir
    return jax.interpreters.mlir.apply_primitive(prim, *args, **params)
  compiled_fun = xla_primitive_callable(prim, *unsafe_map(arg_spec, args),
                                        **params)
  return compiled_fun(*args)

# TODO(phawkins): update code referring to xla.apply_primitive to point here.
xla.apply_primitive = apply_primitive


@util.cache()
def xla_primitive_callable(prim, *arg_specs: ArgSpec, **params):
  avals, arg_devices = util.unzip2(arg_specs)
  donated_invars = (False,) * len(arg_specs)
  device = _device_from_arg_devices(arg_devices)
  def prim_fun(*args):
    out = prim.bind(*args, **params)
    if prim.multiple_results:
      return out
    else:
      return out,
  compiled = _xla_callable_uncached(lu.wrap_init(prim_fun), device, None,
                                    prim.name, donated_invars, *arg_specs)
  if not prim.multiple_results:
    return lambda *args, **kw: compiled(*args, **kw)[0]
  else:
    return compiled


def _device_from_arg_devices(devices: Sequence[Optional[Device]]) -> Optional[Device]:
  """Given devices of inputs, determine where to perform a computation.

  Args:
    devices: list where each element is a either a `Device` instance or `None`.
  Returns:
    A `Device` instance or None.
  Raises:
    ValueError if input devices are inconsistent.
  """
  try:
    device, = {d for d in devices if d is not None} or (None,)
    return device
  except ValueError as err:
    msg = "primitive arguments must be colocated on the same device, got {}"
    raise ValueError(msg.format(", ".join(map(str, devices)))) from err


# JIT execution

def _xla_call_impl(fun: lu.WrappedFun, *args, device, backend, name,
                   donated_invars, inline):
  if config.jax_enable_mlir:
    import jax.interpreters.mlir
    return jax.interpreters.mlir._xla_call_impl_mlir(
        fun, *args, device=device, backend=backend, name=name,
        donated_invars=donated_invars, inline=inline)

  del inline  # Only used at tracing time
  compiled_fun = _xla_callable(fun, device, backend, name, donated_invars,
                               *unsafe_map(arg_spec, args))
  try:
    out = compiled_fun(*args)
  except FloatingPointError:
    assert config.jax_debug_nans or config.jax_debug_infs  # compiled_fun can only raise in this case
    print("Invalid value encountered in the output of a jit/pmap-ed function. "
          "Calling the de-optimized version.")
    # We want to run the wrapped function again (after _xla_callable already ran
    # it), but linear_util.WrappedFun instances are meant to be run only once.
    # In addition to re-executing the Python code, which is usually undesirable
    # but which config.jax_debug_nans is meant to opt into, we'll be re-executing
    # any linear_util.py-style side effects, i.e. re-populating Stores created
    # by any transformation_with_aux's applied to fun. Since this is
    # intentional here, to avoid "Store occupied" errors we clone the WrappedFun
    # with empty stores.
    stores = [lu.Store() for _ in fun.stores]
    clone = lu.WrappedFun(fun.f, fun.transforms, stores, fun.params)
    with core.new_sublevel():
      _ = clone.call_wrapped(*args)  # probably won't return
  return out

xla.xla_call_p.def_impl(_xla_call_impl)


def _xla_callable_uncached(fun: lu.WrappedFun, device, backend, name,
                           donated_invars, *arg_specs):
  return lower_xla_callable(fun, device, backend, name, donated_invars,
                            *arg_specs).compile().unsafe_call

_xla_callable = lu.cache(_xla_callable_uncached)


def lower_xla_callable(fun: lu.WrappedFun, device, backend, name,
                       donated_invars, *arg_specs):
  if device is not None and backend is not None:
    raise ValueError("can't specify both a device and a backend for jit, "
                     "got device={} and backend={}".format(device, backend))

  abstract_args, arg_devices = util.unzip2(arg_specs)
  jaxpr, out_avals, consts = pe.trace_to_jaxpr_final(
      fun, abstract_args, pe.debug_info_final(fun, "jit"))
  if any(isinstance(c, core.Tracer) for c in consts):
    raise UnexpectedTracerError("Encountered an unexpected tracer.")
  jaxpr, kept_const_idx, kept_var_idx = _prune_unused_inputs(jaxpr)
  consts = [c for i, c in enumerate(consts) if i in kept_const_idx]
  pruned_arg_specs = (a for i, a in enumerate(arg_specs) if i in kept_var_idx)
  abstract_args, arg_devices = util.unzip2(pruned_arg_specs)
  donated_invars = [
      x for i, x in enumerate(donated_invars) if i in kept_var_idx
  ]
  map(prefetch, itertools.chain(consts, jaxpr_literals(jaxpr)))
  jaxpr = apply_outfeed_rewriter(jaxpr)

  nreps = jaxpr_replicas(jaxpr)
  device = _xla_callable_device(nreps, backend, device, arg_devices)
  backend = xb.get_device_backend(device) if device else xb.get_backend(backend)

  # Computations that only produce constants and/or only rearrange their inputs,
  # which are often produced from partial evaluation, don't need compilation,
  # and don't need to evaluate their arguments.
  if not jaxpr.eqns:
    return XlaComputation(
        name, None, True, None, jaxpr, consts, device, abstract_args, out_avals,
        kept_var_idx)

  if not _on_exit:
    log_priority = logging.WARNING if config.jax_log_compiles else logging.DEBUG
    logging.log(log_priority, "Compiling %s (%s) for args %s.",
                fun.__name__, id(fun), abstract_args)

  if nreps > 1:
    warnings.warn(
        f"The jitted function {name} includes a pmap. Using "
         "jit-of-pmap can lead to inefficient data movement, as the outer jit "
         "does not preserve sharded data representations and instead collects "
         "input and output arrays onto a single device. "
         "Consider removing the outer jit unless you know what you're doing. "
         "See https://github.com/google/jax/issues/2926.")

  if nreps > xb.device_count(backend):
    raise ValueError(
        f"compiling computation `{name}` that requires {nreps} replicas, but "
        f"only {xb.device_count(backend)} XLA devices are available.")

  if xb.process_count() > 1 and (nreps > 1 or jaxpr_has_pmap(jaxpr)):
    raise NotImplementedError(
        "jit of multi-host pmap not implemented (and jit-of-pmap can cause "
        "extra data movement anyway, so maybe you don't want it after all).")

  tuple_args = len(abstract_args) > 100  # pass long arg lists as tuple for TPU

  c = xc.XlaBuilder(f"jit_{fun.__name__}")
  xla_consts = xla._xla_consts(c, consts)
  xla_args, donated_invars = xla._xla_callable_args(c, abstract_args, tuple_args,
                                                donated_invars=donated_invars)
  platform = backend.platform
  ctx = xla.TranslationContext(c, platform, xla.AxisEnv(nreps, (), ()),
                               xla.extend_name_stack(xla.wrap_name(name, 'jit')))
  out_nodes = xla.jaxpr_subcomp(ctx, jaxpr, xla_consts, *xla_args)
  # There is a non-zero cost to building an output tuple, particularly on TPU.
  # Avoid it if the output arity is 1.
  output = out_nodes[0] if len(out_nodes) == 1 else xc.ops.Tuple(c, out_nodes)
  if platform in ("gpu", "tpu"):
    donated_invars = xla.set_up_aliases(
        c, xla_args, c.GetShape(output), donated_invars, tuple_args)
  if any(donated_invars):
    # TODO(tomhennigan): At call time we should mark these buffers as deleted.
    unused_donations = [str(c.GetShape(a))
                        for a, d in zip(xla_args, donated_invars) if d]
    warnings.warn("Some donated buffers were not usable: {}".format(
        ", ".join(unused_donations)))
  built = c.build(output)
  return XlaComputation(
      name, built, False, donated_invars, nreps, device, backend, tuple_args,
      abstract_args, out_avals, kept_var_idx)


def prefetch(x):
  if isinstance(x, device_array.DeviceArray):
    x.copy_to_host_async()
  return x


def jaxpr_literals(jaxpr):
  """Generates all the literals inside a jaxpr, including nested subjaxprs."""
  for eqn in jaxpr.eqns:
    for v in eqn.invars:
      if type(v) is core.Literal:
        yield v.val
  for subjaxpr in core.subjaxprs(jaxpr):
    yield from jaxpr_literals(subjaxpr)


def jaxpr_has_pmap(jaxpr):
  """Whether there is an xla_pmap primitive anywhere inside a Jaxpr."""
  for eqn in jaxpr.eqns:
    if 'xla_pmap' in eqn.primitive.name:
      return True
  for subjaxpr in core.subjaxprs(jaxpr):
    if jaxpr_has_pmap(subjaxpr):
      return True
  return False

def _prune_unused_inputs(
    jaxpr: core.Jaxpr) -> Tuple[core.Jaxpr, Set[int], Set[int]]:
  used = {v for v in jaxpr.outvars if isinstance(v, core.Var)}
  # TODO(zhangqiaorjc): Improve the DCE algorithm by also pruning primitive
  # applications that do not produce used outputs. Must handle side-effecting
  # primitives and nested jaxpr.
  used.update(
      v for eqn in jaxpr.eqns for v in eqn.invars if isinstance(v, core.Var))
  kept_const_idx, new_constvars = util.unzip2(
      (i, v) for i, v in enumerate(jaxpr.constvars) if v in used)
  kept_var_idx, new_invars = util.unzip2(
      (i, v) for i, v in enumerate(jaxpr.invars) if v in used)
  new_jaxpr = core.Jaxpr(new_constvars, new_invars, jaxpr.outvars, jaxpr.eqns)
  return new_jaxpr, set(kept_const_idx), set(kept_var_idx)


# We can optionally set a Jaxpr rewriter that can be applied just before
# compilation. This mechanism is used for compiling id_tap, we can
# remove it once we bring the id_tap implementation into the core.
outfeed_rewriter: Optional[Callable[[core.Jaxpr], core.Jaxpr]] = None
def apply_outfeed_rewriter(jaxpr: core.Jaxpr) -> core.Jaxpr:
  if outfeed_rewriter is not None:
    return outfeed_rewriter(jaxpr)
  else:
    return jaxpr

outfeed_primitives: Set[core.Primitive] = set()
def jaxpr_uses_outfeed(jaxpr: core.Jaxpr) -> bool:
  """Finds if there are outfeed primitives anywhere inside a Jaxpr."""
  return any(primitive_uses_outfeed(eqn.primitive, eqn.params)
             for eqn in jaxpr.eqns)

def _param_uses_outfeed(param):
  if type(param) is core.Jaxpr:
    if jaxpr_uses_outfeed(param):
      return True
  elif type(param) is core.ClosedJaxpr:
    if jaxpr_uses_outfeed(param.jaxpr):
      return True
  return False

def primitive_uses_outfeed(prim: core.Primitive, params: Dict) -> bool:
  if prim in outfeed_primitives:
    return True
  for param in params.values():
    if isinstance(param, tuple):
      if any(unsafe_map(_param_uses_outfeed, param)):
        return True
    elif _param_uses_outfeed(param):
      return True
  return False


def jaxpr_replicas(jaxpr) -> int:
  """The number of replicas needed for a jaxpr.

  For a eqn, multiply the `axis_size` with the `jaxpr_replicas` of the
  subjaxprs. For a list of eqns, take the maximum number of replicas.
  """
  if isinstance(jaxpr, core.ClosedJaxpr):
    jaxpr = jaxpr.jaxpr
  return max(unsafe_map(eqn_replicas, jaxpr.eqns), default=1)

# TODO(mattjj): this function assumes that only pmap has a parameter named
# axis_size, and that it corresponds to cross-replica mapping
def eqn_replicas(eqn):
  call_jaxpr = eqn.params.get("call_jaxpr")
  if call_jaxpr:
    return eqn.params.get('axis_size', 1) * jaxpr_replicas(call_jaxpr)
  elif eqn.primitive in xla._initial_style_primitives:
    return initial_style_primitive_replicas(eqn.params)
  else:
    return 1

def initial_style_primitive_replicas(params):
  return max(core.traverse_jaxpr_params(jaxpr_replicas, params).values(), default=1)


def _xla_callable_device(nreps, backend, device, arg_devices):
  if nreps > 1:
    if device is not None or backend is not None:
      raise ValueError(f"can't specify device or backend for jit-of-pmap, "
                       f"got device={device} and backend={backend}")
    return None
  else:
    if device is None and backend is None:
      return _device_from_arg_devices(arg_devices)
    elif device is not None and backend is None:
      return device
    elif device is None and backend is not None:
      return xb.get_backend(backend).get_default_device_assignment(1)[0]
    else:
      assert False  # Unreachable given the error check in _xla_callable


# Result handlers

def aval_to_result_handler(device: Optional[Device],
                           aval: core.AbstractValue) -> Callable:
  try:
    return xla_result_handlers[type(aval)](device, aval)
  except KeyError as err:
    raise TypeError(f"No xla_result_handler for type: {type(aval)}") from err

def array_result_handler(device: Optional[Device], aval: core.ShapedArray):
  if aval.dtype is dtypes.float0:
    return lambda _: np.zeros(aval.shape, dtypes.float0)
  return partial(device_array.make_device_array, core.raise_to_shaped(aval), device)


xla_result_handlers: Dict[Type[core.AbstractValue], Callable[..., Callable]] = {
    core.AbstractUnit: lambda _, __: lambda _: core.unit,
    core.ShapedArray: array_result_handler,
    core.ConcreteArray: array_result_handler,
}
xla_result_handlers[core.AbstractToken] = lambda _, __: lambda _: core.token


def needs_check_special():
  return config.jax_debug_infs or config.jax_debug_nans

def check_special(name, bufs):
  if needs_check_special():
    for buf in bufs:
      _check_special(name, buf.xla_shape(), buf)

def _check_special(name, xla_shape, buf):
  assert not xla_shape.is_tuple()
  if dtypes.issubdtype(xla_shape.element_type(), np.inexact):
    if config.jax_debug_nans and np.any(np.isnan(buf.to_py())):
      raise FloatingPointError(f"invalid value (nan) encountered in {name}")
    if config.jax_debug_infs and np.any(np.isinf(buf.to_py())):
      raise FloatingPointError(f"invalid value (inf) encountered in {name}")


def _execute_compiled(name: str, compiled: XlaExecutable,
                      output_buffer_counts: Optional[Sequence[int]], handlers,
                      kept_var_idx, *args):
  device, = compiled.local_devices()
  input_bufs = list(
      itertools.chain.from_iterable(
          device_put(x, device)
          for i, x in enumerate(args)
          if x is not core.token and i in kept_var_idx))
  out_bufs = compiled.execute(input_bufs)
  check_special(name, out_bufs)
  if output_buffer_counts is None:
    return (handlers[0](*out_bufs),)
  return tuple(
      handler(*bs) for handler, bs in
      unsafe_zip(handlers, xla._partition_outputs(output_buffer_counts, out_bufs)))


def _execute_replicated(name: str, compiled: XlaExecutable,
                        output_buffer_counts: Optional[Sequence[int]], handlers,
                        kept_var_idx, *args):
  input_bufs = [
      list(
          itertools.chain.from_iterable(
              device_put(x, device)
              for i, x in enumerate(args)
              if x is not core.token and i in kept_var_idx))
      for device in compiled.local_devices()
  ]
  out_bufs = [
      buf[0] for buf in compiled.execute_sharded_on_local_devices(
          list(zip(*input_bufs)))
  ]
  check_special(name, out_bufs)
  if output_buffer_counts is None:
    return (handlers[0](*out_bufs),)
  return tuple(
      handler(*bs) for handler, bs in
      unsafe_zip(handlers, xla._partition_outputs(output_buffer_counts, out_bufs)))


def _execute_trivial(jaxpr, device: Optional[Device], consts, avals, handlers,
                     kept_var_idx, *args):
  env = {core.unitvar: core.unit}
  pruned_args = (x for i, x in enumerate(args) if i in kept_var_idx)
  map(env.setdefault, jaxpr.invars, pruned_args)
  map(env.setdefault, jaxpr.constvars, consts)
  outs = [xla.canonicalize_dtype(v.val) if type(v) is core.Literal else env[v]
          for v in jaxpr.outvars]
  return [_copy_device_array_to_device(x, device) if device_array.type_is_device_array(x)
          else h(*device_put(x, device)) for h, x in zip(handlers, outs)]


class XlaComputation:
  name: str
  _is_trivial: bool
  _executable: Optional['XlaCompiledComputation']
  _donated_invars: Optional[Sequence[bool]]

  def __init__(self, name: str, hlo, is_trivial: bool,
               donated_invars: Optional[Sequence[bool]], *compile_args):
    self.name = name
    self._hlo = hlo
    self._is_trivial = is_trivial
    self._donated_invars = donated_invars
    self._executable = None
    self.compile_args = compile_args

  def is_trivial(self):
    return self._is_trivial

  def hlo(self):
    if self.is_trivial():
      raise ValueError("A trivial computation has no HLO")
    return self._hlo

  def compile(self) -> 'XlaCompiledComputation':
    if self._executable is None:
      if self.is_trivial():
        self._executable = XlaCompiledComputation.from_trivial_jaxpr(
            *self.compile_args)
      else:
        self._executable = XlaCompiledComputation.from_xla_computation(
            self.name, self.hlo(), *self.compile_args)
    return self._executable

def backend_compile(backend, built_c, options):
  # we use a separate function call to ensure that XLA compilation appears
  # separately in Python profiling results
  return backend.compile(built_c, compile_options=options)

# TODO(phawkins): update users.
xla.backend_compile = backend_compile

def compile_or_get_cached(backend, computation, compile_options):
    # Avoid import cycle between jax and jax.experimental
    from jax.experimental.compilation_cache import compilation_cache as cc
    # Persistent compilation cache only implemented on TPU.
    # TODO(skye): add warning when initializing cache on unsupported default platform
    if cc.is_initialized() and backend.platform == 'tpu':
        cached_executable = cc.get_executable(computation, compile_options, backend)
        if cached_executable is not None:
            logging.info('Persistent compilation cache hit')
            return cached_executable
        else:
            compiled = backend_compile(backend, computation, compile_options)
            cc.put_executable(computation, compile_options, compiled, backend)
            return compiled
    return backend_compile(backend, computation, compile_options)


class XlaCompiledComputation:
  def __init__(self, xla_executable, in_avals, kept_var_idx, unsafe_call):
    self._xla_executable = xla_executable
    self.in_avals = in_avals
    self._kept_var_idx = kept_var_idx
    self.unsafe_call = unsafe_call

  @staticmethod
  def from_xla_computation(
      name: str,
      xla_computation,
      nreps: int,
      device,
      backend,
      tuple_args: bool,
      in_avals,
      out_avals,
      kept_var_idx) -> 'XlaCompiledComputation':
    result_handlers = map(partial(aval_to_result_handler, device), out_avals)
    options = xb.get_compile_options(
        num_replicas=nreps,
        num_partitions=1,
        device_assignment=(device.id,) if device else None)
    options.parameter_is_tupled_arguments = tuple_args
    compiled = compile_or_get_cached(backend, xla_computation, options)
    buffer_counts = (None if len(out_avals) == 1 else
                     [len(xla.aval_to_xla_shapes(aval)) for aval in out_avals])
    execute = _execute_compiled if nreps == 1 else _execute_replicated
    unsafe_call = partial(execute, name, compiled, buffer_counts,
                          result_handlers, kept_var_idx)
    return XlaCompiledComputation(compiled, in_avals, kept_var_idx, unsafe_call)

  def is_trivial(self):
    return self._xla_executable == None

  def xla_executable(self):
    if self.is_trivial():
      raise ValueError("A trivial compiled computation has no XLA executable")
    return self._xla_executable

  @staticmethod
  def from_trivial_jaxpr(jaxpr, consts, device, in_avals, out_avals,
                         kept_var_idx) -> 'XlaCompiledComputation':
    result_handlers = map(partial(aval_to_result_handler, device), out_avals)
    unsafe_call = partial(_execute_trivial, jaxpr, device, consts,
                          out_avals, result_handlers, kept_var_idx)
    return XlaCompiledComputation(None, in_avals, kept_var_idx, unsafe_call)

  def call(self, *args):
    arg_specs = unsafe_map(arg_spec, args)
    arg_avals = [spec[0] for i, spec in enumerate(arg_specs)
                 if i in self._kept_var_idx]
    check_arg_avals_for_call(self.in_avals, arg_avals)
    return self.unsafe_call(*args)

def check_arg_avals_for_call(ref_avals, arg_avals):
  if len(ref_avals) != len(arg_avals):
    raise TypeError(
        f"Computation compiled for {len(ref_avals)} inputs "
        f"but called with {len(arg_avals)}")
  for ref_aval, arg_aval in zip(ref_avals, arg_avals):
    if not core.typematch(ref_aval, arg_aval):
      ref_avals_fmt = ', '.join(str(a) for a in ref_avals)
      arg_avals_fmt = ', '.join(str(a) for a in arg_avals)
      raise TypeError(
        f"Computation compiled for input types:\n  {ref_avals_fmt}\n"
        f"called with:\n  {arg_avals_fmt}")


def device_put(x, device: Optional[Device] = None) -> Tuple[Any]:
  x = xla.canonicalize_dtype(x)
  try:
    return device_put_handlers[type(x)](x, device)
  except KeyError as err:
    raise TypeError(f"No device_put handler for type: {type(x)}") from err

# TODO(phawkins): update users.
xla.device_put = device_put

def _device_put_array(x, device: Optional[Device]):
  backend = xb.get_device_backend(device)
  if x.dtype is dtypes.float0:
    x = np.zeros(x.shape, dtype=np.dtype(bool))
  return (backend.buffer_from_pyval(x, device),)

def _device_put_scalar(x, device):
  return _device_put_array(dtypes.coerce_to_array(x), device)

def _device_put_unit(_, device):
  backend = xb.get_device_backend(device)
  return (backend.buffer_from_pyval(np.zeros((), dtype=np.dtype('bool')),
                                    device),)

_scalar_types = dtypes.python_scalar_dtypes.keys()

device_put_handlers: Dict[Any, Callable[[Any, Optional[Device]], Tuple[Any]]] = {
  core.Unit: _device_put_unit
}
device_put_handlers.update((t, _device_put_array) for t in array_types)
device_put_handlers.update((t, _device_put_scalar) for t in _scalar_types)
device_put_handlers[core.Token] = lambda x, _: (x,)


def _device_put_device_array(x: Union[device_array.DeviceArrayProtocol, device_array._DeviceArray], device: Optional[Device]):
  x = _copy_device_array_to_device(x, device)
  return (x.device_buffer,)
for t in device_array.device_array_types:
  device_put_handlers[t] = _device_put_device_array

def _copy_device_array_to_device(x: Union[device_array.DeviceArrayProtocol, device_array._DeviceArray], device: Optional[xc.Device]) -> Union[device_array.DeviceArrayProtocol, device_array._DeviceArray]:
  if device is None:
    # no copying to be done because there's no target specified
    return x
  elif xb.get_device_backend(device).platform == x.device_buffer.platform():
    # source and target platforms are the same
    if x.device_buffer.device() == device:
      # no copying to be done because source equals target
      if x._device == device:
        return x
      else:
        moved_buf = x.device_buffer  # We need to change stickyness
    else:
      # move the buffer with a device-to-device copy
      moved_buf = x.device_buffer.copy_to_device(device)
  else:
    # buffers from different XLA backends are passed through the host.
    backend = xb.get_device_backend(device)
    moved_buf = backend.buffer_from_pyval(x.device_buffer.to_py(), device)
  return device_array.make_device_array(x.aval, device, moved_buf)


def _device_put_impl(x, device: Optional[Device] = None):
  if device_array.type_is_device_array(x):
    return _copy_device_array_to_device(x, device)

  try:
    a = xla.abstractify(x)
  except TypeError as err:
    raise TypeError(
        f"Argument '{x}' of type {type(x)} is not a valid JAX type") from err
  return aval_to_result_handler(device, a)(*device_put(x, device))

device_put_p = core.Primitive('device_put')
device_put_p.def_impl(_device_put_impl)
device_put_p.def_abstract_eval(lambda x, device=None: x)
xla.translations[device_put_p] = lambda c, x, device=None: x
ad.deflinear2(device_put_p, lambda cotangent, _, **kwargs: [cotangent])
masking.defvectorized(device_put_p)
batching.defvectorized(device_put_p)
