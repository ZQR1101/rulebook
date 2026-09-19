import math
import os
import re
from typing import Any

from backend.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    SUPPORTED_MODELS,
    get_config,
    get_model_api_settings,
)
from backend.history_utils import context_prompt, history_prompt

_default_llm = None
DEFAULT_INPUT_PRICE_USD_PER_1M = 0.20
DEFAULT_OUTPUT_PRICE_USD_PER_1M = 0.80
EXPLAIN_TASK = (
    "请用简单易懂的中文解释用户输入。"
    "如果用户明确要求内容结构、篇幅或详细程度，优先遵循用户要求；"
    "否则控制在 300 字以内，只包含定义、3 个核心要点和 1 个简短例子，避免重复展开。"
)


def normalize_model(model: str | None) -> str:
    if model in SUPPORTED_MODELS:
        return model
    return DEFAULT_MODEL


def build_llm(
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2000,
):
    from langchain_openai import ChatOpenAI

    selected_model = normalize_model(model or get_config().model)
    base_url, api_key, _api_key_source = get_model_api_settings(selected_model)

    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url or DEFAULT_BASE_URL,
        model=selected_model,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _model_env_key(model: str, kind: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "_", model).strip("_").upper()
    return f"TOKEN_PRICE_{normalized}_{kind}_USD_PER_1M"


def _read_float_env(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None

    try:
        return float(value)
    except ValueError:
        return None


def get_token_pricing(model: str | None) -> dict:
    selected_model = normalize_model(model)
    model_input_price = _read_float_env(_model_env_key(selected_model, "INPUT"))
    model_output_price = _read_float_env(_model_env_key(selected_model, "OUTPUT"))
    input_price = model_input_price if model_input_price is not None else _read_float_env("TOKEN_PRICE_INPUT_USD_PER_1M")
    output_price = model_output_price if model_output_price is not None else _read_float_env("TOKEN_PRICE_OUTPUT_USD_PER_1M")
    source = "env" if input_price is not None or output_price is not None else "default_estimate"

    return {
        "currency": "USD",
        "input_price_per_1m": input_price if input_price is not None else DEFAULT_INPUT_PRICE_USD_PER_1M,
        "output_price_per_1m": output_price if output_price is not None else DEFAULT_OUTPUT_PRICE_USD_PER_1M,
        "source": source,
    }


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)

    return str(content or "")


def _prompt_to_text(prompt: Any) -> str:
    if isinstance(prompt, list):
        return "\n".join(_content_to_text(getattr(item, "content", item)) for item in prompt)

    return _content_to_text(getattr(prompt, "content", prompt))


def estimate_text_tokens(text: Any) -> int:
    content = _content_to_text(text).strip()
    if not content:
        return 0

    return max(1, math.ceil(len(content) / 4))


def _usage_value(usage: dict | None, *keys: str) -> int | None:
    if not usage:
        return None

    for key in keys:
        value = usage.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue

    return None


def _response_usage(response) -> dict | None:
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict) and usage:
        return usage

    response_metadata = getattr(response, "response_metadata", None)
    if isinstance(response_metadata, dict):
        usage = response_metadata.get("token_usage") or response_metadata.get("usage")
        if isinstance(usage, dict) and usage:
            return usage

    additional_kwargs = getattr(response, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict):
        usage = additional_kwargs.get("usage")
        if isinstance(usage, dict) and usage:
            return usage

    return None


def estimate_token_cost(input_tokens: int, output_tokens: int, model: str | None) -> dict:
    pricing = get_token_pricing(model)
    input_cost = input_tokens * pricing["input_price_per_1m"] / 1_000_000
    output_cost = output_tokens * pricing["output_price_per_1m"] / 1_000_000

    return {
        "currency": pricing["currency"],
        "input": round(input_cost, 8),
        "output": round(output_cost, 8),
        "total": round(input_cost + output_cost, 8),
        "input_price_per_1m": pricing["input_price_per_1m"],
        "output_price_per_1m": pricing["output_price_per_1m"],
        "source": pricing["source"],
    }


def build_usage_record(response, prompt: Any, model: str | None) -> dict:
    usage = _response_usage(response)
    prompt_text = _prompt_to_text(prompt)
    response_text = _content_to_text(getattr(response, "content", ""))

    input_tokens = _usage_value(usage, "input_tokens", "prompt_tokens")
    output_tokens = _usage_value(usage, "output_tokens", "completion_tokens")
    total_tokens = _usage_value(usage, "total_tokens")
    token_source = "api" if usage else "estimated"

    if input_tokens is None and total_tokens is not None and output_tokens is not None:
        input_tokens = max(total_tokens - output_tokens, 0)
    if output_tokens is None and total_tokens is not None and input_tokens is not None:
        output_tokens = max(total_tokens - input_tokens, 0)

    if input_tokens is None:
        input_tokens = estimate_text_tokens(prompt_text)
        token_source = "estimated" if token_source != "api" else "mixed"
    if output_tokens is None:
        output_tokens = estimate_text_tokens(response_text)
        token_source = "estimated" if token_source != "api" else "mixed"
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens

    return {
        "model": normalize_model(model),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "token_source": token_source,
        "estimated_cost": estimate_token_cost(input_tokens, output_tokens, model),
    }


def summarize_usage_records(records: list[dict]) -> dict:
    if not records:
        return {}

    input_tokens = sum(int(record.get("input_tokens", 0)) for record in records)
    output_tokens = sum(int(record.get("output_tokens", 0)) for record in records)
    total_tokens = sum(int(record.get("total_tokens", 0)) for record in records)
    cost = {
        "currency": "USD",
        "input": round(sum(float(record.get("estimated_cost", {}).get("input", 0)) for record in records), 8),
        "output": round(sum(float(record.get("estimated_cost", {}).get("output", 0)) for record in records), 8),
        "total": round(sum(float(record.get("estimated_cost", {}).get("total", 0)) for record in records), 8),
        "source": "mixed" if len({record.get("estimated_cost", {}).get("source") for record in records}) > 1 else records[0].get("estimated_cost", {}).get("source", "default_estimate"),
    }
    token_sources = {record.get("token_source") for record in records}

    return {
        "token_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "source": token_sources.pop() if len(token_sources) == 1 else "mixed",
        },
        "estimated_cost": cost,
        "llm_calls": records,
    }


class UsageTrackingLLM:
    def __init__(self, wrapped_llm, model: str | None = None):
        self._wrapped_llm = wrapped_llm
        self.model = normalize_model(model)
        self.usage_records: list[dict] = []

    def invoke(self, *args, **kwargs):
        prompt = args[0] if args else kwargs.get("input") or kwargs.get("messages") or ""
        response = self._wrapped_llm.invoke(*args, **kwargs)
        self.usage_records.append(build_usage_record(response, prompt, self.model))
        return response

    def __getattr__(self, name):
        return getattr(self._wrapped_llm, name)


def track_llm_usage(llm_instance, model: str | None = None):
    if hasattr(llm_instance, "usage_records"):
        return llm_instance

    return UsageTrackingLLM(llm_instance, model)


def get_llm_usage_records(llm_instance) -> list[dict]:
    return list(getattr(llm_instance, "usage_records", []))


def get_llm_usage_record_count(llm_instance) -> int:
    return len(getattr(llm_instance, "usage_records", []))


def summarize_llm_usage_since(llm_instance, start_index: int) -> dict:
    return summarize_usage_records(get_llm_usage_records(llm_instance)[start_index:])


def attach_usage_to_runtime_info(runtime_info: dict | None, llm_instance) -> dict:
    usage_summary = summarize_usage_records(get_llm_usage_records(llm_instance))
    if not usage_summary:
        return runtime_info or {}

    return {
        **(runtime_info or {}),
        **usage_summary,
    }


def get_default_llm():
    global _default_llm

    if _default_llm is None:
        _default_llm = build_llm()

    return _default_llm


class LazyLLM:
    def invoke(self, *args, **kwargs):
        return get_default_llm().invoke(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(get_default_llm(), name)


llm = LazyLLM()


def chat(
    text: str,
    context=None,
    custom_llm=None,
    history_context: str | None = None,
    memory_context: str | None = None,
) -> str:
    active_llm = custom_llm or llm
    prompt = text

    if context:
        prompt = context_prompt("回答用户问题", text, context, history_context, memory_context)
    elif history_context or memory_context:
        prompt = history_prompt(
            "回答用户当前问题",
            text,
            history_context or "",
            memory_context,
        )
    else:
        prompt = text

    response = active_llm.invoke(prompt)
    return response.content


def explain(
    text: str,
    context=None,
    custom_llm=None,
    history_context: str | None = None,
    memory_context: str | None = None,
) -> str:
    active_llm = custom_llm or llm

    if context:
        prompt = context_prompt(EXPLAIN_TASK, text, context, history_context, memory_context)
    elif history_context or memory_context:
        prompt = history_prompt(
            EXPLAIN_TASK,
            text,
            history_context or "",
            memory_context,
        )
    else:
        prompt = f"{EXPLAIN_TASK}\n\n用户输入：\n{text}"

    response = active_llm.invoke(prompt)
    return response.content


def summarize(
    text: str,
    context=None,
    custom_llm=None,
    history_context: str | None = None,
    memory_context: str | None = None,
) -> str:
    active_llm = custom_llm or llm

    if context:
        prompt = context_prompt(
            "请总结与用户输入相关的知识库内容",
            text,
            context,
            history_context,
            memory_context,
        )
    elif history_context or memory_context:
        prompt = history_prompt(
            "请总结当前用户输入；如果当前输入依赖上文，请结合历史对话理解",
            text,
            history_context or "",
            memory_context,
        )
    else:
        prompt = f"请总结以下内容。如果内容很短或像一个主题，请先解释它的含义，再做简短总结：\n{text}"

    response = active_llm.invoke(prompt)
    return response.content


def generate_questions(
    text: str,
    context=None,
    custom_llm=None,
    history_context: str | None = None,
    memory_context: str | None = None,
) -> str:
    active_llm = custom_llm or llm

    if context:
        prompt = context_prompt(
            "请基于知识库内容出 3 道练习题，并给出答案",
            text,
            context,
            history_context,
            memory_context,
        )
    elif history_context or memory_context:
        prompt = history_prompt(
            "请根据当前用户要求出 3 道练习题，并给出答案；必要时结合历史对话理解主题",
            text,
            history_context or "",
            memory_context,
        )
    else:
        prompt = f"请根据以下主题或知识点出 3 道练习题，并给出答案：\n{text}"

    response = active_llm.invoke(prompt)
    return response.content
