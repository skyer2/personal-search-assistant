"""Versioned answer units: the only material eligible for final delivery."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

CheckStatus = Literal["pass", "fail", "unknown"]
UnitStatus = Literal["pending", "valid", "invalid", "unknown"]


@dataclass(frozen=True)
class UnitField:
    value: Any
    value_kind: str
    claim_ids: tuple[str, ...] = ()
    premise_claim_ids: tuple[str, ...] = ()
    rationale: str = ""
    limitation_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UnitField":
        return cls(
            value=data.get("value"),
            value_kind=str(data.get("value_kind") or "unknown"),
            claim_ids=tuple(str(item) for item in data.get("claim_ids") or [] if str(item)),
            premise_claim_ids=tuple(str(item) for item in data.get("premise_claim_ids") or [] if str(item)),
            rationale=str(data.get("rationale") or ""),
            limitation_status=str(data.get("limitation_status") or ""),
        )


@dataclass(frozen=True)
class ValidationCheck:
    check_id: str
    status: CheckStatus
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UnitValidation:
    status: UnitStatus = "pending"
    validator_version: str = "unit-validator-v2"
    checks: tuple[ValidationCheck, ...] = ()
    evidence_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "UnitValidation":
        row = data or {}
        status = str(row.get("status") or "pending")
        if status not in {"pending", "valid", "invalid", "unknown"}:
            status = "unknown"
        return cls(
            status=status,  # type: ignore[arg-type]
            validator_version=str(row.get("validator_version") or "unit-validator-v2"),
            checks=tuple(
                ValidationCheck(
                    check_id=str(item.get("check_id") or ""),
                    status=(str(item.get("status")) if str(item.get("status")) in {"pass", "fail", "unknown"} else "unknown"),  # type: ignore[arg-type]
                    reason_codes=tuple(str(code) for code in item.get("reason_codes") or []),
                )
                for item in row.get("checks") or [] if isinstance(item, dict)
            ),
            evidence_version=str(row.get("evidence_version") or ""),
        )


@dataclass(frozen=True)
class AnswerUnit:
    unit_id: str
    ask_id: str
    spec_revision: int
    kind: str
    entity_ids: tuple[str, ...]
    fields: dict[str, UnitField]
    validation: UnitValidation = field(default_factory=UnitValidation)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "ask_id": self.ask_id,
            "spec_revision": self.spec_revision,
            "kind": self.kind,
            "entity_ids": list(self.entity_ids),
            "fields": {key: value.to_dict() for key, value in self.fields.items()},
            "validation": self.validation.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnswerUnit":
        return cls(
            unit_id=str(data.get("unit_id") or ""),
            ask_id=str(data.get("ask_id") or ""),
            spec_revision=int(data.get("spec_revision") or 0),
            kind=str(data.get("kind") or ""),
            entity_ids=tuple(str(item) for item in data.get("entity_ids") or [] if str(item)),
            fields={
                str(key): UnitField.from_dict(value)
                for key, value in dict(data.get("fields") or {}).items()
                if isinstance(value, dict)
            },
            validation=UnitValidation.from_dict(data.get("validation") if isinstance(data.get("validation"), dict) else None),
        )


__all__ = ["AnswerUnit", "UnitField", "UnitValidation", "ValidationCheck"]
