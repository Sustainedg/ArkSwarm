#!/usr/bin/env python3
"""替身内核：把指定环境变量的值原样吐回标准输出。

用于验证两件事：凭据确实以环境变量注入到子进程；运行层会把值从事件里擦掉。
"""
import json
import os
import sys

name = sys.argv[1] if len(sys.argv) > 1 else "DEMO_TOKEN"
sys.stdin.read()
print(json.dumps({"type": "session", "session_id": "fake-env-1"}), flush=True)
print(json.dumps({"type": "message", "delta": f"token={os.environ.get(name, '')}"}), flush=True)
print(json.dumps({"type": "result", "usage": {"input_tokens": 1, "output_tokens": 2}}), flush=True)
