"""Windows 兼容处理（不改官方代码）。

1. flash_head_model.py 顶层 import 了 xfuser（多卡序列并行库，依赖 NCCL，Windows 装不上）。
   单卡推理时 dist 未初始化，xfuser 的函数根本不会被调用，所以装不上时注入一个桩模块即可。
2. torch.compile 需要 triton；Windows 需要单独装 triton-windows，没有就自动关闭编译。
"""
from __future__ import annotations

import sys
import types

from loguru import logger


def install_xfuser_stub() -> bool:
    """真 xfuser 可用就用真的；否则注入桩模块。返回当前生效的是否为桩（可重复调用）。"""
    if getattr(sys.modules.get("xfuser"), "__flashhead_stub__", False):
        return True
    try:
        import xfuser.core.distributed  # noqa: F401
        import xfuser.core.long_ctx_attention  # noqa: F401
        return False
    except Exception as e:  # ImportError 以及 xfuser 内部依赖缺失导致的各种异常
        logger.warning(f"xfuser 不可用（{type(e).__name__}: {e}），注入单卡桩模块；多卡序列并行不可用")

    def _unavailable(*_a, **_k):
        raise RuntimeError("xfuser 未安装：序列并行（多卡 USP）在当前环境不可用")

    pkg = types.ModuleType("xfuser")
    pkg.__flashhead_stub__ = True
    core = types.ModuleType("xfuser.core")
    dist_mod = types.ModuleType("xfuser.core.distributed")
    dist_mod.get_sequence_parallel_rank = lambda: 0
    dist_mod.get_sequence_parallel_world_size = lambda: 1
    dist_mod.get_sp_group = _unavailable
    dist_mod.init_distributed_environment = _unavailable
    dist_mod.initialize_model_parallel = _unavailable
    dist_mod.get_world_group = _unavailable
    attn_mod = types.ModuleType("xfuser.core.long_ctx_attention")
    attn_mod.xFuserLongContextAttention = _unavailable
    pkg.core = core
    core.distributed = dist_mod
    core.long_ctx_attention = attn_mod
    sys.modules.update({
        "xfuser": pkg,
        "xfuser.core": core,
        "xfuser.core.distributed": dist_mod,
        "xfuser.core.long_ctx_attention": attn_mod,
    })
    return True


def resolve_compile(setting) -> bool:
    if isinstance(setting, bool):
        return setting
    if str(setting).lower() in ("true", "1", "yes", "on"):
        return True
    if str(setting).lower() in ("false", "0", "no", "off"):
        return False
    try:  # auto
        import triton  # noqa: F401
        return True
    except Exception:
        logger.warning("未检测到 triton，torch.compile 自动关闭（Windows 可 pip install triton-windows）")
        return False


def attention_backend() -> str:
    """当前官方模型实际会用的注意力实现（与 flash_head_model.flash_attention 的优先级一致）。"""
    try:
        from flash_head.src.modules import flash_head_model as m
    except Exception:
        return "unknown"
    if m.SAGE_ATTN_AVAILABLE:
        return "sageattention"
    if m.FLASH_ATTN_3_AVAILABLE:
        return "flash_attn3"
    if m.FLASH_ATTN_2_AVAILABLE:
        return "flash_attn2"
    return "torch_sdpa"
