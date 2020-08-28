# Copyright 2020 Google LLC
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

from functools import partial
import operator

import numpy as np
import jax.numpy as jnp
from jax import scipy as jsp
from jax import lax, device_put, jit
from jax.tree_util import tree_leaves, tree_map, tree_multimap, tree_structure, tree_reduce, Partial
from jax.util import safe_map as map


_dot = partial(jnp.dot, precision=lax.Precision.HIGHEST)
_vdot = partial(jnp.vdot, precision=lax.Precision.HIGHEST)


def _vdot_real_part(x, y):
  """Vector dot-product guaranteed to have a real valued result."""
  # all our uses of vdot() in CG are for computing an operator of the form
  # `z^T M z` where `M` is positive definite and Hermitian, so the result is
  # real valued:
  # https://en.wikipedia.org/wiki/Definiteness_of_a_matrix#Definitions_for_complex_matrices
  result = _vdot(x.real, y.real)
  if jnp.iscomplexobj(x) or jnp.iscomplexobj(y):
    result += _vdot(x.imag, y.imag)
  return result


# aliases for working with pytrees

def _vdot_tree(x, y, assume_real=True):
  if assume_real:
    return sum(tree_leaves(tree_multimap(_vdot_real_part, x, y)))
  else:
    return sum(tree_leaves(tree_multimap(_vdot, x, y)))


def _mul(scalar, tree):
  return tree_map(partial(operator.mul, scalar), tree)


def _div(tree, scalar):
  return tree_map(partial(lambda v: v / scalar), tree)


_add = partial(tree_multimap, operator.add)
_sub = partial(tree_multimap, operator.sub)
_dot_tree = partial(tree_multimap, _dot)


@Partial
def _identity(x):
  return x


def _cg_solve(A, b, x0=None, *, maxiter, tol=1e-5, atol=0.0, M=_identity):

  # tolerance handling uses the "non-legacy" behavior of scipy.sparse.linalg.cg
  bs = _vdot_tree(b, b)
  atol2 = jnp.maximum(jnp.square(tol) * bs, jnp.square(atol))

  # https://en.wikipedia.org/wiki/Conjugate_gradient_method#The_preconditioned_conjugate_gradient_method

  def cond_fun(value):
    x, r, gamma, p, k = value
    rs = gamma if M is _identity else _vdot_tree(r, r)
    return (rs > atol2) & (k < maxiter)

  def body_fun(value):
    x, r, gamma, p, k = value
    Ap = A(p)
    alpha = gamma / _vdot_tree(p, Ap)
    x_ = _add(x, _mul(alpha, p))
    r_ = _sub(r, _mul(alpha, Ap))
    z_ = M(r_)
    gamma_ = _vdot_tree(r_, z_)
    beta_ = gamma_ / gamma
    p_ = _add(z_, _mul(beta_, p))
    return x_, r_, gamma_, p_, k + 1

  r0 = _sub(b, A(x0))
  p0 = z0 = M(r0)
  gamma0 = _vdot_tree(r0, z0)
  initial_value = (x0, r0, gamma0, p0, 0)

  x_final, *_ = lax.while_loop(cond_fun, body_fun, initial_value)

  return x_final


def _shapes(pytree):
  return map(jnp.shape, tree_leaves(pytree))


def cg(A, b, x0=None, *, tol=1e-5, atol=0.0, maxiter=None, M=None):
  """Use Conjugate Gradient iteration to solve ``Ax = b``.

  The numerics of JAX's ``cg`` should exact match SciPy's ``cg`` (up to
  numerical precision), but note that the interface is slightly different: you
  need to supply the linear operator ``A`` as a function instead of a sparse
  matrix or ``LinearOperator``.

  Derivatives of ``cg`` are implemented via implicit differentiation with
  another ``cg`` solve, rather than by differentiating *through* the solver.
  They will be accurate only if both solves converge.

  Parameters
  ----------
  A : function
      Function that calculates the matrix-vector product ``Ax`` when called
      like ``A(x)``. ``A`` must represent a hermitian, positive definite
      matrix, and must return array(s) with the same structure and shape as its
      argument.
  b : array or tree of arrays
      Right hand side of the linear system representing a single vector. Can be
      stored as an array or Python container of array(s) with any shape.

  Returns
  -------
  x : array or tree of arrays
      The converged solution. Has the same structure as ``b``.
  info : None
      Placeholder for convergence information. In the future, JAX will report
      the number of iterations when convergence is not achieved, like SciPy.

  Other Parameters
  ----------------
  x0 : array
      Starting guess for the solution. Must have the same structure as ``b``.
  tol, atol : float, optional
      Tolerances for convergence, ``norm(residual) <= max(tol*norm(b), atol)``.
      We do not implement SciPy's "legacy" behavior, so JAX's tolerance will
      differ from SciPy unless you explicitly pass ``atol`` to SciPy's ``cg``.
  maxiter : integer
      Maximum number of iterations.  Iteration will stop after maxiter
      steps even if the specified tolerance has not been achieved.
  M : function
      Preconditioner for A.  The preconditioner should approximate the
      inverse of A.  Effective preconditioning dramatically improves the
      rate of convergence, which implies that fewer iterations are needed
      to reach a given error tolerance.

  See also
  --------
  scipy.sparse.linalg.cg
  jax.lax.custom_linear_solve
  """
  if x0 is None:
    x0 = tree_map(jnp.zeros_like, b)

  b, x0 = device_put((b, x0))

  if maxiter is None:
    size = sum(bi.size for bi in tree_leaves(b))
    maxiter = 10 * size  # copied from scipy

  if M is None:
    M = _identity

  if tree_structure(x0) != tree_structure(b):
    raise ValueError(
        'x0 and b must have matching tree structure: '
        f'{tree_structure(x0)} vs {tree_structure(b)}')

  if _shapes(x0) != _shapes(b):
    raise ValueError(
        'arrays in x0 and b must have matching shapes: '
        f'{_shapes(x0)} vs {_shapes(b)}')

  cg_solve = partial(
      _cg_solve, x0=x0, tol=tol, atol=atol, maxiter=maxiter, M=M)

  # real-valued positive-definite linear operators are symmetric
  def real_valued(x):
    return not issubclass(x.dtype.dtype, np.complexfloating)
  symmetric = all(map(real_valued, tree_leaves(b)))
  x = lax.custom_linear_solve(
      A, b, solve=cg_solve, transpose_solve=cg_solve, symmetric=symmetric)
  info = None  # TODO(shoyer): return the real iteration count here
  return x, info


def _project_on_columns(A, v):
  v_proj = tree_multimap(
    lambda X, y: jnp.einsum("...n,...->n", X.conj(), y),
    A,
    v,
  )
  return tree_reduce(operator.add, v_proj)


def _iterative_classical_gram_schmidt(Q, x, iterations=2):
  """Orthogonalize x against the columns of Q."""
  # "twice is enough"
  # http://slepc.upv.es/documentation/reports/str1.pdf

  # This assumes that Q's leaves all have the same dimension in the last
  # axis.
  r = jnp.zeros((tree_leaves(Q)[0].shape[-1]))
  q = x

  for _ in range(iterations):
    h = _project_on_columns(Q, q)
    q = _sub(q, tree_map(lambda X: jnp.dot(X, h), Q))
    r = _add(r, h)
  return q, r


def kth_arnoldi_iteration(k, A, M, V, H, tol):
    # https://en.wikipedia.org/wiki/Arnoldi_iteration#The_Arnoldi_iteration
    v = tree_map(lambda x: x[..., k], V)  # Gets V[:, k]
    v = A(M(v))

    def gram_schmidt_step(r, v_i):
      h_i = _vdot_tree(r, v_i)
      r_i = _sub(r, _mul(h_i, v_i))
      return r_i, h_i
    v, h = lax.scan(gram_schmidt_step, v, xs=V.T)
    #v, h = _iterative_classical_gram_schmidt(V, v, iterations=1)
    unit_v, v_norm = _safe_normalize(v, return_norm=True)
    V = tree_multimap(lambda X, y: X.at[..., k + 1].set(y), V, unit_v)
    h = h.at[k + 1].set(v_norm)
    H = H.at[k, :].set(h)
    return V, H


def _safe_normalize(x, return_norm=False):
  norm = jnp.sqrt(_vdot_tree(x, x, assume_real=False))
  thresh = jnp.finfo(x.dtype).eps

  normalized_x, norm = lax.cond(
    norm > thresh,
    lambda y: (_div(y, norm), norm),
    lambda y: (_mul(0.*norm, y), thresh),  # To get the dtype right
    x,
  )
  if return_norm:
    return normalized_x, norm
  else:
    return normalized_x


@jit
def _lstsq(a, b):
  Q, R = jnp.linalg.qr(a)
  out = jsp.linalg.solve_triangular(R[:, :-1], b[:-1])
  return out


def _gmres(A, b, x0, tol, n_kry, M):
  """
  Implements a single restart of GMRES. The n_kry-dimensional Krylov subspace
  K(A, x0) = span(A(x0), A@x0, A@A@x0, ..., A^n_kry @ x0) is built, and the
  projection of the true solution into this subspace is returned.
  """
  # https://www-users.cs.umn.edu/~saad/Calais/PREC.pdf
  residual = _sub(b, A(x0))
  unit_residual, beta = _safe_normalize(residual, return_norm=True)

  V = tree_map(
    lambda x: jnp.pad(x[..., None], ((0, 0),) * x.ndim + ((0, n_kry),)),
    unit_residual,
  )
  H = jnp.zeros((n_kry, n_kry + 1), jnp.result_type(*tree_leaves(b)))
  print(H.shape)

  for k in range(n_kry):
    V, H = kth_arnoldi_iteration(k, A, M, V, H, tol)

  dtype = beta.dtype
  e1 = jnp.concatenate([jnp.ones((1,), dtype), jnp.zeros((n_kry,), dtype)])
  y = jsp.linalg.solve(H[:, :-1].T, beta*e1[:-1])
  dx = M(tree_map(lambda X: jnp.dot(X[..., :-1], y), V))
  x = _add(x0, dx)

  residual = _sub(b, A(x)) # TODO: Remove this
  err = _vdot_tree(residual, residual, assume_real=False)
  done = err < tol
  return x, done


def _gmres_solve(A, b, x0, tol, atol, restart, maxiter, M):
  """
  The main function call wrapped by custom_linear_solve. Repeatedly calls GMRES
  to find the projected solution within the order-``restart``
  Krylov space K(A, x0, restart), using the result of the previous projection
  in place of x0 each time, terminating early if the magitude of the residual
  dips beneath max(tol^2 * (b@b), atol^2).
  """
  bs = _vdot_tree(b, b, assume_real=False)
  true_tol = jnp.maximum(jnp.square(tol) * bs, jnp.square(atol))

  def cond_fun(value):
    x, k, done = value
    return ~done & (k < maxiter)

  def body_fun(value):
    x, k, _ = value
    x, done = _gmres(A, b, x, true_tol, restart, M)
    return x, k + 1, done
  x_final, k, done = lax.while_loop(cond_fun, body_fun, (x0, 0, False))
  info = lax.cond(done, lambda y: 0, lambda y: k, 0)
  return x_final


#  def _gmres_solve(A, b, x0, tol, atol, restart, maxiter, M):
#    """
#    The main function call wrapped by custom_linear_solve.
#    """
#    bs = _vdot_tree(b, b, assume_real=False)
#    atol2 = jnp.maximum(jnp.square(tol) * bs, jnp.square(atol))
#    num_restarts = maxiter // restart

#    def cond_fun(value):
#      x, residual, k = value
#      sqr_error = _vdot_tree(residual, residual, assume_real=False)
#      return (sqr_error > atol2) & (k < num_restarts) & ~jnp.isnan(sqr_error)

#    def body_fun(value):
#      x, residual, k = value
#      x = _gmres(A, b, x, restart, M, residual)
#      residual = _sub(b, A(x))
#      return x, residual, k + 1

#    residual = _sub(b, A(x0))
#    if num_restarts:
#      x, residual, _ = lax.while_loop(
#        cond_fun, body_fun, (x0, residual, 0))
#    else:
#      x = x0

#    iters = maxiter % restart
#    sqr_error = _vdot_tree(residual, residual)
#    if iters > 0:  # TODO: fix the extraneous call
#      x_final = lax.cond(
#        sqr_error > atol2,
#        true_fun=lambda values: _gmres(A, b, values[0], iters, M, values[1]),
#        false_fun=lambda values: values[0],
#        operand=(x, residual),
#      )
#    else:
#      x_final = x
#    return x_final


def gmres(A, b, x0=None, *, tol=1e-5, atol=0.0, restart=20, maxiter=None,
          M=None):
  """
  GMRES solves the linear system A x = b for x, given A and b. A is specified
  as a function performing A(vi) -> vf = A @ vi, and in principle need not have
  any particular special properties, such as symmetry. However, convergence
  is often slow for nearly symmetric operators.

  Parameters
  ----------
  A: function
     Function that calculates the linear map (e.g. matrix-vector product)
     ``Ax`` when called like ``A(x)``. ``A`` must return array(s) with the same
     structure and shape as its argument.
  b : array or tree of arrays
      Right hand side of the linear system representing a single vector. Can be
      stored as an array or Python container of array(s) with any shape.

  Returns
  -------
  x : array or tree of arrays
      The converged solution. Has the same structure as ``b``.
  info : None
      Placeholder for convergence information. In the future, JAX will report
      the number of iterations when convergence is not achieved, like SciPy.

  Other Parameters
  ----------------
  x0 : array, optional
       Starting guess for the solution. Must have the same structure as ``b``.
       If this is unspecified, a (logical) vector of zeroes is used.
  tol, atol : float, optional
      Tolerances for convergence, ``norm(residual) <= max(tol*norm(b), atol)``.
      We do not implement SciPy's "legacy" behavior, so JAX's tolerance will
      differ from SciPy unless you explicitly pass ``atol`` to SciPy's ``gmres``.
  restart : integer, optional
      Size of the Krylov subspace (``number of iterations") built between
      restarts. GMRES works by approximating the true solution x as its
      projection into a Krylov space of this dimension - this parameter
      therefore bounds the maximum accuracy achievable from any guess
      solution. Larger values increase both number of iterations and iteration
      cost, but may be necessary for convergence. The algorithm terminates
      early if convergence is achieved before the full subspace is built.
      Default is 20.
  maxiter : integer
      Maximum number of iterations.  If convergence has not been achieved
      after projecting into the size-``restart`` Krylov space, GMRES will
      try again, using the previous result as the new guess, up to this
      many times. If the optimal solution within a Krylov space of the
      given dimension is not converged up to the requested tolerance, these
      restarts will not improve the accuracy, so care should be taken when
      increasing this parameter.
  M : function
      Preconditioner for A.  The preconditioner should approximate the
      inverse of A.  Effective preconditioning dramatically improves the
      rate of convergence, which implies that fewer iterations are needed
      to reach a given error tolerance.

  See also
  --------
  scipy.sparse.linalg.gmres
  jax.lax.custom_linear_solve
  """

  if x0 is None:
    x0 = tree_map(jnp.zeros_like, b)
  if M is None:
    M = _identity

  size = sum(bi.size for bi in tree_leaves(b))
  if maxiter is None:
    maxiter = 10 * size  # copied from scipy
  if restart > size:
    restart = size

  if tree_structure(x0) != tree_structure(b):
    raise ValueError(
      'x0 and b must have matching tree structure: '
      f'{tree_structure(x0)} vs {tree_structure(b)}')

  b, x0 = device_put((b, x0))

  def _solve(A, b):
    return _gmres_solve(A, b, x0, tol, atol, restart, maxiter, M)

  x = lax.custom_linear_solve(A, b, solve=_solve, transpose_solve=_solve)
  info = None
  return x, info
