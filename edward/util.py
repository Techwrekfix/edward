from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import six
import tensorflow as tf

from copy import deepcopy
from tensorflow.python.ops import control_flow_ops

distributions = tf.contrib.distributions
sg = tf.contrib.bayesflow.stochastic_graph


def build_op(org_instance, dict_swap=None, scope="built", replace_itself=False, build_q=True):
    """Build a new node in the TensorFlow graph from `org_instance`,
    where any of its ancestors existing in `dict_swap` are
    replaced with `dict_swap`'s corresponding value.

    The building is done recursively, so any `Operation` whose output
    is required to evaluate `org_instance` is also built (if it isn't
    already built within the new scope). This is with the exception of
    `tf.Variable`s and `tf.placeholder`s, which are reused and not newly built.

    Parameters
    ----------
    org_instance : sg.DistributionTensor, tf.Variable, tf.Tensor, or tf.Operation
        Node to add in graph with replaced ancestors.
    dict_swap : dict, optional
        Distribution tensors, variables, tensors, or operations to
        swap with. Its keys are what `org_instance` may depend on,
        and its values are the corresponding object (of the same type)
        that is used in exchange.
    scope : str, optional
        A scope for the new node(s). This is used to avoid name
        conflicts with the original node(s).
    replace_itself : bool, optional
        Whether to replace `org_instance` itself if it exists in
        `dict_swap`. (This is used for the recursion.)
    build_q : bool, optional
        Whether to build the replaced tensors too (if not already
        built within the new scope). Otherwise will reuse them.

    Returns
    -------
    sg.DistributionTensor, tf.Variable, tf.Tensor, or tf.Operation
        The built node.

    Raises
    ------
    TypeError
        If `org_instance` is not one of the above types.

    Examples
    --------
    >>> x = tf.constant(2.0)
    >>> y = tf.constant(3.0)
    >>> z = x * y
    >>>
    >>> qx = tf.constant(4.0)
    >>> # The TensorFlow graph is currently
    >>> # `x` -> `z` <- y`, `qx`
    >>>
    >>> # This adds a subgraph with newly built nodes,
    >>> # `built/qx` -> `built/z` <- `built/y`
    >>> z_new = build_op(z, {x: qx})
    >>>
    >>> sess = tf.Session()
    >>> sess.run(z)
    6.0
    >>> sess.run(z_new)
    12.0
    """
    if not isinstance(org_instance, sg.DistributionTensor) and \
       not isinstance(org_instance, tf.Variable) and \
       not isinstance(org_instance, tf.Tensor) and \
       not isinstance(org_instance, tf.Operation):
        raise TypeError("Could not build instance: " + str(org_instance))

    # Swap instance if in dictionary.
    if org_instance in dict_swap and replace_itself:
        org_instance = dict_swap[org_instance]
        if not build_q:
            return org_instance
    elif isinstance(org_instance, tf.Tensor) and replace_itself:
        # Deal with case when `org_instance` is the associated tensor
        # from the DistributionTensor, e.g., `z.value()`. If
        # `dict_swap={z: qz}`, we aim to swap it with `qz.value()`.
        for key, value in six.iteritems(dict_swap):
            if isinstance(key, sg.DistributionTensor):
                if org_instance == key.value():
                    org_instance = value.value()
                    if not build_q:
                        return org_instance
                    break

    graph = tf.get_default_graph()
    new_name = scope + '/' + org_instance.name

    # If an instance of the same name exists, return appropriately.
    # Do this for stochastic tensors.
    stochastic_tensors = {x.name: x for x in graph.get_collection('_stochastic_tensor_collection_')}
    if new_name in stochastic_tensors:
        return stochastic_tensors[new_name]

    # Do this for tensors and operations.
    try:
        already_present = graph.as_graph_element(new_name,
                                                 allow_tensor=True,
                                                 allow_operation=True)
        return already_present
    except:
        pass

    # If instance is a variable, return it; do not re-build any.
    # Note we check variables via their name and not their type. This
    # is because if we get variables through an op's inputs, it has
    # type tf.Tensor: we can only tell it is a variable via its name.
    variables = {x.name: x for x in graph.get_collection(tf.GraphKeys.VARIABLES)}
    if org_instance.name in variables:
        return graph.get_tensor_by_name(variables[org_instance.name].name)

    # Do the same for placeholders. Same logic holds.
    # TODO assume placeholders are all in this collection
    placeholders = {x.name: x for x in graph.get_collection('placeholders')}
    if org_instance.name in placeholders:
        return graph.get_tensor_by_name(placeholders[org_instance.name].name)

    if isinstance(org_instance, sg.DistributionTensor):
        dist_tensor = org_instance

        # If it has buildable arguments, build them.
        dist_args = {}
        for key, value in six.iteritems(dist_tensor._dist_args):
            if isinstance(value, sg.DistributionTensor) or \
               isinstance(value, tf.Variable) or \
               isinstance(value, tf.Tensor) or \
               isinstance(value, tf.Operation):
               value = build_op(value, dict_swap, scope, True, build_q)

            dist_args[key] = value

        dist_args['name'] = new_name + dist_tensor.distribution.name

        # Build a new `dist_tensor` with any newly built arguments.
        # We do this by instantiating another DistributionTensor,
        # whose elements will be replaced.
        with tf.name_scope("TEMPORARY"):
            # TODO get all temporary distribution tensors in the same
            # name scope
            new_dist_tensor = sg.DistributionTensor(
                distributions.Bernoulli, p=tf.constant([0.0]))

        for key, value in six.iteritems(dist_tensor.__dict__):
            if key not in ['_name', '_dist_args', '_dist', '_value']:
                setattr(new_dist_tensor, key, deepcopy(value))

        setattr(new_dist_tensor, '_name', new_name)
        setattr(new_dist_tensor, '_dist_args', dist_args)
        setattr(new_dist_tensor, '_dist',
                new_dist_tensor._dist_cls(**new_dist_tensor._dist_args))
        setattr(new_dist_tensor, '_value',
                new_dist_tensor._create_value())
        return new_dist_tensor
    elif isinstance(org_instance, tf.Tensor):
        tensor = org_instance

        # A tensor is one of the outputs of its underlying
        # op. Therefore build the op itself.
        op = tensor.op
        new_op = build_op(op, dict_swap, scope, True, build_q)

        output_index = op.outputs.index(tensor)
        new_tensor = new_op.outputs[output_index]
        new_tensor.set_shape(tensor.get_shape())

        # Add built tensor to collections that the original one is in.
        for name, collection in tensor.graph._collections.items():
            if tensor in collection:
                graph.add_to_collection(name, new_tensor)

        return new_tensor
    else:  # tf.Operation
        op = org_instance

        # If it has an original op, build it.
        if op._original_op is not None:
            new_original_op = build_op(op._original_op, dict_swap, scope, True, build_q)
        else:
            new_original_op = None

        # If it has control inputs, build them.
        new_control_inputs = [build_op(x, dict_swap, scope, True, build_q)
                              for x in op.control_inputs]

        # If it has inputs, build them.
        new_inputs = [build_op(x, dict_swap, scope, True, build_q)
                      for x in op.inputs]

        # Make a copy of the node def.
        # As an instance of tensorflow.core.framework.graph_pb2.NodeDef, it
        # stores string-based info such as name, device, and type of the op.
        # It is unique to every Operation instance.
        new_node_def = deepcopy(op.node_def)
        new_node_def.name = new_name

        # Copy the other inputs needed for initialization.
        output_types = op._output_types[:]
        input_types = op._input_types[:]

        # Make a copy of the op def.
        # It is unique to every Operation type.
        op_def = deepcopy(op.op_def)

        new_op = tf.Operation(new_node_def,
                              graph,
                              new_inputs,
                              output_types,
                              new_control_inputs,
                              input_types,
                              new_original_op,
                              op_def)

        # Use Graph's private methods to add the op.
        graph._add_op(new_op)
        graph._record_op_seen_by_control_dependencies(new_op)
        for device_function in reversed(graph._device_function_stack):
            new_op._set_device(device_function(new_op))

        return new_op


def cumprod(xs):
    """Cumulative product of a tensor along its outer dimension.

    https://github.com/tensorflow/tensorflow/issues/813

    Parameters
    ----------
    xs : tf.Tensor
        A 1-D or higher tensor.

    Returns
    -------
    tf.Tensor
        A tensor with `cumprod` applied along its outer dimension.

    Raises
    ------
    InvalidArgumentError
        If the input has Inf or NaN values.
    """
    dependencies = [tf.verify_tensor_all_finite(xs, msg='')]
    xs = control_flow_ops.with_dependencies(dependencies, xs)
    xs = tf.cast(xs, dtype=tf.float32)

    values = tf.unpack(xs)
    out = []
    prev = tf.ones_like(values[0])
    for val in values:
        s = prev * val
        out.append(s)
        prev = s

    result = tf.pack(out)
    return result


def dot(x, y):
    """Compute dot product between a 2-D tensor and a 1-D tensor.

    If x is a ``[M x N]`` matrix, then y is a ``M``-vector.

    If x is a ``M``-vector, then y is a ``[M x N]`` matrix.

    Parameters
    ----------
    x : tf.Tensor
        A 1-D or 2-D tensor (see above).
    y : tf.Tensor
        A 1-D or 2-D tensor (see above).

    Returns
    -------
    tf.Tensor
        A 1-D tensor of length ``N``.

    Raises
    ------
    InvalidArgumentError
        If the inputs have Inf or NaN values.
    """
    dependencies = [tf.verify_tensor_all_finite(x, msg=''),
                    tf.verify_tensor_all_finite(y, msg='')]
    x = control_flow_ops.with_dependencies(dependencies, x)
    y = control_flow_ops.with_dependencies(dependencies, y)
    x = tf.cast(x, dtype=tf.float32)
    y = tf.cast(y, dtype=tf.float32)

    if len(x.get_shape()) == 1:
        vec = x
        mat = y
        return tf.matmul(tf.expand_dims(vec, 0), mat)
    else:
        mat = x
        vec = y
        return tf.matmul(mat, tf.expand_dims(vec, 1))


def get_dims(x):
    """Get values of each dimension.

    Parameters
    ----------
    x : tf.Tensor or np.ndarray
        A n-D tensor.

    Returns
    -------
    list of int
        Python list containing dimensions of ``x``.
    """
    if isinstance(x, tf.Tensor) or isinstance(x, tf.Variable):
        dims = x.get_shape()
        if len(dims) == 0: # scalar
            return []
        else: # array
            return [dim.value for dim in dims]
    elif isinstance(x, np.ndarray):
        return list(x.shape)
    else:
        raise NotImplementedError()


def get_session():
    """Get the globally defined TensorFlow session.

    If the session is not already defined, then the function will create
    a global session.

    Returns
    -------
    _ED_SESSION : tf.InteractiveSession
    """
    global _ED_SESSION
    if tf.get_default_session() is None:
        _ED_SESSION = tf.InteractiveSession()
    else:
        _ED_SESSION = tf.get_default_session()

    return _ED_SESSION


def hessian(y, xs):
    """Calculate Hessian of y with respect to each x in xs.

    Parameters
    ----------
    y : tf.Tensor
        Tensor to calculate Hessian of.
    xs : list of tf.Variable
        List of TensorFlow variables to calculate with respect to.
        The variables can have different shapes.

    Returns
    -------
    tf.Tensor
        A 2-D tensor where each row is
        .. math:: \partial_{xs} ( [ \partial_{xs} y ]_j ).

    Raises
    ------
    InvalidArgumentError
        If the inputs have Inf or NaN values.
    """
    dependencies = [tf.verify_tensor_all_finite(y, msg='')]
    dependencies.extend([tf.verify_tensor_all_finite(x, msg='') for x in xs])

    with tf.control_dependencies(dependencies):
        # Calculate flattened vector grad_{xs} y.
        grads = tf.gradients(y, xs)
        grads = [tf.reshape(grad, [-1]) for grad in grads]
        grads = tf.concat(0, grads)
        # Loop over each element in the vector.
        mat = []
        d = grads.get_shape()[0]
        if not isinstance(d, int):
            d = grads.eval().shape[0]

        for j in range(d):
            # Calculate grad_{xs} ( [ grad_{xs} y ]_j ).
            gradjgrads = tf.gradients(grads[j], xs)
            # Flatten into vector.
            hi = []
            for l in range(len(xs)):
                hij = gradjgrads[l]
                # return 0 if gradient doesn't exist; TensorFlow returns None
                if hij is None:
                    hij = tf.zeros(xs[l].get_shape(), dtype=tf.float32)

                hij = tf.reshape(hij, [-1])
                hi.append(hij)

            hi = tf.concat(0, hi)
            mat.append(hi)

        # Form matrix where each row is grad_{xs} ( [ grad_{xs} y ]_j ).
        return tf.pack(mat)


def kl_multivariate_normal(loc_one, scale_one, loc_two=0.0, scale_two=1.0):
    """Calculate the KL of multivariate normal distributions with
    diagonal covariances.

    Parameters
    ----------
    loc_one : tf.Tensor
        A 0-D tensor, 1-D tensor of length n, or 2-D tensor of shape M
        x n where each row represents the mean of a n-dimensional
        Gaussian.
    scale_one : tf.Tensor
        A tensor of same shape as ``loc_one``, representing the
        standard deviation.
    loc_two : tf.Tensor, optional
        A tensor of same shape as ``loc_one``, representing the
        mean of another Gaussian.
    scale_two : tf.Tensor, optional
        A tensor of same shape as ``loc_one``, representing the
        standard deviation of another Gaussian.

    Returns
    -------
    tf.Tensor
        For 0-D or 1-D tensor inputs, outputs the 0-D tensor
        ``KL( N(z; loc_one, scale_one) || N(z; loc_two, scale_two) )``
        For 2-D tensor inputs, outputs the 1-D tensor
        ``[KL( N(z; loc_one[m,:], scale_one[m,:]) || N(z; loc_two[m,:], scale_two[m,:]) )]_{m=1}^M``

    Raises
    ------
    InvalidArgumentError
        If the location variables have Inf or NaN values, or if the scale
        variables are not positive.
    """
    dependencies = [tf.verify_tensor_all_finite(loc_one, msg=''),
                    tf.verify_tensor_all_finite(loc_two, msg=''),
                    tf.assert_positive(scale_one),
                    tf.assert_positive(scale_two)]
    loc_one = control_flow_ops.with_dependencies(dependencies, loc_one)
    scale_one = control_flow_ops.with_dependencies(dependencies, scale_one)
    loc_one = tf.cast(loc_one, tf.float32)
    scale_one = tf.cast(scale_one, tf.float32)

    if loc_two == 0.0 and scale_two == 1.0:
        # With default arguments, we can avoid some intermediate computation.
        out = tf.square(scale_one) + tf.square(loc_one) - \
              1.0 - 2.0 * tf.log(scale_one)
    else:
        loc_two = control_flow_ops.with_dependencies(dependencies, loc_two)
        scale_two = control_flow_ops.with_dependencies(dependencies, scale_two)
        loc_two = tf.cast(loc_two, tf.float32)
        scale_two = tf.cast(scale_two, tf.float32)
        out = tf.square(scale_one/scale_two) + \
              tf.square((loc_two - loc_one)/scale_two) - \
              1.0 + 2.0 * tf.log(scale_two) - 2.0 * tf.log(scale_one)

    if len(out.get_shape()) <= 1: # scalar or vector
        return 0.5 * tf.reduce_sum(out)
    else: # matrix
        return 0.5 * tf.reduce_sum(out, 1)


def log_mean_exp(input_tensor, reduction_indices=None, keep_dims=False):
    """Compute the ``log_mean_exp`` of elements in a tensor, taking
    the mean across axes given by ``reduction_indices``.

    Parameters
    ----------
    input_tensor : tf.Tensor
        The tensor to reduce. Should have numeric type.
    reduction_indices : int or list of int, optional
        The dimensions to reduce. If `None` (the default), reduces all
        dimensions.
    keep_dims : bool, optional
        If true, retains reduced dimensions with length 1.

    Returns
    -------
    tf.Tensor
        The reduced tensor.

    Raises
    ------
    InvalidArgumentError
        If the input has Inf or NaN values.
    """
    dependencies = [tf.verify_tensor_all_finite(input_tensor, msg='')]
    input_tensor = control_flow_ops.with_dependencies(dependencies, input_tensor)
    input_tensor = tf.cast(input_tensor, dtype=tf.float32)

    x_max = tf.reduce_max(input_tensor, reduction_indices, keep_dims=True)
    return tf.squeeze(x_max) + tf.log(tf.reduce_mean(
        tf.exp(input_tensor - x_max), reduction_indices, keep_dims))


def log_sum_exp(input_tensor, reduction_indices=None, keep_dims=False):
    """Compute the ``log_sum_exp`` of elements in a tensor, taking
    the sum across axes given by ``reduction_indices``.

    Parameters
    ----------
    input_tensor : tf.Tensor
        The tensor to reduce. Should have numeric type.
    reduction_indices : int or list of int, optional
        The dimensions to reduce. If `None` (the default), reduces all
        dimensions.
    keep_dims : bool, optional
        If true, retains reduced dimensions with length 1.

    Returns
    -------
    tf.Tensor
        The reduced tensor.

    Raises
    ------
    InvalidArgumentError
        If the input has Inf or NaN values.
    """
    dependencies = [tf.verify_tensor_all_finite(input_tensor, msg='')]
    input_tensor = control_flow_ops.with_dependencies(dependencies, input_tensor);
    input_tensor = tf.cast(input_tensor, dtype=tf.float32)

    x_max = tf.reduce_max(input_tensor, reduction_indices, keep_dims=True)
    return tf.squeeze(x_max) + tf.log(tf.reduce_sum(
        tf.exp(input_tensor - x_max), reduction_indices, keep_dims))


def logit(x):
    """Evaluate :math:`\log(x / (1 - x))` elementwise.

    Parameters
    ----------
    x : tf.Tensor
        A n-D tensor.

    Returns
    -------
    tf.Tensor
        A tensor of same shape as input.

    Raises
    ------
    InvalidArgumentError
        If the input is not between :math:`(0,1)` elementwise.
    """
    dependencies = [tf.assert_positive(x),
                    tf.assert_less(x, 1.0)]
    x = control_flow_ops.with_dependencies(dependencies, x)
    x = tf.cast(x, dtype=tf.float32)

    return tf.log(x) - tf.log(1.0 - x)


def multivariate_rbf(x, y=0.0, sigma=1.0, l=1.0):
    """Squared-exponential kernel

    .. math:: k(x, y) = \sigma^2 \exp{ -1/(2l^2) \sum_i (x_i - y_i)^2 }

    Parameters
    ----------
    x : tf.Tensor
        A n-D tensor.
    y : tf.Tensor, optional
        A tensor of same shape as ``x``.
    sigma : tf.Tensor, optional
        A 0-D tensor, representing the standard deviation of radial
        basis function.
    l : tf.Tensor, optional
        A 0-D tensor, representing the lengthscale of radial basis
        function.

    Returns
    -------
    tf.Tensor
        A tensor of one less dimension than the input.

    Raises
    ------
    InvalidArgumentError
        If the mean variables have Inf or NaN values, or if the scale
        and length variables are not positive.
    """
    dependencies = [tf.verify_tensor_all_finite(x, msg=''),
                    tf.verify_tensor_all_finite(y, msg=''),
                    tf.assert_positive(sigma),
                    tf.assert_positive(l)]
    x = control_flow_ops.with_dependencies(dependencies, x)
    y = control_flow_ops.with_dependencies(dependencies, y)
    sigma = control_flow_ops.with_dependencies(dependencies, sigma)
    l = control_flow_ops.with_dependencies(dependencies, l)
    x = tf.cast(x, dtype=tf.float32)
    y = tf.cast(y, dtype=tf.float32)
    sigma = tf.cast(sigma, dtype=tf.float32)
    l = tf.cast(l, dtype=tf.float32)

    return tf.pow(sigma, 2.0) * \
           tf.exp(-1.0/(2.0*tf.pow(l, 2.0)) * \
           tf.reduce_sum(tf.pow(x - y , 2.0)))


def rbf(x, y=0.0, sigma=1.0, l=1.0):
    """Squared-exponential kernel element-wise

    .. math:: k(x, y) = \sigma^2 \exp{ -1/(2l^2) (x - y)^2 }

    Parameters
    ----------
    x : tf.Tensor
        A n-D tensor.
    y : tf.Tensor, optional
        A tensor of same shape as ``x``.
    sigma : tf.Tensor, optional
        A 0-D tensor, representing the standard deviation of radial
        basis function.
    l : tf.Tensor, optional
        A 0-D tensor, representing the lengthscale of radial basis
        function.

    Returns
    -------
    tf.Tensor
        A tensor of one less dimension than the input.

    Raises
    ------
    InvalidArgumentError
        If the mean variables have Inf or NaN values, or if the scale
        and length variables are not positive.
    """
    dependencies = [tf.verify_tensor_all_finite(x, msg=''),
                    tf.verify_tensor_all_finite(y, msg=''),
                    tf.assert_positive(sigma),
                    tf.assert_positive(l)]
    x = control_flow_ops.with_dependencies(dependencies, x)
    y = control_flow_ops.with_dependencies(dependencies, y)
    sigma = control_flow_ops.with_dependencies(dependencies, sigma)
    l = control_flow_ops.with_dependencies(dependencies, l)
    x = tf.cast(x, dtype=tf.float32)
    y = tf.cast(y, dtype=tf.float32)
    sigma = tf.cast(sigma, dtype=tf.float32)
    l = tf.cast(l, dtype=tf.float32)

    return tf.pow(sigma, 2.0) * \
           tf.exp(-1.0/(2.0*tf.pow(l, 2.0)) * tf.pow(x - y , 2.0))


def set_seed(x):
    """Set seed for both NumPy and TensorFlow.

    Parameters
    ----------
    x : int, float
        seed
    """
    np.random.seed(x)
    tf.set_random_seed(x)


def softplus(x):
    """Elementwise Softplus function

    .. math:: \log(1 + \exp(x))

    If input `x < -30`, returns `0.0` exactly.

    If input `x > 30`, returns `x` exactly.

    TensorFlow can't currently autodiff through ``tf.nn.softplus()``.

    Parameters
    ----------
    x : tf.Tensor
        A n-D tensor.

    Returns
    -------
    tf.Tensor
        A tensor of same shape as input.

    Raises
    ------
    InvalidArgumentError
        If the input has Inf or NaN values.
    """
    dependencies = [tf.verify_tensor_all_finite(x, msg='')]
    x = control_flow_ops.with_dependencies(dependencies, x)
    x = tf.cast(x, dtype=tf.float32)

    result = tf.log(1.0 + tf.exp(x))

    less_than_thirty = tf.less(x, -30.0)
    result = tf.select(less_than_thirty, tf.zeros_like(x), result)

    greater_than_thirty = tf.greater(x, 30.0)
    result = tf.select(greater_than_thirty, x, result)

    return result


def to_simplex(x):
    """Transform real vector of length ``(K-1)`` to a simplex of dimension ``K``
    using a backward stick breaking construction.

    Parameters
    ----------
    x : tf.Tensor
        A 1-D or 2-D tensor.

    Returns
    -------
    tf.Tensor
        A tensor of same shape as input but with last dimension of
        size ``K``.

    Raises
    ------
    InvalidArgumentError
        If the input has Inf or NaN values.

    Notes
    -----
    x as a 3-D or higher tensor is not guaranteed to be supported.
    """
    dependencies = [tf.verify_tensor_all_finite(x, msg='')]
    x = control_flow_ops.with_dependencies(dependencies, x)
    x = tf.cast(x, dtype=tf.float32)

    if isinstance(x, tf.Tensor) or isinstance(x, tf.Variable):
        shape = get_dims(x)
    else:
        shape = x.shape

    if len(shape) == 1:
        n_rows = ()
        K_minus_one = shape[0]
        eq = -tf.log(tf.cast(K_minus_one - tf.range(K_minus_one),
                             dtype=tf.float32))
        z = tf.sigmoid(eq + x)
        pil = tf.concat(0, [z, tf.constant([1.0])])
        piu = tf.concat(0, [tf.constant([1.0]), 1.0 - z])
        S = cumprod(piu)
        return S * pil
    else:
        n_rows = shape[0]
        K_minus_one = shape[1]
        eq = -tf.log(tf.cast(K_minus_one - tf.range(K_minus_one),
                             dtype=tf.float32))
        z = tf.sigmoid(eq + x)
        pil = tf.concat(1, [z, tf.ones([n_rows, 1])])
        piu = tf.concat(1, [tf.ones([n_rows, 1]), 1.0 - z])
        # cumulative product along 1st axis
        S = tf.pack([cumprod(piu_x) for piu_x in tf.unpack(piu)])
        return S * pil
