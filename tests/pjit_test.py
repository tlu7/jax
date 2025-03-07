# Copyright 2021 Google LLC
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

import re
from functools import partial
import logging
import threading
import unittest
from collections import OrderedDict, namedtuple

from absl.testing import absltest
from absl.testing import parameterized
import numpy as np

import jax
import jax.numpy as jnp
from jax._src import test_util as jtu
from jax.errors import JAXTypeError
from jax import lax
# TODO(skye): do we still wanna call this PartitionSpec?
from jax.experimental import PartitionSpec as P
from jax.experimental.maps import xmap, mesh, Mesh
from jax.experimental import gsda
import jax.experimental.pjit as pjit_lib
from jax.experimental.pjit import (pjit, pjit_p, with_sharding_constraint,
                                   SpecSync, FROM_GSDA)
from jax.interpreters import pxla
from jax.interpreters import xla
from jax._src.lib import xla_client
from jax._src.util import prod, curry, unzip2

from jax.config import config
config.parse_flags_with_absl()


def setUpModule():
  if jax.default_backend() not in {'gpu', 'tpu'}:
    raise unittest.SkipTest("pjit only supports GPU and TPU backends")
  jtu.set_spmd_lowering_flag(True)

def tearDownModule():
  jtu.restore_spmd_lowering_flag()


@curry
def check_1d_2d_mesh(f, set_mesh):
  return parameterized.named_parameters(
    {"testcase_name": "_" + name, "mesh": mesh, "resources": resources}
    for name, mesh, resources in (
      ("2", (("x", 2),), "x"),
      ("2x1", (("x", 2), ("y", 1)), ("x", "y")),
      ("2x2", (("x", 2), ("y", 2)), ("x", "y")),
    ))(jtu.with_mesh_from_kwargs(f) if set_mesh else f)


def create_global_mesh(mesh_shape, axis_names):
  size = prod(mesh_shape)
  if len(jax.devices()) < size:
    raise unittest.SkipTest(f"Test requires {size} local devices")
  mesh_devices = np.array(jax.devices()[:size]).reshape(mesh_shape)
  global_mesh = Mesh(mesh_devices, axis_names)
  return global_mesh


# TODO(skye): make the buffer donation utils part of JaxTestCase
class PJitTest(jtu.BufferDonationTestCase):

  @jtu.with_mesh([('x', 1)])
  def testDeviceBufferAval(self):

    @partial(pjit, in_axis_resources=None, out_axis_resources=P('x'))
    def f(x):
      return x

    shape = (2, 2)
    x = np.arange(prod(shape), dtype=np.float32).reshape(shape)
    actual = f(x)
    expected = x
    self.assertAllClose(actual, expected, check_dtypes=False)
    self.assertIsInstance(actual, pxla.ShardedDeviceArray)
    self.assertLen(actual.device_buffers, 1)
    self.assertAllClose(
        actual.device_buffers[0].to_py(), expected, check_dtypes=False)
    # Repro for a bug on device_buffer aval
    _ = repr(actual.device_buffers)

  @jtu.with_mesh([('x', 2)])
  def testBasic1D(self):
    @partial(pjit,
             in_axis_resources=(P('x'), P('x')),
             out_axis_resources=None)
    def f(x, y):
      return x + y

    shape = (8, 8)
    x = np.arange(prod(shape), dtype=np.float32).reshape(shape)
    actual = f(x, x + 1)
    expected = x + (x + 1)
    self.assertAllClose(actual, expected, check_dtypes=False)
    self.assertIsInstance(actual, pxla.ShardedDeviceArray)
    self.assertLen(actual.device_buffers, 2)
    self.assertAllClose(actual.device_buffers[0].to_py(), expected,
                        check_dtypes=False)

  @jtu.with_mesh([('x', 2), ('y', 2)])
  def testBasic2D(self):
    @partial(pjit,
             in_axis_resources=(P(None, 'x', 'y'), P('y')),
             out_axis_resources=P('x'))
    def f(x, y):
      return x @ y

    x_shape = (8, 6, 4)
    y_shape = (4, 2)
    x = jnp.arange(np.prod(x_shape)).reshape(x_shape)
    y = jnp.arange(np.prod(y_shape)).reshape(y_shape)
    actual = f(x, y)
    expected = x @ y
    self.assertAllClose(actual, expected, check_dtypes=False)
    self.assertIsInstance(actual, pxla.ShardedDeviceArray)
    self.assertLen(actual.device_buffers, 4)

    split0, split1 = np.split(expected, 2)
    self.assertAllClose(actual.device_buffers[0].to_py(), split0,
                        check_dtypes=False)
    self.assertAllClose(actual.device_buffers[1].to_py(), split0,
                        check_dtypes=False)
    self.assertAllClose(actual.device_buffers[2].to_py(), split1,
                        check_dtypes=False)
    self.assertAllClose(actual.device_buffers[3].to_py(), split1,
                        check_dtypes=False)

  @jtu.with_mesh([('x', 2), ('y', 2)])
  def testTwoMeshAxisSharding(self):
    @partial(pjit,
             in_axis_resources=P(('x', 'y'),),
             out_axis_resources=P(('x', 'y'),))
    def f(x, y):
      return x @ y

    shape = (8, 8)
    x = jnp.arange(np.prod(shape)).reshape(shape)
    actual = f(x, x + 1)
    expected = x @ (x + 1)
    self.assertAllClose(actual, expected, check_dtypes=False)
    self.assertIsInstance(actual, pxla.ShardedDeviceArray)
    self.assertLen(actual.device_buffers, 4)

    splits = np.split(expected, 4)
    self.assertAllClose(actual.device_buffers[0].to_py(), splits[0],
                        check_dtypes=False)
    self.assertAllClose(actual.device_buffers[1].to_py(), splits[1],
                        check_dtypes=False)
    self.assertAllClose(actual.device_buffers[2].to_py(), splits[2],
                        check_dtypes=False)
    self.assertAllClose(actual.device_buffers[3].to_py(), splits[3],
                        check_dtypes=False)

  @jtu.with_mesh([('x', 2)])
  def testBufferDonation(self):
    @partial(pjit,
             in_axis_resources=P('x'),
             out_axis_resources=P('x'),
             donate_argnums=0)
    def f(x, y):
      return x + y

    shard = pjit(lambda x: x, in_axis_resources=P('x'),
                 out_axis_resources=P('x'))
    x = shard(jnp.ones((2, 5)) * 4)
    y = shard(jnp.ones((2, 5)) * 2)
    expected = x + y
    self.assertAllClose(f(x, y), expected)
    self.assertNotDeleted(y)
    self.assertDeleted(x)

  @jtu.with_mesh([('x', 2), ('y', 1)])
  def testShardingConstraint(self):
    @partial(pjit, in_axis_resources=None, out_axis_resources=None)
    def f(x):
      y = x + 1
      y = with_sharding_constraint(y, P('x', 'y'))
      return y * 2

    shape = (8, 8)
    x = np.arange(prod(shape)).reshape(shape)
    expected = (x + 1) * 2
    actual = f(x)
    self.assertAllClose(actual, expected, check_dtypes=False)
    self.assertIsInstance(actual, pxla.ShardedDeviceArray)
    self.assertLen(actual.device_buffers, 2)
    self.assertAllClose(actual.device_buffers[0].to_py(), expected,
                        check_dtypes=False)

    hlo = jax.xla_computation(f)(np.ones(shape))
    # Annotation from with_sharding_constraint
    self.assertIn("sharding={devices=[2,1]0,1}", hlo.as_hlo_text())
    # Annotation from pjit
    self.assertIn("sharding={replicated}", hlo.as_hlo_text())

  @jtu.with_mesh([('x', 2), ('y', 1)])
  def testShardingConstraintPyTree(self):
    @partial(pjit, in_axis_resources=None, out_axis_resources=None)
    def f(x):
      x = with_sharding_constraint(x, [P('x', 'y'), P('y', 'x')])
      x = x.copy()
      x[0]["a"] *= 2
      return x

    shape = (8, 8)
    v = np.arange(prod(shape)).reshape(shape)
    x = [{"a": v, "b": v * 2}, v * 3]
    actual = f(x)

    expected = x.copy()
    expected[0]["a"] *= 2
    self.assertAllClose(actual, expected, check_dtypes=False)
    self.assertLen(actual[0]["a"].device_buffers, 2)

    hlo = jax.xla_computation(f)(x)
    # Annotations from with_sharding_constraint
    self.assertIn("sharding={devices=[2,1]0,1}", hlo.as_hlo_text())
    self.assertIn("sharding={devices=[1,2]0,1}", hlo.as_hlo_text())
    # Annotation from pjit
    self.assertIn("sharding={replicated}", hlo.as_hlo_text())

  def testCaching(self):
    def f(x):
      assert should_be_tracing
      return jnp.sin(x) * 2

    x = np.arange(16).reshape(4, 4)
    devices = np.array(list(jax.local_devices())[:4])
    if devices.size < 4:
      raise unittest.SkipTest("Test requires 4 devices")
    devices = devices.reshape((2, 2))
    with mesh(devices, ('x', 'y')):
      should_be_tracing = True
      pjit(f, in_axis_resources=P(('x', 'y')), out_axis_resources=None)(x)
      should_be_tracing = False
      pjit(f, in_axis_resources=P(('x', 'y')), out_axis_resources=None)(x)
    # Re-create the mesh to make sure that has no influence on caching
    with mesh(devices, ('x', 'y')):
      should_be_tracing = False
      pjit(f, in_axis_resources=P(('x', 'y')), out_axis_resources=None)(x)

  @jtu.with_mesh([('x', 2), ('y', 1)])
  def testNested(self):
    # Add a constant captured by the nested pjit to make things more complicated
    h = jnp.arange(4)
    f = pjit(lambda x: x.sum() + h.sum(), in_axis_resources=P('x', 'y'), out_axis_resources=None)
    g = pjit(lambda x: f(jnp.sin(x)), in_axis_resources=P('x', None), out_axis_resources=None)
    x = jnp.arange(16).reshape((4, 4))
    y = g(x)
    self.assertAllClose(y, jnp.sin(x).sum() + h.sum())
    self.assertTrue(hasattr(y, "sharding_spec"))

  @check_1d_2d_mesh(set_mesh=True)
  def testAutodiff(self, mesh, resources):
    if len(mesh) != 2: return
    assert resources == ('x', 'y')
    # Add a constant captured by the nested pjit to make things more complicated
    h = jnp.arange(4)
    f = pjit(lambda x: x.sum(1) * h.sum(),
             in_axis_resources=P('x', 'y'), out_axis_resources=P(('x', 'y')))
    g = pjit(lambda x: f(jnp.sin(x * 4 + 2)),
             in_axis_resources=P('x', None), out_axis_resources=P(('x', 'y')))
    jtu.check_grads(g, (jnp.arange(16, dtype=jnp.float32).reshape((4, 4)) / 100,),
                    order=2)

  @jtu.with_mesh([('x', 2), ('y', 1)])
  def testEvalJaxpr(self):
    x, y = jnp.arange(4), jnp.arange(5)
    f = pjit(lambda x, y: x.sum() + jnp.sin(y),
             in_axis_resources=(P('x'), P('y')),
             out_axis_resources=P('y'))
    f_jaxpr = jax.make_jaxpr(f)(x, y)
    f_eval = jax.core.jaxpr_as_fun(f_jaxpr)
    r, = f_eval(x, y)
    self.assertAllClose(r, x.sum() + jnp.sin(y))

  @jtu.with_mesh([('x', 2)])
  def testNonArrayArg(self):
    self.assertEqual(pjit(lambda x: x + 2,
                          in_axis_resources=None,
                          out_axis_resources=None)(1), 3)

  @jtu.with_mesh([('x', 2)])
  def testNonHashableAxisResources(self):
    x = jnp.arange(4)
    y = pjit(lambda x: {'b': x['a'] + 2},
             in_axis_resources=({'a': P('x')},),
             out_axis_resources={'b': P('x')})({'a': x})
    self.assertAllClose(y, {'b': x + 2})

  @jtu.with_mesh([('x', 2)])
  def testGradOfConstraint(self):
    # Make sure that we can compute grads through sharding constraints
    h = lambda x: jnp.sin(with_sharding_constraint(x, P('x'))).sum()
    f = pjit(lambda x: jax.grad(h)(x),
             in_axis_resources=None, out_axis_resources=None)
    x = jnp.arange(8, dtype=jnp.float32)
    self.assertAllClose(f(x), jnp.cos(x))

  @jtu.with_mesh([('x', 2)])
  def testNoopPartitionSpecs(self):
    noops = [P(), P(None), P(()), P((), None), P(None, None, ())]
    x = jnp.arange(8).reshape((2, 2, 2))
    for spec in noops:
      y = pjit(lambda x: x * 2, in_axis_resources=spec, out_axis_resources=spec)(x)
      self.assertAllClose(y, x * 2)

  @jtu.with_mesh([('x', 2)])
  def testVmapModifiesAxisResources(self):
    h = pjit(lambda x, y: (x + y, x, y), in_axis_resources=P('x'), out_axis_resources=None)
    x = jnp.arange(4)
    y = jnp.arange(5*4).reshape((5, 4))
    jaxpr = jax.make_jaxpr(jax.vmap(h, in_axes=(None, 0)))(x, y).jaxpr
    eqn = jaxpr.eqns[0]
    self.assertIs(eqn.primitive, pjit_p)
    x_sync, y_sync = (spec.sync for spec in eqn.params['in_axis_resources'])
    self.assertEqual(x_sync, SpecSync.IN_SYNC)
    self.assertEqual(y_sync, SpecSync.DIM_PERMUTE)
    x_sync, y_sync, z_sync = (spec.sync for spec in eqn.params['out_axis_resources'])
    self.assertEqual(x_sync, SpecSync.DIM_PERMUTE)
    self.assertEqual(y_sync, SpecSync.IN_SYNC)
    self.assertEqual(z_sync, SpecSync.DIM_PERMUTE)

  @jtu.with_mesh([('x', 2)])
  def testVMap(self):
    f = pjit(lambda x, y: (x + y, x), in_axis_resources=P('x'), out_axis_resources=P('x'))
    x = jnp.arange(4)
    y = jnp.arange(5*4).reshape((5, 4))
    z, w = jax.vmap(f, in_axes=(None, 0), out_axes=(0, None))(x, y)
    self.assertAllClose(z, x + y)
    self.assertAllClose(w, x)
    self.assertEqual(z.sharding_spec.sharding, (pxla.NoSharding(), pxla.Chunked([2])))
    self.assertEqual(w.sharding_spec.sharding, (pxla.Chunked([2]),))

  @jtu.with_mesh([('x', 2)])
  def testVMapShardingConstraint(self):
    f = pjit(lambda x: with_sharding_constraint(x, P('x')),
             in_axis_resources=P(), out_axis_resources=P('x'))
    x = jnp.arange(5*4).reshape((5, 4))
    jaxpr = jax.make_jaxpr(jax.vmap(f))(x)
    pjit_eqn, = jaxpr.eqns
    constraint_eqn, = pjit_eqn.params['jaxpr'].eqns
    self.assertEqual(constraint_eqn.params['axis_resources'].partitions, ((), ('x',)))
    self.assertEqual(constraint_eqn.params['axis_resources'].sync, SpecSync.DIM_PERMUTE)

  @jtu.with_mesh([('x', 2), ('y', 1)])
  def testShardingInXMap(self):
    h = pjit(lambda x: x, in_axis_resources=P('x'), out_axis_resources=None)
    f = xmap(lambda x: h(x * 2), in_axes=['i', ...], out_axes=['i', ...],
             axis_resources={'i': 'y'})
    x = jnp.arange(16).reshape((4, 4))
    rule = xla._translations[pjit_p]
    test_rule_called = False
    def _test_rule(*args, **kwargs):
      nonlocal test_rule_called
      test_rule_called = True
      in_axis_resources = kwargs['in_axis_resources']
      self.assertEqual(len(in_axis_resources), 1)
      self.assertIn(('y',), in_axis_resources[0].partitions)
      return rule(*args, **kwargs)
    try:
      xla._translations[pjit_p] = _test_rule
      f(x)
      self.assertTrue(test_rule_called)
    finally:
      xla._translations[pjit_p] = rule

  @jtu.with_mesh([('x', 2)])
  def testLowerWithDuckTyping(self):
    x = jax.ShapeDtypeStruct((2, 2), jnp.float32)
    # Make sure this doesn't crash
    pjit(lambda x: x + 4,
         in_axis_resources=P('x'), out_axis_resources=P('x')).lower(x)

  @jtu.with_mesh([('x', 2)])
  def testLowerDonateArgnumsAvailable(self):
    x = jax.ShapeDtypeStruct((2, 2), jnp.float32)
    def f(*args):
      x, *_ = args
      return x
    f_low = pjit(f, donate_argnums=(0,),
                 in_axis_resources=P('x'), out_axis_resources=P('x')).lower(x)
    f_com = f_low.compile()
    f_low.donate_argnums == f_com.donate_argnums == (0,)

  def testInfeed(self):
    devices = np.array(jax.local_devices())
    nr_devices = len(devices)
    shape = (nr_devices * 3, nr_devices * 5)

    def f_for_jit(x):
      token = lax.create_token(x)
      (y,), token = lax.infeed(
          token, shape=(jax.ShapedArray(x.shape, np.float32),))
      (z,), token = lax.infeed(
          token, shape=(jax.ShapedArray(x.shape, np.float32),))
      (w,), token = lax.infeed(
          token, shape=(jax.ShapedArray(x.shape, np.float32),))

      return x + y + z + w

    x = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    y = x * 2.
    z = x * 3.
    w = x * 4.

    # Transfer data to infeed before executing the function. For GPUs, the
    # execution of the compiled function is blocking, so transferring data
    # to infeed before executing ensures that the execution does not deadlock
    # waiting for the infeed data.
    logging.info('Transfering to infeed for the jit call')
    d = devices[0]
    d.transfer_to_infeed((y,))
    d.transfer_to_infeed((z,))
    d.transfer_to_infeed((w,))

    # JIT
    logging.info('Making jit call')
    res0 = jax.jit(f_for_jit)(x)
    self.assertAllClose(res0, x + y + z + w, check_dtypes=True)

    # PJIT
    def f_for_pjit(x):
      token = lax.create_token(x)
      # A replicated infeed
      (y,), token = lax.infeed(
          token,
          shape=(jax.ShapedArray(x.shape, np.float32),),
          partitions=(None,))
      # An infeed sharded on first axis
      (z,), token = lax.infeed(
          token,
          shape=(jax.ShapedArray(x.shape, np.float32),),
          partitions=(P(nr_devices, 1),))
      # An infeed sharded on second axis
      (w,), token = lax.infeed(
          token,
          shape=(jax.ShapedArray(x.shape, np.float32),),
          partitions=(P(1, nr_devices),))
      return x + y + z + w

    logging.info('Transfering to infeed for the pjit call')
    for didx, d in enumerate(devices):
      # Transfer the whole array to all devices for replicated.
      d.transfer_to_infeed((y,))
      # For sharded infeed, transfer only the needed slices to each device.
      d.transfer_to_infeed((z[3 * didx:3 * didx + 3, :]))
      d.transfer_to_infeed((w[:, 5 * didx:5 * didx + 5],))

    with mesh(devices, ['d']):
      logging.info('Making pjit call')
      res = pjit(
          f_for_pjit, in_axis_resources=(P('d'),), out_axis_resources=P('d'))(
              x)

    self.assertAllClose(res0, res, check_dtypes=True)

  def testOutfeed(self):
    devices = np.array(jax.local_devices())
    nr_devices = len(devices)
    shape = (nr_devices * 3, nr_devices * 5)

    def f(x):
      token = lax.create_token(x)
      token = lax.outfeed(token, x, partitions=(None,))
      token = lax.outfeed(token, x, partitions=(P(nr_devices, 1),))
      token = lax.outfeed(token, x, partitions=(P(1, nr_devices),))
      return x

    x = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)

    def dispatch():
      with mesh(devices, ['d']):
        logging.info('Making pjit call')
        pjit(f, in_axis_resources=(P('d'),), out_axis_resources=P('d'))(x)
    execution = threading.Thread(target=dispatch)
    execution.start()

    def check_outfeed(d, x):
      y, = d.transfer_from_outfeed(
          xla_client.shape_from_pyval((x,)).with_major_to_minor_layout_if_absent())
      self.assertAllClose(x, y, check_dtypes=True)

    logging.info('Transfering from outfeed for the pjit call')
    for didx, d in enumerate(devices):
      # Transfer the whole array from all devices for replicated.
      check_outfeed(d, x)
      # For sharded outfeed, the results are sliced.
      check_outfeed(d, x[3 * didx:3 * didx + 3, :])
      check_outfeed(d, x[:, 5 * didx:5 * didx + 5])

    execution.join()

  @jtu.with_mesh([('x', 2)])
  def testWithCustomPRNGKey(self):
    if not config.jax_enable_custom_prng:
      raise unittest.SkipTest("test requires jax_enable_custom_prng")
    key = jax.prng.seed_with_impl(jax.prng.rbg_prng_impl, 87)
    # Make sure this doesn't crash
    pjit(lambda x: x, in_axis_resources=(None), out_axis_resources=(None))(key)

  @jtu.with_mesh([('x', 2), ('y', 2)])
  def testLowerCompile(self):
    @partial(pjit,
             in_axis_resources=P(('x', 'y'),),
             out_axis_resources=P(('x', 'y'),))
    def f(x, y):
      return x @ y

    shape = (8, 8)
    x = jnp.arange(np.prod(shape)).reshape(shape)
    expected = x @ (x + 1)

    exe = f.lower(x, x + 1).compile()
    actual = exe(x, x + 1)

    splits = np.split(expected, 4)
    self.assertAllClose(actual.device_buffers[0].to_py(), splits[0],
                        check_dtypes=False)
    self.assertAllClose(actual.device_buffers[1].to_py(), splits[1],
                        check_dtypes=False)
    self.assertAllClose(actual.device_buffers[2].to_py(), splits[2],
                        check_dtypes=False)
    self.assertAllClose(actual.device_buffers[3].to_py(), splits[3],
                        check_dtypes=False)

  @jtu.with_mesh([('x', 2), ('y', 2)])
  def testLowerCompileWithKwargs(self):
    @partial(pjit,
             in_axis_resources=P(('x', 'y'),),
             out_axis_resources=P(('x', 'y'),))
    def f(x, y, **kwargs):
      return x @ y

    shape = (8, 8)
    x = jnp.arange(np.prod(shape)).reshape(shape)
    exe = f.lower(x, x + 1).compile()

    self.assertRaisesRegex(
        NotImplementedError,
        "function was compiled by a transformation that does not support "
        "keyword arguments, but called with keyword arguments: a, b",
        lambda: exe(x, x + 1, a=1, b=2))

  @jtu.with_mesh([('x', 2), ('y', 2)])
  def testLowerCompileInTreeMismatch(self):
    @partial(pjit,
             in_axis_resources=P(('x', 'y'),),
             out_axis_resources=P(('x', 'y'),))
    def f(x, y):
      return x @ y

    shape = (8, 8)
    x = jnp.arange(np.prod(shape)).reshape(shape)
    exe = f.lower(x, x + 1).compile()

    self.assertRaisesRegex(
        TypeError, "function compiled for .*, called with .*",
        lambda: exe([x], [x + 1]))

  @jtu.with_mesh([('x', 2), ('y', 2)])
  def testLowerCompileArgTypeMismatch(self):
    @partial(pjit,
             in_axis_resources=P(('x', 'y'),),
             out_axis_resources=P(('x', 'y'),))
    def f(x, y):
      return x @ y

    shape = (8, 8)
    x = jnp.arange(np.prod(shape)).reshape(shape)
    x_f32 = x.astype(jnp.float32)
    x_i32 = x.astype(jnp.int32)
    exe = f.lower(x_f32, x_f32).compile()
    self.assertRaisesRegex(
        TypeError,
        "Computation compiled for input types:\n.*float32.*\n"
        "called with:\n.*int32.*",
        lambda: exe(x_i32, x_i32))


class GSDAPjitTest(jtu.JaxTestCase):

  @jtu.with_mesh([('x', 4), ('y', 2)])
  def test_pjit_gsda_single_output(self):
    global_mesh = create_global_mesh((4, 2), ('x', 'y'))
    global_input_shape = (8, 2)
    mesh_axes = P('x', 'y')
    input_data = np.arange(
        prod(global_input_shape)).reshape(global_input_shape)
    def cb(index):
      return input_data[index]

    gsda_obj = gsda.GlobalShardedDeviceArray.from_callback(
        global_input_shape, global_mesh, mesh_axes, cb)

    with jax._src.config.gsda_out(True):
      @partial(pjit, in_axis_resources=FROM_GSDA, out_axis_resources=P('x', 'y'))
      def f(x):
        return x @ x.T
      expected_matrix_mul = input_data @ input_data.T

      out = f(gsda_obj)
      self.assertIsInstance(out, gsda.GlobalShardedDeviceArray)
      self.assertEqual(out.shape, (8, 8))
      self.assertEqual(out.local_shards[0].data.shape, (2, 4))
      self.assertDictEqual(out._global_mesh.shape, {'x': 4, 'y': 2})
      for s in out.local_shards:
        self.assertArraysEqual(s.data, expected_matrix_mul[s.index])

      with self.assertRaisesRegex(
          ValueError,
          ('For a non-GSDA input, the corresponding resource in '
           'in_axis_resources cannot be `pjit.FROM_GSDA`.')):
        f(input_data)

  @jtu.with_mesh([('x', 4), ('y', 2)])
  def test_pjit_gsda_multi_input_multi_output(self):
    global_mesh = create_global_mesh((4, 2), ('x', 'y'))
    global_input_shape = (8, 2)
    input_data = np.arange(
        prod(global_input_shape)).reshape(global_input_shape)
    def cb(index):
      return input_data[index]

    mesh_axes1 = P('x', 'y')
    gsda1 = gsda.GlobalShardedDeviceArray.from_callback(
        global_input_shape, global_mesh, mesh_axes1, cb)
    mesh_axes2 = P('x')
    gsda2 = gsda.GlobalShardedDeviceArray.from_callback(
        global_input_shape, global_mesh, mesh_axes2, cb)
    mesh_axes3 = P(('x', 'y'))
    gsda3 = gsda.GlobalShardedDeviceArray.from_callback(
        global_input_shape, global_mesh, mesh_axes3, cb)
    mesh_axes4 = P(None)
    gsda4 = gsda.GlobalShardedDeviceArray.from_callback(
        global_input_shape, global_mesh, mesh_axes4, cb)

    with jax._src.config.gsda_out(True):
      @partial(
          pjit,
          # `FROM_GSDA` will be replicated for all the inputs.
          in_axis_resources=FROM_GSDA,
          out_axis_resources=(mesh_axes1, mesh_axes4, mesh_axes2, mesh_axes3))
      def f(x, y, z, a):
        return x @ x.T, y, z, a
      out1, out2, out3, out4 = f(gsda1, gsda2, gsda3, gsda4)

      self.assertIsInstance(out1, gsda.GlobalShardedDeviceArray)
      self.assertEqual(out1.shape, (8, 8))
      self.assertEqual(out1.local_shards[0].data.shape, (2, 4))
      self.assertEqual(out1.local_shards[0].index, (slice(0, 2), slice(0, 4)))
      self.assertEqual(out1.local_shards[1].index, (slice(0, 2), slice(4, 8)))
      self.assertListEqual([s.replica_id for s in out1.local_shards],
                           [0, 0, 0, 0, 0, 0, 0, 0])
      expected_matrix_mul = input_data @ input_data.T
      for s in out1.local_shards:
        self.assertArraysEqual(s.data, expected_matrix_mul[s.index])

      self.assertIsInstance(out2, gsda.GlobalShardedDeviceArray)
      self.assertEqual(out2.shape, (8, 2))
      self.assertEqual(out2.local_shards[0].data.shape, (8, 2))
      self.assertEqual(out2.local_shards[0].index, (slice(None), slice(None)))
      self.assertEqual(out2.local_shards[1].index, (slice(None), slice(None)))
      self.assertListEqual([s.replica_id for s in out2.local_shards],
                           [0, 1, 2, 3, 4, 5, 6, 7])
      for s in out2.local_shards:
        self.assertArraysEqual(s.data, input_data)

      self.assertIsInstance(out3, gsda.GlobalShardedDeviceArray)
      self.assertEqual(out3.shape, (8, 2))
      self.assertEqual(out3.local_shards[0].data.shape, (2, 2))
      self.assertEqual(out3.local_shards[0].index, (slice(0, 2), slice(None)))
      self.assertEqual(out3.local_shards[1].index, (slice(0, 2), slice(None)))
      self.assertListEqual([s.replica_id for s in out3.local_shards],
                           [0, 1, 0, 1, 0, 1, 0, 1])
      for s in out3.local_shards:
        self.assertArraysEqual(s.data, input_data[s.index])

      self.assertIsInstance(out4, gsda.GlobalShardedDeviceArray)
      self.assertEqual(out4.shape, (8, 2))
      self.assertEqual(out4.local_shards[0].data.shape, (1, 2))
      self.assertEqual(out4.local_shards[0].index, (slice(0, 1), slice(None)))
      self.assertEqual(out4.local_shards[1].index, (slice(1, 2), slice(None)))
      self.assertListEqual([s.replica_id for s in out4.local_shards],
                           [0, 0, 0, 0, 0, 0, 0, 0])
      for s in out4.local_shards:
        self.assertArraysEqual(s.data, input_data[s.index])

  @jtu.with_mesh([('x', 4), ('y', 2)])
  def test_pjit_gsda_mixed_inputs(self):
    global_mesh = create_global_mesh((4, 2), ('x', 'y'))
    global_input_shape = (8, 2)
    mesh_axes = P('x', 'y')
    input_data = np.arange(
        prod(global_input_shape)).reshape(global_input_shape)
    def cb(index):
      return input_data[index]

    gsda_obj = gsda.GlobalShardedDeviceArray.from_callback(
        global_input_shape, global_mesh, mesh_axes, cb)

    with jax._src.config.gsda_out(True):
      @partial(pjit,
               in_axis_resources=(FROM_GSDA, P('x', 'y')),
               out_axis_resources=(P('x', 'y'), P(('x', 'y'))))
      def f(x, y):
        return x @ x.T, y @ y.T
      expected_matrix_mul = input_data @ input_data.T

      out1, out2 = f(gsda_obj, input_data)
      self.assertIsInstance(out1, gsda.GlobalShardedDeviceArray)
      self.assertEqual(out1.shape, (8, 8))
      self.assertEqual(out1.local_shards[0].data.shape, (2, 4))
      self.assertDictEqual(out1._global_mesh.shape, {'x': 4, 'y': 2})
      for s in out1.local_shards:
        self.assertArraysEqual(s.data, expected_matrix_mul[s.index])

      self.assertIsInstance(out2, gsda.GlobalShardedDeviceArray)
      self.assertEqual(out2.shape, (8, 8))
      self.assertEqual(out2.local_shards[0].data.shape, (1, 8))
      self.assertDictEqual(out2._global_mesh.shape, {'x': 4, 'y': 2})
      for s in out2.local_shards:
        self.assertArraysEqual(s.data, expected_matrix_mul[s.index])

  @jtu.with_mesh([('x', 2), ('y', 2)])
  def test_pjit_gsda_mesh_mismatch(self):
    global_mesh = create_global_mesh((4, 2), ('x', 'y'))
    global_input_shape = (8, 2)
    mesh_axes = ['x', 'y']
    global_input_data = np.arange(
        prod(global_input_shape), dtype=np.float32).reshape(global_input_shape)
    def cb(index):
      return global_input_data[index]

    gsda_obj = gsda.GlobalShardedDeviceArray.from_callback(
        global_input_shape, global_mesh, mesh_axes, cb)

    with self.assertRaisesRegex(
        ValueError,
        "Pjit's mesh and GSDA's mesh should be equal."):
      @partial(pjit, in_axis_resources=FROM_GSDA, out_axis_resources=P('x', 'y'))
      def f(x):
        return x
      f(gsda_obj)

  @jtu.with_mesh([('x', 4), ('y', 2)])
  def test_pjit_gsda_wrong_resource_for_gsda_input(self):
    global_mesh = create_global_mesh((4, 2), ('x', 'y'))
    global_input_shape = (8, 2)
    mesh_axes = ['x']
    global_input_data = np.arange(
        prod(global_input_shape), dtype=np.float32).reshape(global_input_shape)
    def cb(index):
      return global_input_data[index]

    gsda_obj = gsda.GlobalShardedDeviceArray.from_callback(
        global_input_shape, global_mesh, mesh_axes, cb)
    with self.assertRaisesWithLiteralMatch(
        ValueError,
        ("Got an input GSDA to pjit with different partitioning than specified "
         "in the in_axis_resources argument to pjit. The paritioning must "
         "match, or use `jax.experimental.pjit.FROM_GSDA` in `in_axis_resources`. "
         "Got GSDA spec: <partitions=(('x',),) sync=2>, "
         "pjit spec: <partitions=(('x',), ('y',)) sync=2>")):
      @partial(pjit, in_axis_resources=P('x', 'y'), out_axis_resources=P('x', 'y'))
      def f(x):
        return x
      f(gsda_obj)


def spec_regex(s):
  return str(s).replace(r"(", r"\(").replace(r")", r"\)")

class PJitErrorTest(jtu.JaxTestCase):
  @check_1d_2d_mesh(set_mesh=True)
  def testNonDivisibleArgs(self, mesh, resources):
    x = jnp.ones((3, 2))
    spec = P(resources, None)
    mesh_size = str(np.prod([dim[1] for dim in mesh], dtype=np.int64))
    with self.assertRaisesRegex(ValueError,
                                r"One of pjit arguments.*" + spec_regex(spec) + r".*"
                                r"implies that the size of its dimension 0 should be "
                                r"divisible by " + mesh_size + r", but it is equal to 3"):
      pjit(lambda x: x, in_axis_resources=spec, out_axis_resources=None)(x)

  @check_1d_2d_mesh(set_mesh=True)
  def testNonDivisibleOuts(self, mesh, resources):
    x = jnp.ones((3, 2))
    spec = P(resources, None)
    mesh_size = str(np.prod([dim[1] for dim in mesh], dtype=np.int64))
    with self.assertRaisesRegex(ValueError,
                                r"One of pjit outputs.*" + spec_regex(spec) + r".*"
                                r"implies that the size of its dimension 0 should be "
                                r"divisible by " + mesh_size + r", but it is equal to 3"):
      pjit(lambda x: x, in_axis_resources=None, out_axis_resources=P(resources, None))(x)

  @check_1d_2d_mesh(set_mesh=True)
  def testNonDivisibleConstraint(self, mesh, resources):
    x = jnp.ones((3, 2))
    spec = P(resources,)
    mesh_size = str(np.prod([dim[1] for dim in mesh], dtype=np.int64))
    with self.assertRaisesRegex(ValueError,
                                r"One of with_sharding_constraint arguments"
                                r".*" + spec_regex(spec) + r".*implies that the size of "
                                r"its dimension 0 should be divisible by " + mesh_size +
                                r", but it is equal to 3"):
      pjit(lambda x: with_sharding_constraint(x, spec),
           in_axis_resources=None, out_axis_resources=None)(x)

  @check_1d_2d_mesh(set_mesh=False)
  @jtu.with_mesh([('z', 1)])
  def testUndefinedResourcesArgs(self, mesh, resources):
    x = jnp.ones((2, 2))
    spec = P(resources,)
    with self.assertRaisesRegex(ValueError,
                                r"One of pjit arguments.*" + spec_regex(spec) + r", "
                                r"but resource axis x is undefined."):
      pjit(lambda x: x, in_axis_resources=spec, out_axis_resources=None)(x)

  @check_1d_2d_mesh(set_mesh=False)
  @jtu.with_mesh([('z', 1)])
  def testUndefinedResourcesOuts(self, mesh, resources):
    x = jnp.ones((2, 2))
    spec = P(resources,)
    with self.assertRaisesRegex(ValueError,
                                r"One of pjit outputs.*" + spec_regex(spec) + r", "
                                r"but resource axis x is undefined."):
      pjit(lambda x: x, in_axis_resources=None, out_axis_resources=spec)(x)

  @check_1d_2d_mesh(set_mesh=False)
  @jtu.with_mesh([('z', 1)])
  def testUndefinedResourcesConstraint(self, mesh, resources):
    x = jnp.ones((2, 2))
    spec = P(resources,)
    with self.assertRaisesRegex(ValueError,
                                r"One of with_sharding_constraint arguments"
                                r".*" + spec_regex(spec) + r", but resource axis "
                                r"x is undefined."):
      pjit(lambda x: with_sharding_constraint(x, spec),
           in_axis_resources=None, out_axis_resources=None)(x)

  @jtu.with_mesh([('x', 2), ('y', 1)])
  def testRankTooLowArgs(self):
    x = jnp.arange(2)
    spec = P('x', 'y')
    error = (r"One of pjit arguments.*" + spec_regex(spec) + r", which implies "
             r"that it has a rank of at least 2, but it is 1")
    with self.assertRaisesRegex(ValueError, error):
      pjit(lambda x: x.sum(), in_axis_resources=spec, out_axis_resources=None)(x)

  @jtu.with_mesh([('x', 2), ('y', 1)])
  def testRankTooLowOuts(self):
    x = jnp.arange(2)
    spec = P('x', 'y')
    error = (r"One of pjit outputs.*" + spec_regex(spec) + r", which implies "
             r"that it has a rank of at least 2, but it is 0")
    with self.assertRaisesRegex(ValueError, error):
      pjit(lambda x: x.sum(), in_axis_resources=None, out_axis_resources=spec)(x)

  @jtu.with_mesh([('x', 2), ('y', 1)])
  def testRankTooLowConstraint(self):
    x = jnp.arange(2)
    spec = P('x', 'y')
    error = (r"One of with_sharding_constraint arguments " +
             r"was given.*" + spec_regex(spec) + r", which implies "
             r"that it has a rank of at least 2, but it is 1")
    with self.assertRaisesRegex(ValueError, error):
      pjit(lambda x: with_sharding_constraint(x, spec),
           in_axis_resources=None, out_axis_resources=None)(x)

  @jtu.with_mesh([('x', 2), ('y', 1)])
  def testRepeatedInResources(self):
    x = jnp.arange(2)
    for spec in [P('x', 'x'), P('x', ('y', 'x'))]:
      error = (r"A single in_axis_resources specification can map every mesh "
               r"axis to at most one positional dimension, but " +
               spec_regex(spec) + " has duplicate entries for `x`")
      with self.assertRaisesRegex(ValueError, error):
        pjit(lambda x: x, in_axis_resources=spec, out_axis_resources=None)(x)

  @jtu.with_mesh([('x', 2), ('y', 1)])
  def testRepeatedOutResources(self):
    x = jnp.arange(2)
    for spec in [P('x', 'x'), P('x', ('y', 'x'))]:
      error = (r"A single out_axis_resources specification can map every mesh "
               r"axis to at most one positional dimension, but " +
               spec_regex(spec) + " has duplicate entries for `x`")
      with self.assertRaisesRegex(ValueError, error):
        pjit(lambda x: x, in_axis_resources=None, out_axis_resources=spec)(x)

  @jtu.with_mesh([('x', 2)])
  def testInputShardsXMapAxis(self):
    spec = P('x')
    f = xmap(pjit(lambda x: x + 2, in_axis_resources=spec, out_axis_resources=None),
             in_axes=['i', ...], out_axes=['i', ...], axis_resources={'i': 'x'})
    x = jnp.arange(4).reshape((2, 2))
    error = (r"pjit input has an axis resources specification of " +
             spec_regex(spec) + r" that uses one or more mesh axes already used by "
             r"xmap to partition a named axis appearing in its named_shape \(both "
             r"use mesh axes `x`\)")
    with self.assertRaisesRegex(JAXTypeError, error):
      f(x)

  @jtu.with_mesh([('x', 2)])
  def testOutputShardsXMapAxis(self):
    spec = P('x')
    f = xmap(pjit(lambda x: x + 2, in_axis_resources=None, out_axis_resources=spec),
             in_axes=['i', ...], out_axes=['i', ...], axis_resources={'i': 'x'})
    x = jnp.arange(4).reshape((2, 2))
    error = (r"pjit output has an axis resources specification of " +
             spec_regex(spec) + r" that uses one or more mesh axes already used by "
             r"xmap to partition a named axis appearing in its named_shape \(both "
             r"use mesh axes `x`\)")
    with self.assertRaisesRegex(JAXTypeError, error):
      f(x)

  @jtu.with_mesh([('x', 2)])
  def testConstraintShardsXMapAxis(self):
    spec = P('x')
    f = xmap(lambda x: with_sharding_constraint(x, axis_resources=spec),
             in_axes=['i', ...], out_axes=['i', ...], axis_resources={'i': 'x'})
    x = jnp.arange(4).reshape((2, 2))
    error = (r"with_sharding_constraint input has an axis resources specification of " +
             spec_regex(spec) + r" that uses one or more mesh axes already used by "
             r"xmap to partition a named axis appearing in its named_shape \(both "
             r"use mesh axes `x`\)")
    with self.assertRaisesRegex(JAXTypeError, error):
      f(x)

  @jtu.with_mesh([('x', 2)])
  def testCatchesInnerXMapErrors(self):
    f = pjit(xmap(lambda x, y: x, in_axes=(['i'], ['j']), out_axes=['i', 'j'],
                  axis_resources={'i': 'x', 'j': 'x'}),
             in_axis_resources=None, out_axis_resources=None)
    x = jnp.arange(4)
    with self.assertRaises(JAXTypeError):
      f(x, x)

  def testEmptyMesh(self):
    error = (r"pjit requires a non-empty mesh! Are you sure that it's defined "
             r"at the call site?")
    with self.assertRaisesRegex(RuntimeError, error):
      pjit(lambda x: x, in_axis_resources=None, out_axis_resources=None)(jnp.arange(4))

  @jtu.with_mesh([('x', 2)])
  def testAxisResourcesMismatch(self):
    x = jnp.ones([])
    p = [None, None, None]
    pjit(lambda x: x, (p,), p)([x, x, x])  # OK
    error = re.escape(
        r"pjit in_axis_resources specification must be a tree prefix of the "
        r"corresponding value, got specification (None, None, None) for value "
        r"tree PyTreeDef((*, *)). Note that pjit in_axis_resources that are "
        r"non-trivial pytrees should always be wrapped in a tuple representing "
        r"the argument list.")
    with self.assertRaisesRegex(ValueError, error):
      pjit(lambda x, y: x, p, p)(x, x)  # Error, but make sure we hint at tupling
    # TODO(apaszke): Disable implicit list casts and enable this
    # error = re.escape(
    # r"pjit in_axis_resources specification must be a tree prefix of the "
    # r"corresponding value, got specification (None, None, None) for value "
    # r"tree PyTreeDef(([*, *, *],)). Note that pjit in_axis_resources that "
    # r"are non-trivial pytrees should always be wrapped in a tuple representing "
    # r"the argument list. In particular, you're passing in a single argument "
    # r"which means that pjit in_axis_resources might need to be wrapped in a "
    # r"singleton tuple.")
    # with self.assertRaisesRegex(ValueError, error):
    # pjit(lambda x: x, p, p)([x, x, x])  # Error, but make sure we hint at singleton tuple
    error = re.escape(
        r"pjit out_axis_resources specification must be a tree prefix of the "
        r"corresponding value, got specification [[None, None, None], None] for "
        r"value tree PyTreeDef([*, *, *]).")
    with self.assertRaisesRegex(ValueError, error):
      pjit(lambda x: x, (p,), [p, None])([x, x, x])  # Error, we raise a generic tree mismatch message

  @jtu.with_mesh([('x', 2)])
  def testNestedDifferentResources(self):
    @partial(pjit, in_axis_resources=P('x'), out_axis_resources=None)
    def f(x):
      with mesh(np.array([jax.local_devices()[0]]), ('x')):
        @partial(pjit, in_axis_resources=P('x'), out_axis_resources=None)
        def h(x):
          return x
        return h(x)
    xshape = (2, 5, 6)
    x = jnp.arange(np.prod(xshape)).reshape(xshape)
    with self.assertRaisesRegex(RuntimeError,
                                "Changing the physical mesh is not allowed.*"):
      f(x)


class UtilTest(jtu.JaxTestCase):

  def testOpShardingRoundTrip(self):
    FakeDevice = namedtuple('FakeDevice', ['id'])
    mesh_named_shape = OrderedDict([('a', 2), ('b', 3), ('c', 4), ('d', 7), ('e', 4)])
    mesh_axes, mesh_shape = unzip2(mesh_named_shape.items())
    devices = [FakeDevice(i) for i in range(np.prod(list(mesh_shape)))]
    mesh = pxla.Mesh(np.array(devices).reshape(*mesh_shape), tuple(mesh_axes))

    dims = 5
    aval = jax.core.ShapedArray((len(devices),) * dims, jnp.float32)
    def roundtrip(spec):
      op_sharding = pjit_lib.get_aval_sharding_proto(aval, spec, mesh)
      parsed_spec = pjit_lib.parse_op_sharding(op_sharding, mesh).partitions
      self.assertEqual(parsed_spec[:len(spec)], spec)
      self.assertEqual(parsed_spec[len(spec):], ((),) * (len(parsed_spec) - len(spec)))

    special_specs = [P()]
    for spec in special_specs:
      roundtrip(spec)

    rng = np.random.default_rng(1)
    for i in range(100):
      spec = [()] * dims
      for axis in rng.permutation(mesh_axes)[:rng.integers(low=1, high=len(mesh_axes) + 1)]:
        spec[rng.choice(dims)] += (axis,)
      roundtrip(P(*spec))

  @parameterized.named_parameters(
      ("linear", {'x': 0, 'y': 1, 'z': 2}, (('x',), ('y',), ('z',))),
      ("combine", {'x': 0, 'y': 0, 'z': 1}, (('x', 'y'), ('z',))),
      ("skip", {'x': 0, 'y': 0, 'z': 2}, (('x', 'y'), None, ('z',))),
      ("multi_skip", {'x': 0, 'y': 1, 'z': 3}, (('x',), ('y',), None, ('z',))),
  )
  def test_array_mapping_to_axis_resources(self, inp, expected_out):
    self.assertEqual(pxla.array_mapping_to_axis_resources(inp), expected_out)

if __name__ == '__main__':
  absltest.main(testLoader=jtu.JaxTestLoader())
