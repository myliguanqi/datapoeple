"""生成火山 TTS 的假 SSE 响应样本（测试用，不访问真实接口）。运行：python tests/fixtures/volc_sse/make_fixtures.py

每个块是 1600 个采样的 s16le PCM（16k 下 0.1 秒的 440Hz 正弦），base64 放在 data 字段里。
"""
import base64
import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))


def chunk(i: int, n: int = 1600) -> str:
    t = (np.arange(n) + i * n) / 16000
    pcm = (np.sin(2 * np.pi * 440 * t) * 0.3 * 32767).astype("<i2")
    return base64.b64encode(pcm.tobytes()).decode()


def ev(obj) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False)


FILES = {
    "normal.txt": [ev({"code": 0, "message": "", "data": chunk(0)}), "", ev({"code": 0, "data": chunk(1)}), "",
                   ev({"code": 20000000, "message": "OK", "data": None})],
    "multi.txt": [ev({"code": 0, "data": chunk(i)}) for i in range(6)] + [ev({"code": 20000000, "message": "OK"})],
    "error.txt": [ev({"code": 0, "data": chunk(0)}), ev({"code": 45000001, "message": "speaker not found", "data": chunk(9)}),
                  ev({"code": 20000000, "message": "OK"})],
    "empty.txt": [ev({"code": 20000000, "message": "OK"})],
    "nonjson.txt": [": keep-alive", "event: message", "data: not-json{", ev({"code": 0, "audio": chunk(0)}),
                    "data: [DONE]"],
}

if __name__ == "__main__":
    for name, lines in FILES.items():
        with open(os.path.join(HERE, name), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
    print("ok", sorted(FILES))
