import api
import uvicorn
import os
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from milvus_client import get_milvus_client
from postgresql_client import get_postgresql_client
import logging
import asyncio
from langchain_openai import ChatOpenAI
from typing import Set
from auto_store_memory_from_psql import run_compression_task
from redis_cache import get_redis_client, close_redis_client
from evaluation_store import EvaluationStore
from profile_store import ProfileStore
from user_settings import UserSettingsStore
from auth import AuthStore, IdentityMiddleware, allowed_origins, auth_mode
import auth

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s-%(name)s-%(levelname)s-%(message)s'
)
logger = logging.getLogger(__name__)

# 定时任务，每3分钟检查一次是否提取存在psql中的消息
async def check_and_store_memory():
    """检查并提取存在psql中的消息"""
    summarize_model = ChatOpenAI(
        model=os.getenv("SUMMARIZATION_MODEL", "qwen-turbo"),
        openai_api_key=os.getenv("DASHSCOPE_API_KEY"),
        openai_api_base=os.getenv("BASE_URL"),
        temperature=0.6
    )
    while True:
        try:
            await run_compression_task(model=summarize_model)
        except Exception as e:
            logger.error(f"记忆存储任务失败: {e}")
        await asyncio.sleep(180)

# 全局追踪所有后台任务
background_tasks: Set[asyncio.Task] = set()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""    
    logger.info("=" * 20 + "应用启动，开始初始化连接..." + "=" * 20)
    
    # 初始化标志
    milvus_initialized = False
    postgresql_initialized = False
    
    try:    
        try:
            # 初始化 PostgreSQL 客户端
            app.state.postgresql_client = await get_postgresql_client()
            app.state.evaluation_store = EvaluationStore(app.state.postgresql_client.pool)
            await app.state.evaluation_store.initialize()
            from conversation_store import ConversationStore
            app.state.conversation_store = ConversationStore(app.state.postgresql_client.pool)
            await app.state.conversation_store.initialize()
            from assistant.store import AssistantStore
            app.state.assistant_store = AssistantStore(app.state.postgresql_client.pool)
            await app.state.assistant_store.initialize()
            from assistant.reminders import ReminderStore
            app.state.reminder_store = ReminderStore(app.state.postgresql_client.pool)
            await app.state.reminder_store.initialize()
            app.state.profile_store = ProfileStore(app.state.postgresql_client.pool)
            await app.state.profile_store.initialize()
            app.state.user_settings_store = UserSettingsStore(app.state.postgresql_client.pool)
            await app.state.user_settings_store.initialize()
            auth_mode()  # Invalid modes fail closed during startup.
            app.state.auth_store = AuthStore(app.state.postgresql_client.pool)
            await app.state.auth_store.initialize()
            from citation_service import CitationService
            app.state.citation_service = CitationService(app.state.evaluation_store, app.state.postgresql_client.pool)
            await app.state.citation_service.initialize()
            postgresql_initialized = True
            logger.info("【PostgreSQL】 客户端初始化完成")
        except Exception as e:
            logger.error(f"PostgreSQL 客户端初始化失败: {e}")
            raise
        
        try:
            # 初始化 Milvus 客户端
            app.state.milvus_client = await get_milvus_client()
            milvus_initialized = True
            logger.info("【Milvus】 客户端初始化完成")
        except Exception as e:
            logger.error(f"Milvus 客户端初始化失败: {e}")
            raise

        # ===== 新增：预热 Redis 连接 =====
        try:
            app.state.redis_client = await get_redis_client()
            logger.info("【Redis】 客户端初始化完成")
        except Exception as e:
            logger.error(f"Redis 客户端初始化失败: {e}")
            raise

        from graph.repository import GraphRepository, graph_exists
        from rag.settings import env_bool
        # The management page works while automatic extraction is disabled.
        # A worker may execute only explicitly consented jobs or enabled auto jobs.
        await GraphRepository(app.state.postgresql_client.pool).initialize()
        if await graph_exists(app.state.postgresql_client.pool):
            from graph.worker import run_worker
            graph_task = asyncio.create_task(run_worker(app.state.postgresql_client, app.state.milvus_client))
            background_tasks.add(graph_task)
            graph_task.add_done_callback(background_tasks.discard)

        #所有连接成功后创建后台定时任务，每3分钟检查一次是否提取存在psql中的消息存为记忆
        from assistant.reminders import run_reminder_worker
        reminder_task = asyncio.create_task(run_reminder_worker(app.state.reminder_store))
        background_tasks.add(reminder_task)
        reminder_task.add_done_callback(background_tasks.discard)
        task = asyncio.create_task(check_and_store_memory())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        
        
    except Exception as e:
        logger.error(f"初始化失败: {e}")
        raise
    
    yield  # 应用运行期间
    
    # 关闭时清理
    logger.info("=" * 20 + "清理连接..." + "=" * 20)

    
    try:  
        # Stop background writers before closing the connections they use.
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)

        # 关闭 Milvus 连接
        if milvus_initialized and app.state.milvus_client:
            await app.state.milvus_client.close()
            logger.info("✓ Milvus 连接已关闭")
        
        # 关闭 PostgreSQL 连接
        if postgresql_initialized and app.state.postgresql_client:
            await app.state.postgresql_client.close()
            logger.info("✓ PostgreSQL 连接已关闭")

        await close_redis_client()
        logger.info("✓ Redis 连接已关闭")

    except Exception as e:
        logger.error(f"清理连接时出错: {e}")

    logger.info("应用已关闭")


# 创建应用并传入 lifespan
app = FastAPI(title="EchoMind-个性化问答助手", lifespan=lifespan)

# 将项目中定义的所有 API 端点注册到应用中
app.include_router(api.router)
app.include_router(auth.router)
from graph_api import router as graph_router
from citations_api import router as citations_router
app.include_router(graph_router)
app.include_router(citations_router)
from operations_api import router as operations_router
app.include_router(operations_router)
from conversations_api import router as conversations_router
app.include_router(conversations_router)
from assistant_api import router as assistant_router
app.include_router(assistant_router)

# 解决前端跨域访问问题
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(IdentityMiddleware)

@app.get('/health')
async def health():
    return {'status':'ok'}

# 禁用静态资源缓存
@app.middleware("http")
async def _no_cache(request, call_next):
    response = await call_next(request)
    path = request.url.path or ""
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8000)),
        reload=False
    )
