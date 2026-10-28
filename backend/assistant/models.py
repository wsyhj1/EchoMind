"""The same validated actions are used by forms, commands, and agent proposals."""
from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class CreateProject(StrictModel):
    client_create_id: UUID
    title: str = Field(min_length=1, max_length=120)
    goal: str = Field(default='', max_length=3000)
    scope_id: str = Field(default='默认知识库', min_length=1, max_length=128)


class TaskDraft(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=3000)
    category: Literal['work', 'study'] = 'work'
    priority: Literal['high', 'normal', 'low'] = 'normal'
    estimated_minutes: int | None = Field(default=None, ge=5, le=1440, strict=True)
    due_date: date | None = None


class AddTask(TaskDraft):
    kind: Literal['add_task']


class AddTasks(StrictModel):
    kind: Literal['add_tasks']
    tasks: list[TaskDraft] = Field(min_length=1,max_length=10)


class UpdateTask(StrictModel):
    kind: Literal['update_task']
    task_id: UUID
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=3000)
    status: Literal['todo', 'in_progress', 'blocked', 'done', 'cancelled'] | None = None
    category: Literal['work', 'study'] | None = None
    priority: Literal['high', 'normal', 'low'] | None = None
    estimated_minutes: int | None = Field(default=None, ge=5, le=1440, strict=True)
    due_date: date | None = None
    completion_note: str | None = Field(default=None, max_length=3000)
    blocker: str | None = Field(default=None, max_length=3000)

    @model_validator(mode='after')
    def check_patch(self):
        changes = self.model_fields_set - {'kind', 'task_id'}
        if not changes:
            raise ValueError('请提供要修改的任务字段。')
        for field in changes - {'estimated_minutes', 'due_date'}:
            if getattr(self, field) is None:
                raise ValueError(f'{field}不能为null。')
        if self.status == 'done' and not self.completion_note:
            raise ValueError('完成任务时请填写完成依据；这将记录为你的完成报告。')
        if self.status == 'blocked' and not self.blocker:
            raise ValueError('请填写阻碍原因。')
        return self


class AddNote(StrictModel):
    kind: Literal['add_note']
    note_kind: Literal['progress', 'decision', 'blocker', 'learning'] = 'progress'
    content: str = Field(min_length=1, max_length=4000)
    supersedes_note_id: UUID | None = None


class AddResource(StrictModel):
    kind: Literal['add_resource']
    resource_type: Literal['document', 'link']
    label: str = Field(min_length=1, max_length=200)
    reference: str = Field(min_length=1, max_length=2000)

    @model_validator(mode='after')
    def valid_reference(self):
        if self.resource_type == 'document':
            import re
            if not re.fullmatch(r'[a-fA-F0-9]{64}', self.reference):
                raise ValueError('文档引用必须是已有文档的SHA256。')
        else:
            parsed = urlsplit(self.reference)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('资料链接须为不含账号密码的HTTP(S)地址。')
            if any(ord(char) < 32 or char in '<>"' for char in self.reference):
                raise ValueError('链接包含非法字符。')
        return self


class EditProject(StrictModel):
    kind: Literal['edit_project']
    title: str | None = Field(default=None, min_length=1, max_length=120)
    goal: str | None = Field(default=None, max_length=3000)
    archived: bool | None = None

    @model_validator(mode='after')
    def check_patch(self):
        changes = self.model_fields_set - {'kind'}
        if not changes or any(getattr(self, key) is None for key in changes):
            raise ValueError('请提供有效项目修改。')
        return self


Action = Annotated[AddTask | AddTasks | UpdateTask | AddNote | AddResource | EditProject, Field(discriminator='kind')]
ACTION = TypeAdapter(Action)


class ApplyAction(StrictModel):
    operation_id: UUID
    expected_revision: int = Field(ge=1, strict=True)
    action: Action


class CommandRequest(StrictModel):
    operation_id: UUID
    expected_revision: int = Field(ge=1, strict=True)
    text: str = Field(min_length=1, max_length=5000)


class ProposalDecision(StrictModel):
    operation_id: UUID
    expected_revision: int = Field(ge=1, strict=True)


def action_data(action):
    """Keep explicit nulls only for fields that support clearing."""
    return action.model_dump(mode='json', exclude_unset=True)
