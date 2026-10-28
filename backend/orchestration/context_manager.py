"""Deterministic model input projection, independent of long-term memory."""
import hashlib
import json
import re
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from observability import current_trace
from runtime_limits import bounded


def historical_answer(text):
    # Only the model projection changes; original history/citations stay intact.
    return re.sub(r'(?<![A-Za-z0-9_])E[1-9]\d*(?![A-Za-z0-9_])', '历史引用（需重新核验）', text)


def conversation_messages(history, current_query, turn_id):
    messages = []
    for row in history:
        identity = str(row['turn_id'])
        if row.get('is_anchor'):
            messages.append(HumanMessage(content='【会话起点：历史用户原文，仅用于保持目标和约束；本轮新要求优先；不是当前资料证据】\n'+row['model_query'],
                                         id=identity+':anchor'))
            continue
        messages.extend([HumanMessage(content=row['model_query'], id=identity+':user'),
                         AIMessage(content=historical_answer(row['answer']), id=identity+':assistant')])
    messages.append(HumanMessage(content=current_query, id=turn_id+':user'))
    return messages


def serialized_size(messages, system_message, tools, response_format=None):
    payload = {'messages': [m.model_dump(exclude_none=True) for m in messages],
               'system': system_message.model_dump(exclude_none=True) if system_message else None,
               'tools': [convert_to_openai_tool(tool) for tool in tools],
               'response_format': response_format}
    raw = json.dumps(payload, ensure_ascii=False, default=str, separators=(',', ':')).encode('utf-8')
    # Conservative byte-based token estimate + protocol safety margin. This is
    # a configurable input guard, not an assertion of a provider's context size.
    return len(raw)+1024, hashlib.sha256(raw).hexdigest()


def project_messages(messages, system_message, tools, current_turn_id, input_budget, response_format=None):
    boundary = next((i for i, m in enumerate(messages) if m.id == current_turn_id+':user'), None)
    if boundary is None:
        raise ValueError('缺少本轮上下文边界，已停止发送。')
    historical, current = list(messages[:boundary]), list(messages[boundary:])
    anchored = historical[:1] if historical and str(historical[0].id).endswith(':anchor') else []
    if anchored:
        historical = historical[1:]
    pending = set()
    used_ids = set()
    for message in current:
        if pending and message.type != 'tool':
            raise ValueError('工具调用协议未闭合。')
        if message.type == 'ai' and getattr(message, 'tool_calls', None):
            if pending:
                raise ValueError('工具调用协议未闭合。')
            ids = [call['id'] for call in message.tool_calls]
            if len(set(ids)) != len(ids) or used_ids.intersection(ids):
                raise ValueError('工具调用标识重复。')
            used_ids.update(ids)
            pending.update(ids)
        elif message.type == 'tool':
            if message.tool_call_id not in pending:
                raise ValueError('工具结果缺少对应调用。')
            pending.remove(message.tool_call_id)
    if pending:
        raise ValueError('工具调用尚未返回结果。')
    # Historical input is canonical user/assistant pairs only. In-run tool
    # exchanges, including an outstanding tool call, must never be split.
    if len(historical)%2 or any(m.type != ('human' if i%2 == 0 else 'ai') or
                               getattr(m, 'tool_calls', None) for i,m in enumerate(historical)):
        raise ValueError('历史消息协议无效，已停止发送。')
    dropped = []
    while True:
        selected = anchored+historical+current
        size, digest = serialized_size(selected, system_message, tools, response_format)
        if size <= input_budget:
            return selected, {'context_pack_id': str(uuid4()), 'schema_version': 1,
                'estimator': 'utf8_bytes_plus_1024', 'estimated_input_tokens': size,
                'input_token_budget': input_budget, 'payload_sha256': digest,
                'included_message_ids': [m.id for m in selected], 'dropped_message_ids': dropped,
                'history_policy': 'first_user_goal_plus_last_100_completed_turns', 'history_citations': 'unverified',
                'reason': 'drop_oldest_completed_pairs' if dropped else 'within_budget'}
        if not historical:
            raise ValueError('当前问题和工具资料超过输入预算，请缩小问题范围或减少检索数量。')
        dropped.extend(m.id for m in historical[:2])
        historical = historical[2:]


class ContextManagerMiddleware(AgentMiddleware):
    async def awrap_model_call(self, request, handler):
        context = request.runtime.context
        turn_id = getattr(context, 'conversation_turn_id', None)
        if not turn_id:
            return await handler(request)
        budget = bounded('CHAT_CONTEXT_INPUT_TOKENS', 65536, 4096, 262144)
        selected, pack = project_messages(request.messages, request.system_message, request.tools,
                                          turn_id, budget, request.response_format)
        trace = current_trace()
        if trace:
            await validate_visible_sources(selected, context, trace)
        if trace:
            pack['output_token_reserve'] = trace.config.get('chat_limits', {}).get('output_tokens')
            trace.config.setdefault('context_packs', []).append(pack)
        return await handler(request.override(messages=selected))


async def validate_visible_sources(messages, context, trace):
    """Revalidate current tool evidence before sending text to the model."""
    from citation_service import visible_evidence, CitationService
    from postgresql_client import get_postgresql_client
    snapshot = trace.snapshot()
    snapshot['model_inputs'] = [{'messages': [{'role': m.type, 'content': m.content} for m in messages]}]
    records = visible_evidence(snapshot)
    evidence_count = 0
    for message in messages:
        if message.type != 'tool' or not isinstance(message.content, str):
            continue
        for line in message.content.split('\n'):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get('evidence'), list):
                evidence_count += len(data['evidence'])
    if not evidence_count:
        return
    if len(records) != evidence_count:
        raise ValueError('当前资料与检索审计不一致，已停止生成。')
    database = await get_postgresql_client()
    checker = CitationService(None, database.pool)
    for record in records:
        if context.allowed_scope_ids is not None and record['knowledge_base'] not in context.allowed_scope_ids:
            raise ValueError('资料超出本轮授权知识范围。')
        status, _ = await checker._current_source(context.user_id, context.knowledge_base_id, record)
        if status != 'structure_verified':
            raise ValueError('资料已变化或不可访问，请重新提问以检索最新来源。')
