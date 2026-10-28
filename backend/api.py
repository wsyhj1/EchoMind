from documents_process import TempDocumentProcessor, DocumentProcessor
from hash_storage import HashStorage
from fastapi import (
    APIRouter,
    UploadFile,
    File,
    HTTPException,
    BackgroundTasks,
    Request,
    status,
    Depends,
    Query,
)
from typing import List, Dict, Optional
import logging
import sys
import asyncio
import hashlib
import uuid
import aiofiles
import os
from fastapi.responses import StreamingResponse
from agent import stream_agent_response
from observability import RunTrace
from evaluation_config import METRICS, evaluator_config
from pydantic import BaseModel, Field
from typing import Literal
from uuid import UUID
from profile_store import ProfileConflict
from user_settings import SettingsValues, SettingsEdit, SettingsConflict, public_runtime_settings
from auth import require_matching_user
from skill_registry import public_skills, resolve_command, SkillCommandError, registry_metadata
from schemas import (
    DocumentUploadResponse,
    DocumentDeleteResponse,
    CreateKnowledgeBaseResponse,
    DeleteKnowledgeBaseResponse,
    GetKnowledgeBaseResponse,
    GetKnowledgeBaseFilesResponse,
    FileItem,
)


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(name)s-%(levelname)s-%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

router = APIRouter()

# 配置常量
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200mb
CHUNK_SIZE = 1024 * 1024  # 1mb
MAX_CONCURRENT_UPLOADS = 10


# 全局实例
hash_storage = HashStorage()  # 异步版本


upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)


# 定义依赖函数
async def get_postgresql_client(request: Request):
    """获取 PostgreSQL 客户端依赖"""
    return request.app.state.postgresql_client


async def get_milvus_client(request: Request):
    """获取 Milvus 客户端依赖"""
    return request.app.state.milvus_client


@router.post("/document_upload", response_model=DocumentUploadResponse)
async def file_upload(
    request: Request,
    file: UploadFile = File(...),
    knowledge_base_id: str = File(...),
    user_id: int = File(...),  # 新增 user_id 参数
    background_tasks: BackgroundTasks = None,
):
    """上传文档并进行embedding,支持文件去重"""

    user_id = require_matching_user(request, user_id)

    # 初始化变量，避免作用域问题
    temp_file_path = None

    async with upload_semaphore:
        try:
            filename = file.filename
            logger.info(f"文件名: {filename}")
            file_lower = filename.lower()

            # 文件类型验证
            if not file_lower.endswith((".pdf", ".docx", ".doc")):
                raise HTTPException(status_code=400, detail="仅支持PDF和Word文档")

            # 修复：检查文件大小，处理 file.size 为 None 的情况
            if file.size is not None and file.size > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件过大，最大支持 {MAX_FILE_SIZE // (1024*1024)}MB",
                )

            # 检查客户端连接状态
            if await request.is_disconnected():
                raise HTTPException(status_code=400, detail="客户端已断开连接")

            # 创建哈希对象和临时文件处理对象
            sha256 = hashlib.sha256()
            temp_processor = TempDocumentProcessor()

            temp_file_path = temp_processor.temp_dir / f"{uuid.uuid4()}_{filename}"

            # 流式计算哈希，写入文件
            try:
                async with aiofiles.open(temp_file_path, "wb") as temp_file:
                    total_size = 0
                    while True:
                        # 检查客户端连接状态
                        if await request.is_disconnected():
                            raise HTTPException(
                                status_code=400, detail="客户端已断开连接"
                            )

                        chunk = await file.read(CHUNK_SIZE)
                        if not chunk:
                            break

                        total_size += len(chunk)
                        # 实时检查文件大小
                        if total_size > MAX_FILE_SIZE:
                            raise HTTPException(
                                status_code=413,
                                detail=f"文件过大，最大支持 {MAX_FILE_SIZE // (1024*1024)}MB",
                            )

                        sha256.update(chunk)
                        await temp_file.write(chunk)

                    file_hash = sha256.hexdigest()
                    logger.info(
                        f"文件上传完成: {filename}, 哈希: {file_hash[:16]}, 大小: {total_size} bytes"
                    )

            except Exception as e:
                # 异常时清理临时文件
                if temp_file_path and temp_file_path.exists():
                    try:
                        # ✅ 改为异步删除
                        await temp_processor.delete_temp_file(temp_file_path)
                    except:
                        pass
                raise

            # 再次检查客户端连接状态
            if await request.is_disconnected():
                if temp_file_path and temp_file_path.exists():
                    try:
                        # 异步删除
                        await temp_processor.delete_temp_file(temp_file_path)
                    except:
                        pass
                raise HTTPException(status_code=400, detail="客户端已断开连接")

            # 异步调用检查文件是否重复（传入 user_id）
            from graph.repository import pending_delete
            database = await get_postgresql_client(request)
            if await pending_delete(database.pool, user_id, knowledge_base_id):
                await temp_processor.delete_temp_file(temp_file_path)
                raise HTTPException(status_code=409, detail="知识库正在完成删除清理，请稍后重试")
            if await hash_storage.is_file_duplicate(
                file_hash, knowledge_base_id, user_id
            ):
                logger.info(f"文件已存在，跳过处理: {filename}")
                if temp_file_path and temp_file_path.exists():
                    try:
                        # 异步删除
                        await temp_processor.delete_temp_file(temp_file_path)
                    except:
                        pass

                return DocumentUploadResponse(
                    filename=filename,
                    message="文件已存在，无需重复上传",
                    file_hash=file_hash,
                    knowledge_base_id=knowledge_base_id,
                    is_duplicate=True,
                )

            # 确保 background_tasks 不为 None
            if background_tasks is None:
                background_tasks = BackgroundTasks()

            # DocumentProcessor 需要改为异步版本
            document_instance = DocumentProcessor(hash_storage)

            # 将文档处理任务添加到后台（处理完成后会自动清理临时文件，传入 user_id）
            # process_document 需要是异步函数
            background_tasks.add_task(
                document_instance.process_document,
                temp_file_path=temp_file_path,
                filename=filename,
                file_hash=file_hash,
                knowledge_base_id=knowledge_base_id,
                user_id=user_id,  # 新增
            )

            # 立即返回
            return DocumentUploadResponse(
                filename=filename,
                message="文档已上传，正在后台处理中",
                file_hash=file_hash,
                knowledge_base_id=knowledge_base_id,
                is_duplicate=False,
            )

        except HTTPException:
            raise
        except asyncio.CancelledError:
            logger.warning(
                f"请求被取消: {filename if 'filename' in locals() else 'unknown'}"
            )
            raise HTTPException(status_code=499, detail="客户端已取消请求")
        except Exception as e:
            logger.error(f"文件上传错误: {str(e)}", exc_info=True)
            # 清理临时文件
            if temp_file_path and temp_file_path.exists():
                try:
                    await temp_processor.delete_temp_file(temp_file_path)
                except:
                    pass
            raise HTTPException(status_code=500, detail=f"文件处理失败: {str(e)}")


@router.post("/knowledge-bases")
async def create_knowledge_base(
    knowledge_base_id: str,
    user_id: int,
    postgresql_client=Depends(get_postgresql_client),
):
    """创建知识库"""
    result = await postgresql_client.create_knowledge_base(knowledge_base_id, user_id)

    if not result["success"]:
        if "已存在" in result["message"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=result["message"]
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=result["message"]
            )

    return CreateKnowledgeBaseResponse(
        knowledge_base_id=knowledge_base_id,
        status="success",
        message=result["message"],
    )


@router.delete(
    "/knowledge-bases/{knowledge_base_id}", response_model=DeleteKnowledgeBaseResponse
)
async def delete_knowledge_base(
    knowledge_base_id: str,
    user_id: int,
    postgresql_client=Depends(get_postgresql_client),
    milvus_client=Depends(get_milvus_client),
):
    """删除知识库（先删 PostgreSQL，成功后再删 Milvus）"""
    try:
        # 1. 删除 PostgreSQL 中的知识库
        pg_result = await postgresql_client.delete_knowledge_base(
            knowledge_base_id, user_id
        )

        # 检查 PostgreSQL 删除是否成功
        if not pg_result.get("success", False):
            logger.error(
                f"PostgreSQL删除知识库失败: {pg_result.get('message', '未知错误')}"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=pg_result.get("message", "知识库不存在或无权删除"),
            )

        # 2. PostgreSQL 删除成功，继续删除 Milvus 中的子块
        try:
            child_chunks_deleted = await milvus_client.delete_knowledge_file_chunks(
                knowledge_base_id=knowledge_base_id,
                user_id=user_id,
            )
            logger.info(f"Milvus 删除成功，删除了 {child_chunks_deleted} 个子块")
            from graph.repository import acknowledge_delete
            await acknowledge_delete(postgresql_client.pool, user_id, knowledge_base_id)
        except Exception as e:
            # Milvus 删除失败，但 PostgreSQL 已删除成功
            logger.error(
                f"Milvus 删除失败（PostgreSQL 已删除知识库 {knowledge_base_id}）: {e}"
            )
            # 可以选择是否抛出异常，这里选择抛出异常让用户知道部分失败
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"PostgreSQL 删除成功，但 Milvus 删除失败: {str(e)}",
            )

        # 3. 全部删除成功
        return DeleteKnowledgeBaseResponse(
            knowledge_base_id=knowledge_base_id,
            status="success",
            message=f"知识库删除成功",
            child_chunks_deleted=child_chunks_deleted,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除知识库失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除知识库失败: {str(e)}",
        )


@router.get("/knowledge-bases")
async def get_user_knowledge_bases(
    user_id: int,
    postgresql_client=Depends(get_postgresql_client),
):
    """获取用户的所有知识库"""
    result = await postgresql_client.get_user_knowledge_bases(user_id)
    print("查询用户知识库结果:", result)

    if not result["success"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=result["message"]
        )

    return GetKnowledgeBaseResponse(
        status="success",
        message=result["message"],
        knowledge_bases=result["knowledge_bases"],
        count=result["count"],
    )


@router.get(
    "/knowledge-bases/{knowledge_base_id}/files",
    response_model=GetKnowledgeBaseFilesResponse,
)
async def get_knowledge_base_files(
    knowledge_base_id: str,
    user_id: int,
    postgresql_client=Depends(get_postgresql_client),
):
    """获取知识库中的所有文件"""
    result = await postgresql_client.get_knowledge_base_files(
        knowledge_base_id, user_id
    )

    if not result["success"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=result["message"]
        )

    return GetKnowledgeBaseFilesResponse(
        status="success",
        message=result["message"],
        count=result["count"],
        files=[
            FileItem(
                file_hash=file["file_hash"],
                file_name=file["file_name"],
                uploaded_at=file["uploaded_at"],
            )
            for file in result["files"]
        ],
    )


@router.delete(
    "/knowledge-bases/{knowledge_base_id}/documents/{file_hash}",
    response_model=DocumentDeleteResponse,
)
async def delete_file(
    file_hash: str,
    knowledge_base_id: str,
    user_id: int,
    postgresql_client=Depends(get_postgresql_client),
    milvus_client=Depends(get_milvus_client),
):
    """删除文件-通过文件哈希删除（先删 PostgreSQL，成功后再删 Milvus）"""
    deleted_parent_count = 0
    deleted_child_count = 0

    try:
        # 1. 删除 PostgreSQL 中的父块
        pg_result = await postgresql_client.delete_file(
            knowledge_base_id=knowledge_base_id,
            file_hash=file_hash,
            user_id=user_id,
        )

        # 检查 PostgreSQL 删除是否成功
        if not pg_result.get("success", False):
            # PostgreSQL 删除失败，直接返回错误
            logger.error(f"PostgreSQL删除失败: {pg_result.get('message', '未知错误')}")
            raise HTTPException(
                status_code=400,
                detail=f"PostgreSQL删除失败: {pg_result.get('message', '未知错误')}",
            )

        # 获取删除的父块数量（用于返回信息）
        deleted_parent_count = pg_result.get("parent_chunks_deleted", 0)

        # 2. PostgreSQL 删除成功，继续删除 Milvus 中的子块
        try:
            deleted_child_count = await milvus_client.delete_file_chunks(
                knowledge_base_id=knowledge_base_id,
                file_hash=file_hash,
                user_id=user_id,
            )
            logger.info(f"Milvus 删除成功，删除了 {deleted_child_count} 个子块")
            from graph.repository import acknowledge_delete
            await acknowledge_delete(postgresql_client.pool, user_id, knowledge_base_id, file_hash)
        except Exception as e:
            # Milvus 删除失败，但 PostgreSQL 已删除成功
            # 记录错误但返回部分成功的信息
            logger.error(f"Milvus 删除失败（PostgreSQL 已删除成功）: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"PostgreSQL 删除成功，但 Milvus 删除失败: {str(e)}",
            )

        # 3. 全部删除成功
        return DocumentDeleteResponse(
            message=f"成功删除 {deleted_parent_count} 个父块和 {deleted_child_count} 个子块",
            knowledge_base_id=knowledge_base_id,
            status="success",
            parent_chunks_deleted=deleted_parent_count,
            child_chunks_deleted=deleted_child_count,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除失败: {e}")
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")


class ProfileFields(BaseModel):
    model_config = {"extra": "forbid"}
    background: str = Field(default="", max_length=2000)
    goals: str = Field(default="", max_length=2000)
    interests: str = Field(default="", max_length=2000)
    preferences: str = Field(default="", max_length=2000)


class ProfileEdit(BaseModel):
    model_config = {"extra": "forbid"}
    fields: ProfileFields
    expected_revision: int = Field(ge=1)


class ProfileSettings(BaseModel):
    model_config = {"extra": "forbid"}
    expected_revision: int = Field(ge=1)
    personalization_enabled: Optional[bool] = None
    auto_update_enabled: Optional[bool] = None


async def profile_operation(operation):
    try:
        return await operation
    except LookupError as error:
        raise HTTPException(404, str(error))
    except ProfileConflict as error:
        raise HTTPException(409, str(error))


@router.get("/user-profile")
async def get_user_profile(request: Request, user_id: int = Query(ge=1)):
    return await profile_operation(request.app.state.profile_store.get(user_id))


@router.put("/user-profile")
async def edit_user_profile(payload: ProfileEdit, request: Request, user_id: int = Query(ge=1)):
    return await profile_operation(request.app.state.profile_store.save_fields(
        user_id, payload.fields.model_dump(), payload.expected_revision))


@router.patch("/user-profile/settings")
async def set_profile_settings(payload: ProfileSettings, request: Request, user_id: int = Query(ge=1)):
    if payload.personalization_enabled is None and payload.auto_update_enabled is None:
        raise HTTPException(422, "请选择要修改的设置")
    return await profile_operation(request.app.state.profile_store.settings(
        user_id, payload.expected_revision, personalization_enabled=payload.personalization_enabled,
        auto_update_enabled=payload.auto_update_enabled))


@router.delete("/user-profile")
async def clear_user_profile(request: Request, user_id: int = Query(ge=1), expected_revision: int = Query(ge=1)):
    return await profile_operation(request.app.state.profile_store.clear(user_id, expected_revision))


@router.get('/skills')
async def list_registered_skills():
    """Public catalog metadata; protected by the application's identity middleware."""
    return {'skills': public_skills(), 'version': registry_metadata()['version']}


@router.get("/chat_with_agent/stream")
async def chat_with_agent(
    knowledge_base_id: str,
    request: Request,
    query: str = Query(min_length=1,max_length=8000),
    user_id: int = Query(ge=1),
    background_tasks: BackgroundTasks = None,
    session_id: str = Query(default="legacy", pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    top_k: Optional[int] = Query(default=None, ge=1, le=20),
    memory_enabled: Optional[bool] = None,
    rerank_enabled: Optional[bool] = None,
    retrieval_mode: Optional[Literal["hybrid", "dense"]] = None,
    graph_enabled: Optional[bool] = None,
    evidence_enabled: Optional[bool] = None,
    evaluation_mode: bool = False,
): 
    """
    流式返回 agent 响应
    """
    try:
        command = resolve_command(query)
    except SkillCommandError as error:
        raise HTTPException(422, str(error)) from None
    if command is not None and command.kind == 'local':
        raise HTTPException(422, '这是界面本地命令，请在 EchoMind 输入框中执行。')
    conversations = getattr(request.app.state, 'conversation_store', None)
    if conversations is not None:
        try:
            if await conversations.by_session(user_id, session_id):
                raise HTTPException(409, '请使用会话发送入口，避免绕过范围绑定和并发保护。')
        except LookupError:
            raise HTTPException(404, '会话不存在或不可访问。') from None
    # Evaluation keeps reproducible defaults; explicit request options always win.
    saved = None if evaluation_mode else await settings_operation(request.app.state.user_settings_store.get(user_id))
    values = SettingsValues.model_validate(saved["values"] if saved else {}).model_dump()
    values.update({key: value for key, value in {
        "top_k": top_k, "memory_enabled": memory_enabled, "rerank_enabled": rerank_enabled,
        "retrieval_mode": retrieval_mode, "graph_enabled": graph_enabled,
        "evidence_enabled": evidence_enabled}.items() if value is not None})
    trace = RunTrace(user_id, session_id, query, knowledge_base_id)
    trace.config["settings_revision"] = saved["revision"] if saved else None
    return StreamingResponse(
        stream_agent_response(
            user_message=command.argument if command is not None else query,
            knowledge_base_id=knowledge_base_id,
            user_id=user_id,
            background_tasks=background_tasks,
            trace=trace,
            store=request.app.state.evaluation_store,
            session_id=session_id, top_k=values["top_k"],
            memory_enabled=values["memory_enabled"] and not evaluation_mode,
            rerank_enabled=values["rerank_enabled"], retrieval_mode=values["retrieval_mode"],
            graph_enabled=values["graph_enabled"],
            evidence_enabled=values["evidence_enabled"],
            evaluation_mode=evaluation_mode,
            skill_id=command.skill_id if command is not None else None,
            skill_version=command.version if command is not None else None,
        ),
        media_type="text/plain; charset=utf-8",
    )


async def settings_operation(operation):
    try:
        return await operation
    except LookupError as error:
        raise HTTPException(404, str(error))
    except SettingsConflict as error:
        raise HTTPException(409, str(error))


@router.get("/user-settings")
async def get_user_settings(request: Request, user_id: int = Query(ge=1)):
    saved = await settings_operation(request.app.state.user_settings_store.get(user_id))
    return {**saved, "defaults": SettingsValues().model_dump(), "runtime": public_runtime_settings()}


@router.put("/user-settings")
async def save_user_settings(payload: SettingsEdit, request: Request, user_id: int = Query(ge=1)):
    saved = await settings_operation(request.app.state.user_settings_store.save(
        user_id, payload.values, payload.expected_revision))
    return {**saved, "defaults": SettingsValues().model_dump(), "runtime": public_runtime_settings()}


class EvaluationRequest(BaseModel):
    metrics: List[Literal["faithfulness", "response_relevancy", "context_precision", "context_recall",
                          "factual_correctness", "precision_at_k", "recall_at_k"]] = Field(
                              default_factory=lambda: list(METRICS), min_length=1, max_length=7)
    reference: Optional[str] = Field(default=None, max_length=30000)
    reference_context_ids: Optional[List[str]] = Field(default=None, max_length=200)
    requires_retrieval: Optional[bool] = None


@router.get("/runs")
async def list_chat_runs(request: Request, user_id: int = Query(ge=1),
                         session_id: str = Query(pattern=r"^[A-Za-z0-9_-]{1,64}$"),
                         limit: int = Query(default=50, ge=1, le=100)):
    return await request.app.state.evaluation_store.list_runs(user_id, session_id, limit)


@router.get("/runs/{run_id}")
async def get_chat_run(run_id: UUID, request: Request, user_id: int = Query(ge=1)):
    run = await request.app.state.evaluation_store.get_run(run_id, user_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


@router.delete("/runs/{run_id}")
async def delete_chat_run(run_id: UUID, request: Request, user_id: int = Query(ge=1)):
    conversations = getattr(request.app.state, 'conversation_store', None)
    if conversations is not None and await conversations.pool.fetchval('''SELECT EXISTS(
        SELECT 1 FROM chat_turns t JOIN chat_conversations c USING(conversation_id)
        WHERE t.run_id=$1 AND c.user_id=$2)''', run_id, user_id):
        raise HTTPException(409, '此运行关联会话消息，请使用会话归档保留完整历史。')
    result = await request.app.state.evaluation_store.pool.execute(
        "DELETE FROM evaluation_runs WHERE run_id=$1 AND user_id=$2", run_id, user_id)
    if result == "DELETE 0":
        raise HTTPException(404, "Run not found")
    return {"status": "deleted"}


@router.post("/runs/{run_id}/evaluations", status_code=202)
async def evaluate_chat_run(run_id: UUID, payload: EvaluationRequest, request: Request,
                            user_id: int = Query(ge=1)):
    data = payload.model_dump()
    data["metrics"] = sorted(set(data["metrics"]))
    data["reference"] = data["reference"].strip() if data["reference"] else None
    if data["reference_context_ids"] is not None:
        data["reference_context_ids"] = sorted(set(data["reference_context_ids"]))
    data["config"] = evaluator_config()
    try:
        return await request.app.state.evaluation_store.create_job(run_id, user_id, data)
    except LookupError:
        raise HTTPException(404, "Run not found")
    except ValueError as error:
        raise HTTPException(409, str(error))


@router.get("/evaluations/{job_id}")
async def get_evaluation(job_id: UUID, request: Request, user_id: int = Query(ge=1)):
    job = await request.app.state.evaluation_store.get_job(job_id, user_id)
    if job is None:
        raise HTTPException(404, "Evaluation not found")
    return job


@router.post("/evaluations/{job_id}/retry", status_code=202)
async def retry_evaluation(job_id: UUID, request: Request, user_id: int = Query(ge=1)):
    try:
        return await request.app.state.evaluation_store.retry_job(job_id, user_id)
    except LookupError:
        raise HTTPException(404, "Evaluation not found")
    except ValueError as error:
        raise HTTPException(409, str(error))
