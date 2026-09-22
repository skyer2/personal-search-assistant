"""User ask contract and semantic fidelity gate.

The UserAskContract is the first semantic authority: it records what the user
actually asked. Downstream nodes may only reference its asks; they must never
rewrite the meaning of an ask.
"""

from app.research.intent.fidelity import (
    SemanticFidelityResult,
    evaluate_semantic_fidelity,
)
from app.research.intent.user_ask import (
    UserAsk,
    UserAskContract,
    compile_user_ask_contract,
    split_user_clauses,
)

__all__ = [
    "SemanticFidelityResult",
    "UserAsk",
    "UserAskContract",
    "compile_user_ask_contract",
    "evaluate_semantic_fidelity",
    "split_user_clauses",
]
