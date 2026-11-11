"""Fused selective scan (Mamba v1, real A, variable B/C) in Triton, fp32.

    dt  = softplus(delta + delta_bias)            (torch threshold 20)
    h_t = exp(dt_t * A) * h_{t-1} + dt_t * u_t * B_t
    y_t = <h_t, C_t> + D * u_t
    out = y * silu(z)
The state h stays in registers, like the official CUDA kernel. Same recurrence as selective_scan_blocked in
__init__.py (`reuse_file.py --selfcheck` compares them). Part of the mamba_ssm stand-in (Apache License 2.0).
"""
import torch
import triton
import triton.language as tl


@triton.jit
def _scan_kernel(u_ptr, dt_ptr, z_ptr, B_ptr, C_ptr, A_ptr, D_ptr, bias_ptr, out_ptr, L, DIM,
                 N: tl.constexpr, BLOCK_D: tl.constexpr):
    pid_b = tl.program_id(0)
    pid_d = tl.program_id(1)
    offs_d = pid_d * BLOCK_D + tl.arange(0, BLOCK_D)
    mask_d = offs_d < DIM
    offs_n = tl.arange(0, N)
    A = tl.load(A_ptr + offs_d[:, None] * N + offs_n[None, :], mask=mask_d[:, None], other=0.0)
    Dv = tl.load(D_ptr + offs_d, mask=mask_d, other=0.0)
    bias = tl.load(bias_ptr + offs_d, mask=mask_d, other=0.0)
    h = tl.zeros([BLOCK_D, N], dtype=tl.float32)
    # layouts: u/dt/z/out [b, l, d]; B/C [b, l, n] (all contiguous)
    base_d = pid_b * L * DIM
    base_n = pid_b * L * N
    for t in range(L):
        u = tl.load(u_ptr + base_d + t * DIM + offs_d, mask=mask_d, other=0.0)
        dt = tl.load(dt_ptr + base_d + t * DIM + offs_d, mask=mask_d, other=0.0) + bias
        dt = tl.where(dt <= 20.0, tl.log(1.0 + tl.exp(dt)), dt)
        Bt = tl.load(B_ptr + base_n + t * N + offs_n)
        Ct = tl.load(C_ptr + base_n + t * N + offs_n)
        h = tl.exp(dt[:, None] * A) * h + (dt * u)[:, None] * Bt[None, :]
        y = tl.sum(h * Ct[None, :], axis=1) + u * Dv
        zt = tl.load(z_ptr + base_d + t * DIM + offs_d, mask=mask_d, other=0.0)
        y = y * (zt * tl.sigmoid(zt))
        tl.store(out_ptr + base_d + t * DIM + offs_d, y, mask=mask_d)


def selective_scan_triton(u, delta, A, B, C, D, z, delta_bias, block_d=64):
    """u, delta, z: [b, d, l]; B, C: [b, n, l]; A: [d, n]; D, delta_bias: [d]. Returns [b, d, l]."""
    b, dim, L = u.shape
    n = A.shape[1]
    u_ = u.float().transpose(1, 2).contiguous()
    dt_ = delta.float().transpose(1, 2).contiguous()
    z_ = z.float().transpose(1, 2).contiguous()
    B_ = B.float().transpose(1, 2).contiguous()
    C_ = C.float().transpose(1, 2).contiguous()
    out = torch.empty_like(u_)
    grid = (b, triton.cdiv(dim, block_d))
    _scan_kernel[grid](u_, dt_, z_, B_, C_, A.float().contiguous(), D.float().contiguous(),
                       delta_bias.float().contiguous(), out, L, dim, N=n, BLOCK_D=block_d)
    return out.transpose(1, 2)
