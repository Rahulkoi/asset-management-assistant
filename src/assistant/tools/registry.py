"""Tool registry and dispatcher.

The agent runtime never calls a handler directly — it names a tool and passes
raw arguments, and this module decides whether that call is allowed to happen:

  1. allowlist   — an unknown tool name returns an error result, never an exception
  2. validation  — arguments are parsed by the tool's Pydantic model first
  3. dispatch    — only then does the handler run
  4. containment — expected data errors become structured results the model can
                   recover from; unexpected ones are logged and reported without
                   leaking internals into the conversation
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from assistant.agent.confirm import ConfirmationError, ConfirmationStore
from assistant.db import repo
from assistant.tools.schemas import to_provider_schema

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Everything a handler may touch. Handlers get no ambient access to
    globals, so a tool cannot reach outside what it was given."""

    session_id: str
    actor: str = "user"
    db_path: Path | None = None
    confirmations: ConfirmationStore = field(default_factory=ConfirmationStore)
    retriever: Any = None  # set once the policy index is built


@dataclass
class ToolOutcome:
    ok: bool
    data: dict[str, Any]
    error_code: str | None = None

    @property
    def is_error(self) -> bool:
        return not self.ok

    @classmethod
    def success(cls, **data: Any) -> ToolOutcome:
        return cls(ok=True, data=data)

    @classmethod
    def failure(cls, message: str, code: str = "tool_error", **extra: Any) -> ToolOutcome:
        return cls(ok=False, data={"error": message, **extra}, error_code=code)


@dataclass
class ToolDefinition:
    name: str
    description: str
    params_model: type[BaseModel]
    handler: Callable[[Any, ToolContext], ToolOutcome]
    kind: str = "read"  # "read" | "write"
    enum_fields: tuple[str, ...] = ()

    @property
    def is_write(self) -> bool:
        return self.kind == "write"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"Tool {definition.name!r} is already registered")
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def definitions(self) -> list[ToolDefinition]:
        return [self._tools[name] for name in sorted(self._tools)]

    def specs(self, db_path: Path | None = None) -> list[dict[str, Any]]:
        """Provider-agnostic tool declarations, with enums read from live data."""
        enums = _live_enums(db_path)
        specs = []
        for definition in self.definitions():
            field_enums = {
                field_name: enums[field_name]
                for field_name in definition.enum_fields
                if field_name in enums
            }
            specs.append(
                {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": to_provider_schema(definition.params_model, field_enums),
                }
            )
        return specs

    def dispatch(self, name: str, raw_args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        definition = self.get(name)
        if definition is None:
            # A hallucinated tool name is a normal event, not a crash. Telling the
            # model what does exist lets it correct itself on the next turn.
            return ToolOutcome.failure(
                f"There is no tool called {name!r}. Available tools: {', '.join(self.names())}.",
                code="unknown_tool",
            )

        try:
            params = definition.params_model(**(raw_args or {}))
        except ValidationError as exc:
            return ToolOutcome.failure(
                f"Invalid arguments for {name}: {_explain_validation(exc)}",
                code="invalid_arguments",
            )

        try:
            return definition.handler(params, ctx)
        except repo.NotFoundError as exc:
            return ToolOutcome.failure(str(exc), code="not_found")
        except repo.ConflictError as exc:
            return ToolOutcome.failure(str(exc), code="conflict")
        except ConfirmationError as exc:
            return ToolOutcome.failure(str(exc), code="confirmation_required")
        except repo.RepoError as exc:
            return ToolOutcome.failure(str(exc), code="data_error")
        except Exception:  # noqa: BLE001 - deliberate boundary
            logger.exception("Unhandled error in tool %s", name)
            return ToolOutcome.failure(
                f"The {name} tool failed unexpectedly. Tell the user you could not "
                "complete that step; do not retry it more than once.",
                code="internal_error",
            )


def _explain_validation(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error["loc"]) or "(root)"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def _live_enums(db_path: Path | None) -> dict[str, list[str]]:
    """Read allowed values from the database so schemas can never drift from data."""
    try:
        return {
            "category": repo.distinct_values("category", db_path),
            "location": repo.distinct_values("location", db_path),
            "status": ["Assigned", "Available", "In Repair", "Retired"],
            "condition": ["New", "Good", "Fair", "Poor"],
        }
    except repo.RepoError:
        # No database yet (fresh checkout). Schemas still work, just unconstrained.
        return {}
