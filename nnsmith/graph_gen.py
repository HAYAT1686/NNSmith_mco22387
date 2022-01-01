import z3  # Always import z3 first to avoid incompatibility issue.
# See https://github.com/Z3Prover/z3/issues/5656
import networkx as nx
import torch
from torch import nn
import numpy as np

import pickle
from typing import Dict, NamedTuple, Tuple, List
from inspect import signature
import traceback
import random
import time
import os
import copy

from nnsmith.error import NNSmithInternalError, SanityCheck, ConstraintError
from nnsmith.abstract.op import *
from nnsmith.backends import DiffTestBackend
from nnsmith.export import torch2onnx
from nnsmith.input_gen import InputGenV3, TorchNaNChecker


class RequiredDimNotFound(Exception):
    pass


ALIVE_SHAPE_TYPE = List[Tuple[int, ShapeVar, int]]


class SymbolNet(nn.Module):
    def __init__(self, graph: nx.MultiDiGraph, model: z3.ModelRef, verbose=False, alive_shapes: ALIVE_SHAPE_TYPE = None,
                 record_intermediate=False):
        super(SymbolNet, self).__init__()
        self.verbose = verbose
        self.tensors = []  # 1) edges; 2) leaf nodes; 3) input -> 0;
        self.ref_cnt = []  # ref cnt -> tensors; erased on 0;
        self.instructions = []  # <Func, <input idx>, <output idx>>
        self.n_output = 0
        # keep track of layers and weights so that the tracing can work properly
        self.mlist = nn.ModuleList()
        self.graph = graph
        # NOTE: All leaf nodes are output tensors.
        self.alive_shapes = alive_shapes
        if alive_shapes is None:
            warnings.warn(
                "Please supply `alive_shapes` if possible. This will be used to check dtype correctness.")
        # whether or not to register intermediate tensors as output tensors. Useful (at least) for checking nan
        self.record_intermediate = record_intermediate

        InputInfo = NamedTuple(
            'InputInfo', [('op', Input), ('oid', int), ('node_id', int), ('input_name', str)])
        self.input_info: List[InputInfo] = []

        tmp_op_output_map = {}  # node id -> output idx in tensors;
        for node_id in nx.topological_sort(graph):
            n_inp = graph.nodes[node_id]['nin']
            n_out = graph.nodes[node_id]['nout']

            tmp_op_output_map[node_id] = len(self.tensors)
            for _ in range(n_out):
                self.tensors.append(None)
                self.ref_cnt.append(0)

            input_idx = [None] * n_inp
            output_idx = [None] * n_out
            op = concretize(graph.nodes[node_id]['op'], model)

            # Glob inputs
            for from_node, _, (out_idx, in_idx) in graph.in_edges(node_id, data='operand_idx'):
                required = tmp_op_output_map[from_node] + out_idx
                input_idx[in_idx] = required
                self.ref_cnt[required] += 1

            # Glob outputs
            out_edges = graph.out_edges(node_id, data='operand_idx')
            if len(out_edges) == 0:  # leaf node
                # create fake output indices
                output_idx = list(range(
                    tmp_op_output_map[node_id], tmp_op_output_map[node_id] + n_out))
                for out_idx in output_idx:
                    self.ref_cnt[out_idx] += 1
                    self.n_output += 1
            else:
                for _, _, (out_idx, in_idx) in out_edges:
                    output_idx[out_idx] = tmp_op_output_map[node_id] + out_idx

            if not isinstance(op, Input):
                cur_op = op.torch()
                if isinstance(cur_op, nn.Module):
                    self.mlist.append(cur_op)
                self.instructions.append(
                    (cur_op, input_idx, output_idx, op, node_id))
            else:  # Should be input node
                SanityCheck.true(type(op) is Input, 'type(op) should be Input')
                SanityCheck.eq(len(output_idx), 1)
                self.input_info.append(
                    InputInfo(op=op, oid=output_idx[0], node_id=node_id, input_name=f'i{op.idx}'))
        if self.verbose:
            print('input_info=', self.input_info)
        self.input_spec = {
            f'i{ii.op.idx}': ii.op.shape for ii in self.input_info}
        self.plausible_input_shape = {f'i{ii.op.idx}': ShapeVar(
            ii.op.shape, dtype=ii.op.dtype) for ii in self.input_info}

    def _check_out_dtype(self, outputs, node_id, op):
        if self.alive_shapes is None:
            return
        msg_head = f'In dtype checking for {op} (#{node_id}): '
        shape_indices = self.graph.nodes[node_id]['shape_indices']
        SanityCheck.eq(len(outputs), len(shape_indices), msg_head +
                       f'{len(outputs)} != {len(shape_indices)}')
        for out, shape_idx in zip(outputs, shape_indices):
            SanityCheck.eq(out.dtype, self.alive_shapes[shape_idx][1].dtype.value, msg_head +
                           f'torch dtype ({out.dtype}) != symbolic dtype ({self.alive_shapes[shape_idx][1].dtype.value})')

    @torch.no_grad()
    def forward(self, *xs):
        local_ref_cnt = self.ref_cnt.copy()
        self.tensors = [None for _ in self.tensors]
        for ii in self.input_info:
            self.tensors[ii.oid] = xs[ii.op.idx]
        for inst, inps, outs, op, node_id in self.instructions:
            input_tensors = [self.tensors[idx] for idx in inps]
            if isinstance(op, Div):
                if (input_tensors[1] == 0).any():
                    input_tensors[1] = torch.clip(
                        input_tensors[1], torch.ones(size=[1], dtype=input_tensors[1].dtype))
            if self.verbose:
                print(
                    f'executing instruction op={op}, node_id={node_id}, inps={inps}, outs={outs}')
                print('input_tensors=')
                for i in input_tensors:
                    print(f'  (shape={i.shape} dtype={i.dtype})')
            outputs = inst(*input_tensors)
            if not isinstance(outputs, list):
                outputs = [outputs]
            self._check_out_dtype(outputs, node_id, op)
            if self.verbose:
                print('outputs=', ','.join(
                    f'(shape={i.shape} dtype={i.dtype})' for i in outputs))
            for idx in inps:
                local_ref_cnt[idx] -= 1
                if local_ref_cnt[idx] == 0 and not self.record_intermediate:
                    self.tensors[idx] = None
            for idx, output in list(zip(outs, outputs)):
                SanityCheck.none(self.tensors[idx], 'tensor[{}] is not None.'.format(
                    idx))
                if local_ref_cnt[idx] > 0:  # Will be used.
                    self.tensors[idx] = output
        return tuple(t for t in self.tensors if t is not None)


class SimpleGenerator:
    def __init__(self, min_dims=[1, 3, 48, 48], skip=[], viz_sbs=False, megabyte_lim=6 * 1024, seed=None, verbose=False, use_bitvec=False):
        self.verbose = verbose
        auto_infer_in_dtypes(self.verbose)

        self.op_candidates = [op for op in ALL_OP_TYPES if op not in skip]
        self.solver = z3.Solver()
        self.solver.set("threads", 4)
        # 4 bytes per float (assume we use float32)
        self.limit_float = 1024**2 * megabyte_lim / 4

        # Node -> op: AbsOpBase
        # Edge -> shape_idx:-> self.alive_shapes
        self.abstract_graph = nx.MultiDiGraph()

        # <op idx, shape variable, output operand idx>
        self.alive_shapes: ALIVE_SHAPE_TYPE = []
        # dim size -> list[shape idx -> output_tensor_pool]
        self.dim2shape_idx: Dict[int, List[int]] = {}
        self.viz_cnt = 0
        self.is_viz_sbs = viz_sbs

        self.use_bitvec = use_bitvec
        # self.input_shape = self.insert_input_node(min_dims)
        self.min_dims = min_dims
        self.n_floats = 0
        self.n_inps = 0
        self.last_soln = None

    def new_sym(self, name):
        if self.use_bitvec:
            return z3.BitVec(name, 8)
        else:
            return z3.Int(name)

    @abstractmethod
    def insert_input_node(self, min_dims, shape=None, dtype=DType.float32) -> ShapeVar:
        raise NotImplementedError

    @abstractmethod
    def try_insert_node(self, node: AbsOpBase, ishape_indices: List[int]) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_symbol_solutions(self) -> List:
        raise NotImplementedError

    def extra_exit_check(self) -> bool:
        """
        Returns:
            bool: add more checks to determine whether to exit the generation.
        """
        return False

    # def concretize_input_shape(self, model):
    #     shape = []
    #     for s in self.input_shape.shape:
    #         if isinstance(s, z3.ExprRef):
    #             shape.append(model.eval(s, model_completion=True).as_long())
    #         else:
    #             shape.append(s)
    #     return shape

    def abstract_gen(self, max_node_size=10, max_gen_millisec=2000):
        z3.set_param(
            "smt.phase_selection",
            5,
            "smt.arith.random_initial_value",
            True,
            "sat.phase",
            "random",
            "timeout",
            max_gen_millisec // 3,
        )
        self.insert_input_node(self.min_dims)
        self.insert_input_node(self.min_dims)
        self.insert_input_node(self.min_dims)
        init_time = time.time()
        while time.time() - init_time < max_gen_millisec / 1000 and len(
                self.abstract_graph.nodes) < max_node_size:
            if self.extra_exit_check():
                break
            node_t = self.pick_next_op_type()
            self.try_insert_node_type(node_t)
        if len(self.abstract_graph.nodes) != max_node_size:
            print(
                f'[WARNING]: graph size: {len(self.abstract_graph.nodes)} != expected size: {max_node_size}')
        # self.fix_graph_dependency()

    def shape_idx_to_op_idx(self, shape_idx: int) -> int:
        return self.alive_shapes[shape_idx][0]

    def check_sat(self, *assumptions):
        start = time.time()
        cres = self.solver.check(*assumptions)
        if self.verbose:
            print(cres, '<-- checking time:',
                  int((time.time() - start) * 1000), 'ms')

            if cres == z3.unsat:
                print(f'Unsat core: {self.solver.unsat_core()}')
        if cres == z3.sat:
            self.last_soln = self.solver.model()
        return cres

    def pick_next_op_type(self):
        return random.choice(self.op_candidates)

    def insert_node(self, node: AbsOpBase, ishape_indices: List[int], oshapes: List[ShapeVar] = None):
        if oshapes is None:
            input_shapes = [self.alive_shapes[idx][1]
                            for idx in ishape_indices]
            oshapes = node.shape_fn(copy.deepcopy(input_shapes))

        new_node_idx = len(self.abstract_graph.nodes)
        shape_idx_st = len(self.alive_shapes)
        shape_indices = []
        for i, shape_var in enumerate(oshapes):
            if node.out_dims[i] == -1:
                node.out_dims[i] = len(shape_var.shape)
            else:
                SanityCheck.eq(node.out_dims[i], len(shape_var.shape), "{}'s dimension size is not {} in {}".format(
                    shape_var.shape, node.out_dims[i], node.__class__.__name__))
            shape_idx = len(self.alive_shapes)
            shape_indices.append(shape_idx)
            self.alive_shapes.append((new_node_idx, shape_var, i))
            self.dim2shape_idx.setdefault(
                len(shape_var.shape), []).append(shape_idx)
        shape_idx_ed = len(self.alive_shapes)

        self.abstract_graph.add_node(
            new_node_idx, op=node, nin=len(ishape_indices), nout=len(oshapes),
            label=f'#{new_node_idx}, [{shape_idx_st},{shape_idx_ed}), {node}', shape_indices=shape_indices)

        for in_operand_idx, idx in enumerate(ishape_indices):
            old_node_idx, svar, out_operand_idx = self.alive_shapes[idx]
            self.abstract_graph.add_edge(old_node_idx, new_node_idx, shape_idx=idx, operand_idx=(
                out_operand_idx, in_operand_idx), label=f'{out_operand_idx}-{in_operand_idx}: {svar}')

        if self.is_viz_sbs:
            self.viz()

    def try_insert_node_type_at(self, node_t, ishape_indices: List[int]) -> bool:
        if self.verbose:
            print(f'Inserting node #{len(self.abstract_graph.nodes)}: '
                  f'trying to insert node type {node_t.__name__}')
        if issubclass(node_t, Input):
            try:
                self.insert_input_node(self.min_dims)
            # TODO: check the exception type (ideally only z3 check_failure), don't drop internal errors
            except:
                return False
            return True
        op_param_n = signature(node_t).parameters
        op_id = len(self.abstract_graph.nodes)
        op_params = [self.new_sym('op%s_%s' % (op_id, k))
                     for k in range(len(op_param_n))]

        op: AbsOpBase = node_t(*op_params)

        try:
            if self.try_insert_node(op, ishape_indices):
                return True
        except RequiredDimNotFound:
            if self.verbose:
                traceback.print_exc()
            return False
        except ConstraintError:
            if self.verbose:
                traceback.print_exc()
            return False

        return False

    def try_insert_node_type(self, node_t, max_shape_var_pick_time=3) -> bool:
        if self.verbose:
            print(f'Inserting node #{len(self.abstract_graph.nodes)}: '
                  f'trying to insert node type {node_t.__name__}')
        if issubclass(node_t, Input):
            try:
                self.insert_input_node(self.min_dims)
            # TODO: check the exception type (ideally only z3 check_failure), don't drop internal errors
            except:
                return False
            return True
        op_param_n = signature(node_t).parameters
        op_id = len(self.abstract_graph.nodes)
        op_params = [self.new_sym('op%s_%s' % (op_id, k))
                     for k in range(len(op_param_n))]

        op: AbsOpBase = node_t(*op_params)

        n_inp = len(op.inp_dims)
        same_input_dims = op.same_inp_dims

        dim_spec_list = []

        if same_input_dims:  # find `n_inp` under the same input shapes.
            final_dim = -1
            for dim in op.inp_dims:
                if dim != -1:
                    if final_dim == -1:
                        final_dim = dim
                    else:
                        SanityCheck.eq(final_dim, dim)
            if final_dim == -1:
                final_dim = random.choice(list(self.dim2shape_idx.keys()))
            dim_spec_list = [final_dim] * n_inp
        else:  # inputs have different dimension sizes.
            dim_spec_list = op.inp_dims

        try:
            for _ in range(max_shape_var_pick_time):
                # reinstantiate to make sure extra_attrs are up-to-date
                op: AbsOpBase = node_t(*op_params)
                ishape_indices = self.pick_shape_var_idx(
                    node_t, dim_spec_list, random.choice(op.in_dtypes))
                if self.try_insert_node(op, ishape_indices):
                    return True
        except RequiredDimNotFound:
            if self.verbose:
                traceback.print_exc()
            return False
        except ConstraintError:
            if self.verbose:
                traceback.print_exc()
            return False

        return False

    def filter_alive_shapes(self, ndim, dtype):
        # TODO(JK): consider same_in_dtypes
        cans = range(len(self.alive_shapes))

        cans = list(filter(  # filter with ndim
            lambda sid: self.alive_shapes[sid][1].ndims == ndim or ndim == -1, cans))
        if len(cans) == 0:
            raise RequiredDimNotFound(
                'Cannot find a shape variable with #dimensions %s.' % ndim)

        cans = list(filter(  # filter with dtype
            lambda sid: self.alive_shapes[sid][1].dtype == dtype, cans))
        if len(cans) == 0:
            raise RequiredDimNotFound(
                'Cannot find a shape variable with #dimensions %s and dtype %s.' % (ndim, dtype))

        return cans

    def pick_alive_shape(self, node_t, candidates):
        return random.choice(candidates)

    def pick_shape_var_idx(self, node_t, ndim_list: List[int], dtype_comb: DTypeComb) -> List[int]:
        """Randomly pick indices to shape variables from the output pool.

        Args:
            ndim_list (List[int]): required dimension sizes of the shape variables.

        Returns:
            List[int]: indices to applicable shape variables.
        """

        shape_var_candidates = []
        if self.verbose:
            print('dtype_comb:', dtype_comb)

        for i, ndim in enumerate(ndim_list):
            candidates = self.filter_alive_shapes(
                ndim=ndim, dtype=dtype_comb[i])
            shape_var_candidates.append(
                self.pick_alive_shape(node_t, candidates))

        return shape_var_candidates

    def viz(self, filename: str = None):
        if filename is None:
            filename = f'step{self.viz_cnt}.png'
        G = self.abstract_graph
        nx.drawing.nx_pydot.write_dot(G, 'graph.dot')
        os.system(f'dot -Tpng graph.dot > {filename}')
        self.viz_cnt += 1


class PureSymbolGen(SimpleGenerator):
    def insert_input_node(self, min_dims, dtype=DType.float32) -> ShapeVar:
        input_tensor_shape = ShapeVar(
            shape=[self.new_sym('i%s_s%s' % (self.n_inps, k)) for k in range(len(min_dims))], dtype=dtype)
        input_node = Input(self.n_inps, dtype, *input_tensor_shape.shape)

        self.insert_node(input_node, [], oshapes=[input_tensor_shape])
        for c in input_tensor_shape.gt_zero():
            self.solver.add(c)

        if not self.use_bitvec:  # bit vector is randomizable
            # The batch size should not have a big min size (avoid unnecessary computation);
            # FIXME: input constraints will make SMT solving costly.
            for i in range(len(input_tensor_shape.shape)):
                self.solver.add(input_tensor_shape.shape[i] >= min_dims[i])
        check_res = self.check_sat()
        # FIXME sometimes the constraints are too complicated to return stable result.
        SanityCheck.eq(check_res, z3.sat,
                       msg=f'Constraints not sat but {check_res}.')
        self.n_floats = nnsmith_add(
            self.n_floats, input_tensor_shape.nelement())
        self.n_inps += 1
        return input_tensor_shape

    def try_insert_node(self, node: AbsOpBase, ishape_indices: List[int]) -> bool:
        input_shapes = [self.alive_shapes[idx][1] for idx in ishape_indices]
        node.post_symbolize(
            input_shapes, f'op{len(self.abstract_graph.nodes)}_', self.new_sym)
        constraints = node.requires(input_shapes)

        if self.verbose:
            print('---> Trying to solve: ', node, constraints)
            print('---> total constraints: \n',
                  '\n'.join(sorted(map(str, set(self.solver.assertions())))))
            # self.viz('currentgraph.png')

        # make a copy
        output_shapes = node.shape_fn(copy.deepcopy(input_shapes))

        for shape in output_shapes:
            for c in shape.gt_zero():
                constraints.append(c)

        for s in output_shapes:
            self.n_floats = nnsmith_add(self.n_floats, s.nelement())

        check_res = self.check_sat(
            *constraints, nnsmith_le(self.n_floats, self.limit_float))
        if check_res == z3.unknown:  # Timeout thing.
            self.on_timeout(node, ishape_indices)

        if check_res != z3.sat:
            return False

        for c in constraints:
            self.solver.add(c)

        self.insert_node(node, ishape_indices, output_shapes)
        return True

    def on_timeout(self, node: AbsOpBase, ishape_indices: List[int]):
        pass

    def get_symbol_solutions(self) -> List:
        SanityCheck.not_none(self.last_soln)
        return self.last_soln
        # res = self.solver.check()
        # assert res == z3.sat, res
        # return self.solver.model()


class GenerationTable:
    # Hyper-parameters
    _MAX_CONF = 4.0
    _BASE_VAL = 1.0
    _MIN_CONF = 0.1
    _INIT_VAL = 2.0

    def __init__(self):
        self.np_table = np.ones((len(ALL_OP_TYPES), len(
            ALL_OP_TYPES) - 1)) * self._INIT_VAL  # do not count Input

        # Close those impossible connections.
        for src_t in ALL_OP_TYPES:
            for tar_t in ALL_OP_TYPES:
                if tar_t is Input:
                    continue

                inp_dims = tar_t(
                    *[None for _ in signature(tar_t).parameters]).inp_dims
                out_dims = src_t(
                    *[None for _ in signature(src_t).parameters]).out_dims

                if -1 in inp_dims or -1 in out_dims or set(inp_dims).intersection(out_dims):
                    continue

                self.np_table[self.row_mapper(
                    src_t)][self.col_mapper(tar_t)] = 0.

    def row_mapper(self, t):
        if isinstance(t, int):
            return t
        return ALL_OP_TYPES.index(t)

    def col_mapper(self, t):
        if isinstance(t, int):
            return t
        return ALL_OP_TYPES.index(t) - 1

    def on_new_cov(self, src_t, tar_t):
        if self.row_mapper(src_t) == 0:  # Ignore input node.
            return
        val = self.np_table[self.row_mapper(src_t)][self.col_mapper(tar_t)]
        self.np_table[self.row_mapper(src_t)][self.col_mapper(
            tar_t)] = min(self._MAX_CONF, max(self._BASE_VAL, val * 1.1))

    def on_no_cov(self, src_t, tar_t):
        if self.row_mapper(src_t) == 0:
            return
        self.np_table[self.row_mapper(
            src_t)][self.col_mapper(tar_t)] = self._BASE_VAL  # reset.

    def on_unsolvable(self, src_t, tar_t):
        if self.row_mapper(src_t) == 0:
            return
        val = self.np_table[self.row_mapper(src_t)][self.col_mapper(tar_t)]
        self.np_table[self.row_mapper(src_t)][self.col_mapper(
            tar_t)] = max(self._MIN_CONF, min(self._BASE_VAL, val * 0.9))

    def lookup(self, src_t, tar_t):
        return self.np_table[self.row_mapper(src_t)][self.col_mapper(tar_t)]

    def __len__(self):
        return len(self.np_table)

    def __getitem__(self, t):
        return self.np_table[self.row_mapper(t)]


class CoverageTableGen(PureSymbolGen):
    def __init__(self, table: GenerationTable, state, **kwargs):
        self.table = table
        SanityCheck.true('unsolvable' in state, 'unsolvable not in state')
        self.state = state
        super(CoverageTableGen, self).__init__(**kwargs)

    def pick_alive_shape(self, node_t, candidates):
        # node_t target node...
        # candidates -> outputs of input nodes...
        successor_probability = self.table.np_table.transpose()[
            self.table.col_mapper(node_t)]
        candidate_ops = [type(self.abstract_graph.nodes[self.alive_shapes[alive_shape_idx][0]]['op'])
                         for alive_shape_idx in candidates]
        candidate_indices = [self.table.row_mapper(op) for op in candidate_ops]
        candidate_conf = successor_probability[candidate_indices]
        if candidate_conf.sum() == 0:
            raise NNSmithInternalError(
                f'Candidate prob is zero -- candidates: {[op.__name__ for op in candidate_ops]} -> {node_t}')
        return np.random.choice(candidates, p=candidate_conf / candidate_conf.sum())

    def pick_next_op_type(self):
        probability_vector = self.table.np_table.sum(axis=1)
        return np.random.choice(ALL_OP_TYPES, p=probability_vector / probability_vector.sum())

    def on_timeout(self, node: AbsOpBase, ishape_indices: List[int]):
        # node -> ishape_indices :: on_unsolvable
        for idx in ishape_indices:
            self.state['unsolvable'].append(
                (type(node).__name__, type(self.abstract_graph.nodes[self.alive_shapes[idx][0]]['op']).__name__))


def parse_args():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--max_nodes', type=int, default=5)
    parser.add_argument('--min_dims', type=list, default=[1, 3, 48, 48])
    parser.add_argument('--timeout', type=int, default=50000)
    parser.add_argument('--viz_sbs', action='store_true',
                        help='visualize the step by step')
    parser.add_argument('--output_path', type=str, default='output.onnx')
    parser.add_argument('--seed', type=int)
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--use_bitvec', action='store_true')
    parser.add_argument('--viz_graph', action='store_true')
    return parser.parse_args()


def random_model_gen(
        min_dims=[1, 3, 48, 48],
        viz_sbs=False,
        max_nodes=5,
        seed=None,
        use_bitvec=False,
        timeout=50000,
        verbose=False):
    if verbose:
        strt_time = time.time()

    gen = PureSymbolGen(min_dims=min_dims,
                        viz_sbs=viz_sbs, seed=seed, verbose=verbose, use_bitvec=use_bitvec)
    gen.abstract_gen(max_node_size=max_nodes,
                     max_gen_millisec=timeout)
    if verbose:
        print(
            f'{time.time() - strt_time}s to generate a graph w/ {len(gen.abstract_graph.nodes())} nodes')

    solution = gen.get_symbol_solutions()
    if verbose:
        print(
            f'{len(solution)} symbols and {len(gen.solver.assertions())} constraints.')
        print(solution)

    return gen, solution


def table_model_gen(
        table,
        state,
        min_dims=[1, 3, 48, 48],
        viz_sbs=False,
        max_nodes=5,
        seed=None,
        use_bitvec=False,
        timeout=50000,
        verbose=False):
    if verbose:
        strt_time = time.time()

    gen = CoverageTableGen(table=table, state=state, min_dims=min_dims,
                           viz_sbs=viz_sbs, seed=seed, verbose=verbose, use_bitvec=use_bitvec)

    gen.abstract_gen(max_node_size=max_nodes,
                     max_gen_millisec=timeout)
    if verbose:
        print(
            f'{time.time() - strt_time}s to generate a graph w/ {len(gen.abstract_graph.nodes())} nodes')

    solution = gen.get_symbol_solutions()
    if verbose:
        print(
            f'{len(solution)} symbols and {len(gen.solver.assertions())} constraints.')
        print(solution)

    return gen, solution


if __name__ == '__main__':
    args = parse_args()

    strt_time = time.time()

    seed = args.seed
    if seed is None:
        # If we have not selected a seed, choose random one.
        seed = random.getrandbits(32)
    np.random.seed(seed)  # debugging purposes for input_gen
    print(f"Using seed {seed}")
    random.seed(seed)

    gen, solution = random_model_gen(min_dims=args.min_dims, viz_sbs=args.viz_sbs, max_nodes=args.max_nodes,
                                     use_bitvec=args.use_bitvec, timeout=args.timeout, verbose=args.verbose)

    if args.verbose or args.viz_graph:
        gen.viz(args.output_path + '.png')

    net = SymbolNet(gen.abstract_graph, solution, verbose=args.verbose,
                    alive_shapes=gen.alive_shapes)
    net.eval()
    torch2onnx(model=net, filename=args.output_path, verbose=args.verbose)

    model = DiffTestBackend.get_onnx_proto(args.output_path)

    # turn this on so that nan in the intermediate tensors can be detected too
    net.record_intermediate = True
    input_gen = InputGenV3(TorchNaNChecker(net))
    input_st = time.time()
    rngs = input_gen.infer_domain(model)
    infer_succ = rngs is not None
    ed_time = time.time()

    stats = {
        'gen_succ': True,
        'infer_succ': infer_succ,
        'elapsed_time': ed_time - strt_time,
        'gen_model_time': input_st - strt_time,
        'infer_domain_time': ed_time - input_st,
        'rngs': rngs,
        'seed': seed,
    }
    pickle.dump(stats, open(args.output_path + '-stats.pkl', 'wb'))
