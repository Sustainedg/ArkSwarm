#!/usr/bin/env python3
"""协作流程的替身智能体：按 prompt 里的节点 key 回不同的 work_order。

用法：``fake_collab_agent.py <script.json> <state_dir>``

* 剧本是 JSON：``{"ceo": [ {...}, ... ], "se": [...] }``；同一个 key 被调用多次时按顺序取，
  用完最后一个之后重复最后一个（方便写「第二次才通过」这种审查剧本）。
* 收到的 prompt 追加写进 ``<state_dir>/prompts.jsonl``，供测试断言「下游只看到工单、
  看不到上游的对话原文」。
* 每个角色都会输出一句固定的闲聊「（<key>）我来处理。」——如果隔离被破坏，
  这句话就会出现在别人的 prompt 里，测试立刻能发现。
"""
import json
import os
import re
import sys
import time

script_path, state_dir = sys.argv[1], sys.argv[2]
os.makedirs(state_dir, exist_ok=True)
prompt = sys.stdin.read()

with open(os.path.join(state_dir, "prompts.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"prompt": prompt}, ensure_ascii=False) + "\n")

match = re.search(r"你的岗位：([^（\n]+)（([^）\n]+)）", prompt)
node_key = match.group(2) if match else "unknown"

with open(script_path, encoding="utf-8") as handle:
    script = json.load(handle)
steps = script.get(node_key) or []
counter_path = os.path.join(state_dir, f"{node_key}.count")
index = 0
if os.path.exists(counter_path):
    with open(counter_path, encoding="utf-8") as handle:
        index = int((handle.read().strip() or "0"))
with open(counter_path, "w", encoding="utf-8") as handle:
    handle.write(str(index + 1))
payload = steps[min(index, len(steps) - 1)] if steps else {"action": "ask_user", "question": "？"}


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)
    time.sleep(0.002)


emit({"type": "session", "session_id": f"fake-{node_key}-{index}"})
for piece in ["（", node_key, "）", "我来处理。"]:
    emit({"type": "message", "delta": piece})
if isinstance(payload, str):
    # 剧本给字符串时，原样输出（用来模拟「产出不符合工单协议」）
    emit({"type": "message", "delta": payload})
else:
    emit(
        {
            "type": "message",
            "delta": "\n```work_order\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```\n",
        }
    )
emit({"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}})
