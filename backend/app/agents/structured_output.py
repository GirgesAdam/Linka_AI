from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from typing import Any, TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ValidationError

from app.agents.llm_runtime import invoke_model

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    pass


class StructuredOutputSchemaCompatibilityError(RuntimeError):
    pass


# Validation keywords kept for Linka's local Pydantic validation rather than sent
# to the model provider. This keeps generation schemas small and deterministic.
_LOCAL_ONLY_SCHEMA_KEYS = frozenset(
    {
        "default",
        "examples",
        "pattern",
        "minLength",
        "maxLength",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "const",
        "title",
    }
)


def canonicalize_provider_json_schema(
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Compile Pydantic JSON Schema to Linka's conservative provider subset.

    Transformations:
    - inline local `#/$defs/...` references;
    - remove `$defs` / `$ref`;
    - convert nullable `anyOf` to `type: [<type>, "null"]`;
    - convert supported single-value `const` constraints to `enum`;
    - remove local-only validation keywords.

    General unions are rejected rather than silently weakened. Full business
    validation always runs locally with the original Pydantic model.
    """
    root = deepcopy(schema)
    definitions = root.get("$defs", {})

    def resolve_reference(ref: str, stack: tuple[str, ...]) -> dict[str, Any]:
        prefix = "#/$defs/"
        if not ref.startswith(prefix):
            raise StructuredOutputSchemaCompatibilityError(
                f"Unsupported external JSON Schema reference: {ref}"
            )
        name = ref[len(prefix) :]
        if name not in definitions:
            raise StructuredOutputSchemaCompatibilityError(
                f"Unknown JSON Schema definition: {name}"
            )
        if name in stack:
            raise StructuredOutputSchemaCompatibilityError(
                f"Recursive JSON Schema definition is not supported: {name}"
            )
        resolved = walk(deepcopy(definitions[name]), (*stack, name))
        if not isinstance(resolved, dict):
            raise StructuredOutputSchemaCompatibilityError(
                f"Definition {name} did not resolve to an object schema."
            )
        return resolved

    def nullable_any_of(
        node: dict[str, Any],
        stack: tuple[str, ...],
    ) -> dict[str, Any]:
        branches = node.get("anyOf")
        if not isinstance(branches, list):
            raise StructuredOutputSchemaCompatibilityError("JSON Schema anyOf must be an array.")

        null_indexes = [
            index
            for index, branch in enumerate(branches)
            if isinstance(branch, dict)
            and branch.get("type") == "null"
            and set(branch.keys()) <= {"type", "title", "description"}
        ]
        non_null = [branch for index, branch in enumerate(branches) if index not in null_indexes]

        if len(null_indexes) != 1 or len(non_null) != 1:
            raise StructuredOutputSchemaCompatibilityError(
                "Provider schema contains a non-nullable anyOf union."
            )

        base = walk(non_null[0], stack)
        if not isinstance(base, dict) or "type" not in base:
            raise StructuredOutputSchemaCompatibilityError(
                "Nullable schema branch must resolve to a typed schema."
            )

        current_type = base["type"]
        type_values = list(current_type) if isinstance(current_type, list) else [current_type]
        if "null" not in type_values:
            type_values.append("null")
        base["type"] = type_values

        for key, value in node.items():
            if key == "anyOf" or key in _LOCAL_ONLY_SCHEMA_KEYS or key == "$defs":
                continue
            base[key] = walk(value, stack)
        return base

    def walk(value: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [walk(item, stack) for item in value]

        if not isinstance(value, dict):
            return value

        if "$ref" in value:
            ref = value["$ref"]
            resolved = resolve_reference(ref, stack)
            for key, sibling in value.items():
                if key == "$ref":
                    continue
                if key in _LOCAL_ONLY_SCHEMA_KEYS:
                    continue
                resolved[key] = walk(sibling, stack)
            return resolved

        if "anyOf" in value:
            return nullable_any_of(value, stack)

        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key == "$defs":
                continue
            if key == "const":
                if isinstance(item, str) or (
                    isinstance(item, (int, float)) and not isinstance(item, bool)
                ):
                    cleaned["enum"] = [item]
                continue
            if key in _LOCAL_ONLY_SCHEMA_KEYS:
                continue
            cleaned[key] = walk(item, stack)
        return cleaned

    root_title = root.get("title")
    result = walk(root)
    if not isinstance(result, dict):
        raise StructuredOutputSchemaCompatibilityError(
            "Root structured-output schema must be an object."
        )
    if isinstance(root_title, str) and root_title:
        result["title"] = root_title
    return result


@lru_cache(maxsize=32)
def _cached_provider_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Cache deterministic Pydantic-to-provider schema compilation per process."""
    return canonicalize_provider_json_schema(schema.model_json_schema())


def invoke_typed_structured_output(
    *,
    model: BaseChatModel,
    schema: type[T],
    messages: list[BaseMessage],
) -> T:
    """Provider JSON Schema generation followed by full local validation.

    There is no regex parser, free-form JSON recovery, or lexical intent fallback.
    """
    provider_schema = deepcopy(_cached_provider_schema(schema))
    structured = model.with_structured_output(
        schema=provider_schema,
        method="json_schema",
        strict=True,
    )

    result = invoke_model(lambda: structured.invoke(messages))

    try:
        if isinstance(result, schema):
            return result
        return schema.model_validate(result)
    except (ValidationError, TypeError, ValueError) as exc:
        raise StructuredOutputError(
            "LLM provider returned structured data that failed local validation."
        ) from exc
