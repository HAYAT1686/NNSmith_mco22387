import os
import warnings
from typing import Dict, List, Tuple

import numpy as np
import torch
from multipledispatch import dispatch
from torch.utils.mobile_optimizer import optimize_for_mobile

from nnsmith.backends.factory import BackendCallable, BackendFactory
from nnsmith.materialize.torch import TorchModel

# Check https://pytorch.org/tutorials/recipes/recipes/tuning_guide.html
# for more PyTorch-internal options.
NNSMITH_PTJIT_OPT_MOBILE = os.getenv("NNSMITH_PTJIT_OPT_MOBILE", "0") == "1"


class TorchJIT(BackendFactory):
    def __init__(self, target="cpu", optmax: bool = False, **kwargs):
        super().__init__(target, optmax)
        if self.target == "cpu":
            self.device = torch.device("cpu")
        elif self.target == "cuda":
            self.device = torch.device("cuda")
            torch.backends.cudnn.benchmark = True
        else:
            raise ValueError(
                f"Unknown target: {self.target}. Only `cpu` and `cuda` are supported."
            )

    @property
    def system_name(self) -> str:
        return "torchjit"

    @dispatch(TorchModel)
    def make_backend(self, model: TorchModel) -> BackendCallable:
        torch_net = model.torch_model.to(self.device).eval()
        trace_inp = [ts.to(self.device) for ts in torch_net.get_random_inps().values()]
        with torch.no_grad():
            with warnings.catch_warnings():
                warnings.simplefilter(
                    "ignore",
                    category=torch.jit.TracerWarning,
                )
                exported = torch.jit.trace(
                    torch_net,
                    trace_inp,
                )
                exported = torch.jit.freeze(exported)  # Fronzen graph.
                exported = torch.jit.optimize_for_inference(exported)
                if self.target == "cpu" and NNSMITH_PTJIT_OPT_MOBILE:
                    exported = optimize_for_mobile(exported)

        def closure(inputs: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
            input_ts = [torch.from_numpy(v).to(self.device) for _, v in inputs.items()]
            with torch.no_grad():
                output: Tuple[torch.Tensor] = exported(*input_ts)
            return {
                k: v.cpu().detach().resolve_conj().numpy()
                if v.is_conj()
                else v.cpu().detach().numpy()
                for k, v in zip(torch_net.output_like.keys(), output)
            }

        return closure

    @property
    def import_libs(self) -> List[str]:
        return ["import torch"]

    def emit_compile(self, opt_name: str, mod_name: str, inp_name: str) -> str:
        return f"{opt_name} = torch.jit.trace({mod_name}, [torch.from_numpy(v).to('{self.device.type}') for v in {inp_name}])"

    def emit_run(self, out_name: str, opt_name: str, inp_name: str) -> str:
        return f"""{out_name} = {opt_name}(*[torch.from_numpy(v).to('{self.device.type}') for v in {inp_name}])
{out_name} = [v.cpu().detach() for v in {out_name}] # torch2numpy
{out_name} = [v.resolve_conj().numpy() if v.is_conj() else v.numpy() for v in {out_name}] # torch2numpy"""
