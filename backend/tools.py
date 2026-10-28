import asyncio
from documents_process import rerank_documents
from postgresql_client import get_postgresql_client
from schemas import RerankDocumentItem
from milvus_client import get_milvus_client
import logging
import sys
from langgraph.config import get_stream_writer
import time
from schemas import ContextSchema
from langchain.tools import tool, ToolRuntime
import config
from observability import timed, measure, record_context
from rag.retrieval_service import RetrievalService
from rag.types import RetrievalScope, RetrievalOptions


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(name)s-%(levelname)s-%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


@tool("get_raw_conversation_by_summary_id")
@timed("history", "history")
async def get_raw_conversation_by_summary_id(
    summary_id: str,
    runtime: ToolRuntime[ContextSchema]
) -> str:
    """
    获取指定摘要ID对应的完整原始对话记录。
    
    该工具返回按时间顺序排列的原始对话文本，包含每条消息的时间戳、角色和完整内容。
    
    适用场景：
    - 摘要内容不够详细，需要查看完整的对话细节
    - 用户明确要求查看原始对话记录
    - 需要对对话内容进行精确分析、引用或二次处理
    - 摘要中遗漏了关键信息，需要补充上下文
    
    注意：
    - 只能在获取到对应摘要summary_id后调用，确保摘要存在且已压缩
    - 原始对话可能较长，调用前应评估是否需要完整内容
    - 仅在摘要无法满足需求时调用，避免不必要的开销
    - 返回格式：[时间戳] 角色: 消息内容
    
    Args:
        summary_id: 摘要ID，用于标识需要检索的对话摘要

    Returns:
        str: 格式化的原始对话文本，每条消息独立成行，包含时间戳、角色和内容
    """
    user_id = runtime.context.user_id
    postgresql_client = await get_postgresql_client()
    writer = get_stream_writer()
    writer(f"🔍 正在检索原始对话...")
    start_time = time.time()
    raw_conversation = await postgresql_client.get_raw_conversation_by_summary_id(
        summary_id=summary_id,
        user_id=user_id,
        thread_id=str(user_id) if runtime.context.session_id == "legacy" else f"{user_id}:{runtime.context.session_id}"
    )
    writer(f"🔍 原始对话检索完成，耗时{time.time() - start_time:.2f}秒")
    record_context("final", "history", raw_conversation, included_in_prompt=True, source_id=summary_id)
    return raw_conversation


@tool("get_memory")
@timed("memory", "memory")
async def get_memory(
    query: str,
    semantic_k: int,
    episodic_k: int,
    procedural_k: int,
    runtime: ToolRuntime[ContextSchema],
) -> str:
    """
    检索用户长期记忆，用于个性化回答、事实验证和历史连贯性。

    【调用时机】满足以下任一条件即调用：
    - 用户提到之前、刚才、上次等回忆性词汇
    - 重复提问或用户表现困惑
    - 询问操作步骤、个人偏好、习惯设置
    - 任何需要记住历史对话的场景

    【参数决策】（k值 0-5，0=不需要，3-5=核心依赖）
    - semantic_k（事实/概念）：事实问答3-5，操作问题2-3，历史回忆1-2
    - episodic_k（历史/对话）：重复提问/回忆3-5，事实问答2-3，操作问题1-2
    - procedural_k（步骤/方法）：操作问题3-5，事实问答2-3，历史回忆1-2

    Args:
        query: 用户原始问题（如果过于模糊或口语化可做适当改写）
        semantic_k: 语义记忆召回数量
        episodic_k: 情节记忆召回数量
        procedural_k: 程序记忆召回数量
    """
    logger.info(f"{'---'*20}开始检索记忆hybrid_retrieval_memories{'---'*20}")
    logger.info(f"ai希望检索的各种记忆数量: 语义记忆{semantic_k}, 情节记忆{episodic_k}, 程序记忆{procedural_k}")
    user_id = runtime.context.user_id
    writer = get_stream_writer()
    writer(f"🔍 正在检索记忆库...")
    start_time = time.time()
    milvus_client = await get_milvus_client()
    memories_dict = await milvus_client.hybrid_retrieval_memories(
        query,
        user_id=user_id,
        summary_k=1,
        semantic_k=semantic_k,
        episodic_k=episodic_k,
        procedural_k=procedural_k,
    )
    count = 1
    for memory in memories_dict.values():
        if isinstance(memory, list):
            count += len(memory)
        
    writer(f"🔍 检索到{count}条记忆，耗时{time.time() - start_time:.2f}秒")
    now = int(time.time())
    memories_text = config.MEMORY_USAGE_PROMPT.format(
        memories_dict=memories_dict,
        current_timestamp=now,
    )
    record_context("final", "memory", memories_text, included_in_prompt=True, query=query)
    return memories_text


@tool("search_knowledge_base")
@timed("knowledge_tool", "knowledge")
async def search_knowledge_base(query: str, runtime: ToolRuntime[ContextSchema]) -> str:
    """检索私有知识库的相关证据。query 为清晰的检索问题；范围由当前会话决定。"""
    context = runtime.context
    writer = get_stream_writer()
    writer("正在检索知识库…")
    client = await get_milvus_client()
    database = await get_postgresql_client()
    result = await RetrievalService(client, database, rerank_documents).retrieve_context(
        query, RetrievalScope(context.user_id, context.knowledge_base_id, context.allowed_scope_ids), context.top_k,
        RetrievalOptions(retrieval_mode=context.retrieval_mode, rerank_enabled=context.rerank_enabled,
                         graph_enabled=context.graph_enabled, evidence_enabled=context.evidence_enabled))
    writer(f"检索完成，返回 {len(result.selected)} 个证据片段")
    return result.context_text()


"""rerank_result的结构示例：
{'output': {'results': [{'document': {'text': '20 世纪80 年代末'}, 'index': 0, 
'relevance_score': 0.886919463597282}]}, 'usage': {'total_tokens': 1224}, 
'request_id': '2252323b-a3ba-4ef5-a203-e305b64249e1'}  
"""
