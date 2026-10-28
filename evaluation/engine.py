"""RAGAS 0.4.3 adapter. Planning and ID metrics do not import RAGAS or call APIs."""
import asyncio
import json
import math
import os
import re
from time import perf_counter


def clean_error(error):
    return re.sub(r"sk-[A-Za-z0-9_-]+|Bearer\s+\S+", "<REDACTED>", str(error))[:1000]


def _tool_evidence(messages):
    """Decode structured tool evidence, retaining plain legacy outputs separately.

    Matching JSON serialization as raw text drops PDF paragraphs containing
    newlines or quotation marks. Decode only top-level evidence records; values
    in unrelated JSON metadata must never become quoted document evidence.
    """
    plain, evidence = [], []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "tool" or not isinstance(content, str):
            continue
        structured = False
        # JSON strings may legally contain U+2028/U+2029/U+0085. They are
        # document characters, not the ASCII newline separating our payload.
        for line in content.split("\n"):
            if not line.lstrip().startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            structured = True
            records = payload.get("evidence")
            if isinstance(records, list):
                evidence.extend(item for item in records if isinstance(item, dict)
                                and isinstance(item.get("text"), str))
        if not structured:
            plain.append(content)
    return plain, evidence


def _context_visible(context, plain, evidence):
    text = context.get("text")
    if not isinstance(text, str) or not text:
        return False
    if any(text in content for content in plain):
        return True
    return any(item["text"] == text
               and (not context.get("file_name") or item.get("file") == context["file_name"])
               and (not context.get("knowledge_base_id") or item.get("knowledge_base") == context["knowledge_base_id"])
               for item in evidence)


def final_knowledge(run):
    contexts = [c for c in run.get("contexts", []) if c.get("source_type") == "knowledge"
                and c.get("stage") == "final" and c.get("included_in_prompt")]
    if run.get("model_inputs"):
        messages = run["model_inputs"][-1].get("messages", [])
        plain, evidence = _tool_evidence(messages)
        contexts = [c for c in contexts if _context_visible(c, plain, evidence)]
    return contexts


def supporting_contexts(run):
    selected = [c for c in run.get("contexts", []) if c.get("included_in_prompt") and c.get("text")]
    contexts = [c["text"] for c in selected]
    inputs = run.get("model_inputs", [])
    if inputs:
        messages = inputs[-1].get("messages", [])
        plain, evidence = _tool_evidence(messages)
        non_tool = [m["content"] for m in messages
                    if m.get("role") != "tool" and isinstance(m.get("content"), str)]
        contexts = [c["text"] for c in selected if _context_visible(c, plain, evidence)
                    or any(c["text"] in text for text in non_tool)]
        # Previous user facts and actual ToolMessages may support memory-aware answers.
        last_human = max((i for i, m in enumerate(messages) if m.get("role") == "human"), default=-1)
        for i, message in enumerate(messages):
            if message.get("role") == "tool" or (message.get("role") == "human" and i < last_human):
                content = message.get("content")
                if isinstance(content, str) and content:
                    contexts.append(content)
    return list(dict.fromkeys(contexts))


def metric_plan(run, payload, metric):
    """Return (ready kwargs, immediate result). Missing truth is never a zero score."""
    knowledge = final_knowledge(run)
    contexts = [c["text"] for c in knowledge]
    reference = payload.get("reference")
    required = payload.get("requires_retrieval") is True
    common = {"user_input": run["query"], "response": run.get("answer", "")}

    def na(reason):
        return None, {"status": "not_applicable", "value": None, "reason": reason}

    if metric in ("precision_at_k", "recall_at_k"):
        truth = payload.get("reference_context_ids")
        if not truth:
            return na("缺少非空的相关证据 ID 标注")
        k = int(run.get("config", {}).get("top_k", 5))
        returned = list(dict.fromkeys(c.get("source_id") for c in knowledge if c.get("source_id")))[:k]
        hit = len(set(returned) & set(truth))
        denominator = k if metric == "precision_at_k" else len(set(truth))
        return None, {"status": "completed", "value": hit / denominator, "method": "id_at_k",
                      "details": {"hits": hit, "k": k, "returned": len(returned), "relevant": len(set(truth)),
                                  "routing_failure": required and not knowledge}}
    if metric == "faithfulness":
        evidence = supporting_contexts(run)
        if not evidence:
            return na("没有可用于忠实度判断的实际证据；闲聊不计零分")
        return {**common, "retrieved_contexts": evidence}, None
    if metric == "response_relevancy":
        return common, None
    if metric == "factual_correctness":
        if not reference:
            return na("缺少标准答案 reference")
        return {"response": common["response"], "reference": reference}, None
    if metric in ("context_precision", "context_recall"):
        if metric == "context_recall" and not reference:
            return na("缺少标准答案 reference，不能计算上下文召回")
        if not contexts:
            if required:
                return None, {"status": "completed", "value": 0.0, "method": "empty_retrieval_policy",
                              "reason": "标注要求检索，但本轮没有知识库证据", "routing_failure": True}
            return na("本轮未检索知识库证据")
        if reference:
            return {"user_input": run["query"], "retrieved_contexts": contexts, "reference": reference}, None
        return {**common, "retrieved_contexts": contexts}, None
    raise ValueError("Unsupported metric: " + metric)


class RagasEvaluator:
    def __init__(self, config, api_key, transport=None):
        self.config = config
        self.api_key = api_key
        self.metrics = {}
        self.client = None
        self.http = None
        self.transport = transport
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "embedding_tokens": 0}

    async def _initialize(self):
        if self.client is not None and self.metrics:
            return
        if self.client is not None:
            await self.client.close()
        os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
        import httpx
        from openai import AsyncOpenAI
        from ragas.llms import llm_factory
        from ragas.embeddings import OpenAIEmbeddings
        from ragas.metrics.collections import (Faithfulness, AnswerRelevancy, ContextPrecisionWithReference,
                                              ContextPrecisionWithoutReference, ContextRecall, FactualCorrectness)

        async def before_request(request):
            if self.usage["requests"] >= 50:
                raise RuntimeError("评测超过单任务 50 次 API 调用上限")
            self.usage["requests"] += 1

        async def after_response(response):
            await response.aread()
            try:
                usage = response.json().get("usage", {})
                if response.request.url.path.endswith("embeddings"):
                    self.usage["embedding_tokens"] += usage.get("total_tokens", 0)
                else:
                    self.usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
                    self.usage["completion_tokens"] += usage.get("completion_tokens", 0)
            except (ValueError, AttributeError):
                pass

        self.http = httpx.AsyncClient(timeout=45, transport=self.transport,
                                      event_hooks={"request": [before_request], "response": [after_response]})
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.config["base_url"],
                                  http_client=self.http, max_retries=1)
        # 0.4.3 selects Instructor JSON mode internally; mode= would leak to the API.
        options = {"temperature": 0, "max_retries": 2}
        if ("dashscope" in self.config["base_url"] and self.config["model"].startswith(("qwen3", "qwen-plus", "qwen-flash"))
                and "thinking" not in self.config["model"]):
            # Qwen hybrid models need non-thinking mode for non-streaming JSON judging.
            options["extra_body"] = {"enable_thinking": False}
        llm = llm_factory(self.config["model"], client=self.client, **options)
        embeddings = OpenAIEmbeddings(model=self.config["embedding_model"], client=self.client)
        self.metrics = {
            "faithfulness": Faithfulness(llm=llm),
            "response_relevancy": AnswerRelevancy(llm=llm, embeddings=embeddings),
            "context_precision": ContextPrecisionWithReference(llm=llm),
            "context_precision_proxy": ContextPrecisionWithoutReference(llm=llm),
            "context_recall": ContextRecall(llm=llm),
            "factual_correctness": FactualCorrectness(llm=llm, mode="f1"),
        }

    async def evaluate(self, run, payload):
        started = perf_counter()
        scores = {}
        try:
            for metric in payload["metrics"]:
                kwargs, immediate = metric_plan(run, payload, metric)
                if immediate is not None:
                    scores[metric] = immediate
                    continue
                metric_start = perf_counter()
                try:
                    if sum(len(str(value)) for value in kwargs.values()) > 60000:
                        raise ValueError("评测样本超过 60000 字符上限，未截断证据或发送此指标请求")
                    await self._initialize()
                    metric_id = "context_precision_proxy" if metric == "context_precision" and not payload.get("reference") else metric
                    result = await asyncio.wait_for(self.metrics[metric_id].ascore(**kwargs),
                                                     timeout=self.config.get("timeout_seconds", 90))
                    value = float(result.value)
                    if not math.isfinite(value):
                        raise ValueError("裁判返回了非有限分数")
                    scores[metric] = {"status": "completed", "value": value, "metric_id": metric_id,
                                      "method": "ragas", "proxy": metric_id.endswith("proxy"),
                                      "duration_ms": round((perf_counter() - metric_start) * 1000, 2)}
                except Exception as error:
                    scores[metric] = {"status": "failed", "value": None, "error": clean_error(error),
                                      "duration_ms": round((perf_counter() - metric_start) * 1000, 2)}
        finally:
            if self.client is not None:
                await self.client.close()
                self.client = None
                self.metrics = {}
        return {"scores": scores, "config": self.config, "usage": self.usage,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
                "routing_failure": payload.get("requires_retrieval") is True and not final_knowledge(run),
                "context_count": len(final_knowledge(run))}
