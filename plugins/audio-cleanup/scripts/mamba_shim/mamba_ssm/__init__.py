"""Pure-PyTorch stand-in for mamba_ssm.Mamba (v1), so RE-USE runs without compiling the mamba CUDA kernels
(no nvcc / MSVC CUDA toolchain needed, works on Windows).

Derived from mamba_ssm (https://github.com/state-spaces/mamba, Copyright (c) 2023 Tri Dao and Albert Gu,
Apache License 2.0, see ../LICENSE-APACHE-2.0.txt). Changes: only the Mamba v1 module, inference only; parameter
names and shapes kept identical to mamba_ssm.modules.mamba_simple.Mamba so official checkpoints load unchanged;
forward pass = the reference (non-fused) path: in_proj -> depthwise causal conv1d + SiLU -> x_proj -> dt_proj ->
selective scan (fp32) -> + u*D -> * SiLU(z) -> out_proj. The scan runs in a fused Triton kernel on CUDA
(triton_scan.py, same recurrence, state in registers) or, without Triton, as a memory-bounded PyTorch loop.
Env REUSE_NO_TRITON=1 forces the PyTorch loop.
"""
import math
import os

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange

SCAN_BLOCK = 64

try:
    from .triton_scan import selective_scan_triton
    USE_TRITON = os.environ.get("REUSE_NO_TRITON") != "1"
except Exception:  # triton unavailable -> pure PyTorch scan
    USE_TRITON = False


def selective_scan_blocked(u, delta, A, B, C, D=None, z=None, delta_bias=None, delta_softplus=False,
                           block=SCAN_BLOCK):
    """Selective-scan recurrence (real A, variable B/C [b, n, l]) in fp32; deltaA / deltaB*u are materialised in
    blocks of `block` time steps to bound memory."""
    dtype_in = u.dtype
    u, delta = u.float(), delta.float()
    if delta_bias is not None:
        delta = delta + delta_bias[..., None].float()
    if delta_softplus:
        delta = F.softplus(delta)
    B, C = B.float(), C.float()
    batch, dim, L = u.shape
    h = A.new_zeros((batch, dim, A.shape[1]))
    y = u.new_empty((batch, dim, L))
    for s in range(0, L, block):
        e = min(s + block, L)
        d_blk = delta[:, :, s:e]
        dA = torch.exp(torch.einsum("bdl,dn->bldn", d_blk, A))
        dBu = torch.einsum("bdl,bnl,bdl->bldn", d_blk, B[:, :, s:e], u[:, :, s:e])
        hs = torch.empty_like(dBu)
        for i in range(e - s):
            h = torch.addcmul(dBu[:, i], dA[:, i], h)
            hs[:, i] = h
        y[:, :, s:e] = torch.einsum("bldn,bnl->bdl", hs, C[:, :, s:e])
    out = y if D is None else y + u * rearrange(D, "d -> d 1")
    if z is not None:
        out = out * F.silu(z)
    return out.to(dtype=dtype_in)


class Mamba(nn.Module):
    def __init__(self, d_model, d_state=16, d_conv=4, expand=2, dt_rank="auto", conv_bias=True, bias=False,
                 device=None, dtype=None, **kwargs):
        super().__init__()
        fk = {"device": device, "dtype": dtype}
        self.d_model, self.d_state, self.d_conv, self.expand = d_model, d_state, d_conv, expand
        self.d_inner = int(self.expand * self.d_model)
        self.dt_rank = math.ceil(self.d_model / 16) if dt_rank == "auto" else dt_rank
        self.in_proj = nn.Linear(self.d_model, self.d_inner * 2, bias=bias, **fk)
        self.conv1d = nn.Conv1d(self.d_inner, self.d_inner, bias=conv_bias, kernel_size=d_conv, groups=self.d_inner,
                                padding=d_conv - 1, **fk)
        self.act = nn.SiLU()
        self.x_proj = nn.Linear(self.d_inner, self.dt_rank + self.d_state * 2, bias=False, **fk)
        self.dt_proj = nn.Linear(self.dt_rank, self.d_inner, bias=True, **fk)
        A = torch.arange(1, self.d_state + 1, dtype=torch.float32).repeat(self.d_inner, 1).contiguous()
        self.A_log = nn.Parameter(torch.log(A))
        self.D = nn.Parameter(torch.ones(self.d_inner))
        self.out_proj = nn.Linear(self.d_inner, self.d_model, bias=bias, **fk)

    def forward(self, hidden_states):
        _, seqlen, _ = hidden_states.shape
        xz = rearrange(self.in_proj.weight @ rearrange(hidden_states, "b l d -> d (b l)"), "d (b l) -> b d l",
                       l=seqlen)
        if self.in_proj.bias is not None:
            xz = xz + rearrange(self.in_proj.bias.to(dtype=xz.dtype), "d -> d 1")
        A = -torch.exp(self.A_log.float())
        x, z = xz.chunk(2, dim=1)
        x = self.act(self.conv1d(x)[..., :seqlen])
        x_dbl = self.x_proj(rearrange(x, "b d l -> (b l) d"))
        dt, B, C = torch.split(x_dbl, [self.dt_rank, self.d_state, self.d_state], dim=-1)
        dt = rearrange(self.dt_proj.weight @ dt.t(), "d (b l) -> b d l", l=seqlen)
        B = rearrange(B, "(b l) dstate -> b dstate l", l=seqlen).contiguous()
        C = rearrange(C, "(b l) dstate -> b dstate l", l=seqlen).contiguous()
        if USE_TRITON and x.is_cuda:
            y = selective_scan_triton(x, dt, A, B, C, self.D, z, self.dt_proj.bias)
        else:
            y = selective_scan_blocked(x, dt, A, B, C, self.D.float(), z=z, delta_bias=self.dt_proj.bias.float(),
                                       delta_softplus=True)
        return self.out_proj(rearrange(y, "b d l -> b l d"))
