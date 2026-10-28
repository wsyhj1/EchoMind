"""Allowlisted metrics and non-secret, versioned evaluator configuration."""
import os

METRICS = ("faithfulness", "response_relevancy", "context_precision", "context_recall",
           "factual_correctness", "precision_at_k", "recall_at_k")


def evaluator_config():
    model = os.getenv("EVAL_MODEL", os.getenv("AGENT_BASE_MODEL", "qwen-plus"))
    base_url = os.getenv("EVAL_BASE_URL", os.getenv("BASE_URL", ""))
    return {"ragas_version": "0.4.3", "rubric_version": "echomind-2", "structured_output": "json",
            "qwen_non_thinking": "dashscope" in base_url and model.startswith(("qwen3", "qwen-plus", "qwen-flash")) and "thinking" not in model,
            "model": model,
            "base_url": base_url,
            "embedding_model": os.getenv("EVAL_EMBEDDING_MODEL", os.getenv("EMBEDDING_MODEL", "text-embedding-v4")),
            "timeout_seconds": min(180, max(10, int(os.getenv("EVAL_TIMEOUT_SECONDS", "90"))))}
