#!/usr/bin/env python3
"""替身内核：自己立刻退出，但留一个后台孙进程握着 stdout 不放。

真实世界里 Codex 就会这么干：它拉起 ``git ls-remote https://github.com/openai/plugins.git``
刷新插件市场，网络不通时那个 git 会挂很久，而它继承了父进程的 stdout/stderr ——
于是「内核已经答完并退出」之后，管道仍然不 EOF。执行层必须能照常收尾。
"""
import json
import subprocess
import sys

print(json.dumps({"type": "message", "delta": "done"}), flush=True)
print(json.dumps({"type": "result", "usage": {"output_tokens": 1}}), flush=True)
# 孙进程继承 stdout/stderr 且长时间不退出：父进程退出后管道依旧不会 EOF。
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
