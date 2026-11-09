#!/usr/bin/env python3
"""Stand-in for a real CLI agent: reads a prompt, emits JSONL on stdout."""
import json
import sys
import time

prompt = sys.stdin.read().strip()
session_id = sys.argv[1] if len(sys.argv) > 1 else "fake-session-1"

def emit(payload):
    print(json.dumps(payload), flush=True)
    time.sleep(0.005)

emit({"type": "session", "session_id": session_id})
for piece in ["Echo: ", prompt[:40], "...", " done"]:
    emit({"type": "message", "delta": piece})
emit({"type": "tool_call", "tool_call": {"id": "t1", "name": "run_shell", "args": {"command": "ls"}}})
emit({"type": "tool_result", "id": "t1", "output": "a.txt"})
emit({"type": "result", "usage": {"input_tokens": 11, "output_tokens": 22}})
