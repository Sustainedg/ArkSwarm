"""工单协议：三级容错解析、语义校验与交付载荷渲染。

这套用例是整条协作链路的保险丝 —— 解析器一旦开始「猜」，角色之间就会传错话。
"""
from __future__ import annotations

from app.collab.schemas import TicketAction
from app.collab.tickets import (
    FENCE,
    extract_body,
    parse_work_order,
    render_protocol_guide,
    sanitize_json_text,
    validate_order,
)

GOOD = """我理解了这个需求，先安排下去。

```work_order
{
  "action": "delegate",
  "assignee": "cto",
  "ticket": {
    "title": "登录页暗色主题",
    "goal": "不改后端就能切换主题",
    "requirements": ["新增主题开关", "持久化选择"],
    "acceptance": ["刷新后主题保持"],
    "constraints": ["只改 frontend/src/**"]
  }
}
```
"""


def test_parse_standard_fenced_block():
    result = parse_work_order(GOOD)
    assert result.ok and result.source == "fence"
    order = result.order
    assert order is not None
    assert order.action is TicketAction.DELEGATE
    assert order.assignee == "cto"
    assert order.ticket is not None
    assert order.ticket.title == "登录页暗色主题"
    assert order.ticket.requirements == ["新增主题开关", "持久化选择"]
    # 正文只保留给人看的部分，工单块不重复出现在卡片上
    assert extract_body(GOOD) == "我理解了这个需求，先安排下去。"


def test_parse_tolerates_trailing_comma_and_control_chars():
    text = '```work_order\n{"action":"submit",\x07"summary":"做完了","artifacts":["a.py",],}\n```'
    result = parse_work_order(text)
    assert result.ok
    assert result.order is not None
    assert result.order.summary == "做完了"
    # 字符串形态的 artifacts 会被规整成 {kind, path}
    assert result.order.artifacts == [{"kind": "file", "path": "a.py"}]


def test_parse_repairs_smart_quotes():
    text = '```work_order\n{“action”:“submit”,“summary”:“ok”}\n```'
    result = parse_work_order(text)
    assert result.ok and result.order is not None and result.order.summary == "ok"


def test_parse_recovers_from_truncated_output():
    # 输出被截断：围栏没闭合、JSON 少了右括号
    text = '```work_order\n{"action":"submit","summary":"做完了","artifacts":["a.py"]'
    result = parse_work_order(text)
    assert result.ok, result.error
    assert result.order is not None and result.order.summary == "做完了"

    # 截断发生在字符串中间时，按最后一个逗号回退
    text = '```work_order\n{"action":"submit","summary":"做完了一半'
    result = parse_work_order(text)
    assert result.ok, result.error
    assert result.order is not None and result.order.action is TicketAction.SUBMIT


def test_parse_falls_back_to_inline_json():
    text = '我想了想，就这样吧：{"action":"done","remark":"收工"} 以上。'
    result = parse_work_order(text)
    assert result.ok and result.source == "inline"
    assert result.order is not None and result.order.remark == "收工"


def test_parse_rejects_multiple_blocks_instead_of_guessing():
    text = f'```{FENCE}\n{{"action":"done"}}\n```\n补充一句：\n```{FENCE}\n{{"action":"submit","summary":"x"}}\n```'
    result = parse_work_order(text)
    assert not result.ok
    assert "多个" in (result.error or "")


def test_parse_reports_missing_action_and_unknown_action():
    assert "action" in (parse_work_order(f'```{FENCE}\n{{"summary":"没有动作"}}\n```').error or "")
    error = parse_work_order(f'```{FENCE}\n{{"action":"coding"}}\n```').error or ""
    assert "coding" in error and "delegate" in error


def test_parse_reports_garbage_and_empty_output():
    assert not parse_work_order("我这就去办，稍等。").ok
    assert not parse_work_order("   ").ok
    # 缺字段时给出的是可读原因，而不是抛异常
    assert parse_work_order(f'```{FENCE}\n{{"action":\n```').error


def test_sanitize_keeps_json_semantics():
    assert sanitize_json_text('{"a": 1,}') == '{"a": 1}'
    assert sanitize_json_text('\ufeff{"a":\x00 1}') == '{"a": 1}'


def test_validate_order_enforces_contract_and_whitelist():
    order = parse_work_order(GOOD).order
    assert order is not None

    # 派发对象不在白名单里 → 明确指出允许谁
    error = validate_order(order, allowed_actions=[TicketAction.DELEGATE], allowed_assignees=["se"])
    assert error is not None and "se" in error

    assert validate_order(order, allowed_actions=[TicketAction.DELEGATE], allowed_assignees=["cto"]) is None

    # 越权动作
    error = validate_order(order, allowed_actions=[TicketAction.SUBMIT], allowed_assignees=["cto"])
    assert error is not None and "delegate" in error

    # 审查必须给结论；打回必须给意见
    review = parse_work_order(f'```{FENCE}\n{{"action":"review","verdict":"changes_requested"}}\n```').order
    assert review is not None
    error = validate_order(review, allowed_actions=[TicketAction.REVIEW], allowed_assignees=[])
    assert error is not None and "review_comment" in error

    pass_order = parse_work_order(f'```{FENCE}\n{{"action":"review","verdict":"pass"}}\n```').order
    assert pass_order is not None
    assert validate_order(pass_order, allowed_actions=[TicketAction.REVIEW], allowed_assignees=[]) is None

    # 非入口节点不能向用户提问
    ask = parse_work_order(f'```{FENCE}\n{{"action":"ask_user","question":"要暗色还是亮色？"}}\n```').order
    assert ask is not None
    assert validate_order(ask, allowed_actions=[TicketAction.ASK_USER], allowed_assignees=[], is_entry=False)


def test_protocol_guide_states_the_fence_and_the_whitelist():
    guide = render_protocol_guide(
        duty_label="协调者",
        actions=[TicketAction.DELEGATE, TicketAction.ASK_USER],
        assignees=["cto"],
    )
    assert FENCE in guide
    assert "cto" in guide
    assert "delegate" in guide and "ask_user" in guide
    # 示例用四重围栏包住，避免模型把示例当成真输出照抄
    assert "````" in guide
