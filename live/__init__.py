"""SoulX-FlashHead 实时直播服务层。

主进程（FastAPI）只负责调度、TTS、音视频时钟和推流，不导入 torch；
模型推理放在每张显卡一个的 worker 子进程里（见 live/worker）。
"""

__version__ = "0.1.0"
