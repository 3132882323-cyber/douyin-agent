"""Deterministic, local-only rules for Dian Agent diagnostics.

Knowledge packs are data, not executable code.  This module intentionally
does not import an AI client and never uses ``eval``/``exec``.  A knowledge
pack may decide when a recommendation is shown, but the non-overridable
safety policy below decides what that recommendation is allowed to claim.
"""

from __future__ import annotations

import ast
import copy
import math
import re
from types import MappingProxyType
from typing import Any, Iterable

RULE_SCHEMA_VERSION = 1
MAX_RULES = 500
MAX_EXPRESSION_DEPTH = 8
MAX_FORMULA_LENGTH = 160

# These values belong to the program release, never to a downloaded pack.
# Keep the mapping flat so MappingProxyType is sufficient to make it read-only.
HARD_SAFETY_GUARDRAILS = MappingProxyType(
    {
        "execution_enabled": False,
        "requires_user_confirmation": True,
        "requires_preflight_reread": True,
        "requires_postflight_verification": True,
        "rollback_snapshot_required": True,
        "max_increase_percent": 15.0,
        "max_decrease_percent": 30.0,
        "max_daily_actions": 20,
        "authorization_ttl_seconds": 600,
    }
)

SEVERITIES = {"info", "low", "medium", "high", "critical"}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
COMPARISON_OPERATORS = {
    ">",
    ">=",
    "<",
    "<=",
    "==",
    "eq",
    "!=",
    "ne",
    "in",
    "not_in",
    "contains",
    "between",
    "exists",
    "not_exists",
}
RECOMMENDATION_TYPES = {
    "reduce_budget",
    "increase_budget",
    "adjust_bid",
    "pause_plan",
    "set_schedule",
    "replace_creative",
    "check_inventory",
    "review_product",
    "review_livestream",
    "manual_review",
}
MONEY_ACTIONS = {"reduce_budget", "increase_budget", "adjust_bid"}
DRAFTABLE_ACTIONS = {"reduce_budget", "increase_budget", "adjust_bid", "pause_plan", "set_schedule"}
PROTECTED_RESULT_KEYS = {
    "can_execute",
    "execution_enabled",
    "policy",
    "safety",
    "guardrails",
    "requires_user_confirmation",
    "requires_preflight_reread",
    "requires_postflight_verification",
    "rollback_snapshot_required",
    "max_increase_percent",
    "max_decrease_percent",
    "max_daily_actions",
    "authorization_ttl_seconds",
}
_MISSING = object()


class RulePackError(ValueError):
    """Raised when rule data is structurally unsafe or invalid."""


def _safe_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _lookup(document: Any, path: str) -> Any:
    if not isinstance(path, str) or not path or len(path) > 160:
        return _MISSING
    current = document
    for part in path.split("."):
        if not part or part.startswith("_"):
            return _MISSING
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


def _formula_value(node: ast.AST, settings: dict[str, Any], depth: int = 0) -> float:
    if depth > MAX_EXPRESSION_DEPTH:
        raise RulePackError("formula is too deeply nested")
    if isinstance(node, ast.Expression):
        return _formula_value(node.body, settings, depth + 1)
    if isinstance(node, ast.Constant):
        number = _safe_number(node.value)
        if number is None:
            raise RulePackError("formula constants must be finite numbers")
        return number
    if isinstance(node, ast.Name) and not node.id.startswith("_"):
        number = _safe_number(settings.get(node.id))
        if number is None:
            raise RulePackError(f"formula setting is missing or non-numeric: {node.id}")
        return number
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _formula_value(node.operand, settings, depth + 1)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left = _formula_value(node.left, settings, depth + 1)
        right = _formula_value(node.right, settings, depth + 1)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise RulePackError("formula division by zero")
        return left / right
    raise RulePackError("formula contains an unsupported expression")


def _resolve_operand(spec: Any, facts: dict[str, Any], settings: dict[str, Any], depth: int = 0) -> Any:
    if depth > MAX_EXPRESSION_DEPTH:
        raise RulePackError("operand is too deeply nested")
    if not isinstance(spec, dict):
        return spec
    if "literal" in spec:
        return copy.deepcopy(spec["literal"])
    if "field" in spec:
        return _lookup(facts, str(spec["field"]))
    if "setting" in spec:
        value = _lookup(settings, str(spec["setting"]))
        return spec.get("default", _MISSING) if value is _MISSING else value
    operations = [name for name in ("add", "subtract", "multiply", "divide", "min", "max") if name in spec]
    if len(operations) == 1:
        operation = operations[0]
        raw_args = spec[operation]
        if not isinstance(raw_args, list) or len(raw_args) < 2 or len(raw_args) > 8:
            raise RulePackError(f"{operation} expects 2-8 operands")
        values = [_safe_number(_resolve_operand(item, facts, settings, depth + 1)) for item in raw_args]
        if any(value is None for value in values):
            return _MISSING
        numbers = [float(value) for value in values if value is not None]
        if operation == "add":
            return sum(numbers)
        if operation == "subtract":
            return numbers[0] - sum(numbers[1:])
        if operation == "multiply":
            result = 1.0
            for number in numbers:
                result *= number
            return result
        if operation == "divide":
            result = numbers[0]
            for number in numbers[1:]:
                if number == 0:
                    return _MISSING
                result /= number
            return result
        return min(numbers) if operation == "min" else max(numbers)
    raise RulePackError("unsupported operand object")


def _expected_value(condition: dict[str, Any], facts: dict[str, Any], settings: dict[str, Any]) -> Any:
    if "right" in condition:
        return _resolve_operand(condition["right"], facts, settings)
    if "formula" in condition:
        formula = str(condition["formula"] or "")
        if not formula or len(formula) > MAX_FORMULA_LENGTH:
            raise RulePackError("formula is empty or too long")
        try:
            parsed = ast.parse(formula, mode="eval")
        except SyntaxError as exc:
            raise RulePackError("formula syntax is invalid") from exc
        return _formula_value(parsed, settings)
    if "value_from" in condition:
        return _lookup(settings, str(condition["value_from"]))
    if "setting" in condition:
        value = _lookup(settings, str(condition["setting"]))
        return condition.get("default", _MISSING) if value is _MISSING else value
    return copy.deepcopy(condition.get("value"))


def _compare(left: Any, operator: str, right: Any) -> bool:
    if operator == "exists":
        return left is not _MISSING and left is not None
    if operator == "not_exists":
        return left is _MISSING or left is None
    if left is _MISSING or right is _MISSING:
        return False
    if operator in {">", ">=", "<", "<="}:
        left_number = _safe_number(left)
        right_number = _safe_number(right)
        if left_number is None or right_number is None:
            return False
        return {
            ">": left_number > right_number,
            ">=": left_number >= right_number,
            "<": left_number < right_number,
            "<=": left_number <= right_number,
        }[operator]
    if operator in {"==", "eq"}:
        return left == right
    if operator in {"!=", "ne"}:
        return left != right
    if operator in {"in", "not_in"}:
        if not isinstance(right, (list, tuple, set, str, dict)):
            return False
        found = left in right
        return not found if operator == "not_in" else found
    if operator == "contains":
        return isinstance(left, (list, tuple, set, str, dict)) and right in left
    if operator == "between":
        if not isinstance(right, (list, tuple)) or len(right) != 2:
            return False
        value = _safe_number(left)
        lower = _safe_number(right[0])
        upper = _safe_number(right[1])
        return value is not None and lower is not None and upper is not None and lower <= value <= upper
    raise RulePackError(f"unsupported comparison operator: {operator}")


def _matches(condition: Any, facts: dict[str, Any], settings: dict[str, Any], depth: int = 0) -> bool:
    if depth > MAX_EXPRESSION_DEPTH:
        raise RulePackError("condition is too deeply nested")
    if isinstance(condition, list):
        return all(_matches(item, facts, settings, depth + 1) for item in condition)
    if not isinstance(condition, dict):
        raise RulePackError("condition must be an object or list")
    group_keys = [key for key in ("all", "any", "not") if key in condition]
    if group_keys:
        if len(group_keys) != 1:
            raise RulePackError("condition group must use exactly one of all, any or not")
        key = group_keys[0]
        if key == "not":
            return not _matches(condition[key], facts, settings, depth + 1)
        children = condition[key]
        if not isinstance(children, list) or not children:
            raise RulePackError(f"{key} condition must be a non-empty list")
        matches = (_matches(item, facts, settings, depth + 1) for item in children)
        return all(matches) if key == "all" else any(matches)
    field = str(condition.get("field") or "")
    operator = str(condition.get("operator") or "==").lower()
    if not field:
        raise RulePackError("condition field is required")
    left = _lookup(facts, field)
    right = None if operator in {"exists", "not_exists"} else _expected_value(condition, facts, settings)
    return _compare(left, operator, right)


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()[:limit]


def _validate_formula_shape(node: ast.AST, depth: int = 0) -> None:
    if depth > MAX_EXPRESSION_DEPTH:
        raise RulePackError("formula is too deeply nested")
    if isinstance(node, ast.Expression):
        _validate_formula_shape(node.body, depth + 1)
        return
    if isinstance(node, ast.Constant) and _safe_number(node.value) is not None:
        return
    if isinstance(node, ast.Name) and not node.id.startswith("_"):
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        _validate_formula_shape(node.operand, depth + 1)
        return
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        _validate_formula_shape(node.left, depth + 1)
        _validate_formula_shape(node.right, depth + 1)
        return
    raise RulePackError("formula contains an unsupported expression")


def _validate_operand_shape(spec: Any, depth: int = 0) -> None:
    if depth > MAX_EXPRESSION_DEPTH:
        raise RulePackError("operand is too deeply nested")
    if not isinstance(spec, dict):
        return
    direct = [key for key in ("literal", "field", "setting") if key in spec]
    operations = [name for name in ("add", "subtract", "multiply", "divide", "min", "max") if name in spec]
    if len(direct) + len(operations) != 1:
        raise RulePackError("operand object must have exactly one operation")
    if direct:
        if direct[0] in {"field", "setting"} and not str(spec[direct[0]] or ""):
            raise RulePackError(f"operand {direct[0]} is empty")
        return
    operation = operations[0]
    args = spec[operation]
    if not isinstance(args, list) or len(args) < 2 or len(args) > 8:
        raise RulePackError(f"{operation} expects 2-8 operands")
    for item in args:
        _validate_operand_shape(item, depth + 1)


def _validate_condition_shape(condition: Any, depth: int = 0) -> None:
    if depth > MAX_EXPRESSION_DEPTH:
        raise RulePackError("condition is too deeply nested")
    if isinstance(condition, list):
        if not condition:
            raise RulePackError("condition list must not be empty")
        for item in condition:
            _validate_condition_shape(item, depth + 1)
        return
    if not isinstance(condition, dict):
        raise RulePackError("condition must be an object or list")
    group_keys = [key for key in ("all", "any", "not") if key in condition]
    if group_keys:
        if len(group_keys) != 1:
            raise RulePackError("condition group must use exactly one of all, any or not")
        key = group_keys[0]
        children = condition[key]
        if key == "not":
            _validate_condition_shape(children, depth + 1)
            return
        if not isinstance(children, list) or not children:
            raise RulePackError(f"{key} condition must be a non-empty list")
        for item in children:
            _validate_condition_shape(item, depth + 1)
        return
    field = str(condition.get("field") or "")
    if not field or any(not part or part.startswith("_") for part in field.split(".")):
        raise RulePackError("condition field is invalid")
    operator = str(condition.get("operator") or "==").lower()
    if operator not in COMPARISON_OPERATORS:
        raise RulePackError(f"unsupported comparison operator: {operator}")
    sources = [key for key in ("right", "formula", "value_from", "setting", "value") if key in condition]
    if operator not in {"exists", "not_exists"} and len(sources) != 1:
        raise RulePackError("condition must define exactly one comparison value")
    if "right" in condition:
        _validate_operand_shape(condition["right"], depth + 1)
    if "formula" in condition:
        formula = str(condition["formula"] or "")
        if not formula or len(formula) > MAX_FORMULA_LENGTH:
            raise RulePackError("formula is empty or too long")
        try:
            parsed = ast.parse(formula, mode="eval")
        except SyntaxError as exc:
            raise RulePackError("formula syntax is invalid") from exc
        _validate_formula_shape(parsed, depth + 1)


def _guard_action(raw_action: Any) -> tuple[dict[str, Any], list[str]]:
    ignored: list[str] = []
    action = raw_action if isinstance(raw_action, dict) else {}
    for key in sorted(PROTECTED_RESULT_KEYS):
        if key in action:
            ignored.append(key)
    action_type = _clean_text(action.get("type") or "manual_review", 48)
    blocked_reasons: list[str] = []
    if action_type not in RECOMMENDATION_TYPES:
        blocked_reasons.append("UNSUPPORTED_RECOMMENDATION_TYPE")
        action_type = "manual_review"

    result: dict[str, Any] = {
        "type": action_type,
        "label": _clean_text(action.get("label"), 120),
        "observation_minutes": max(0, min(int(_safe_number(action.get("observation_minutes")) or 0), 7 * 24 * 60)),
        "execution_enabled": False,
        "can_execute": False,
        "requires_user_confirmation": True,
    }
    change_percent = _safe_number(action.get("change_percent"))
    if action_type in MONEY_ACTIONS:
        if change_percent is None:
            blocked_reasons.append("CHANGE_PERCENT_MISSING")
        else:
            result["change_percent"] = round(change_percent, 2)
            if change_percent > HARD_SAFETY_GUARDRAILS["max_increase_percent"]:
                blocked_reasons.append("INCREASE_LIMIT_EXCEEDED")
            if change_percent < -HARD_SAFETY_GUARDRAILS["max_decrease_percent"]:
                blocked_reasons.append("DECREASE_LIMIT_EXCEEDED")
            if action_type == "reduce_budget" and change_percent >= 0:
                blocked_reasons.append("REDUCTION_DIRECTION_INVALID")
            if action_type == "increase_budget" and change_percent <= 0:
                blocked_reasons.append("INCREASE_DIRECTION_INVALID")
    result["blocked_reasons"] = blocked_reasons
    result["eligible_for_action_draft"] = not blocked_reasons and action_type in DRAFTABLE_ACTIONS
    return result, ignored


def validate_rules(pack: dict[str, Any]) -> list[dict[str, str]]:
    """Return per-rule structural errors without executing any rule."""
    errors: list[dict[str, str]] = []
    if not isinstance(pack, dict):
        return [{"rule_id": "", "error": "pack must be an object"}]
    if int(pack.get("schema_version") or 0) != RULE_SCHEMA_VERSION:
        errors.append({"rule_id": "", "error": "unsupported rule schema version"})
    rules = pack.get("rules")
    if not isinstance(rules, list):
        errors.append({"rule_id": "", "error": "rules must be a list"})
        return errors
    if len(rules) > MAX_RULES:
        errors.append({"rule_id": "", "error": f"rule count exceeds {MAX_RULES}"})
    seen: set[str] = set()
    for index, rule in enumerate(rules[:MAX_RULES]):
        rule_id = _clean_text(rule.get("rule_id") if isinstance(rule, dict) else "", 80)
        try:
            if not isinstance(rule, dict):
                raise RulePackError("rule must be an object")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,79}", rule_id):
                raise RulePackError("rule_id is invalid")
            if rule_id in seen:
                raise RulePackError("rule_id is duplicated")
            seen.add(rule_id)
            if "conditions" not in rule:
                raise RulePackError("conditions are required")
            _validate_condition_shape(rule["conditions"])
            if not isinstance(rule.get("result"), dict):
                raise RulePackError("result must be an object")
            severity = str(rule["result"].get("level") or "medium").lower()
            if severity not in SEVERITIES:
                raise RulePackError("result level is invalid")
        except (RulePackError, TypeError, ValueError) as exc:
            errors.append({"rule_id": rule_id or f"index:{index}", "error": str(exc)})
    return errors


class RuleEngine:
    """Evaluate a verified knowledge pack against one local fact snapshot."""

    def __init__(self, pack: dict[str, Any]):
        errors = validate_rules(pack)
        if errors:
            raise RulePackError("invalid knowledge rules: " + "; ".join(item["error"] for item in errors[:5]))
        self._pack = copy.deepcopy(pack)

    @property
    def pack_version(self) -> str:
        return str(self._pack.get("pack_version") or "")

    def evaluate(self, facts: dict[str, Any], settings: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(facts, dict):
            raise ValueError("facts must be an object")
        settings = settings or {}
        if not isinstance(settings, dict):
            raise ValueError("settings must be an object")
        diagnostics: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        rules: Iterable[dict[str, Any]] = self._pack.get("rules", [])
        for rule in rules:
            if rule.get("enabled", True) is False:
                continue
            rule_id = str(rule["rule_id"])
            try:
                if not _matches(rule["conditions"], facts, settings):
                    continue
                raw_result = rule["result"]
                action, ignored = _guard_action(raw_result.get("action"))
                ignored.extend(sorted(key for key in PROTECTED_RESULT_KEYS if key in raw_result))
                level = str(raw_result.get("level") or "medium").lower()
                item = {
                    "rule_id": rule_id,
                    "rule_version": int(rule.get("version") or 1),
                    "priority": max(0, min(int(rule.get("priority") or 100), 10000)),
                    "level": level,
                    "title": _clean_text(raw_result.get("title"), 160),
                    "message": _clean_text(raw_result.get("message"), 500),
                    "dedupe_key": _clean_text(raw_result.get("dedupe_key") or rule_id, 100),
                    "action": action,
                    "acceptance": copy.deepcopy(raw_result.get("acceptance"))
                    if isinstance(raw_result.get("acceptance"), dict)
                    else {},
                    "guardrail_overrides_ignored": sorted(set(ignored)),
                }
                diagnostics.append(item)
            except (RulePackError, TypeError, ValueError, OverflowError) as exc:
                errors.append({"rule_id": rule_id, "error": str(exc)})

        # One deterministic winner per dedupe key.  Lower priority is stronger.
        diagnostics.sort(key=lambda item: (item["priority"], SEVERITY_ORDER[item["level"]], item["rule_id"]))
        deduped: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for item in diagnostics:
            if item["dedupe_key"] in seen_keys:
                continue
            seen_keys.add(item["dedupe_key"])
            deduped.append(item)
        return {
            "mode": "deterministic_local",
            "ai_required": False,
            "pack_version": self.pack_version,
            "diagnostics": deduped,
            "matched_count": len(deduped),
            "rule_errors": errors,
            "safety_policy": dict(HARD_SAFETY_GUARDRAILS),
        }
