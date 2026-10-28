"""Streaming Agent with request-local timings and durable evaluation snapshots."""
import asyncio
import json
import logging
import os
from copy import deepcopy
from typing import AsyncGenerator
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import BackgroundTasks
from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware, dynamic_prompt, ModelRequest, SummarizationMiddleware
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.redis.aio import AsyncRedisSaver

import config
from observability import RunTrace, ModelTimingCallback, measure, record_context, safe_error, current_trace
from profile_store import ProfileStore, describe_personalization
from postgresql_client import get_postgresql_client
from schemas import ContextSchema
from tools import search_knowledge_base, get_memory, get_raw_conversation_by_summary_id
from rag.settings import env_bool
from runtime_limits import ChatLimits
from skill_registry import build_skill_prompt, get_skill, SkillCommandError
from orchestration.context_manager import ContextManagerMiddleware, conversation_messages

load_dotenv()
logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL")
TTL_CONFIG = {"default_ttl": 60, "refresh_on_read": True}
model = ChatOpenAI(model=os.getenv("AGENT_BASE_MODEL"), api_key=os.getenv("DASHSCOPE_API_KEY"),
                   base_url=os.getenv("BASE_URL"), temperature=0.7, streaming=True, stream_usage=True,
                   max_tokens=ChatLimits.from_env().output_tokens,max_retries=0)
summarize_model = ChatOpenAI(model=os.getenv("SUMMARIZATION_MODEL", "qwen-turbo"),
                            api_key=os.getenv("DASHSCOPE_API_KEY"), base_url=os.getenv("BASE_URL"), temperature=0.2)


def build_system_prompt(context) -> str:
    snapshot = context.profile_snapshot or {}
    profile = snapshot.get("profile_text", "") if snapshot.get("enabled") else ""
    if profile:
        trace = current_trace()
        if not snapshot.get("injected"):
            record_context("prompt", "profile", profile, included_in_prompt=True,
                           revision=snapshot["revision"], profile_source=snapshot["source"])
        snapshot["injected"] = True
        snapshot["reason"] = "injected"
        if trace:
            trace.personalization = snapshot.copy()
    template = config.SYSTEM_PROMPT_DEFAULT if context.knowledge_base_id == "默认知识库" else config.SYSTEM_PROMPT_SPECIFIC
    if getattr(context, 'conversation_turn_id', None):
        template = config.CONTEXTUAL_SYSTEM_PROMPT
    prompt = template.format(user_profile=profile)
    skill_id = getattr(context, 'skill_id', None)
    if skill_id:
        # Only the server-owned catalog supplies workflow instructions. User
        # arguments and retrieved documents never become a skill definition.
        prompt += '\n\n【本轮技能工作流】\n' + build_skill_prompt(skill_id)
    project = getattr(context, 'project_snapshot', None)
    if project:
        from assistant.service import render_project_context
        project_text = render_project_context(project)
        prompt += '\n\n' + project_text
        record_context('prompt','project',project_text,included_in_prompt=True,
                       project_id=project['project']['project_id'],revision=project['project']['revision'])
    return prompt


@dynamic_prompt
async def dynamic_prompt(request: ModelRequest) -> str:
    return build_system_prompt(request.runtime.context)


async def get_or_create_agent(checkpointer, memory_enabled=True, *, contextual=False, project_enabled=False, proposals_enabled=True):
    # A compiled graph must not retain the previous request's closed checkpointer.
    tools = [search_knowledge_base]
    middleware = [ToolCallLimitMiddleware(tool_name="search_knowledge_base", run_limit=1), dynamic_prompt]
    if memory_enabled:
        tools += [get_memory, get_raw_conversation_by_summary_id]
        middleware += [ToolCallLimitMiddleware(tool_name="get_memory", run_limit=1)]
        if not contextual:
            middleware += [SummarizationMiddleware(model=summarize_model, trigger=("tokens", 10000), keep=("messages", 20))]
    if contextual:
        middleware.append(ContextManagerMiddleware())
    if project_enabled:
        from assistant.tools import get_project_workspace, propose_project_action
        tools.append(get_project_workspace)
        middleware.append(ToolCallLimitMiddleware(tool_name='get_project_workspace',run_limit=2))
        if proposals_enabled:
            tools.append(propose_project_action)
            middleware.append(ToolCallLimitMiddleware(tool_name='propose_project_action',run_limit=4))
    return create_agent(model=model, tools=tools, context_schema=ContextSchema,
                        checkpointer=checkpointer, middleware=middleware)


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    return ""


def event(kind, **fields):
    return json.dumps({"type": kind, "version": 1, **fields}, ensure_ascii=False, default=str) + "\n"


async def stream_agent_response(user_message: str, knowledge_base_id: str, user_id: int,
                                background_tasks: BackgroundTasks = None, *, trace=None, store=None,
                                session_id="legacy", top_k=5, memory_enabled=True,
                                rerank_enabled=True, retrieval_mode="hybrid", evaluation_mode=False,
                                graph_enabled=None, evidence_enabled=None, skill_id=None,
                                skill_version=None, conversation_history=None,
                                conversation_turn_id=None, project_snapshot=None) -> AsyncGenerator[str, None]:
    trace = trace or RunTrace(user_id, session_id, user_message, knowledge_base_id)
    limits = ChatLimits.from_env()
    trace.config['chat_limits'] = {'total_seconds':limits.total_seconds,'model_calls':limits.model_calls,'output_tokens':limits.output_tokens}
    if skill_id:
        build_skill_prompt(skill_id)  # Reject unknown/local skills before model work.
        actual_version = get_skill(skill_id).version
        if skill_version is not None and skill_version != actual_version:
            raise SkillCommandError('技能版本已变化，请重新选择技能。')
        trace.config['skill'] = {'id':skill_id,'version':actual_version}
    trace.config.update({"model": os.getenv("AGENT_BASE_MODEL"), "embedding_model": os.getenv("EMBEDDING_MODEL"),
                         "rerank_model": os.getenv("RERANK_MODEL"), "top_k": top_k,
                         "memory_enabled": memory_enabled, "rerank_enabled": rerank_enabled,
                         "retrieval_mode": retrieval_mode, "evaluation_mode": evaluation_mode,
                         "graph_enabled": graph_enabled if graph_enabled is not None else env_bool("GRAPH_RAG_ENABLED"),
                         "evidence_enabled": evidence_enabled if evidence_enabled is not None else env_bool("EVIDENCE_CHECK_ENABLED")})
    thread_id = str(user_id) if session_id == "legacy" else f"{user_id}:{session_id}"
    contextual = conversation_turn_id is not None
    if contextual:
        # PostgreSQL is authoritative for inter-turn continuity. A fresh graph
        # thread per turn prevents expired/stale checkpoints and old tool E IDs
        # from silently becoming this turn's evidence.
        thread_id += ':run:'+trace.run_id
        trace.config['context_source'] = 'postgresql_completed_turns'
    answer_parts = []
    persistence_error = None
    with trace.activate():
        try:
            yield event("run", run_id=trace.run_id, session_id=session_id, skill=trace.config.get('skill'))
            remaining = limits.total_seconds - trace.elapsed()/1000
            if remaining <= 0:
                raise TimeoutError('本轮处理已达到时间上限')
            async with asyncio.timeout(remaining), AsyncRedisSaver.from_conn_string(redis_url=REDIS_URL, ttl=TTL_CONFIG) as checkpointer:
                with measure("checkpoint", "redis"):
                    await checkpointer.asetup()
                if memory_enabled and not evaluation_mode:
                    with measure("profile", "postgresql"):
                        database = await get_postgresql_client()
                        profile_state = await ProfileStore(database.pool).get(user_id)
                        trace.personalization = describe_personalization(profile_state)
                else:
                    trace.personalization = {"enabled": False, "injected": False, "revision": None,
                                             "source": "empty", "profile_text": "", "reason": "disabled"}
                with measure("agent_setup", "agent"):
                    if project_snapshot and contextual and not evaluation_mode:
                        agent = await get_or_create_agent(checkpointer, memory_enabled, contextual=True,
                            project_enabled=True, proposals_enabled=not bool(skill_id))
                    else:
                        agent = await get_or_create_agent(checkpointer, memory_enabled, contextual=True) if contextual else await get_or_create_agent(checkpointer, memory_enabled)
                context = ContextSchema(user_id=user_id, knowledge_base_id=knowledge_base_id, top_k=top_k,
                                        session_id=session_id, memory_enabled=memory_enabled,
                                        rerank_enabled=rerank_enabled, retrieval_mode=retrieval_mode,
                                        graph_enabled=graph_enabled,
                                        evidence_enabled=evidence_enabled,
                                        profile_snapshot=trace.personalization, skill_id=skill_id,
                                        conversation_turn_id=conversation_turn_id,
                                        allowed_scope_ids=tuple(trace.config.get('scope_snapshot', [])) if contextual else None,
                                        project_snapshot=deepcopy(project_snapshot) if not evaluation_mode else None)
                messages = conversation_messages(conversation_history or [], user_message, conversation_turn_id) if contextual else [{"role": "user", "content": user_message}]
                async for mode, chunk in bounded_agent_stream(agent,
                    {"messages": messages},
                    {"configurable": {"thread_id": thread_id}, "callbacks": [ModelTimingCallback(trace)]},
                    stream_mode=["messages", "custom"], context=context,
                    remaining_seconds=limits.total_seconds-trace.elapsed()/1000):
                    if mode == "messages":
                        message, metadata = chunk
                        text = text_content(getattr(message, "content", ""))
                        if metadata.get("langgraph_node") == "model" and text:
                            trace.mark_answer()
                            answer_parts.append(text)
                            yield event("answer", content=text)
                    elif mode == "custom":
                        yield event("status", content=chunk)
            trace.finish("".join(answer_parts))
            if background_tasks and not evaluation_mode and not contextual:
                background_tasks.add_task(save_conversation_messages, user_id, thread_id, user_message, trace.answer)
        except (asyncio.CancelledError, GeneratorExit):
            trace.finish("".join(answer_parts), "cancelled")
            raise
        except Exception as exc:
            logger.exception("Chat run failed: %s", trace.run_id)
            if isinstance(exc, TimeoutError) and not str(exc):
                exc = TimeoutError('本轮处理已达到时间上限，请稍后重试或缩小问题范围')
            trace.finish("".join(answer_parts), "failed", exc)
            yield event("error", content=safe_error(exc))
        finally:
            if trace.status == "running":
                trace.finish("".join(answer_parts), "cancelled")
            if store is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(store.save_run(trace.snapshot())), timeout=5)
                    trace.persisted = True
                except Exception as error:
                    logger.exception("Could not persist chat trace %s", trace.run_id)
                    if contextual:
                        trace.finish(''.join(answer_parts), 'failed', error)
                        persistence_error = '回答未能确认为已保存，请刷新会话核对后再发送。'
        if persistence_error:
            yield event('error', content=persistence_error)
        yield event("metrics", run_id=trace.run_id, timings=trace.timings(), status=trace.status,
                    persisted=trace.persisted, personalization=trace.personalization)
        yield event("done", run_id=trace.run_id, status=trace.status)


async def save_conversation_messages(user_id, thread_id, user_message, ai_message):
    try:
        database = await get_postgresql_client()
        await database.add_conversation_message(user_id, thread_id, "human", user_message)
        await database.add_conversation_message(user_id, thread_id, "ai", ai_message)
    except Exception:
        logger.exception("Could not persist conversation")


async def bounded_agent_stream(agent,*args,remaining_seconds,**kwargs):
    if remaining_seconds<=0:
        raise TimeoutError('本轮处理已达到时间上限')
    async with asyncio.timeout(remaining_seconds):
        async for item in agent.astream(*args,**kwargs):
            yield item
