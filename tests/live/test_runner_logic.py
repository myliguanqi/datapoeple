"""运行：python tests/live/test_runner_logic.py（需要 torch + CUDA + 官方依赖，不需要模型权重）

FlashHeadRunner 逻辑验证：真实 FlashHeadPipeline 的 prepare_params / generate / reset_person_name，
只把 DiT、VAE、wav2vec 换成微型假模块（没有真权重）。验证：
  1. Windows 无 xfuser 时官方模块能导入（兼容桩）
  2. 块大小 lite=24 / pro=28，输出 uint8 [N,512,512,3]
  3. 同一模型多会话交替生成时状态互不干扰（与单独生成逐位一致）
  4. lite->pro 切换时运动帧由尾帧重新编码，之后正常生成
  5. 换形象、关闭会话、模型占用时卸载报错、warmup、info
"""
import os, sys, tempfile, types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT); os.chdir(ROOT)
import numpy as np, cv2

results = []
def check(name, ok, detail=""):
    results.append(ok); print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)

# ---- 1. 兼容桩 + 官方模块导入 ----
from live.worker import compat
stub = compat.install_xfuser_stub()
check("无 xfuser 时注入桩", stub and "xfuser.core.distributed" in sys.modules)
import torch, torch.nn as nn, torch.nn.functional as F
from flash_head.src.pipeline import flash_head_pipeline as fhp
from flash_head.src.modules import flash_head_model
check("官方 pipeline / model 模块导入成功", hasattr(fhp, "FlashHeadPipeline"), f"attention={compat.attention_backend()}")
check("单卡时 dist 未初始化", not torch.distributed.is_initialized())

# ---- 微型假模块 ----
class FakeVAE:
    def __init__(self, ts, ss, ch, device):
        self.ts, self.ss, self.ch, self.device = ts, ss, ch, device
    def encode(self, video):            # [1,3,T,H,W] -> [C,t,h,w]
        v = video[0].float()
        T = v.shape[1]
        idx = [0] + list(range(self.ts, T, self.ts)) if T > 1 else [0]
        frames = v[:, idx]                                  # [3,t,H,W]
        z = F.avg_pool2d(frames.permute(1, 0, 2, 3), self.ss).permute(1, 0, 2, 3)  # [3,t,h,w]
        reps = -(-self.ch // 3)
        return z.repeat(reps, 1, 1, 1)[:self.ch].to(video.dtype)
    def decode(self, z):                # [C,t,h,w] -> [1,3,T,H,W]
        x = z[:3].float()
        t = x.shape[1]
        up = F.interpolate(x.permute(1, 0, 2, 3), scale_factor=self.ss, mode="nearest").permute(1, 0, 2, 3)
        frames = [up[:, :1]] + [up[:, i:i + 1].repeat(1, self.ts, 1, 1) for i in range(1, t)]
        return torch.cat(frames, 1).clamp(-1, 1).unsqueeze(0).to(torch.bfloat16)

class FakeDiT(nn.Module):
    def forward(self, x, timestep, context, y):
        assert context.shape[:3] == (1, 33, 5), context.shape
        return x * 0.9 + context.float().mean() * 0.01

class TinyPipeline(fhp.FlashHeadPipeline):
    """复用官方 prepare_params / generate，只替换 __init__ 里的权重加载。"""
    def __init__(self, checkpoint_dir, model_type, wav2vec_dir, device="cuda", param_dtype=torch.bfloat16,
                 use_usp=False, num_timesteps=1000, use_timestep_transform=True):
        self.param_dtype, self.device, self.rank, self.use_usp = param_dtype, device, 0, False
        self.model_type, self.use_ltx = model_type, model_type == "lite"
        ts, ss, ch = (8, 32, 128) if self.use_ltx else (4, 8, 16)
        self.vae = FakeVAE(ts, ss, ch, device)
        self.model = FakeDiT()
        self.config = types.SimpleNamespace(vae_stride=(ts, ss, ss), out_dim=ch)
        self.sp_size, self.num_timesteps, self.use_timestep_transform = 1, num_timesteps, use_timestep_transform
    def preprocess_audio(self, speech_array, sr=16000, fps=25):   # 确定性的假 wav2vec：[T, 12, 768]
        n = int(len(speech_array) * fps / sr)
        a = torch.from_numpy(np.asarray(speech_array, np.float32)[: n * (sr // fps)].reshape(n, -1).std(1, keepdims=True))
        return (a[:, None, :] * torch.linspace(0, 1, 768)[None, None, :]).repeat(1, 12, 1).to(self.device)

fhp.FlashHeadPipeline = TinyPipeline

# ---- runner ----
from live.config import load_config
from live.worker.flashhead_runner import FlashHeadRunner
tmp = tempfile.mkdtemp()
for d in ("Model_Lite", "VAE_LTX", "Model_Pro", "VAE_Wan", "wav2vec"):
    os.makedirs(os.path.join(tmp, d))
def make_cfg():
    return load_config(os.path.join(ROOT, "live/configs/live.yaml"), {"models": {
        "ckpt_dir": tmp, "wav2vec_dir": os.path.join(tmp, "wav2vec"), "compile": False, "warmup": True}})
cfg = make_cfg()

def new_runner():
    r = FlashHeadRunner(make_cfg(), gpu_id=0)
    r.load_model("lite"); r.load_model("pro")
    return r

r1 = new_runner()
li, pi = r1.model_info["lite"], r1.model_info["pro"]
check("lite 块 = 24 帧 / 15360 采样, motion 9", (li["chunk_frames"], li["chunk_samples"], li["motion_frames"]) == (24, 15360, 9), str(li))
check("pro 块 = 28 帧 / 17920 采样, motion 5", (pi["chunk_frames"], pi["chunk_samples"], pi["motion_frames"]) == (28, 17920, 5), str(pi))
info = r1.info()
check("info()", info["gpu_name"] and set(info["models"]) == {"lite", "pro"} and info["xfuser_stub"], f"{info['gpu_name']} free={info['mem_free_gb']}G")

flip = os.path.join(tmp, "flip.png")
cv2.imwrite(flip, cv2.flip(cv2.imread(os.path.join(ROOT, "examples/girl.png")), 1))
girl = os.path.join(ROOT, "examples/girl.png")
rng = np.random.default_rng(0)
audios = [rng.standard_normal(15360).astype(np.float32) * 0.1 * (k % 3) for k in range(6)]

# 单独生成 A
r1.open_session("A", "lite", girl, seed=1)
solo = [r1.generate("A", a)["frames"] for a in audios]
check("lite 输出 uint8 [24,512,512,3]", solo[0].dtype == np.uint8 and solo[0].shape == (24, 512, 512, 3))

# 新 runner：A/B 交替（同模型）+ C（pro）
r2 = new_runner()
r2.open_session("A", "lite", girl, seed=1)
r2.open_session("B", "lite", flip, seed=2)
r2.open_session("C", "pro", girl, seed=3)
mixed = []
for k, a in enumerate(audios):
    mixed.append(r2.generate("A", a)["frames"])
    fb = r2.generate("B", audios[-1 - k])["frames"]
    fc = r2.generate("C", rng.standard_normal(17920).astype(np.float32) * 0.1)["frames"]
same = all(np.array_equal(x, y) for x, y in zip(solo, mixed))
check("多会话交替生成，A 的输出与单独生成逐位一致", same,
      f"max diff={max(int(np.abs(x.astype(int) - y.astype(int)).max()) for x, y in zip(solo, mixed))}")
check("pro 输出 [28,512,512,3]", fc.shape == (28, 512, 512, 3))
check("A、B 画面不同（各自参考图）", not np.array_equal(mixed[-1], fb))

# 模型切换
s = r2.sessions["A"]
tail = s.tail.clone()
info = r2.switch_model("A", "pro")
pipe = r2.pipes["pro"]
expect = pipe.vae.encode(tail[:, -5:].unsqueeze(0).to(pipe.device, pipe.param_dtype))
check("切换后 chunk 信息为 pro", info["chunk_frames"] == 28 and r2.sessions["A"].model == "pro")
check("切换后运动帧 = 新 VAE 编码旧模型尾帧", torch.equal(pipe.latent_motion_frames, expect), str(tuple(pipe.latent_motion_frames.shape)))
check("切换后能继续生成 pro 块", r2.generate("A", np.zeros(17920, np.float32))["frames"].shape == (28, 512, 512, 3))
check("C（同为 pro）状态未被 A 切换破坏", r2.generate("C", np.zeros(17920, np.float32))["frames"].shape == (28, 512, 512, 3))
try:
    r2.generate("A", np.zeros(15360, np.float32)); bad = False
except ValueError:
    bad = True
check("音频长度不对时报错", bad)

# 换形象 / 卸载 / 关闭
r2.set_avatar("B", girl)
check("换形象后尾帧清空、可继续生成", r2.sessions["B"].tail is None and r2.generate("B", audios[0])["frames"].shape[0] == 24)
try:
    r2.unload_model("lite"); refused = False
except RuntimeError:
    refused = True
check("模型被会话占用时拒绝卸载", refused)
for sid in ("A", "B", "C"):
    r2.close_session(sid)
r2.unload_model("lite")
check("关闭会话后可卸载", "lite" not in r2.pipes and not r2.sessions)
print(f"\n{sum(results)}/{len(results)} 通过")
