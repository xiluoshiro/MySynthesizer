from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from .candidates import generate_candidates
from .features import ObjectFeatures
from .intent import CraftIntent
from .models import CandidateObject, SynthObject


class CandidateGenerator(Protocol):
    def generate(
        self,
        a: SynthObject,
        b: SynthObject,
        a_features: ObjectFeatures,
        b_features: ObjectFeatures,
        intent: CraftIntent,
        max_candidates: int,
    ) -> list[CandidateObject]:
        ...


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]], *, timeout: float) -> str:
        ...


class RuleCandidateGenerator:
    def generate(
        self,
        a: SynthObject,
        b: SynthObject,
        a_features: ObjectFeatures,
        b_features: ObjectFeatures,
        intent: CraftIntent,
        max_candidates: int,
    ) -> list[CandidateObject]:
        return generate_candidates(a, b, a_features, b_features, intent, max_candidates)


class CompositeCandidateGenerator:
    def __init__(self, generators: list[CandidateGenerator]) -> None:
        self.generators = generators
        self.last_errors: list[str] = []

    def generate(
        self,
        a: SynthObject,
        b: SynthObject,
        a_features: ObjectFeatures,
        b_features: ObjectFeatures,
        intent: CraftIntent,
        max_candidates: int,
    ) -> list[CandidateObject]:
        self.last_errors = []
        unique: dict[tuple[str, str], CandidateObject] = {}
        for generator in self.generators:
            for candidate in generator.generate(a, b, a_features, b_features, intent, max_candidates):
                key = (candidate.name, candidate.type)
                if key not in unique:
                    unique[key] = candidate
                if len(unique) >= max_candidates:
                    return list(unique.values())
            last_error = getattr(generator, "last_error", None)
            if last_error:
                self.last_errors.append(str(last_error))
        return list(unique.values())


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str | None
    model: str | None
    timeout: float

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            base_url=os.environ.get("MYSYNTH_LLM_BASE_URL", "https://api.openai.com/v1"),
            api_key=_empty_to_none(os.environ.get("MYSYNTH_LLM_API_KEY")),
            model=_empty_to_none(os.environ.get("MYSYNTH_LLM_MODEL")),
            timeout=_float_env("MYSYNTH_LLM_TIMEOUT", 30.0),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.model)


class OpenAICompatibleLLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def complete(self, messages: list[dict[str, str]], *, timeout: float) -> str:
        if not self.config.is_configured:
            raise RuntimeError("LLM is not configured; set MYSYNTH_LLM_API_KEY and MYSYNTH_LLM_MODEL")
        body = json.dumps(
            {
                "model": self.config.model,
                "messages": messages,
                "temperature": 0.7,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload["choices"][0]["message"]["content"])


class StaticLLMClient:
    def __init__(self, response: str) -> None:
        self.response = response

    def complete(self, messages: list[dict[str, str]], *, timeout: float) -> str:
        return self.response


class LLMCandidateGenerator:
    REQUIRED_FIELDS = {"name", "type", "description", "emoji", "core_tags", "anchors", "source_reason"}

    def __init__(
        self,
        *,
        client: LLMClient | None = None,
        config: LLMConfig | None = None,
    ) -> None:
        self.config = config or LLMConfig.from_env()
        fake_response = os.environ.get("MYSYNTH_LLM_FAKE_RESPONSE")
        if client is None and fake_response is not None:
            client = StaticLLMClient(fake_response)
        self.client = client or OpenAICompatibleLLMClient(self.config)
        self.last_error: str | None = None

    def generate(
        self,
        a: SynthObject,
        b: SynthObject,
        a_features: ObjectFeatures,
        b_features: ObjectFeatures,
        intent: CraftIntent,
        max_candidates: int,
    ) -> list[CandidateObject]:
        self.last_error = None
        try:
            content = self.client.complete(_messages_for_request(a, b, a_features, b_features, intent), timeout=self.config.timeout)
            raw_payload = json.loads(content)
            candidates = _candidate_payloads(raw_payload)
            valid: list[CandidateObject] = []
            for raw in candidates:
                candidate = self._validate_payload(raw)
                if candidate is not None:
                    valid.append(candidate)
                if len(valid) >= max_candidates:
                    break
            if not valid:
                self.last_error = "LLM returned no valid candidates"
            return valid
        except Exception as exc:
            self.last_error = str(exc)
            return []

    def _validate_payload(self, raw: object) -> CandidateObject | None:
        if not isinstance(raw, dict):
            return None
        if not self.REQUIRED_FIELDS <= set(raw):
            return None
        try:
            candidate = CandidateObject.model_validate(raw)
        except ValidationError:
            return None
        if not candidate.name.strip() or not candidate.description.strip():
            return None
        return candidate


def build_default_candidate_generator(*, use_llm: bool) -> CandidateGenerator:
    rule_generator = RuleCandidateGenerator()
    if not use_llm:
        return rule_generator
    fake_response = os.environ.get("MYSYNTH_LLM_FAKE_RESPONSE")
    llm_client: LLMClient | None = StaticLLMClient(fake_response) if fake_response is not None else None
    return CompositeCandidateGenerator([LLMCandidateGenerator(client=llm_client), rule_generator])


def _messages_for_request(
    a: SynthObject,
    b: SynthObject,
    a_features: ObjectFeatures,
    b_features: ObjectFeatures,
    intent: CraftIntent,
) -> list[dict[str, str]]:
    payload = {
        "operation": intent.operation,
        "expected_type": intent.expected_type,
        "anchors": sorted(intent.anchors),
        "ingredient_a": _compact_object(a, a_features),
        "ingredient_b": _compact_object(b, b_features),
        "output_schema": {
            "candidates": [
                {
                    "name": "string",
                    "type": "element|item|equipment|creature|concept",
                    "description": "string",
                    "emoji": "string|null",
                    "core_tags": ["string"],
                    "anchors": ["string"],
                    "source_reason": "string",
                }
            ]
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你是本地合成器的候选生成器。只输出 JSON，不要解释。"
                "候选必须具体、可命名，并保持输入对象的核心语义。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False),
        },
    ]


def _compact_object(obj: SynthObject, features: ObjectFeatures) -> dict[str, object]:
    return {
        "id": obj.id,
        "name": obj.name,
        "type": obj.type,
        "description": obj.description,
        "semantic_tags": sorted(features.semantic_tags),
        "anchors": sorted(features.anchors),
        "name_tokens": sorted(features.name_tokens),
    }


def _candidate_payloads(payload: object) -> list[object]:
    if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
        return list(payload["candidates"])
    if isinstance(payload, dict):
        return [payload]
    return []


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default
