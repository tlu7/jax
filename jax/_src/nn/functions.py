# Copyright 2019 Google LLC
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

"""Shared neural network activations and other functions."""


import operator
import numpy as np
from typing import Any, Optional, Tuple, Union

from jax import custom_jvp
from jax._src import dtypes
from jax import lax
from jax import core
from jax.core import AxisName
from .. import util
from jax.scipy.special import expit
from jax.scipy.special import logsumexp as _logsumexp
import jax.numpy as jnp

Array = Any

# activations

@custom_jvp
def relu(x: Array) -> Array:
  r"""Rectified linear unit activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{relu}(x) = \max(x, 0)

  Args:
    x : input array
  """
  return jnp.maximum(x, 0)
relu.defjvps(lambda g, ans, x: lax.select(x > 0, g, lax.full_like(g, 0)))

def softplus(x: Array) -> Array:
  r"""Softplus activation function.

  Computes the element-wise function

  .. math::
    \mathrm{softplus}(x) = \log(1 + e^x)

  Args:
    x : input array
  """
  return jnp.logaddexp(x, 0)

def soft_sign(x: Array) -> Array:
  r"""Soft-sign activation function.

  Computes the element-wise function

  .. math::
    \mathrm{soft\_sign}(x) = \frac{x}{|x| + 1}

  Args:
    x : input array
  """
  return x / (jnp.abs(x) + 1)

def sigmoid(x: Array) -> Array:
  r"""Sigmoid activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{sigmoid}(x) = \frac{1}{1 + e^{-x}}

  Args:
    x : input array
  """
  return expit(x)

def silu(x: Array) -> Array:
  r"""SiLU activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{silu}(x) = x \cdot \mathrm{sigmoid}(x) = \frac{x}{1 + e^{-x}}

  Args:
    x : input array
  """
  return x * sigmoid(x)

swish = silu

def log_sigmoid(x: Array) -> Array:
  r"""Log-sigmoid activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{log\_sigmoid}(x) = \log(\mathrm{sigmoid}(x)) = -\log(1 + e^{-x})

  Args:
    x : input array
  """
  return -softplus(-x)

def elu(x: Array, alpha: Array = 1.0) -> Array:
  r"""Exponential linear unit activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{elu}(x) = \begin{cases}
      x, & x > 0\\
      \alpha \left(\exp(x) - 1\right), & x \le 0
    \end{cases}

  Args:
    x : input array
    alpha : scalar or array of alpha values (default: 1.0)
  """
  safe_x = jnp.where(x > 0, 0., x)
  return jnp.where(x > 0, x, alpha * jnp.expm1(safe_x))

def leaky_relu(x: Array, negative_slope: Array = 1e-2) -> Array:
  r"""Leaky rectified linear unit activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{leaky\_relu}(x) = \begin{cases}
      x, & x \ge 0\\
      \alpha x, & x < 0
    \end{cases}

  where :math:`\alpha` = :code:`negative_slope`.

  Args:
    x : input array
    negative_slope : array or scalar specifying the negative slope (default: 0.01)
  """
  return jnp.where(x >= 0, x, negative_slope * x)

def hard_tanh(x: Array) -> Array:
  r"""Hard :math:`\mathrm{tanh}` activation function.

  Computes the element-wise function:

  .. math::
    \mathrm{hard\_tanh}(x) = \begin{cases}
      -1, & x < -1\\
      x, & -1 \le x \le 1\\
      1, & 1 < x
    \end{cases}

  Args:
    x : input array
  """
  return jnp.where(x > 1, 1, jnp.where(x < -1, -1, x))

def celu(x: Array, alpha: Array = 1.0) -> Array:
  r"""Continuously-differentiable exponential linear unit activation.

  Computes the element-wise function:

  .. math::
    \mathrm{celu}(x) = \begin{cases}
      x, & x > 0\\
      \alpha \left(\exp(\frac{x}{\alpha}) - 1\right), & x \le 0
    \end{cases}

  For more information, see
  `Continuously Differentiable Exponential Linear Units
  <https://arxiv.org/pdf/1704.07483.pdf>`_.

  Args:
    x : input array
    alpha : array or scalar (default: 1.0)
  """
  return jnp.where(x > 0, x, alpha * jnp.expm1(x / alpha))

def selu(x: Array) -> Array:
  r"""Scaled exponential linear unit activation.

  Computes the element-wise function:

  .. math::
    \mathrm{selu}(x) = \lambda \begin{cases}
      x, & x > 0\\
      \alpha e^x - \alpha, & x \le 0
    \end{cases}

  where :math:`\lambda = 1.0507009873554804934193349852946` and
  :math:`\alpha = 1.6732632423543772848170429916717`.

  For more information, see
  `Self-Normalizing Neural Networks
  <https://papers.nips.cc/paper/6698-self-normalizing-neural-networks.pdf>`_.

  Args:
    x : input array
  """
  alpha = 1.6732632423543772848170429916717
  scale = 1.0507009873554804934193349852946
  return scale * elu(x, alpha)

def gelu(x: Array, approximate: bool = True) -> Array:
  r"""Gaussian error linear unit activation function.

  If ``approximate=False``, computes the element-wise function:

  .. math::
    \mathrm{gelu}(x) = \frac{x}{2} \left(1 + \mathrm{erf} \left(
      \frac{x}{\sqrt{2}} \right) \right)

  If ``approximate=True``, uses the approximate formulation of GELU:

  .. math::
    \mathrm{gelu}(x) = \frac{x}{2} \left(1 + \mathrm{tanh} \left(
      \sqrt{\frac{2}{\pi}} \left(x + 0.044715 x^3 \right) \right) \right)

  For more information, see `Gaussian Error Linear Units (GELUs)
  <https://arxiv.org/abs/1606.08415>`_, section 2.

  Args:
    x : input array
    approximate: whether to use the approximate or exact formulation.
  """
  if approximate:
    sqrt_2_over_pi = np.sqrt(2 / np.pi).astype(x.dtype)
    cdf = 0.5 * (1.0 + jnp.tanh(sqrt_2_over_pi * (x + 0.044715 * (x ** 3))))
    return x * cdf
  else:
    return jnp.array(x * (lax.erf(x / np.sqrt(2)) + 1) / 2, dtype=x.dtype)

def glu(x: Array, axis: int = -1) -> Array:
  """Gated linear unit activation function.

  Args:
    x : input array
    axis: the axis along which the split should be computed (default: -1)
  """
  size = x.shape[axis]
  assert size % 2 == 0, "axis size must be divisible by 2"
  x1, x2 = jnp.split(x, 2, axis)
  return x1 * sigmoid(x2)

# other functions

logsumexp = _logsumexp


def log_softmax(x: Array, axis: Optional[Union[int, Tuple[int, ...]]] = -1) -> Array:
  r"""Log-Softmax function.

  Computes the logarithm of the :code:`softmax` function, which rescales
  elements to the range :math:`[-\infty, 0)`.

  .. math ::
    \mathrm{log\_softmax}(x) = \log \left( \frac{\exp(x_i)}{\sum_j \exp(x_j)}
    \right)

  Args:
    x : input array
    axis: the axis or axes along which the :code:`log_softmax` should be
      computed. Either an integer or a tuple of integers.
  """
  shifted = x - lax.stop_gradient(x.max(axis, keepdims=True))
  return shifted - jnp.log(jnp.sum(jnp.exp(shifted), axis, keepdims=True))

def softmax(x: Array, axis: Optional[Union[int, Tuple[int, ...]]] = -1) -> Array:
  r"""Softmax function.

  Computes the function which rescales elements to the range :math:`[0, 1]`
  such that the elements along :code:`axis` sum to :math:`1`.

  .. math ::
    \mathrm{softmax}(x) = \frac{\exp(x_i)}{\sum_j \exp(x_j)}

  Args:
    x : input array
    axis: the axis or axes along which the softmax should be computed. The
      softmax output summed across these dimensions should sum to :math:`1`.
      Either an integer or a tuple of integers.
  """
  unnormalized = jnp.exp(x - lax.stop_gradient(x.max(axis, keepdims=True)))
  return unnormalized / unnormalized.sum(axis, keepdims=True)

def normalize(x: Array,
              axis: Optional[Union[int, Tuple[int, ...]]] = -1,
              mean: Optional[Array] = None,
              variance: Optional[Array] = None,
              epsilon: Array = 1e-5) -> Array:
  """Normalizes an array by subtracting mean and dividing by sqrt(var)."""
  if mean is None:
    mean = jnp.mean(x, axis, keepdims=True)
  if variance is None:
    # this definition is traditionally seen as less accurate than jnp.var's
    # mean((x - mean(x))**2) but may be faster and even, given typical
    # activation distributions and low-precision arithmetic, more accurate
    # when used in neural network normalization layers
    variance = jnp.mean(jnp.square(x), axis, keepdims=True) - jnp.square(mean)
  return (x - mean) * lax.rsqrt(variance + epsilon)

def one_hot(x: Array, num_classes: int, *,
            dtype: Any = jnp.float_, axis: Union[int, AxisName] = -1) -> Array:
  """One-hot encodes the given indicies.

  Each index in the input ``x`` is encoded as a vector of zeros of length
  ``num_classes`` with the element at ``index`` set to one::

    >>> jax.nn.one_hot(jnp.array([0, 1, 2]), 3)
    DeviceArray([[1., 0., 0.],
                  [0., 1., 0.],
                  [0., 0., 1.]], dtype=float32)

  Indicies outside the range [0, num_classes) will be encoded as zeros::

    >>> jax.nn.one_hot(jnp.array([-1, 3]), 3)
    DeviceArray([[0., 0., 0.],
                 [0., 0., 0.]], dtype=float32)

  Args:
    x: A tensor of indices.
    num_classes: Number of classes in the one-hot dimension.
    dtype: optional, a float dtype for the returned values (default :obj:`jnp.float_`).
    axis: the axis or axes along which the function should be
      computed.
  """
  num_classes = core.concrete_or_error(
      int, num_classes,
      "The error arose in jax.nn.one_hot argument `num_classes`.")
  dtype = dtypes.canonicalize_dtype(dtype)
  x = jnp.asarray(x)
  try:
    output_pos_axis = util.canonicalize_axis(axis, x.ndim + 1)
  except TypeError:
    axis_size = lax.psum(1, axis)
    if num_classes != axis_size:
      raise ValueError(f"Expected num_classes to match the size of axis {axis}, "
                       f"but {num_classes} != {axis_size}") from None
    axis_idx = lax.axis_index(axis)
    return jnp.asarray(x == axis_idx, dtype=dtype)
  axis = operator.index(axis)
  lhs = lax.expand_dims(x, (axis,))
  rhs_shape = [1] * x.ndim
  rhs_shape.insert(output_pos_axis, num_classes)
  rhs = lax.broadcast_in_dim(jnp.arange(num_classes, dtype=x.dtype),
                             rhs_shape, (output_pos_axis,))
  return jnp.asarray(lhs == rhs, dtype=dtype)

def relu6(x: Array) -> Array:
  r"""Rectified Linear Unit 6 activation function.

  Computes the element-wise function

  .. math::
    \mathrm{relu6}(x) = \min(\max(x, 0), 6)

  Args:
    x : input array
  """
  return jnp.minimum(jnp.maximum(x, 0), 6.)

def hard_sigmoid(x: Array) -> Array:
  r"""Hard Sigmoid activation function.

  Computes the element-wise function

  .. math::
    \mathrm{hard\_sigmoid}(x) = \frac{\mathrm{relu6}(x + 3)}{6}

  Args:
    x : input array
  """
  return relu6(x + 3.) / 6.

def hard_silu(x: Array) -> Array:
  r"""Hard SiLU activation function

  Computes the element-wise function

  .. math::
    \mathrm{hard\_silu}(x) = x \cdot \mathrm{hard\_sigmoid}(x)

  Args:
    x : input array
  """
  return x * hard_sigmoid(x)

hard_swish = hard_silu
