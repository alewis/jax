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
from jax import lax, device_put
from jax.tree_util import (tree_leaves, tree_map, tree_multimap, tree_structure,
                           tree_reduce, Partial)
from jax.util import safe_map as map


_dot = partial(jnp.dot, precision=lax.Precision.HIGHEST)
_vdot = partial(jnp.vdot, precision=lax.Precision.HIGHEST)

# aliases for working with pytreedef _vdot_real_part(x, y):

def _vdot_real_part(x, y):
  """Vector dot-product guaranteed to have a real valued result despite
     possibly complex input. Thus neglects the real-imaginary cross-terms.
     The result is a real float.
  """
  # all our uses of vdot() in CG are for computing an operator of the form
  #  z^T M z
  #  where M is positive definite and Hermitian, so the result is
  # real valued:
  # https://en.wikipedia.org/wiki/Definiteness_of_a_matrix#Definitions_for_complex_matrices
  vdot = partial(jnp.vdot, precision=lax.Precision.HIGHEST)
  result = vdot(x.real, y.real)
  if jnp.iscomplexobj(x) or jnp.iscomplexobj(y):
    result += vdot(x.imag, y.imag)
  return result.real

def _vdot_real_tree(x, y):
  return sum(tree_leaves(tree_multimap(_vdot_real_part, x, y)))

def _norm_tree(x):
  return jnp.sqrt(_vdot_real_tree(x, x))

def _vdot_tree(x, y):
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
  bs = _vdot_real_tree(b, b)
  atol2 = jnp.maximum(jnp.square(tol) * bs, jnp.square(atol))

  # https://en.wikipedia.org/wiki/Conjugate_gradient_method#The_preconditioned_conjugate_gradient_method

  def cond_fun(value):
    x, r, gamma, p, k = value
    rs = gamma if M is _identity else _vdot_real_tree(r, r)
    return (rs > atol2) & (k < maxiter)

  def body_fun(value):
    x, r, gamma, p, k = value
    Ap = A(p)
    alpha = gamma / _vdot_real_tree(p, Ap)
    x_ = _add(x, _mul(alpha, p))
    r_ = _sub(r, _mul(alpha, Ap))
    z_ = M(r_)
    gamma_ = _vdot_real_tree(r_, z_)
    beta_ = gamma_ / gamma
    p_ = _add(z_, _mul(beta_, p))
    return x_, r_, gamma_, p_, k + 1

  r0 = _sub(b, A(x0))
  p0 = z0 = M(r0)
  gamma0 = _vdot_real_tree(r0, z0)
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
    return not issubclass(x.dtype.type, np.complexfloating)
  symmetric = all(map(real_valued, tree_leaves(b)))
  x = lax.custom_linear_solve(
      A, b, solve=cg_solve, transpose_solve=cg_solve, symmetric=symmetric)
  info = None  # TODO(shoyer): return the real iteration count here
  return x, info


def _safe_normalize(x, return_norm=False, thresh=None):
  """
  Returns the L2-normalized vector (which can be a pytree) x, and optionally
  the computed norm. If the computed norm is less than the threshold `thresh`,
  which by default is the machine precision of x's dtype, it will be
  taken to be 0, and the normalized x to be the zero vector.
  """
  norm = _norm_tree(x)
  dtype = jnp.result_type(*tree_leaves(x))
  if thresh is None:
    thresh = jnp.finfo(norm.dtype).eps
  thresh = thresh.astype(dtype).real

  normalized_x, norm = lax.cond(
    norm > thresh,
    lambda y: (_div(y, norm), norm),
    lambda y: (tree_map(jnp.zeros_like, y), jnp.zeros((), dtype=thresh.dtype)),
    x,
  )
  if return_norm:
    return normalized_x, norm
  else:
    return normalized_x


def _project_on_columns(A, v):
  """
  Returns A.T.conj() @ v.
  """
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
    Qh = tree_map(lambda X: _dot_tree(X, h), Q)
    q = _sub(q, Qh)
    r = _add(r, h)
  return q, r


def kth_arnoldi_iteration(k, A, M, V, H, tol):
  """
  Performs a single (the k'th) step of the Arnoldi process. Thus,
  adds a new orthonormalized Krylov vector A(M(V[:, k])) to V[:, k+1],
  and that vectors overlaps with the existing Krylov vectors to
  H[k, :]. The tolerance 'tol' sets the threshold at which an invariant
  subspace is declared to have been found, in which case the new
  vector is taken to be the zero vector.
  """

  v = tree_map(lambda x: x[..., k], V)  # Gets V[:, k]
  v = A(M(v))
  v, h = _iterative_classical_gram_schmidt(V, v, iterations=1)
  unit_v, v_norm = _safe_normalize(v, return_norm=True, thresh=tol)
  V = tree_multimap(lambda X, y: X.at[..., k + 1].set(y), V, unit_v)

  h = h.at[k + 1].set(v_norm)
  H = H.at[k, :].set(h)
  breakdown = v_norm == 0.
  return V, H, breakdown


def apply_givens_rotations(H_row, givens, k):
  """
  Applies the Givens rotations stored in the vectors cs and sn to the vector
  H_row. Then constructs and applies a new Givens rotation that eliminates
  H_row's k'th element.
  """
  # This call successively applies each of the
  # Givens rotations stored in givens[:, :k] to H_col.

  def apply_ith_rotation(i, H_row):
    cs, sn = givens[i, :]
    H_i = cs * H_row[i] - sn * H_row[i + 1]
    H_ip1 = sn * H_row[i] + cs * H_row[i + 1]
    H_row = H_row.at[i].set(H_i)
    H_row = H_row.at[i + 1].set(H_ip1)
    return H_row

  R_row = lax.fori_loop(0, k, apply_ith_rotation, H_row)

  def givens_rotation(v1, v2):
    t = jnp.sqrt(v1**2 + v2**2)
    cs = v1 / t
    sn = -v2 / t
    return cs, sn
  givens_factors = givens_rotation(R_row[k], R_row[k + 1])
  givens = givens.at[k, :].set(givens_factors)
  cs_k, sn_k = givens_factors

  R_row = R_row.at[k].set(cs_k * R_row[k] - sn_k * R_row[k + 1])
  R_row = R_row.at[k + 1].set(0.)
  return R_row, givens


def _gmres_qr(A, b, x0, unit_residual, residual_norm, inner_tol, restart, M):
  """
  Implements a single restart of GMRES. The restart-dimensional Krylov subspace
  K(A, x0) = span(A(x0), A@x0, A@A@x0, ..., A^restart @ x0) is built, and the
  projection of the true solution into this subspace is returned.
  """
  # https://www-users.cs.umn.edu/~saad/Calais/PREC.pdf
  #  residual = _sub(b, A(x0))
  #  unit_residual, beta = _safe_normalize(residual, return_norm=True)

  V = tree_map(
    lambda x: jnp.pad(x[..., None], ((0, 0),) * x.ndim + ((0, restart),)),
    unit_residual,
  )
  dtype = jnp.result_type(*tree_leaves(b))
  R = jnp.eye(restart, restart + 1, dtype=dtype) # eye to avoid constructing
                                                 # a singular matrix in case
                                                 # of early termination.
  b_norm = _norm_tree(b)

  givens = jnp.zeros((restart, 2), dtype=dtype)
  beta_vec = jnp.zeros((restart + 1), dtype=dtype)
  beta_vec = beta_vec.at[0].set(residual_norm)

  def loop_cond(carry):
    k, err, _, _, _, _ = carry
    return lax.cond(k < restart,
                    lambda x: x[0] > x[1],
                    lambda x: False,
                    (err, inner_tol))
    # return k < restart and err > tol

  def arnoldi_qr_step(carry):
    k, residual_norm, V, R, beta_vec, givens = carry
    V, H, _ = kth_arnoldi_iteration(k, A, M, V, R, inner_tol)
    R_row, givens = apply_givens_rotations(H[k, :], givens, k)
    R = R.at[k, :].set(R_row[:])
    cs, sn = givens[k, :] * beta_vec[k]
    beta_vec = beta_vec.at[k].set(cs)
    beta_vec = beta_vec.at[k + 1].set(sn)
    err = jnp.abs(sn) / b_norm
    return k + 1, err, V, R, beta_vec, givens

  carry = (0, residual_norm, V, R, beta_vec, givens)
  carry = lax.while_loop(loop_cond, arnoldi_qr_step, carry)
  k, residual_norm, V, R, beta_vec, _ = carry

  y = jsp.linalg.solve_triangular(R[:, :-1].T, beta_vec[:-1])
  Vy = tree_map(lambda X: _dot(X[..., :-1], y), V)
  dx = M(Vy)

  x = _add(x0, dx)
  residual = _sub(b, A(x))
  unit_residual, residual_norm = _safe_normalize(residual, return_norm=True)
  return x, unit_residual, residual_norm


def _gmres_plain(A, b, x0, unit_residual, residual_norm, inner_tol, restart, M):
  """
  Implements a single restart of GMRES. The restart-dimensional Krylov subspace
  K(A, x0) = span(A(x0), A@x0, A@A@x0, ..., A^restart @ x0) is built, and the
  projection of the true solution into this subspace is returned.
  """
  # https://www-users.cs.umn.edu/~saad/Calais/PREC.pdf
  V = tree_map(
    lambda x: jnp.pad(x[..., None], ((0, 0),) * x.ndim + ((0, restart),)),
    unit_residual,
  )
  dtype = jnp.result_type(*tree_leaves(b))
  H = jnp.eye(restart, restart + 1, dtype=dtype)

  def loop_cond(carry):
    V, H, breakdown, k = carry
    return lax.cond(k < restart,
                    lambda x: ~x,
                    lambda x: False,
                    breakdown)

  def arnoldi_process(carry):
    V, H, _, k = carry
    V, H, breakdown = kth_arnoldi_iteration(k, A, M, V, H, inner_tol)
    return V, H, breakdown, k + 1

  carry = (V, H, False, 0)
  V, H, _, _ = lax.while_loop(loop_cond, arnoldi_process, carry)

  beta_vec = jnp.zeros((restart,), dtype=dtype)
  beta_vec = beta_vec.at[0].set(residual_norm) # it really is the original value
  y = jsp.linalg.solve(H[:, :-1].T, beta_vec)
  Vy = tree_map(lambda X: _dot(X[..., :-1], y), V)
  dx = M(Vy)
  x = _add(x0, dx)

  residual = _sub(b, A(x))
  unit_residual, residual_norm = _safe_normalize(residual, return_norm=True)
  return x, unit_residual, residual_norm


def _gmres_solve(A, b, x0, outer_tol, inner_tol, restart, maxiter, M,
                 gmres_func):
  """
  The main function call wrapped by custom_linear_solve. Repeatedly calls GMRES
  to find the projected solution within the order-``restart``
  Krylov space K(A, x0, restart), using the result of the previous projection
  in place of x0 each time.
  """
  residual = _sub(b, A(x0))
  unit_residual, residual_norm = _safe_normalize(residual, return_norm=True)

  def cond_fun(value):
    _, k, _, residual_norm = value
    return lax.cond(k < maxiter,
                    lambda x: x[0] > x[1],
                    lambda x: False,
                    (residual_norm, outer_tol))

  def body_fun(value):
    x, k, unit_residual, residual_norm = value
    x, unit_residual, residual_norm = gmres_func(A, b, x, unit_residual,
                                                 residual_norm, inner_tol,
                                                 restart, M)
    return x, k + 1, unit_residual, residual_norm

  initialization = (x0, 0, unit_residual, residual_norm)
  x_final, k, _, err = lax.while_loop(cond_fun, body_fun, initialization)
  # info = lax.cond(converged, lambda y: 0, lambda y: k, 0)
  return x_final  # , info


def gmres(A, b, x0=None, *, tol=1e-5, atol=0.0, restart=20, maxiter=None,
          M=None, qr_mode=False):
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
      cost, but may be necessary for convergence. If qr_mode is
      True, the algorithm terminates
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
  qr_mode : bool
      If True, the algorithm builds an internal Krylov subspace using a QR
      based algorithm, which reduces overhead and improved stability. However,
      it may degrade performance significantly on GPUs or TPUs, in which case
      this flag should be set False.

  See also
  --------
  scipy.sparse.linalg.gmres
  jax.lax.custom_linear_solve
  """

  if x0 is None:
    x0 = tree_map(jnp.zeros_like, b)
  if M is None:
    M = _identity

  try:
    size = sum(bi.size for bi in tree_leaves(b))
  except AttributeError:
    size = len(tree_leaves(b))

  if maxiter is None:
    maxiter = 10 * size  # copied from scipy
  restart = min(restart, size)

  if tree_structure(x0) != tree_structure(b):
    raise ValueError(
      'x0 and b must have matching tree structure: '
      f'{tree_structure(x0)} vs {tree_structure(b)}')

  b, x0 = device_put((b, x0))
  b_norm = _norm_tree(b)
  if b_norm == 0:
    return b, 0
  outer_tol = jnp.maximum(tol * b_norm, atol)

  Mb = M(b)
  Mb_norm = _norm_tree(Mb)
  inner_tol = Mb_norm * min(1.0, outer_tol / b_norm)

  if qr_mode:
    def _solve(A, b):
      return _gmres_solve(A, b, x0, outer_tol, inner_tol, restart, maxiter, M,
                          _gmres_plain)
  else:
    def _solve(A, b):
      return _gmres_solve(A, b, x0, outer_tol, inner_tol, restart, maxiter, M,
                          _gmres_qr)

  x = lax.custom_linear_solve(A, b, solve=_solve, transpose_solve=_solve)

  failed = jnp.isnan(_norm_tree(x))
  info = lax.cond(failed, lambda x: -1, lambda x: 0, 0)
  return x, info
