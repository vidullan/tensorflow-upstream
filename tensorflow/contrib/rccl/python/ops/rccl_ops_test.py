# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Tests for rccl ops. See also the cc test for rccl_communicator."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from functools import partial
import numpy as np

from tensorflow.contrib import rccl
from tensorflow.python.framework import errors
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import gradients
from tensorflow.python.platform import test


def _DeviceTensors(tensors, devices):
  res = []
  for t, d in zip(tensors, devices):
    with ops.device(d):
      res.append(array_ops.identity(t))
  return res


def _RcclAllReduce(rccl_fun, tensors, devices):
  return rccl_fun(_DeviceTensors(tensors, devices))


def _RcclReduce(rccl_fun, tensors, devices):
  receiver = np.random.randint(0, len(devices))
  with ops.device(devices[receiver]):
    return [rccl_fun(_DeviceTensors(tensors, devices))]


def _RcclBroadcast(tensors, devices):
  sender = np.random.randint(0, len(devices))
  with ops.device(devices[sender]):
    tensor = array_ops.identity(tensors[0])
    broadcast = rccl.broadcast(tensor)
  return _DeviceTensors([broadcast] * len(devices), devices)


class RcclTestCase(test.TestCase):

  def assertNotEmpty(self, obj):
    self.assertTrue(obj)

  def _Test(self,
            rccl_reduce,
            numpy_fn,
            device_sets=(['/device:GPU:1', '/device:GPU:2', '/device:GPU:0'],
                         ['/device:GPU:1', '/device:GPU:0'])):
    """Tests that rccl_reduce does the same as reduction with numpy_fn.

    Args:
      rccl_reduce: A function taking a list of tensors and a list of devices,
          and returns a list of reduced tensors and a list of ops to perform the
          reduction.
      numpy_fn: A function taking two tensors and returning the reduction of the
          two.
      device_sets: Tuple of virtual devices to run test on.
    """
    for dtype in [np.float16, np.float32, np.int32, np.int64, np.float64]:
      # Create session inside outer loop to test use of
      # same communicator across multiple sessions.
      with self.test_session(use_gpu=True) as sess:

        for devices in device_sets:
          shape = (3, 4)
          random = (np.random.random_sample(shape) - .5) * 1024
          tensors = []
          for _ in devices:
            tensors.append(random.astype(dtype))
          np_ans = tensors[0]
          for t in tensors[1:]:
            np_ans = numpy_fn(np_ans, t)

          reduce_tensors = rccl_reduce(tensors, devices)
          self.assertNotEmpty(reduce_tensors)

          # Test shape inference.
          for r in reduce_tensors:
            self.assertEqual(shape, r.get_shape())

          result_tensors = [array_ops.identity(t) for t in reduce_tensors]

          # Check GPU availability *after* creating session, see b/68975239.
          if not test.is_gpu_available():
            # If no GPU is available, only test graph construction.
            continue

          # Test execution and results.
          for t in sess.run(result_tensors):
            self.assertAllClose(t, np_ans)

  def _TestGradient(self, rccl_reduce, numpy_fn):
    """Tests the gradient of rccl_reduce.

    Args:
      rccl_reduce: A function taking a list of tensors and a list of devices,
          and returns a list of reduced tensors and a list of ops to perform the
          reduction.
      numpy_fn: A function taking two tensors and returning the gradient of the
          reduction of the two.
    """

    def _Gradient(tensors, devices):
      inputs = [array_ops.placeholder(t.dtype, t.shape) for t in tensors]
      reduce_tensors = rccl_reduce(inputs, devices)
      losses = _DeviceTensors(tensors, [t.device for t in reduce_tensors])
      grads = gradients.gradients(
          reduce_tensors, inputs, losses, colocate_gradients_with_ops=True)
      return [g for g in grads if g is not None]

    self._Test(_Gradient, numpy_fn)


class AllReduceTest(RcclTestCase):

  def testAllReduce(self):
    self._Test(partial(_RcclAllReduce, rccl.all_sum), lambda x, y: x + y)
    self._Test(partial(_RcclAllReduce, rccl.all_prod), lambda x, y: x * y)
    self._Test(partial(_RcclAllReduce, rccl.all_min), np.minimum)
    self._Test(partial(_RcclAllReduce, rccl.all_max), np.maximum)

  def testAllSumGrad(self):
    self._TestGradient(
        partial(_RcclAllReduce, rccl.all_sum), lambda x, y: x + y)

  def testErrors(self):
    with self.assertRaisesRegexp(ValueError, 'Device assignment required'):
      rccl.all_sum([array_ops.identity(np.random.random_sample((3, 4)))])
    with self.assertRaisesRegexp(ValueError, 'Must pass >0 tensors'):
      rccl.all_sum([])


class SingleReduceTest(RcclTestCase):

  def testSum(self):
    self._Test(partial(_RcclReduce, rccl.reduce_sum), lambda x, y: x + y)

  def testSumGrad(self):
    self._TestGradient(partial(_RcclReduce, rccl.reduce_sum), lambda x, y: x)


class BroadcastTest(RcclTestCase):

  def testBroadcast(self):
    self._Test(_RcclBroadcast, lambda x, y: x)

  def testBroadcastSingleDevice(self):
    # Broadcasts on a single device are removed completely during rewrite.
    self._Test(_RcclBroadcast, lambda x, y: x,
               (['/device:GPU:0', '/device:GPU:0'],))

  def testBroadcastToCpuError(self):
    try:
      # Broadcasts to CPU is not supported.
      self._Test(_RcclBroadcast, lambda x, y: x,
                 (['/device:GPU:0', '/device:CPU:0'],))
    except errors.NotFoundError as e:
      self.assertRegexpMatches(
          str(e), "No registered '_RcclBroadcastRecv' OpKernel for CPU devices")
    else:
      # Session isn't executed when no GPU is available.
      if test.is_gpu_available():
        self.fail("Didn't raise NotFoundError trying to broadcast to CPU")


class CombinedTest(RcclTestCase):
  """Test all-reduce vs. single-reduce plus broadcast in one session.run."""

  def _Combined(self, tensors, devices):
    all_reduce_tensors = _RcclAllReduce(rccl.all_sum, tensors, devices)
    single_reduce_tensors = _RcclReduce(rccl.reduce_sum, tensors, devices)
    broadcast_tensors = _RcclBroadcast(single_reduce_tensors, devices)
    return all_reduce_tensors + broadcast_tensors

  def testCombined(self):
    self._Test(self._Combined, lambda x, y: x + y)


if __name__ == '__main__':
  test.main()
