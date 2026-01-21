from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[..., Any]


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any]
    call_id: Optional[str] = None


@dataclass
class ToolResult:
    name: str
    arguments: Dict[str, Any]
    output: Any
    call_id: Optional[str] = None


@dataclass
class AgentTrace:
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_results: List[ToolResult] = field(default_factory=list)


@dataclass
class AgentResponse:
    final: Optional[Dict[str, Any]] = None
    trace: AgentTrace = field(default_factory=AgentTrace)

