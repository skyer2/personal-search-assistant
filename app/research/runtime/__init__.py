"""LangGraph 上的 Research Harness 可执行表示。

Imports stay lazy to keep graph construction lightweight.
"""

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "compile_research_graph":
        from app.research.runtime.graph import compile_research_graph

        return compile_research_graph
    if name == "initial_graph_state":
        from app.research.runtime.graph import initial_graph_state

        return initial_graph_state
    if name == "empty_research_state":
        from app.research.runtime.state import empty_research_state

        return empty_research_state
    raise AttributeError(name)


__all__ = ["compile_research_graph", "empty_research_state", "initial_graph_state"]
