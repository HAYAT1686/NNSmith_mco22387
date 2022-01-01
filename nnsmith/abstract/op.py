from abc import ABC, abstractmethod
from enum import Enum
from functools import reduce
import functools
from typing import List, Tuple, Union, Callable, Type
from inspect import signature
import random
import itertools
import warnings
import os

# Import z3 ahead of torch (See https://github.com/Z3Prover/z3/issues/5656)
import z3
import torch

from nnsmith.error import SanityCheck, ConstraintCheck
# Recommended resources: https://theory.stanford.edu/~nikolaj/programmingz3.html
# Another plausible tool (Interval Analysis): https://simon-rohou.fr/research/tubex-lib/doc/toctree.html
# Please follow the PyTorch API conventions: https://pytorch.org/docs/stable/nn.html

# There are following types of constraints at this point:
# 1. Shape variables must be greater than 0;
# 2. Shape variables must avoid devision by 0;
# 3. Intra-input shape constraints; e.g., add(x, y) where x.shape() must be equal to y.shape();
# 4. Extra constraints introduced by individual operators;

# FIXME: Z3 solving is way slower than numerical computing. Try to use exceptions to reject invalid inputs;
# TODO: add interval analysis for shape dimension size;

ARITH_MAX_WIDTH: int = 64
_INFERRED = False


def align_bvs(left: Union[float, int, z3.ExprRef], right: Union[float, int, z3.ExprRef], carry=False, mult=False):
    left_is_arith = isinstance(left, (int, float, z3.ArithRef))
    right_is_arith = isinstance(right, (int, float, z3.ArithRef))
    # If both values are of arithmetic type, we do not need to do anything.
    if left_is_arith and right_is_arith:
        return (left, right)
    # We assume that the width of an arithmetic type is ARITH_MAX_WIDTH.
    if left_is_arith:
        left_size = ARITH_MAX_WIDTH
    elif isinstance(left, z3.BitVecRef):
        left_size = left.size()
    else:
        raise RuntimeError(
            f"Unsupported alignment value {left} of type {type(left)}")
    # We assume that the width of an arithmetic type is ARITH_MAX_WIDTH.
    if right_is_arith:
        right_size = ARITH_MAX_WIDTH
    elif isinstance(right, z3.BitVecRef):
        right_size = right.size()
    else:
        raise RuntimeError(
            f"Unsupported alignment value {right} of type {type(right)}")
    # Extend the bitvector that is smaller with the necessary amount of zeroes.
    SanityCheck.true(not (
        carry and mult), "Carry and multiplication extension are mutually exclusive")
    SanityCheck.le(left_size, ARITH_MAX_WIDTH,
                   f"Bitvector sizes must not exceed {ARITH_MAX_WIDTH} bits.")
    SanityCheck.le(right_size, ARITH_MAX_WIDTH,
                   f"Bitvector sizes must not exceed {ARITH_MAX_WIDTH} bits.")
    diff = left_size - right_size
    if left_is_arith and diff >= 0:
        right = z3.ZeroExt(diff, right)
        return left, right
    if right_is_arith and diff <= 0:
        left = z3.ZeroExt(abs(diff), left)
        return left, right
    if diff < 0:
        left = z3.ZeroExt(abs(diff), left)
    elif diff > 0:
        right = z3.ZeroExt(diff, right)
    if carry and max(left_size, right_size) < ARITH_MAX_WIDTH:
        left = z3.ZeroExt(1, left)
        right = z3.ZeroExt(1, right)
    if mult:
        max_val = min(max(left_size, right_size), ARITH_MAX_WIDTH)
        left = z3.ZeroExt(max_val, left)
        right = z3.ZeroExt(max_val, right)
    return (left, right)


def nnsmith_mul(left: Union[float, int, z3.ExprRef], right: Union[float, int, z3.ExprRef]):
    left, right = align_bvs(left, right, mult=True)
    return left * right


def nnsmith_add(left: Union[float, int, z3.ExprRef], right: Union[float, int, z3.ExprRef]):
    left, right = align_bvs(left, right, carry=True)
    return left + right


def nnsmith_sub(left: Union[float, int, z3.ExprRef], right: Union[float, int, z3.ExprRef]):
    left, right = align_bvs(left, right)
    return left - right


def nnsmith_eq(left: Union[float, int, z3.ExprRef], right: Union[float, int, z3.ExprRef]):
    left, right = align_bvs(left, right)
    return left == right


def nnsmith_neq(left: Union[float, int, z3.ExprRef], right: Union[float, int, z3.ExprRef]):
    left, right = align_bvs(left, right)
    return left != right


def nnsmith_ge(left: Union[float, int, z3.ExprRef], right: Union[float, int, z3.ExprRef]):
    left, right = align_bvs(left, right)
    if isinstance(left, z3.BitVecRef) or isinstance(right, z3.BitVecRef):
        return z3.UGE(left, right)
    return left >= right


def nnsmith_gt(left: Union[float, int, z3.ExprRef], right: Union[float, int, z3.ExprRef]):
    left, right = align_bvs(left, right)
    if isinstance(left, z3.BitVecRef) or isinstance(right, z3.BitVecRef):
        return z3.UGT(left, right)
    return left > right


def nnsmith_le(left: Union[float, int, z3.ExprRef], right: Union[float, int, z3.ExprRef]):
    left, right = align_bvs(left, right)
    if isinstance(left, z3.BitVecRef) or isinstance(right, z3.BitVecRef):
        return z3.ULE(left, right)
    return left <= right


def nnsmith_lt(left: Union[float, int, z3.ExprRef], right: Union[float, int, z3.ExprRef]):
    left, right = align_bvs(left, right)
    if isinstance(left, z3.BitVecRef) or isinstance(right, z3.BitVecRef):
        return z3.ULT(left, right)
    return left < right


def nnsmith_div(left: Union[float, int, z3.ExprRef], right: Union[float, int, z3.ExprRef]):
    left, right = align_bvs(left, right)
    if isinstance(left, z3.BitVecRef) or isinstance(right, z3.BitVecRef):
        return z3.UDiv(left, right)
    if isinstance(left, int) and isinstance(right, int):
        return left // right
    return left / right


def nnsmith_mod(left: Union[float, int, z3.ExprRef], right: Union[float, int, z3.ExprRef]):
    left, right = align_bvs(left, right)
    if isinstance(left, z3.BitVecRef) or isinstance(right, z3.BitVecRef):
        return z3.URem(left, right)
    return left % right


class DType(Enum):
    # float16 = 'float16'
    float32 = torch.float32
    float64 = torch.float64
    # int8 = 'int8'
    # int16 = 'int16'
    int32 = torch.int32
    int64 = torch.int64
    bool = torch.bool
    # complex64 = 'complex64'
    # complex128 = 'complex128'

    def __repr__(self) -> str:
        return self.name


DTypeComb = Tuple[DType, ...]

DTYPE_ALL = list(DType.__members__.values())
DTYPE_NON_BOOLS = [dtype for dtype in DTYPE_ALL if dtype != DType.bool]
DTYPE_FLOATS = [DType.float32, DType.float64]
DTYPE_INTS = [DType.int32, DType.int64]


class ShapeVar:
    def __init__(self, shape: List[Union[int, z3.ExprRef]], dtype: Union[DType, torch.dtype]):
        self.shape = list(shape)
        self.dtype = DType(dtype)

    def __repr__(self):
        return f'ShapeVar(shape={str(self.shape)}, dtype={self.dtype.value})'

    def gt_zero(self, no_replica=[]):
        ret = []
        for s in self.shape:
            if isinstance(s, z3.ExprRef):
                if not any(str(replica) == str(s) for replica in no_replica):
                    ret.append(nnsmith_gt(s, 0))
            else:
                ConstraintCheck.gt(s, 0)
        return ret

    def torch(self):
        # NOTE: Only for concrete shapes.
        return torch.Size(self.shape)

    def constains_symbol(self) -> bool:
        return any(isinstance(s, z3.ExprRef) for s in self.shape)

    def nelement(self):
        if len(self.shape) == 0:  # Scalar
            return 1
        return reduce(lambda x, y: nnsmith_mul(x, y), self.shape, 1)

    @staticmethod
    def from_torch(torch_tensor):
        return ShapeVar(list(torch_tensor.shape), torch_tensor.dtype)

    @property
    def ndims(self):
        return len(self.shape)


def check_shape_fn(func):
    def wrapper_check_shape_fn(self, input_shapes):
        SanityCheck.true(
            _INFERRED, "Please call auto_infer_in_dtypes before using this function")
        SanityCheck.true(self.out_dims, "Empty output dimensions in {}".format(
            self.__class__.__name__))
        SanityCheck.eq(len(input_shapes), len(self.inp_dims), "{} requires {} inputs, but got {}".format(
            self.__class__.__name__,
            len(self.inp_dims), len(input_shapes)))
        res = func(self, input_shapes)
        SanityCheck.eq(len(res), len(self.out_dims), "{} requires {} outputs, but got {}".format(
            self.__class__.__name__,
            len(self.out_dims), len(res)))
        return res
    return wrapper_check_shape_fn


def check_require_fn(func):
    def wrapper_check_require_fn(self, input_shapes):
        SanityCheck.eq(len(input_shapes), len(self.inp_dims), "{} requires {} inputs, but got {}".format(
            self.__class__.__name__,
            len(self.inp_dims), len(input_shapes)))
        return func(self, input_shapes)
    return wrapper_check_require_fn


def _prepend_to(x, max_dim):
    return [1 for i in range(max_dim - len(x))] + x


def z3_bcast(x: Union[int, z3.ExprRef], y: Union[int, z3.ExprRef], *args: Union[int, z3.ExprRef]):
    return z3.If(y == 1, x, y) if len(args) == 0 else z3_bcast(z3_bcast(x, y), *args)


def broadcast_shapes(*shapes: List[Union[z3.ExprRef, int]]) -> List[Union[z3.ExprRef, int]]:
    """this function does not check the validity of broadcast. Please always pair it with broadcast_cons"""
    SanityCheck.gt(len(shapes), 0)
    if len(shapes) == 1:
        return shapes[0]
    max_dim = max(map(lambda x: len(x), shapes))
    max_shape = [None] * (max_dim)
    for j in range(max_dim):
        i = -j - 1
        args_dim_sz = [_prepend_to(x, max_dim)[i] for x in shapes]
        if any(isinstance(s, z3.ExprRef) for s in args_dim_sz):
            max_shape[i] = z3.simplify(z3_bcast(*args_dim_sz))
        else:
            max_shape[i] = max(*args_dim_sz)
    return max_shape


def broadcast_cons(*shapes: List[Union[z3.ExprRef, int]]) -> List[z3.ExprRef]:
    tgt_shape = broadcast_shapes(*shapes)
    cons = []
    max_dim = len(tgt_shape)
    for j in range(max_dim):
        i = -j - 1
        if isinstance(tgt_shape[i], z3.ExprRef):
            axis_cons = []
            for x in shapes:
                if len(x) > j:
                    axis_cons.append(
                        z3.Or(nnsmith_eq(x[i], tgt_shape[i]), x[i] == 1))
            axis_cons = z3.simplify(z3.And(*axis_cons))
            cons.append(axis_cons)
        else:
            args_dim_sz = [_prepend_to(x, max_dim)[i] for x in shapes]
            valid = all(s == tgt_shape[i] or s == 1 for s in args_dim_sz)
            # TODO(JK): enable this after fixing issue #2
            # assert valid, "Invalid broadcast shapes {}. Specific dim sizes: {}".format(shapes, args_dim_sz)
            cons.append(z3.BoolVal(valid))
    return cons


def broadcast_cons_binary(*shapes: List[Union[z3.ExprRef, int]]) -> List[z3.ExprRef]:
    SanityCheck.eq(len(shapes), 2)
    tgt_shape = broadcast_shapes(*shapes)
    cons = []
    max_dim = len(tgt_shape)
    lhs, rhs = shapes
    lhs = _prepend_to(lhs, max_dim)
    rhs = _prepend_to(rhs, max_dim)
    for j in range(max_dim):
        i = -j - 1
        if isinstance(tgt_shape[i], z3.ExprRef):
            cons.append(z3.simplify(
                z3.Or(lhs[i] == 1, rhs[i] == 1, nnsmith_eq(lhs[i], rhs[i]))))
        else:
            valid = lhs[i] == 1 or rhs[i] == 1 or nnsmith_eq(lhs[i], rhs[i])
            # TODO(JK): enable this after fixing issue #2
            # assert valid, "Invalid broadcast shapes lhs={}, rhs={}".format(lhs, rhs)
            cons.append(z3.BoolVal(valid))
    return cons


def broadcast_to_cons(*shapes: List[Union[z3.ExprRef, int]]) -> List[z3.ExprRef]:
    """Unidirectional broadcast. Last input is the target shape.

    Examples of valid unidirectional broadcast:
    [1, 2, 3] -> [0, 1, 2, 3]
    [1] -> [3]

    Examples of invalid unidirectional broadcast:
    [0, 1, 2, 3] -> [1, 2, 3]
    [3] -> [1]

    Logic: for each dim: src_dim == tgt_dim or src_dim == 1
    """
    srcs, tgt = shapes[:-1], shapes[-1]
    cons = []
    max_dim = len(tgt)
    for src in srcs:
        ConstraintCheck.true(len(src) <= max_dim)
        src = _prepend_to(src, max_dim)
        for i in range(max_dim):
            if isinstance(tgt[i], z3.ExprRef) or isinstance(src[i], z3.ExprRef):
                cons.append(z3.simplify(
                    z3.Or(src[i] == 1, nnsmith_eq(src[i], tgt[i]))))
            else:
                valid = src[i] == 1 or nnsmith_eq(src[i], tgt[i])
                # TODO(JK): enable this after fixing issue #2
                # assert valid, "Invalid broadcast shapes lhs={}, rhs={}".format(lhs, rhs)
                cons.append(z3.BoolVal(valid))
    return cons


class AbsOpBase(ABC):
    # whether this op is broadcastable or not
    bcastable = False
    # input dtypes: enumerates all possible input dtype combinations. Size of the list is the number of combinations.
    # Each element is a tuple of allowed input dtypes. NOTE: len(list) can >= the # of inputs, for handling ops with arbitrary arity.
    # For example, [(DType.float32, DType.float32), (DType.float64, DType.float64), (DType.int32, DType.int32)] means that
    # this op can accept one of float32xfloat32, float64xfloat64, and int32xint32 as input dtypes.
    in_dtypes: List[DTypeComb] = None  # Overwrite me!

    def __init__(self):
        # `[3, 3]` this means this op requires 2 inputs. Where the 1st one has 2 dimensions, and the 2nd one has 3 dimensions.
        # `-1` means arbitrary dimantions; NOTE: but should be concretized during execution.
        # All symbols of correponding operator must be the constructor's parameters.
        self.inp_dims = []
        # NOTE: the concrete values of out_dims are not useful. Just make sure the length is correct.
        # NOTE: the output shape of input dimensions should be concretized during the execution.
        self.out_dims = []
        # Require the input dimension sizes to be equivalent.
        self.same_inp_dims = False
        # NOTE: the input of operator constructors are all Union[int, z3.ExprRef].
        self.extra_attrs = {}

    @abstractmethod  # Overload me!
    # Exception means rejection.
    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        raise NotImplementedError

    @check_shape_fn  # Public API.
    def shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        return self._shape_fn(input_shapes)

    # Overload me!
    # Extra constraints for the input tensors.
    # Exception means rejection.
    def _requires(self, input_shapes: List[ShapeVar]) -> List[z3.ExprRef]:
        return []

    @abstractmethod
    def torch(self) -> Callable[..., torch.Tensor]:
        raise NotImplementedError

    @check_require_fn  # Public API.
    def requires(self, input_shapes):
        return self._requires(input_shapes)

    def post_symbolize(self, input_shapes: List[ShapeVar], prefix: str = None, new_sym=z3.Int):
        """Post symbolize this op according to the input shapes."""
        pass

    def custom_concretize(self, model, symbolic_op):
        """Custom concretize this op using the given z3 model and symbolic op."""
        pass

    def __repr__(self) -> str:
        return self.__class__.__name__


def concretize(op: AbsOpBase, model: z3.ModelRef) -> AbsOpBase:
    construct_param_dict = signature(op.__init__).parameters
    values = []
    symbolic_idx = []
    for idx, key in enumerate(construct_param_dict):
        param = getattr(op, key)
        values.append(param)
        if isinstance(param, z3.ExprRef):
            symbolic_idx.append(idx)
    for idx in symbolic_idx:
        values[idx] = model.eval(values[idx]).as_long()

    concrete_op = op.__class__(*values)
    concrete_op.inp_dims = op.inp_dims
    concrete_op.out_dims = op.out_dims
    concrete_op.same_inp_dims = op.same_inp_dims
    concrete_op.extra_attrs = op.extra_attrs
    concrete_op.custom_concretize(model, op)

    return concrete_op


class UnaryOpBase(AbsOpBase):
    def __init__(self):
        super().__init__()
        self.out_dims = [-1]


class BinaryOpBase(AbsOpBase):
    def __init__(self):
        super().__init__()
        self.out_dims = [-1]


class TernaryOpBase(AbsOpBase):
    def __init__(self):
        super().__init__()
        self.out_dims = [-1]


class ElementWiseUnaryOp(UnaryOpBase):
    def __init__(self):
        super().__init__()
        self.inp_dims = [-1]
        self.out_dims = [-1]

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        SanityCheck.eq(len(input_shapes), 1)
        return [input_shapes[0]]


# class ElementWiseBinaryOp(BinaryOpBase):
#     def __init__(self):
#         super().__init__()
#         self.inp_dims = [-1, -1]
#         self.same_inp_dims = True

#     def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
#         assert len(input_shapes[0].shape) == len(input_shapes[1].shape)
#         return [input_shapes[0]]

#     def _requires(self, input_shapes):
#         assert len(input_shapes[0].shape) == len(input_shapes[1].shape)
#         ret = []
#         for l, r in zip(input_shapes[0].shape, input_shapes[1].shape):
#             if isinstance(l, z3.ExprRef) or isinstance(r, z3.ExprRef):
#                 ret.append(nnsmith_eq(l, r))
#             else:
#                 assert l == r
#         return ret

class BcastBinaryOp(BinaryOpBase):
    bcastable = True
    # by default, output dtype is the same as the first input dtype
    _bcast_out_dtypes = None

    def __init__(self):
        super().__init__()
        self.inp_dims = [-1, -1]
        self.same_inp_dims = False
        self.bcastable = True

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        tgt_shape = broadcast_shapes(*(ish.shape for ish in input_shapes))
        dtype = input_shapes[0].dtype if self._bcast_out_dtypes is None else self._bcast_out_dtypes[0]
        return [ShapeVar(tgt_shape, dtype)]

    def _requires(self, input_shapes):
        return broadcast_cons_binary(*(ish.shape for ish in input_shapes))


class BcastBinaryOp1(BcastBinaryOp):  # +-*/ max min
    in_dtypes = [(i, i) for i in DTYPE_NON_BOOLS]
    _bcast_out_dtypes = None


class BcastBinaryOp2(BcastBinaryOp):  # > < =
    in_dtypes = [(i, i) for i in DTYPE_ALL]
    _bcast_out_dtypes = [DType.bool]


class BcastBinaryOp3(BcastBinaryOp):  # logical and or xor
    in_dtypes = [(DType.bool, DType.bool)]
    _bcast_out_dtypes = [DType.bool]


class Where(TernaryOpBase):
    bcastable = True
    in_dtypes = [(DType.bool, i, i) for i in DTYPE_NON_BOOLS]

    def __init__(self):
        super().__init__()
        self.inp_dims = [-1, -1, -1]
        self.same_inp_dims = False
        self.same_inp_dtypes = True
        self.bcastable = True

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        # assert len(input_shapes[0].shape) == len(input_shapes[1].shape)
        tgt_shape = broadcast_shapes(*(ish.shape for ish in input_shapes))
        dtype = input_shapes[1].dtype
        return [ShapeVar(tgt_shape, dtype)]

    def _requires(self, input_shapes):
        return broadcast_cons(*(ish.shape for ish in input_shapes)) \
            + [input_shapes[1].dtype == input_shapes[2].dtype]

    def torch(self):
        return torch.where


# bcast binary ops from https://github.com/onnx/onnx/blob/master/docs/Broadcasting.md
# TODO bitwise_and/or/xor?
Add = type('Add', (BcastBinaryOp1,), {'torch': lambda self: torch.add})
Sub = type('Sub', (BcastBinaryOp1,), {'torch': lambda self: torch.sub})
Mul = type('Mul', (BcastBinaryOp1,), {'torch': lambda self: torch.mul})
# FIXME: Div will cause fuzzing crash.
Div = type('Div', (BcastBinaryOp1,), {
    'torch': lambda self:
        lambda x, y: torch.div(x, y, rounding_mode='floor' if DType(x.dtype) in DTYPE_INTS else None)})
# NOTE(JK): didn't find multi-input version of Max and Min in torch, so assume binary ops
Max = type('Max', (BcastBinaryOp1,), {'torch': lambda self: torch.max})
Min = type('Min', (BcastBinaryOp1,), {'torch': lambda self: torch.min})

Equal = type('Equal', (BcastBinaryOp2,), {'torch': lambda self: torch.eq})
Greater = type('Greater', (BcastBinaryOp2,), {'torch': lambda self: torch.gt})
Less = type('Less', (BcastBinaryOp2,), {'torch': lambda self: torch.lt})

And = type('And', (BcastBinaryOp3,), {'torch': lambda self: torch.logical_and})
Or = type('Or', (BcastBinaryOp3,), {'torch': lambda self: torch.logical_or})
Xor = type('Xor', (BcastBinaryOp3,), {'torch': lambda self: torch.logical_xor})

Pow = type('Pow', (BcastBinaryOp,), {'torch': lambda self: torch.pow})
# TODO: support exactly what onnx spec says (e.g., int support in the rhs)
Pow.in_dtypes = [(i, i) for i in DTYPE_FLOATS]
# lhs_dtypes = (DType.int32, DType.int64, DType.float32, DType.float64)
# rhs_dtypes = (DType.int32, DType.int64, DType.float32, DType.float64)
# Pow.in_dtypes = itertools.product(lhs_dtypes, rhs_dtypes)

# NOTE(JK): For Mean and Sum there is no corresponding torch op, so we ignore them
# Sum = type('Sum', (BcastBinaryOp,), {'torch': lambda self: torch.sum})
# Mean = type('Mean', (BcastBinaryOp,), {'torch': lambda self: torch.mean})


class StopFoldConst(torch.nn.Module):
    def __init__(self, data):
        super().__init__()
        self.dtype = data.dtype
        self.param = torch.nn.parameter.Parameter(data, requires_grad=False)

    @torch.no_grad()
    def forward(self):
        return self.param.to(self.dtype)


class Constant(AbsOpBase):
    in_dtypes = [()]

    def __str__(self) -> str:
        return super().__str__() + ' ' + str(self.extra_attrs)

    def __init__(self, dim: int):
        super().__init__()
        self.inp_dims = []
        self.out_dims = [dim]
        self.extra_attrs = {'dtype': random.choice(DTYPE_ALL)}

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        SanityCheck.eq(len(input_shapes), 0)
        return [self.shape_var]

    def _requires(self, input_shapes: List[ShapeVar]) -> List[z3.ExprRef]:
        SanityCheck.eq(len(input_shapes), 0)
        return []

    def torch(self) -> Callable[..., torch.Tensor]:
        data = torch.randn(self.shape_var.shape).to(self.shape_var.dtype.value)
        return StopFoldConst(data)


class Constant0D(Constant):
    def __init__(self):
        super().__init__(0)
        # TODO more dtypes

    @property
    def shape_var(self):
        return ShapeVar([], dtype=self.extra_attrs['dtype'])


class Constant1D(Constant):
    def __init__(self, dim0: Union[int, z3.ExprRef]):
        super().__init__(1)
        self.dim0 = dim0

    @property
    def shape_var(self):
        return ShapeVar([self.dim0], dtype=self.extra_attrs['dtype'])


class Constant2D(Constant):
    def __init__(self, dim0: Union[int, z3.ExprRef], dim1: Union[int, z3.ExprRef]):
        super().__init__(2)
        self.dim0 = dim0
        self.dim1 = dim1

    @property
    def shape_var(self):
        return ShapeVar(
            [self.dim0, self.dim1], dtype=self.extra_attrs['dtype'])


class Constant3D(Constant):
    def __init__(self, dim0: Union[int, z3.ExprRef], dim1: Union[int, z3.ExprRef], dim2: Union[int, z3.ExprRef]):
        super().__init__(3)
        self.dim0 = dim0
        self.dim1 = dim1
        self.dim2 = dim2

    @property
    def shape_var(self):
        return ShapeVar(
            [self.dim0, self.dim1, self.dim2], dtype=self.extra_attrs['dtype'])


class Constant4D(Constant):
    def __init__(self, dim0: Union[int, z3.ExprRef], dim1: Union[int, z3.ExprRef], dim2: Union[int, z3.ExprRef], dim3: Union[int, z3.ExprRef]):
        super().__init__(4)
        self.dim0 = dim0
        self.dim1 = dim1
        self.dim2 = dim2
        self.dim3 = dim3

    @property
    def shape_var(self):
        return ShapeVar(
            [self.dim0, self.dim1, self.dim2, self.dim3], dtype=self.extra_attrs['dtype'])


class Input(ElementWiseUnaryOp):
    in_dtypes = [()]

    def __init__(self, idx, dtype, dim0, dim1, dim2, dim3):
        super().__init__()
        self.inp_dims = []
        self.out_dims = [4]
        self.idx = idx
        self.dtype = dtype
        self.dim0 = dim0
        self.dim1 = dim1
        self.dim2 = dim2
        self.dim3 = dim3

    @ property
    def shape(self):
        return [self.dim0, self.dim1, self.dim2, self.dim3]

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        return [ShapeVar(self.shape, self.dtype)]

    def torch(self):
        raise NotImplementedError("This should never be called")


class ReLU(ElementWiseUnaryOp):
    # FIXME(JK): ints are somehow not supported in onnxruntime, which we use to gen inputs.
    # Make it include ints once we use other backends other than onnxruntime.
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.nn.ReLU()


class GELU(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.nn.GELU()


class LeakyReLU(ElementWiseUnaryOp):
    def __init__(self):
        """See https://pytorch.org/docs/stable/generated/torch.nn.LeakyReLU.html
        """
        super().__init__()
        self.negative_slope = 0.01

    def torch(self):
        return torch.nn.LeakyReLU(self.negative_slope)


class PReLU(ElementWiseUnaryOp):
    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.nn.PReLU()


class Sigmoid(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.sigmoid


class Softmax(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def _requires(self, input_shapes: List[ShapeVar]) -> List[z3.ExprRef]:
        ndims = input_shapes[0].ndims
        ConstraintCheck.true(ndims > 0)
        self.extra_attrs['dim'] = random.randint(
            0, ndims - 1)
        return super()._requires(input_shapes)

    def torch(self):
        return torch.nn.Softmax(dim=self.extra_attrs['dim'])


class Sin(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.sin


class Cos(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.cos


class Asin(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.asin


class Acos(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.acos


class Tan(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.tan


class Atan(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.atan


class Abs(ElementWiseUnaryOp):
    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.abs


class Ceil(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.ceil


class Clip(ElementWiseUnaryOp):
    def __init__(self):
        super().__init__()
        self.min = -1
        self.max = 1

    def torch(self):
        return lambda x: torch.clip(x, self.min, self.max)


class Round(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.round


class Sqrt(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.sqrt


class Log2(ElementWiseUnaryOp):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.log2


class Neg(ElementWiseUnaryOp):
    def __init__(self):
        super().__init__()

    def torch(self):
        return torch.neg


class Expand(UnaryOpBase, ABC):
    in_dtypes = [(i,) for i in DTYPE_ALL]
    # expand_dim cannot be symbolic. So just expand it.

    def __init__(self, expand_last_dim: int, expand_n: Union[int, z3.ExprRef]):
        """See https://pytorch.org/docs/stable/generated/torch.Tensor.expand.html
        """
        super().__init__()
        self.inp_dims = [-1]
        SanityCheck.ge(expand_last_dim, 1)
        self.expand_last_dim = expand_last_dim
        self.expand_n = expand_n

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        if self.expand_last_dim <= len(input_shapes[0].shape):
            input_shapes[0].shape[-self.expand_last_dim] = self.expand_n
            return input_shapes
        else:  # expand it;
            # for example. we have:
            #       input shape [u, v]
            #       expand_last_dim <- 4
            #       return [expand_n, 1, u, v] where `1` is padded.
            dtype = input_shapes[0].dtype
            return [ShapeVar([self.expand_n, *([1] * (self.expand_last_dim - len(input_shapes[0].shape) - 1)), *input_shapes[0].shape], dtype)]

    def _requires(self, input_shapes):
        SanityCheck.ge(self.expand_last_dim, 1)

        input_shape = input_shapes[0].shape
        if isinstance(self.expand_n, z3.ExprRef):
            if self.expand_last_dim <= len(input_shape):  # index valid
                cons = [z3.Or(
                    z3.And(
                        nnsmith_eq(input_shape[-self.expand_last_dim], 1),
                        nnsmith_ge(self.expand_n, 1)),
                    z3.And(
                        nnsmith_eq(
                            input_shape[-self.expand_last_dim], self.expand_n),
                        nnsmith_ge(self.expand_n, 1)))]
                return cons
        else:
            # It is also valid to expand to 0. But just too tricky...
            ConstraintCheck.ge(self.expand_n, 1)
            if self.expand_last_dim <= len(input_shape):
                ConstraintCheck.true(input_shape[-self.expand_last_dim] ==
                                     1 or input_shape[-self.expand_last_dim] == self.expand_n)
        return []

    def torch(self):
        return lambda x: x.expand(*self.shape_fn([ShapeVar.from_torch(x)])[0].shape)


class ExpandLast1(Expand):
    def __init__(self, expand_n: Union[int, z3.ExprRef]):
        super().__init__(expand_last_dim=1, expand_n=expand_n)


class ExpandLast2(Expand):
    def __init__(self, expand_n: Union[int, z3.ExprRef]):
        super().__init__(expand_last_dim=2, expand_n=expand_n)


class ExpandLast3(Expand):
    def __init__(self, expand_n: Union[int, z3.ExprRef]):
        super().__init__(expand_last_dim=3, expand_n=expand_n)


class ExpandLast4(Expand):
    def __init__(self, expand_n: Union[int, z3.ExprRef]):
        super().__init__(expand_last_dim=4, expand_n=expand_n)


class NCHWConv2d(UnaryOpBase):
    # FIXME: torch exporter does not support float64, may miss bugs
    in_dtypes = [(DType.float32,)]

    def __init__(self,
                 in_channels: Union[int, z3.ExprRef],
                 out_channels: Union[int, z3.ExprRef],
                 kernel_h_size: Union[int, z3.ExprRef],
                 kernel_w_size: Union[int, z3.ExprRef],
                 stride: Union[int, z3.ExprRef],
                 padding: Union[int, z3.ExprRef]):
        """See https://pytorch.org/docs/stable/generated/torch.nn.Conv2d.html
        """
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_h_size = kernel_h_size
        self.kernel_w_size = kernel_w_size
        self.stride = stride
        self.padding = padding

        self.inp_dims = [4]  # NCHW
        self.out_dims = [4]  # NCHW

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        # not symbolic
        if not isinstance(self.in_channels, z3.ExprRef) and not isinstance(input_shapes[0].shape[1], z3.ExprRef):
            ConstraintCheck.eq(input_shapes[0].shape[1], self.in_channels)

        is_symbolic_inp = input_shapes[0].constains_symbol() or isinstance(self.kernel_w_size, z3.ExprRef) or isinstance(
            self.kernel_h_size, z3.ExprRef) or isinstance(self.stride, z3.ExprRef) or isinstance(self.padding, z3.ExprRef)

        shape_var = ShapeVar([], dtype=input_shapes[0].dtype)
        # Batch dim: just copy
        shape_var.shape.append(input_shapes[0].shape[0])
        shape_var.shape.append(self.out_channels)        # Output channels
        if not is_symbolic_inp:
            shape_var.shape.append(
                (input_shapes[0].shape[2] - self.kernel_h_size + 2 * self.padding) // self.stride + 1)
            shape_var.shape.append(
                (input_shapes[0].shape[3] - self.kernel_w_size + 2 * self.padding) // self.stride + 1)
        else:
            shape_var.shape.append(
                (nnsmith_div(nnsmith_add(nnsmith_sub(input_shapes[0].shape[2], self.kernel_h_size), 2 * self.padding), self.stride) + 1))
            shape_var.shape.append(
                (nnsmith_div(nnsmith_add(nnsmith_sub(input_shapes[0].shape[3], self.kernel_w_size), 2 * self.padding), self.stride) + 1))
        return [shape_var]

    def _requires(self, input_shapes):
        cons = []
        ret = []
        # TODO: Use eager mode for debugging.
        cons.append(nnsmith_eq(self.in_channels, input_shapes[0].shape[1]))
        cons.append(nnsmith_ge(self.out_channels, 1))
        cons.append(nnsmith_ge(self.kernel_h_size, 1))
        cons.append(nnsmith_ge(self.kernel_w_size, 1))
        # TODO(JK): fix the dialation case for the kernel size constraints.
        cons.append(nnsmith_le(self.kernel_h_size,
                    nnsmith_add(input_shapes[0].shape[2], 2 * self.padding)))
        cons.append(nnsmith_le(self.kernel_w_size,
                    nnsmith_add(input_shapes[0].shape[3], 2 * self.padding)))
        cons.append(nnsmith_ge(self.stride, 1))
        cons.append(nnsmith_ge(self.padding, 0))
        # not too extreme to avoid torch exporter issue
        cons.append(nnsmith_le(self.padding, 255))
        for c in cons:
            if isinstance(c, z3.ExprRef):
                ret.append(c)
            else:
                ConstraintCheck.true(c)
        return ret

    def torch(self):
        return torch.nn.Conv2d(self.in_channels, self.out_channels, kernel_size=(self.kernel_h_size, self.kernel_w_size), stride=self.stride,
                               padding=self.padding)


class Reshape(UnaryOpBase, ABC):
    in_dtypes = [(i,) for i in DTYPE_ALL]

    def __init__(self):
        super().__init__()
        self.inp_dims = [-1]
        self.target_shape: List[Union[int, z3.ExprRef]]

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        if -1 not in self.target_shape:
            return [ShapeVar(self.target_shape, dtype=input_shapes[0].dtype)]
        # else
        shape_var = ShapeVar(self.target_shape, dtype=input_shapes[0].dtype)
        auto_dim = -1
        accum = 1
        for i, v in enumerate(self.target_shape):
            # TODO: What to do about bitvectors here?
            if v == -1:
                if auto_dim != -1:
                    raise ValueError(
                        "Only one auto-dim is allowed! "
                        "See https://pytorch.org/docs/stable/generated/torch.reshape.html")
                auto_dim = i
            else:
                accum = nnsmith_mul(accum, v)

        # First see if there's any symbols in the expression
        symbol_indices = []
        for v in input_shapes[0].shape:
            if isinstance(v, z3.ExprRef):
                symbol_indices.append(i)
        if len(symbol_indices) == 0:
            shape_var.shape[auto_dim] = reduce(
                lambda x, y: x * y, input_shapes[0].shape, 1) // accum
        else:
            shape_var.shape[auto_dim] = nnsmith_div(reduce(
                lambda x, y: nnsmith_mul(x, y), input_shapes[0].shape, 1), accum)

        return [shape_var]

    def _requires(self, input_shapes):
        # TODO: How to handle -1 with input shapes?
        # If your target shape is concrete, then your output shape's total pixels must be the same as the input shape's.
        if -1 not in self.target_shape:
            total_pixels = reduce(
                lambda x, y: nnsmith_mul(x, y), self.target_shape, 1)
            cons = [nnsmith_eq(total_pixels, reduce(
                lambda x, y: nnsmith_mul(x, y), input_shapes[0].shape, 1))]
            # should not be too extreme!
            __DIM_LIMIT__ = 4096
            lim = __DIM_LIMIT__
            for s in self.target_shape[::-1]:
                cons.append(nnsmith_le(s, lim))
                lim //= 2
                lim = max(lim, 1)
            return cons
        else:
            # If you use auto mode (specifying -1 for some dimensions), then the total number of input pixels must be exactly divisible by that of the output shape.
            minimul_pixels = reduce(
                lambda x, y: nnsmith_mul(x, y), [v for v in self.target_shape if v != -1], 1)
            return [nnsmith_eq(nnsmith_mod(reduce(lambda x, y: nnsmith_mul(x, y), input_shapes[0].shape, 1), minimul_pixels), 0)]

    def torch(self):
        return lambda x: x.reshape(*self.target_shape)


# Expand 6 times.
class Reshape1D(Reshape):
    # Inputs are target shape.
    def __init__(self, dim0: Union[int, z3.ExprRef]):
        super().__init__()
        self.dim0 = dim0
        self.target_shape = [dim0]
        self.out_dims = [1]


class Reshape2D(Reshape):
    def __init__(self, dim0: Union[int, z3.ExprRef], dim1: Union[int, z3.ExprRef]):
        super().__init__()
        self.dim0 = dim0
        self.dim1 = dim1
        self.target_shape = [dim0, dim1]
        self.out_dims = [2]


class Reshape3D(Reshape):
    def __init__(self, dim0: Union[int, z3.ExprRef], dim1: Union[int, z3.ExprRef], dim2: Union[int, z3.ExprRef]):
        super().__init__()
        self.dim0 = dim0
        self.dim1 = dim1
        self.dim2 = dim2
        self.target_shape = [dim0, dim1, dim2]
        self.out_dims = [3]


class Reshape4D(Reshape):
    def __init__(self, dim0: Union[int, z3.ExprRef], dim1: Union[int, z3.ExprRef], dim2: Union[int, z3.ExprRef],
                 dim3: Union[int, z3.ExprRef]):
        super().__init__()
        self.dim0 = dim0
        self.dim1 = dim1
        self.dim2 = dim2
        self.dim3 = dim3
        self.target_shape = [dim0, dim1, dim2, dim3]
        self.out_dims = [4]

# FIXME: Constraint too complex.


class Reshape5D(Reshape):
    def __init__(self, dim0: Union[int, z3.ExprRef], dim1: Union[int, z3.ExprRef], dim2: Union[int, z3.ExprRef],
                 dim3: Union[int, z3.ExprRef], dim4: Union[int, z3.ExprRef]):
        super().__init__()
        self.dim0 = dim0
        self.dim1 = dim1
        self.dim2 = dim2
        self.dim3 = dim3
        self.dim4 = dim4
        self.target_shape = [dim0, dim1, dim2, dim3, dim4]
        self.out_dims = [5]


class Transpose(UnaryOpBase, ABC):
    def __init__(self):
        """See https://pytorch.org/docs/stable/generated/torch.transpose.html
        """
        super().__init__()
        self.inp_dims = [-1]

    def _init_swap_dims(self, input_shape: List[Union[int, z3.ExprRef]]):
        ConstraintCheck.ge(len(input_shape), 2)
        if 'dim0' not in self.extra_attrs or 'dim1' not in self.extra_attrs:
            max_dim = len(input_shape) - 1
            self.extra_attrs['dim0'] = random.randint(0, max_dim)
            self.extra_attrs['dim1'] = (random.randint(
                1, max_dim) + self.extra_attrs['dim0']) % (1 + max_dim)
        return self.extra_attrs['dim0'], self.extra_attrs['dim1']

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        dim0, dim1 = self._init_swap_dims(input_shapes[0].shape)
        shape_var = input_shapes[0]
        shape_var.shape[dim0], shape_var.shape[dim1] = shape_var.shape[dim1], shape_var.shape[dim0]
        return [shape_var]

    def _requires(self, input_shapes):
        dim0, dim1 = self._init_swap_dims(input_shapes[0].shape)
        SanityCheck.ge(len(input_shapes[0].shape), max(
            dim0, dim1) + 1, f'dim={len(input_shapes[0].shape)}.transpose({dim0},{dim1})')
        return []

    def torch(self):
        def f(x: torch.Tensor):
            dim0, dim1 = self._init_swap_dims(list(x.shape))
            return x.transpose(dim0, dim1)
        return f


# Sum, Min, Max, Mean, ArgMin, ArgMax, Squeeze, Size

class ReduceBase(UnaryOpBase, ABC):
    _reduce_out_dtype = None  # None means same as input dtype

    def __init__(self, num_dim: int):
        super().__init__()
        SanityCheck.ge(num_dim, 1)
        self.num_dim = num_dim
        self.extra_attrs['reduce_dim'] = random.randint(0, self.num_dim - 1)

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        SanityCheck.eq(len(input_shapes[0].shape), self.num_dim)
        shape_var = input_shapes[0]
        svar_list = []
        for i, v in enumerate(shape_var.shape):
            if i != self.extra_attrs['reduce_dim']:
                svar_list.append(v)
        return [ShapeVar(svar_list,
                         input_shapes[0].dtype if self._reduce_out_dtype is None else self._reduce_out_dtype)]

    def _requires(self, input_shapes: List[ShapeVar]):
        SanityCheck.ge(len(input_shapes[0].shape), self.num_dim)
        return []


class SqueezeBase(ReduceBase, ABC):
    def _requires(self, input_shapes):
        SanityCheck.eq(len(input_shapes[0].shape), self.num_dim)
        if isinstance(input_shapes[0].shape[self.extra_attrs['reduce_dim']], z3.ExprRef):
            return [nnsmith_eq(input_shapes[0].shape[self.extra_attrs['reduce_dim']], 1)]
        else:
            ConstraintCheck.eq(
                input_shapes[0].shape[self.extra_attrs['reduce_dim']], 1)
        return []

    def torch(self):
        return lambda x: x.squeeze(self.extra_attrs['reduce_dim'])

# FIXME: Support 1D (vector -> scalar)


class Squeeze2D(SqueezeBase):
    def __init__(self):
        super().__init__(2)
        self.out_dims = [1]
        self.inp_dims = [2]


class Squeeze3D(SqueezeBase):
    def __init__(self):
        super().__init__(3)
        self.out_dims = [2]
        self.inp_dims = [3]


class Squeeze4D(SqueezeBase):
    def __init__(self):
        super().__init__(4)
        self.out_dims = [3]
        self.inp_dims = [4]


class Squeeze5D(SqueezeBase):
    def __init__(self):
        super().__init__(5)
        self.out_dims = [4]
        self.inp_dims = [5]


class ReduceSum(ReduceBase, ABC):
    # pytorch exporter doesn't support int32
    in_dtypes = [(i,) for i in DTYPE_NON_BOOLS if i != DType.int32]

    def torch(self):
        return lambda x: x.sum(self.extra_attrs['reduce_dim'])


class ReduceSum2D(ReduceSum):
    def __init__(self):
        super().__init__(2)
        self.out_dims = [1]
        self.inp_dims = [2]


class ReduceSum3D(ReduceSum):
    def __init__(self):
        super().__init__(3)
        self.out_dims = [2]
        self.inp_dims = [3]


class ReduceSum4D(ReduceSum):
    def __init__(self):
        super().__init__(4)
        self.out_dims = [3]
        self.inp_dims = [4]


class ReduceSum5D(ReduceSum):
    def __init__(self):
        super().__init__(5)
        self.out_dims = [4]
        self.inp_dims = [5]


class ReduceMin(ReduceBase, ABC):
    in_dtypes = [(i,) for i in DTYPE_NON_BOOLS]

    def torch(self):
        return lambda x: x.min(self.extra_attrs['reduce_dim']).values


class ReduceMin2D(ReduceMin):
    def __init__(self):
        super().__init__(2)
        self.out_dims = [1]
        self.inp_dims = [2]


class ReduceMin3D(ReduceMin):
    def __init__(self):
        super().__init__(3)
        self.out_dims = [2]
        self.inp_dims = [3]


class ReduceMin4D(ReduceMin):
    def __init__(self):
        super().__init__(4)
        self.out_dims = [3]
        self.inp_dims = [4]


class ReduceMin5D(ReduceMin):
    def __init__(self):
        super().__init__(5)
        self.out_dims = [4]
        self.inp_dims = [5]


class ReduceMax(ReduceBase, ABC):
    in_dtypes = [(i,) for i in DTYPE_NON_BOOLS]

    def torch(self):
        return lambda x: x.max(self.extra_attrs['reduce_dim']).values


class ReduceMax2D(ReduceMax):
    def __init__(self):
        super().__init__(2)
        self.out_dims = [1]
        self.inp_dims = [2]


class ReduceMax3D(ReduceMax):
    def __init__(self):
        super().__init__(3)
        self.out_dims = [2]
        self.inp_dims = [3]


class ReduceMax4D(ReduceMax):
    def __init__(self):
        super().__init__(4)
        self.out_dims = [3]
        self.inp_dims = [4]


class ReduceMax5D(ReduceMax):
    def __init__(self):
        super().__init__(5)
        self.out_dims = [4]
        self.inp_dims = [5]


class ReduceMean(ReduceBase, ABC):
    in_dtypes = [(i,) for i in DTYPE_FLOATS]

    def torch(self):
        return lambda x: x.mean(self.extra_attrs['reduce_dim'])


class ReduceMean1D(ReduceMean):
    def __init__(self):
        super().__init__(1)
        self.out_dims = [0]
        self.inp_dims = [1]


class ReduceMean2D(ReduceMean):
    def __init__(self):
        super().__init__(2)
        self.out_dims = [1]
        self.inp_dims = [2]


class ReduceMean3D(ReduceMean):
    def __init__(self):
        super().__init__(3)
        self.out_dims = [2]
        self.inp_dims = [3]


class ReduceMean4D(ReduceMean):
    def __init__(self):
        super().__init__(4)
        self.out_dims = [3]
        self.inp_dims = [4]


class ReduceMean5D(ReduceMean):
    def __init__(self):
        super().__init__(5)
        self.out_dims = [4]
        self.inp_dims = [5]


class ArgMin(ReduceBase, ABC):
    # FIXME(JK): ints are somehow not supported in onnxruntime, which we use to gen inputs.
    # Make it include ints once we use other backends other than onnxruntime.
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    _reduce_out_dtype = DType.int64

    def torch(self):
        return lambda x: x.argmin(self.extra_attrs['reduce_dim'])


class ArgMin2D(ArgMin):
    def __init__(self):
        super().__init__(2)
        self.out_dims = [1]
        self.inp_dims = [2]


class ArgMin3D(ArgMin):
    def __init__(self):
        super().__init__(3)
        self.out_dims = [2]
        self.inp_dims = [3]


class ArgMin4D(ArgMin):
    def __init__(self):
        super().__init__(4)
        self.out_dims = [3]
        self.inp_dims = [4]


class ArgMin5D(ArgMin):
    def __init__(self):
        super().__init__(5)
        self.out_dims = [4]
        self.inp_dims = [5]


class ArgMax(ReduceBase, ABC):
    # FIXME(JK): ints are somehow not supported in onnxruntime, which we use to gen inputs.
    # Make it include ints once we use other backends other than onnxruntime.
    in_dtypes = [(i,) for i in DTYPE_FLOATS]
    _reduce_out_dtype = DType.int64

    def torch(self):
        return lambda x: x.argmax(self.extra_attrs['reduce_dim'])


class ArgMax2D(ArgMax):
    def __init__(self):
        super().__init__(2)
        self.out_dims = [1]
        self.inp_dims = [2]


class ArgMax3D(ArgMax):
    def __init__(self):
        super().__init__(3)
        self.out_dims = [2]
        self.inp_dims = [3]


class ArgMax4D(ArgMax):
    def __init__(self):
        super().__init__(4)
        self.out_dims = [3]
        self.inp_dims = [4]


class ArgMax5D(ArgMax):
    def __init__(self):
        super().__init__(5)
        self.out_dims = [4]
        self.inp_dims = [5]


def partialclass(cls, *args, **kwds) -> Type[AbsOpBase]:

    class NewCls(cls):
        __init__ = functools.partialmethod(cls.__init__, *args, **kwds)

    return NewCls


class Concat(AbsOpBase):
    MAX_ARITY = 5
    in_dtypes = [tuple(i for _ in range(5))
                 for i in DTYPE_ALL]  # suport max concat 5 tensors

    def __str__(self) -> str:
        return 'Concat ' + str(self.extra_attrs)

    def __init__(self, arity):
        super().__init__()
        assert arity <= self.MAX_ARITY
        self.arity = arity
        self.inp_dims = [-1] * arity
        self.out_dims = [-1]
        self.same_inp_dims = True

    def _get_axis(self, ndim):
        if 'axis' not in self.extra_attrs:
            self.extra_attrs['axis'] = random.randint(0, ndim - 1)
        return self.extra_attrs['axis']

    def _requires(self, input_shapes: List[ShapeVar]) -> List[z3.ExprRef]:
        ndims = input_shapes[0].ndims
        ConstraintCheck.true(ndims > 0)
        axis = self._get_axis(ndims)
        assert ndims > axis

        assert all(s.ndims == ndims for s in input_shapes)
        assert len(input_shapes) == self.arity
        cons = []
        for d in range(ndims):
            if d != axis:
                cons.extend(nnsmith_eq(s.shape[d], input_shapes[0].shape[d])
                            for s in input_shapes)
        return cons

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        ndims = input_shapes[0].ndims
        ConstraintCheck.true(ndims > 0)
        axis = self._get_axis(ndims)
        os = ShapeVar(input_shapes[0].shape, input_shapes[0].dtype)
        os.shape[axis] = reduce(
            nnsmith_add, [s.shape[axis] for s in input_shapes])
        return [os]

    def torch(self):
        axis = self.extra_attrs['axis']
        return lambda *args: torch.cat(args, axis)


# NOTE(JK) This is ugly. I think the root cause is we are using a class to represent a node type that we want to insert.
# A more flexible approach is to use an instance. For example, to represent Expand node types, instead of classes [ExpandLast1, ExpandLast2, ...],
# use instances [Expand(expand_last_dim=1, expand_n=Placeholder), Expand(2, Placeholder), ...], where the Placeholder represents the params needing z3 to model.

Concat1 = partialclass(Concat, 1)
Concat2 = partialclass(Concat, 2)
Concat3 = partialclass(Concat, 3)
Concat4 = partialclass(Concat, 4)
Concat5 = partialclass(Concat, 5)


class Cast(UnaryOpBase):
    in_dtypes = [(i,) for i in DTYPE_ALL]

    def __init__(self):
        super().__init__()
        self.inp_dims = [-1]
        self.out_dims = [-1]
        self.extra_attrs = {'to': random.choice(DTYPE_ALL)}

    def __str__(self) -> str:
        return 'Cast ' + str(self.extra_attrs)

    def _requires(self, input_shapes: List[ShapeVar]) -> List[z3.ExprRef]:
        return []

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        assert len(input_shapes) == 1
        return [ShapeVar(input_shapes[0].shape, self.extra_attrs['to'])]

    def torch(self):
        return lambda x: x.to(dtype=self.extra_attrs['to'].value)


class Gemm(TernaryOpBase):
    # https://pytorch.org/docs/stable/generated/torch.addmm.html?highlight=addmm#torch.addmm
    in_dtypes = [(i, i, i) for i in DTYPE_NON_BOOLS]

    def __init__(self):
        super().__init__()
        self.inp_dims = [-1, 2, 2]
        self.out_dims = [2]

    def _set_or_get_extra_attrs(self, dtype=None):
        if 'alpha' not in self.extra_attrs:
            assert dtype is not None, 'dtype must be specified at the first time of this call'
            alpha = random.uniform(-2, 2)
            beta = random.uniform(-2, 2)
            if dtype in DTYPE_INTS:
                beta, alpha = int(beta), int(alpha)
            self.extra_attrs['alpha'] = alpha
            self.extra_attrs['beta'] = beta
        return self.extra_attrs

    def _requires(self, input_shapes: List[ShapeVar]):
        ConstraintCheck.true(input_shapes[0].ndims <= 2)
        out_shape = self.shape_fn(input_shapes)[0]
        cons = broadcast_to_cons(input_shapes[0].shape, out_shape.shape)

        # matmul constraint
        mat1, mat2 = input_shapes[1], input_shapes[2]
        cons.append(mat1.shape[1] == mat2.shape[0])
        self._set_or_get_extra_attrs(input_shapes[0].dtype.value)
        return cons

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        mat1, mat2 = input_shapes[1], input_shapes[2]
        return [ShapeVar([mat1.shape[0], mat2.shape[1]], input_shapes[0].dtype)]

    def torch(self):
        extra_attrs = self._set_or_get_extra_attrs()
        return lambda *args: torch.addmm(*args, beta=extra_attrs['beta'], alpha=extra_attrs['alpha'])


class Slice(UnaryOpBase):
    # pytorch slice always exported as a stack of single-dim slices, so only model sinlge-dim slice here
    # pytorch slice only supports forward slicing, so only model forward slicing here
    in_dtypes = [(i,) for i in DTYPE_ALL]
    INT_MAX = 2**63 - 1
    INT_MIN = -2**63

    def __init__(self, start, end, step):
        super().__init__()
        self.inp_dims = [-1]
        self.out_dims = [-1]
        self.start = start
        self.end = end
        self.step = step

    def __str__(self) -> str:
        tail = {'axis': self.extra_attrs['axis']}
        if isinstance(self.start, int):
            tail['start'] = self.start
        if isinstance(self.end, int):
            tail['end'] = self.end
        if isinstance(self.step, int):
            tail['step'] = self.step
        return super().__str__() + ' ' + str(tail)

    def _get_attrs(self, ndims):
        ConstraintCheck.true(ndims > 0)
        if 'axis' not in self.extra_attrs:
            self.extra_attrs['ndims'] = ndims
            self.extra_attrs['axis'] = random.randint(0, ndims - 1)
            self.extra_attrs['region'] = random.choice(
                ['left', 'mid', 'right'])
            if random.uniform(0, 1) < 0.1:
                # torch exporter does not support start=INT_MIN
                # if random.uniform(0, 1) < 0.5:
                #     # because pytorch only supports forward slicing,
                #     # start cannot be INT_MAX, otherwise it slices empty tensor
                #     self.start = self.INT_MIN
                # else:
                self.end = self.INT_MAX
        return self.extra_attrs['axis']

    def get_pos_eqv(self, inp):
        axis = self._get_attrs(inp.ndims)
        reg = self.extra_attrs['region']
        dim = inp.shape[axis]
        if not isinstance(self.start, int):
            # start_pos_eqv = z3.If(nnsmith_lt(self.start, 0),
            #                       nnsmith_add(self.start, dim), self.start)
            start_pos_eqv = nnsmith_add(
                self.start, dim) if reg != 'right' else self.start
        elif self.start not in [self.INT_MAX, self.INT_MIN]:
            start_pos_eqv = self.start + dim if self.start < 0 else self.start
        else:
            start_pos_eqv = 0 if self.start == self.INT_MIN else dim - 1
        if not isinstance(self.end, int):
            # end_pos_eqv = z3.If(nnsmith_lt(self.end, 0),
            #                     nnsmith_add(self.end, dim), self.end)
            end_pos_eqv = nnsmith_add(
                self.end, dim) if reg == 'left' else self.end
        elif self.end not in [self.INT_MAX, self.INT_MIN]:
            end_pos_eqv = self.end + dim if self.end < 0 else self.end
        else:
            end_pos_eqv = -1 if self.end == self.INT_MIN else dim
        return start_pos_eqv, end_pos_eqv

    def _requires(self, input_shapes: List[ShapeVar]):
        inp = input_shapes[0]
        axis = self._get_attrs(inp.ndims)
        reg = self.extra_attrs['region']
        cons = []
        dim = inp.shape[axis]
        # TODO: fix negative numbers when we switch to bit_vec
        # domain for start
        l, r = (0, nnsmith_sub(dim, 1)) if reg == 'right' else (-dim, -1)
        ll, rr = (-dim, -1) if reg == 'left' else (0,
                                                   dim)                # domain for end
        if not isinstance(self.start, int):
            cons.append(z3.And(  # start \in [l, r]
                nnsmith_ge(self.start, l),
                nnsmith_le(self.start, r)))
        if not isinstance(self.end, int):
            cons.append(z3.And(  # end \in [ll, rr]
                nnsmith_ge(self.end, ll),
                nnsmith_le(self.end, rr)))

        cons.append(nnsmith_ge(self.step, 1))  # forward slicing only
        cons.append(nnsmith_le(self.step, dim))
        return cons

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        inp = input_shapes[0]
        axis = self._get_attrs(inp.ndims)
        start_pos_eqv, end_pos_eqv = self.get_pos_eqv(inp)
        s = list(inp.shape)
        s[axis] = nnsmith_div(
            nnsmith_add(nnsmith_sub(end_pos_eqv, start_pos_eqv),
                        nnsmith_sub(self.step, 1)),
            self.step)
        return [ShapeVar(s, input_shapes[0].dtype)]

    def torch(self):
        s = tuple(slice(None, None) if i != self.extra_attrs['axis'] else slice(self.start, self.end, self.step)
                  for i in range(self.extra_attrs['ndims']))
        return lambda x: x[s]


class Pad(UnaryOpBase):
    in_dtypes = [(i,) for i in DTYPE_ALL if i != DType.int64]
    MODES = ['constant', 'reflect', 'replicate']  # onnx only supports these 3
    BLACK_LIST = {
        'constant': [],
        'reflect': [DType.int64, DType.int32, DType.bool],
        'replicate': [DType.int64, DType.int32, DType.bool],
    }

    def __init__(self):
        super().__init__()
        self.inp_dims = [-1]
        self.out_dims = [-1]

    def _init_extra_attrs(self, dtype: DType):
        self.extra_attrs['mode'] = random.choice(self.MODES)
        if self.extra_attrs['mode'] == 'constant':
            if dtype in DTYPE_INTS:
                value = random.randint(-1, 1)
            elif dtype == DType.bool:
                value = random.choice([0, 1])
            elif dtype in DTYPE_FLOATS:
                value = random.uniform(-1, 1)
            else:
                raise ValueError(f'unsupported dtype {dtype}')
        else:
            value = 0.0
        self.extra_attrs['value'] = value

    def post_symbolize(self, input_shapes: List[ShapeVar], prefix: str = '', new_sym=z3.Int):
        ndims, dtype = input_shapes[0].ndims, input_shapes[0].dtype
        ConstraintCheck.true(ndims > 0)
        self._init_extra_attrs(input_shapes[0].dtype)
        # special treatment since torch exporter has limited support for non-constant pad
        ConstraintCheck.true(
            dtype not in self.BLACK_LIST[self.extra_attrs['mode']])
        r = ndims
        if self.extra_attrs['mode'] != 'constant':
            r = min(r, 3)
        npads = random.randint(1, r)
        self.pad = [new_sym(prefix + f'pad_{i}_0') for i in range(npads * 2)]
        for i in range(npads):
            # disable padding for each dimension with probability 0.5
            if random.uniform(0, 1) < 0.5:
                self.pad[i * 2] = 0
                self.pad[i * 2 + 1] = 0

    def custom_concretize(self, model, symbolic_op):
        self.pad = [model.evaluate(p).as_long() if not isinstance(
            p, int) else p for p in symbolic_op.pad]

    def _requires(self, input_shapes: List[ShapeVar]) -> List[z3.ExprRef]:
        ndims = input_shapes[0].ndims
        pad = self.pad
        isv = input_shapes[0].shape
        assert len(pad) % 2 == 0, pad
        assert len(pad) // 2 <= len(isv), pad
        if self.extra_attrs['mode'] != 'constant':
            ConstraintCheck.true((len(pad) // 2) in [ndims - 1, ndims - 2])
        cons = []
        for i in range(len(self.pad) // 2):
            j = len(isv) - 1 - i
            # When using negative padding, neither side should erase more than the original size
            cons.append(nnsmith_ge(nnsmith_add(self.pad[i * 2], isv[j]), 0))
            cons.append(nnsmith_ge(nnsmith_add(
                self.pad[i * 2 + 1], isv[j]), 0))
            # per torch's complaint: Padding size should be less than the corresponding input dimension
            cons.append(nnsmith_lt(self.pad[i * 2], isv[j]))
            cons.append(nnsmith_lt(self.pad[i * 2 + 1], isv[j]))
        return cons

    def _shape_fn(self, input_shapes: List[ShapeVar]) -> List[ShapeVar]:
        isv = input_shapes[0].shape
        assert len(self.pad) % 2 == 0, self.pad
        assert len(self.pad) // 2 <= len(isv), self.pad
        s = list(isv)
        for i in range(len(self.pad) // 2):
            j = len(isv) - 1 - i
            s[j] = nnsmith_add(nnsmith_add(
                s[j], self.pad[i * 2]), self.pad[i * 2 + 1])
        return [ShapeVar(s, input_shapes[0].dtype)]

    def __str__(self):
        return super().__str__() + ' ' + str(self.extra_attrs)

    def torch(self):
        return lambda x: torch.nn.functional.pad(x, self.pad, self.extra_attrs['mode'], self.extra_attrs['value'])


def _glob_leaf_op_classes() -> List[Type[AbsOpBase]]:
    ret = []

    def _glob_leaf_op_classes_rec(cls):
        nonlocal ret
        for c in cls.__subclasses__():
            if c.__subclasses__():
                _glob_leaf_op_classes_rec(c)
            # elif c is not Input:
            else:
                ret.append(c)
    _glob_leaf_op_classes_rec(AbsOpBase)
    return ret


ALL_OP_TYPES = _glob_leaf_op_classes()
ALL_OP_STR2TYPE = {c.__name__: c for c in ALL_OP_TYPES}


def _check_comb(comb: DTypeComb, op: AbsOpBase):
    inps = []
    for dtype, ndims in zip(comb, op.inp_dims):
        if ndims == -1:
            ndims = 2
        # TODO use symbolic solver
        inps.append(torch.empty([2] * ndims, dtype=dtype.value))
    try:
        _ = op.torch()(*inps)
    except Exception as e:
        return False
    return True


def auto_infer_in_dtypes(verbose=False):
    global _INFERRED
    _INFERRED = True
    _WHITE_LIST = (Input, Expand, NCHWConv2d, Reshape)

    def create_op(op_t: Type[AbsOpBase]):
        construct_param_dict = signature(op_t.__init__).parameters
        values = []
        for key, val in construct_param_dict.items():
            if key == 'self':
                continue
            values.append((key, 1))  # TODO consider type hints?
        return op_t(**dict(values))

    for op_t in ALL_OP_TYPES:
        if issubclass(op_t, _WHITE_LIST):
            continue
        if op_t.in_dtypes is not None:
            continue
        if verbose:
            print(f'Try auto inferring input dtype spec for `{op_t.__name__}`')
        valid_combs = None
        op = create_op(op_t)
        in_dtype_combs: List[DTypeComb] = itertools.product(
            DTYPE_ALL, repeat=len(op.inp_dims))
        valid_combs = [
            comb for comb in in_dtype_combs if _check_comb(comb, op)]
        if len(valid_combs) == 0:
            raise RuntimeError(
                f'No valid input dtype combination found for `{op_t.__name__}`')

        if verbose:
            print('infered result:', valid_combs)
        if op_t.in_dtypes is not None:
            # we disable type promotion for bcast binary ops so the difference is fine
            if verbose and valid_combs != op_t.in_dtypes and not issubclass(op_t, (BcastBinaryOp1, BcastBinaryOp2, BcastBinaryOp3)):
                warnings.warn('Inferred result for `{}` different from given one.\nInferred={}\n, given={}'.format(
                    op_t.__name__, valid_combs, op_t.in_dtypes))
        else:
            op_t.in_dtypes = valid_combs


if __name__ == '__main__':
    # Test shape functions
    print(len(ALL_OP_TYPES), 'operators supported:')
    print(ALL_OP_TYPES)

    # ReLU
    lhs = torch.relu(torch.randn(1, 1, 1, 1)).shape
    rhs = torch.Size(ReLU().shape_fn(
        [ShapeVar([1, 1, 1, 1], DType.float32)])[0].shape)
    assert lhs == rhs, f"{lhs} != {rhs}"

    # Add
    a = torch.randn(2, 3, 4, 5)
    b = torch.randn(2, 3, 4, 5)
    c = a + b
    assert c.shape == torch.Size(Add().shape_fn(
        [ShapeVar([2, 3, 4, 5], DType.float32), ShapeVar([2, 3, 4, 5], DType.float32)])[0].shape)

    # Expand
    source_shape = (4, 1)
    a = torch.randn(source_shape)
    abs_op = ExpandLast4(expand_n=2)
    assert a.expand(2, 1, *source_shape).shape == torch.Size(
        abs_op.shape_fn([ShapeVar(source_shape, DType.float32)])[0].shape)

    abs_op = ExpandLast1(expand_n=2)
    rhs = torch.Size(abs_op.shape_fn(
        [ShapeVar(list(source_shape), DType.float32)])[0].shape)
    lhs = a.expand(4, 2).shape
    assert lhs == rhs, f"{lhs} != {rhs}"

    # NCHWConv2d
    source_shape = (2, 3, 24, 24)
    a = torch.randn(*source_shape)
    out = torch.conv2d(a, torch.randn(3, 3, 3, 4), stride=1, padding=1)
    assert out.shape == NCHWConv2d(
        3, 3, 3, 4, 1, 1).shape_fn([ShapeVar(source_shape, DType.float32)])[0].torch()
    print(NCHWConv2d(
        3, 3, 3, 4, 1, 1).shape_fn([ShapeVar([2, *z3.Ints('c h w')], DType.float32)])[0])

    # Reshape
    source_shape = (2, 3, 4)
    target_shape = (1, 2, 3, 2, 2)
    a = torch.randn(*source_shape)
    assert a.reshape(*target_shape).shape == Reshape5D(*target_shape).shape_fn(
        [ShapeVar(source_shape, DType.float32)])[0].torch()

    # Dirty fix for z3 bug by wrapping the context using seprated functions.
    def test_reshape_symbol():  # See https://github.com/Z3Prover/z3/issues/989
        s = z3.Solver()
        v = z3.Ints('a b c d e')
        abs_op = Reshape5D(*v)
        cons = abs_op.requires([ShapeVar(source_shape, DType.float32)])
        for c in cons:
            s.add(c)
        for c in abs_op.shape_fn([ShapeVar(source_shape, DType.float32)])[0].gt_zero():
            s.add(c)
        assert s.check() == z3.sat
        print(s.model())
    test_reshape_symbol()

    # Test `concrete` function.
    p0, p1, p2, p3, p4, p5 = z3.Ints('p0 p1 p2 p3 p4 p5')
    op = NCHWConv2d(p0, p1, p2, p3, p4, p5)
    s = z3.Solver()
    shape = ShapeVar([1, 3, 224, 224], DType.float32)
    for c in op.requires([shape]):
        s.add(c)
    for c in op.shape_fn([shape])[0].gt_zero():
        s.add(c)
    assert s.check() == z3.sat
    model = s.model()
    concrete_op = concretize(op, model)
    assert concrete_op.in_channels == model[p0].as_long()
    assert concrete_op.out_channels == model[p1].as_long()
    assert concrete_op.kernel_h_size == model[p2].as_long()
    assert concrete_op.kernel_w_size == model[p3].as_long()
    assert concrete_op.stride == model[p4].as_long()
    assert concrete_op.padding == model[p5].as_long()
