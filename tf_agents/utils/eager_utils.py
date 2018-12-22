# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Common utilities for TF-Agents.

Example of usage:

  ```python
  from tf_agents.utils import eager_utils

  @eager_utils.run_in_graph_and_eager_modes
  def loss_fn(x, y):
    v = tf.get_variable('v', initializer=tf.ones_initializer(), shape=())
    return v + x - y

  with tfe.graph_mode():
    # loss and train_step are Tensors/Ops in the graph
    loss_op = loss_fn(inputs, labels)
    train_step_op = eager_utils.create_train_step(loss_op, optimizer)
    # Compute the loss and apply gradients to the variables using the optimizer.
    with tf.Session() as sess:
      sess.run(tf.global_variables_initializer())
      for _ in range(num_train_steps):
        loss_value = sess.run(train_step_op)

  with tfe.eager_mode():
    # loss and train_step are lambda functions that can be called.
    loss = loss_fn(inputs, labels)
    train_step = eager_utils.create_train_step(loss, optimizer)
    # Compute the loss and apply gradients to the variables using the optimizer.
    for _ in range(num_train_steps):
      loss_value = train_step()
  ```
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import functools
import inspect
import numpy as np

import six
import tensorflow as tf

from tensorflow.python.util import tf_decorator  # TF internal

nest = tf.contrib.framework.nest

_USE_GLOBAL_STEP = 0


def has_self_cls_arg(func_or_method):
  """Checks if it is method which takes self/cls as the first argument."""
  if isinstance(func_or_method, staticmethod):
    return False
  if inspect.ismethod(func_or_method):
    return True
  if isinstance(func_or_method, classmethod):
    return True
  if six.PY2:
    arg_names = inspect.getargspec(func_or_method).args
  else:
    arg_names = list(inspect.signature(func_or_method).parameters)
  if arg_names and arg_names[0] in ('self', 'cls'):
    return True
  return False


def is_unbound(method):
  """Checks if it is an unbounded method."""
  return not (hasattr(method, '__self__') and method.__self__)


class Future(object):
  """Converts a function or class method call into a future callable."""

  def __init__(self, func_or_method, *args, **kwargs):
    self._func_or_method = func_or_method
    self._args = args
    self._kwargs = copy.copy(kwargs)
    getargspec = inspect.getargspec if six.PY2 else inspect.getfullargspec
    arg_names = getargspec(func_or_method).args
    self._arg_names = arg_names
    self._self = None
    if has_self_cls_arg(func_or_method):
      # Skip the first arg_name self/cls
      self._arg_names = arg_names[1:]
      if is_unbound(func_or_method):
        # For unbound methods we require self/cls as args[0].
        if not args:
          raise ValueError(
              'func_or_method is unbond, but not class/instance provided.')
        else:
          self._self = args[0]
          self._args = args[1:]

  def __call__(self, *args, **kwargs):
    """If *args/**kwargs are given they would replace those given at init.

    Args:
      *args: List of extra arguments.
      **kwargs: Dict of extra keyword arguments.
    Returns:
      The result of func_or_method(*args, **kwargs).
    """
    # By default use the init args.
    call_args = args or self._args
    call_kwargs = copy.copy(self._kwargs)
    for arg_name in self._arg_names[:len(args)]:
      # Remove any original kwargs replaced by new positional args.
      call_kwargs.pop(arg_name, None)
    call_kwargs.update(kwargs)
    if self._self:
      return self._func_or_method(self._self, *call_args, **call_kwargs)
    return self._func_or_method(*call_args, **call_kwargs)


def future_in_eager_mode(func_or_method):
  """Decorator that allow a function/method to run in graph and in eager modes.

  When applied in graph mode it calls the function and return its outputs.
  When applied in eager mode it returns a lambda function that when called
  returns the outputs.

  ```python
  @eager_utils.future_in_eager_mode
  def loss_fn(x):
    v = tf.get_variable('v', initializer=tf.ones_initializer(), shape=())
    return v + x

  with context.graph_mode():
    loss_op = loss_fn(inputs)
    loss_value = sess.run(loss_op)

  with context.eager_mode():
    loss = loss_fn(inputs)
    # Now loss is a Future callable.
    loss_value = loss()

  Args:
    func_or_method: A function or method to decorate.

  Returns:
    Either the output ops of the function/method or a Future (lambda function).
  """
  if not callable(func_or_method):
    raise TypeError('func_or_method must be callable.')

  def decorator(*args, **kwargs):
    if tf.executing_eagerly():
      return Future(func_or_method, *args, **kwargs)
    else:
      return func_or_method(*args, **kwargs)

  return tf_decorator.make_decorator(func_or_method, decorator)


def add_gradients_summaries(grads_and_vars):
  """Add summaries to gradients.

  Args:
    grads_and_vars: A list of gradient to variable pairs (tuples).
  """
  for grad, var in grads_and_vars:
    if grad is not None:
      if isinstance(grad, tf.IndexedSlices):
        grad_values = grad.values
      else:
        grad_values = grad
      var_name = var.name.replace(':', '_')
      tf.contrib.summary.histogram(var_name + '_gradient', grad_values)
      tf.contrib.summary.scalar(var_name + '_gradient_norm',
                                tf.global_norm([grad_values]))
    else:
      tf.logging.info('Var %s has no gradient', var.name)


def create_train_step(loss,
                      optimizer,
                      global_step=_USE_GLOBAL_STEP,
                      total_loss_fn=None,
                      update_ops=None,
                      variables_to_train=None,
                      transform_grads_fn=None,
                      summarize_gradients=False,
                      gate_gradients=tf.train.Optimizer.GATE_OP,
                      aggregation_method=None,
                      colocate_gradients_with_ops=False,
                      check_numerics=True):
  """Creates a train_step that evaluates the gradients and returns the loss.

  Args:
    loss: A (possibly nested tuple of) `Tensor` or function representing
      the loss.
    optimizer: A tf.Optimizer to use for computing the gradients.
    global_step: A `Tensor` representing the global step variable. If left as
      `_USE_GLOBAL_STEP`, then tf.contrib.framework.global_step() is used.
    total_loss_fn: Function to call on loss value to access the final
     item to minimize.
    update_ops: An optional list of updates to execute. If `update_ops` is
      `None`, then the update ops are set to the contents of the
      `tf.GraphKeys.UPDATE_OPS` collection. If `update_ops` is not `None`, but
      it doesn't contain all of the update ops in `tf.GraphKeys.UPDATE_OPS`,
      a warning will be displayed.
    variables_to_train: an optional list of variables to train. If None, it will
      default to all tf.trainable_variables().
    transform_grads_fn: A function which takes a single argument, a list of
      gradient to variable pairs (tuples), performs any requested gradient
      updates, such as gradient clipping or multipliers, and returns the updated
      list.
    summarize_gradients: Whether or not add summaries for each gradient.
    gate_gradients: How to gate the computation of gradients. See tf.Optimizer.
    aggregation_method: Specifies the method used to combine gradient terms.
      Valid values are defined in the class `AggregationMethod`.
    colocate_gradients_with_ops: Whether or not to try colocating the gradients
      with the ops that generated them.
    check_numerics: Whether or not we apply check_numerics.

  Returns:
    In graph mode: A (possibly nested tuple of) `Tensor` that when evaluated,
      calculates the current loss, computes the gradients, applies the
      optimizer, and returns the current loss.
    In eager mode: A lambda function that when is called, calculates the loss,
      then computes and applies the gradients and returns the original
      loss values.
  Raises:
    ValueError: if loss is not callable.
  """
  if total_loss_fn is None:
    total_loss_fn = lambda x: x
  if not callable(total_loss_fn):
    raise ValueError('`total_loss_fn` should be a function.')

  if not tf.executing_eagerly():
    if callable(loss):
      loss = loss()
    if callable(variables_to_train):
      variables_to_train = variables_to_train()
    # Calculate loss first, then calculate train op, then return the original
    # loss conditioned on executing the train op.
    with tf.control_dependencies(nest.flatten(loss)):
      train_op = tf.contrib.training.create_train_op(
          total_loss_fn(loss),
          optimizer,
          global_step=global_step,
          update_ops=update_ops,
          variables_to_train=variables_to_train,
          transform_grads_fn=transform_grads_fn,
          summarize_gradients=summarize_gradients,
          gate_gradients=gate_gradients,
          aggregation_method=aggregation_method,
          colocate_gradients_with_ops=colocate_gradients_with_ops,
          check_numerics=check_numerics)
    with tf.control_dependencies([train_op]):
      return nest.map_structure(lambda t: tf.identity(t, 'loss'), loss)

  if global_step is _USE_GLOBAL_STEP:
    global_step = tf.train.get_or_create_global_step()

  if not callable(loss):
    raise ValueError('`loss` should be a function in eager mode.')

  if not isinstance(loss, Future):
    tf.logging.warning('loss should be an instance of eager_utils.Future')

  def train_step(*args, **kwargs):
    """Creates a Future train_step."""
    # pylint: disable=invalid-name
    _loss = kwargs.pop('_loss')
    _total_loss_fn = kwargs.pop('_total_loss_fn')
    _variables_to_train = kwargs.pop('_variables_to_train')
    with tf.GradientTape() as tape:
      loss_value = _loss(*args)
      total_loss_value = _total_loss_fn(loss_value)
    if _variables_to_train is None:
      _variables_to_train = tape.watched_variables()
    elif callable(_variables_to_train):
      _variables_to_train = _variables_to_train()
    _variables_to_train = nest.flatten(_variables_to_train)
    grads = tape.gradient(total_loss_value, _variables_to_train)
    grads_and_vars = zip(grads, _variables_to_train)
    # pylint: enable=invalid-name

    if transform_grads_fn:
      grads_and_vars = transform_grads_fn(grads_and_vars)

    if summarize_gradients:
      with tf.name_scope('summarize_grads'):
        add_gradients_summaries(grads_and_vars)

    if check_numerics:
      with tf.name_scope('train_op'):
        flat_loss_value = nest.flatten(loss_value)
        flat_loss_value[0] = tf.check_numerics(
            flat_loss_value[0], 'Loss is inf or nan')
        loss_value = nest.pack_sequence_as(loss_value, flat_loss_value)

    optimizer.apply_gradients(grads_and_vars, global_step=global_step)

    return loss_value

  return Future(
      train_step,
      _loss=loss,
      _total_loss_fn=total_loss_fn,
      _variables_to_train=variables_to_train)


def np_function(func=None, get_output_dtypes=None):
  """Decorator that allow a numpy function used in TF both Eager and Graph.

  Similar to `tf.py_func` and `tf.py_function` but it doesn't require defining
  the inputs or the dtypes of the outputs a priori.

  In Eager mode it would convert the tf.Tensors to np.arrays before passing to
  `func` and then convert back the outputs from np.arrays to tf.Tensors.

  In Graph mode it would create different tf.py_function for each combination
  of dtype of the inputs and cache them for reuse.

  NOTE: In Graph mode: if `get_output_dtypes` is not provided then `func` would
  be called with `np.ones()` to infer the output dtypes, and therefore `func`
  should be stateless.

  ```python
  Instead of doing:

  def sum(x):
    return np.sum(x)
  inputs = tf.constant([3, 4])
  outputs = tf.py_function(sum, inputs, Tout=[tf.int64])

  inputs = tf.constant([3., 4.])
  outputs = tf.py_function(sum, inputs, Tout=[tf.float32])

  Do:
  @eager_utils.np_function
  def sum(x):
    return np.sum(x)

  @eager_utils.np_function(get_output_dtypes=lambda _: np.float32)
  def mean(x):
    return np.mean(x)

  inputs = tf.constant([3, 4])
  outputs = sum(inputs)  # Infers that Tout is tf.int64

  inputs = tf.constant([3., 4.])
  outputs = sum(inputs)  # Infers that Tout is tf.float32

  with context.graph_mode():
    outputs = sum(tf.constant([3, 4]))
    outputs2 = sum(tf.constant([3., 4.]))
    sess.run(outputs) # np.array(7)
    sess.run(outputs2) # np.array(7.)

  with context.eager_mode():
    inputs = tf.constant([3, 4])
    outputs = sum(tf.constant([3, 4])) # tf.Tensor([7])
    outputs = sum(tf.constant([3., 4.])) # tf.Tensor([7.])

  ```
  Args:
    func: A numpy function, that takes numpy arrays as inputs and return numpy
      arrays as outputs.
    get_output_dtypes: Optional function that maps input dtypes to output
      dtypes. Example: lambda x: x (outputs have the same dtype as inputs).
      If it is not provided in Graph mode the `func` would be called to infe
      the output dtypes.
  Returns:
    A wrapped function that can be used with TF code.
  """
  def decorated(func):
    """Decorated func."""
    func.memo = {}
    def wrapper(*args, **kwargs):
      """Wrapper."""
      func_part = func
      if kwargs:
        func_part = functools.partial(func, **kwargs)
      if tf.executing_eagerly():
        result = func_part(*nest.map_structure(lambda x: x.numpy(), args))
        convert = lambda x: x if x is None else tf.convert_to_tensor(x)
        return nest.map_structure(convert, result)
      else:
        input_dtypes = tuple([x.dtype for x in nest.flatten(args)])
        if input_dtypes not in func.memo:
          if get_output_dtypes is None:
            zero_args = nest.map_structure(
                lambda x: np.ones(x.shape, x.dtype.as_numpy_dtype), args)
            def compute_output_dtypes(*args):
              """Pass np.ones() as inputs to infer the output dtypes."""
              result = func_part(*args)
              return nest.flatten(nest.map_structure(lambda x: x.dtype, result))
            func.memo[input_dtypes] = compute_output_dtypes(*zero_args)
          else:
            func.memo[input_dtypes] = get_output_dtypes(*input_dtypes)
        output_dtypes = func.memo[input_dtypes]
        return tf.py_function(func_part, inp=args, Tout=output_dtypes)
    return tf_decorator.make_decorator(func, wrapper)
  # This code path is for the `foo = np_function(foo, ...)` use case
  if func is not None:
    return decorated(func)

  # This code path is for the decorator
  # @np_function(...)
  # def foo(...):
  return decorated
