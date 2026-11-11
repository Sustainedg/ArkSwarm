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
    resolve_order_targets,
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


def test_parse_takes_the_last_block_when_a_round_has_several():
    """工具型内核会把草稿和结论写进同一轮：取最后一个，但必须留下可看见的提醒。"""
    text = f'```{FENCE}\n{{"action":"done"}}\n```\n补充一句：\n```{FENCE}\n{{"action":"submit","summary":"x"}}\n```'
    result = parse_work_order(text)
    assert result.ok and result.order is not None
    assert result.order.action is TicketAction.SUBMIT and result.order.summary == "x"
    assert "2 个" in (result.warning or "") and FENCE in (result.warning or "")
    # 没有偏差时不产生噪声
    assert parse_work_order(f'```{FENCE}\n{{"action":"done"}}\n```').warning is None


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

    # 入口节点提问必须给可点选项；只把选项写在正文里不算
    error = validate_order(ask, allowed_actions=[TicketAction.ASK_USER], allowed_assignees=[], is_entry=True)
    assert error is not None and "options" in error


def test_ask_user_parses_and_requires_clickable_options():
    text = f'''```{FENCE}
{{
  "action": "ask_user",
  "question": "首期要先做最小闭环，还是一次做全？",
  "choices": [
    "先做最小闭环",
    {{"label": "一次做全", "value": "首期范围一次做全", "description": "周期更长"}},
    {{"label": "我来补充", "value": "", "freeform": true}}
  ]
}}
```'''
    parsed = parse_work_order(text)
    assert parsed.ok, parsed.error
    assert parsed.order is not None
    options = parsed.order.options
    assert [option.label for option in options] == ["先做最小闭环", "一次做全", "我来补充"]
    assert options[0].value == "先做最小闭环"
    assert options[1].description == "周期更长"
    assert options[2].freeform is True and options[2].value == ""
    assert validate_order(
        parsed.order,
        allowed_actions=[TicketAction.ASK_USER],
        allowed_assignees=[],
        is_entry=True,
    ) is None

    # 少于两条可比较的选项 → 明确拒绝，逼模型把选择做成卡片而不是一段长问题
    single = parse_work_order(
        f'```{FENCE}\n{{"action":"ask_user","question":"怎么办","options":["按默认来"]}}\n```'
    ).order
    assert single is not None
    error = validate_order(
        single, allowed_actions=[TicketAction.ASK_USER], allowed_assignees=[], is_entry=True
    )
    assert error is not None and "至少" in error

    # 只有 freeform 也没有可点的答复
    freeform_only = parse_work_order(
        f'```{FENCE}\n{{"action":"ask_user","question":"怎么办","options":'
        '[{"label":"我来补充","value":"","freeform":true},'
        '{"label":"还是补充","value":"","freeform":true}]}\n```'
    ).order
    assert freeform_only is not None
    error = validate_order(
        freeform_only,
        allowed_actions=[TicketAction.ASK_USER],
        allowed_assignees=[],
        is_entry=True,
    )
    assert error is not None and "可直接点选" in error


def test_protocol_guide_states_the_fence_and_the_whitelist():
    guide = render_protocol_guide(
        duty_label="协调者",
        actions=[TicketAction.DELEGATE, TicketAction.ASK_USER],
        assignees=["cto"],
    )
    assert FENCE in guide
    assert "cto" in guide
    assert "delegate" in guide and "ask_user" in guide
    assert "options" in guide and "freeform" in guide
    # 示例用四重围栏包住，避免模型把示例当成真输出照抄
    assert "````" in guide


def test_display_names_are_normalized_to_node_keys():
    """模型把 assignee 写成「CTO」时，唯一显示名应归一到图上的 key。"""
    order = parse_work_order(
        f'```{FENCE}\n{{"action":"delegate","assignee":"CTO",'
        '"mode":"handoff","ticket":{"title":"继续"}}\n```'
    ).order
    assert order is not None
    resolve_order_targets(order, {"CTO": "dev", "开发": "dev-2"})
    assert order.assignee == "dev"
    assert validate_order(order, allowed_actions=[TicketAction.DELEGATE], allowed_assignees=["dev"]) is None

    # 带括号的展示形式也认；同名节点则要求模型写 key，不猜
    wrapped = parse_work_order(
        f'```{FENCE}\n{{"action":"review","verdict":"changes_requested",'
        '"review_comment":"再改一下","rework_to":"SE（se）"}}\n```'
    ).order
    assert wrapped is not None
    resolve_order_targets(wrapped, {"SE": "se"})
    assert wrapped.rework_to == "se"

    ambiguous = parse_work_order(
        f'```{FENCE}\n{{"action":"delegate","assignee":"CTO","mode":"handoff"}}\n```'
    ).order
    assert ambiguous is not None
    resolve_order_targets(ambiguous, {})  # 调用方对重名节点不提供映射
    assert ambiguous.assignee == "CTO"
