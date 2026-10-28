from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "1.0"
MODE_OBSERVE = "OBSERVE"
MODE_ENFORCE = "ENFORCE"
VALID_MODES = {MODE_OBSERVE, MODE_ENFORCE}
RESOURCE_KEYS = {"USERS", "COMPANIES"}
MODULE_DEPENDENCIES = {
	"FISCAL_GUATEMALA": {"ACCOUNTING"},
	"ASSETS": {"ACCOUNTING"},
	"MANUFACTURING": {"INVENTORY"},
	"POINT_OF_SALE": {"ACCOUNTING", "SALES", "INVENTORY"},
}


class EntitlementContractError(ValueError):
	pass


@dataclass(frozen=True)
class EffectiveEntitlements:
	tenant_id: str
	sequence: int
	mode: str
	resources: dict[str, int]
	modules: dict[str, bool]
	canonical_payload: str
	fingerprint: str

	def as_dict(self) -> dict[str, Any]:
		return {
			"schema_version": SCHEMA_VERSION,
			"tenant_id": self.tenant_id,
			"sequence": self.sequence,
			"mode": self.mode,
			"resources": self.resources,
			"modules": self.modules,
			"fingerprint": self.fingerprint,
		}


def parse_contract(payload: str | dict[str, Any], expected_tenant: str, now: datetime | None = None) -> EffectiveEntitlements:
	value = json.loads(payload) if isinstance(payload, str) else payload
	if not isinstance(value, dict):
		raise EntitlementContractError("entitlement payload must be an object")
	canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	if value.get("schema_version") == SCHEMA_VERSION:
		semantic = {key: value.get(key) for key in ("schema_version", "tenant_id", "sequence", "mode", "values")}
		fingerprint = hashlib.sha256(
			json.dumps(semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
		).hexdigest()
		return _parse_v1(value, expected_tenant, canonical, fingerprint, now or datetime.now(timezone.utc))
	fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
	return _parse_legacy(value, expected_tenant, canonical, fingerprint)


def validate_transition(current_sequence: int, current_fingerprint: str, candidate: EffectiveEntitlements) -> str:
	if candidate.sequence < current_sequence:
		raise EntitlementContractError("entitlement rollback rejected")
	if candidate.sequence == current_sequence:
		if current_fingerprint and current_fingerprint != candidate.fingerprint:
			raise EntitlementContractError("entitlement sequence conflict")
		return "UNCHANGED"
	return "APPLY"


def _parse_v1(
	value: dict[str, Any], expected_tenant: str, canonical: str, fingerprint: str, now: datetime
) -> EffectiveEntitlements:
	tenant_id = _required_text(value.get("tenant_id"), "tenant_id")
	if tenant_id != expected_tenant:
		raise EntitlementContractError("entitlement tenant mismatch")
	sequence = value.get("sequence")
	if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
		raise EntitlementContractError("invalid entitlement sequence")
	mode = _parse_mode(value.get("mode"))
	expires_at = value.get("expires_at")
	if expires_at:
		try:
			expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
		except ValueError as exc:
			raise EntitlementContractError("invalid entitlement expiration") from exc
		if expires.tzinfo is None or expires <= now:
			raise EntitlementContractError("expired entitlement payload")
	values = value.get("values")
	if not isinstance(values, list):
		raise EntitlementContractError("entitlement values must be an array")
	resources: dict[str, int] = {}
	modules: dict[str, bool] = {}
	for item in values:
		if not isinstance(item, dict):
			raise EntitlementContractError("invalid entitlement value")
		kind = str(item.get("kind", "")).strip().upper()
		key = str(item.get("key", "")).strip().upper()
		if kind == "RESOURCE" and key in RESOURCE_KEYS:
			resources[key] = _non_negative_integer(item.get("value"), key)
		elif kind == "MODULE":
			if not key or not isinstance(item.get("value"), bool):
				raise EntitlementContractError("invalid module entitlement")
			modules[key] = item["value"]
	_validate_required_resources(resources)
	_validate_module_dependencies(modules)
	return EffectiveEntitlements(tenant_id, sequence, mode, resources, modules, canonical, fingerprint)


def _parse_legacy(
	value: dict[str, Any], expected_tenant: str, canonical: str, fingerprint: str
) -> EffectiveEntitlements:
	required = {"general", "plan", "configuration", "initial_admin", "overrides"}
	if not required.issubset(value):
		raise EntitlementContractError("invalid legacy entitlement snapshot")
	tenant_id = _required_text(value.get("general", {}).get("domain"), "general.domain")
	if tenant_id != expected_tenant:
		raise EntitlementContractError("entitlement tenant mismatch")
	configuration = value.get("configuration")
	if not isinstance(configuration, dict):
		raise EntitlementContractError("invalid legacy entitlement configuration")
	resources = {
		"USERS": _non_negative_integer(configuration.get("users"), "USERS"),
		"COMPANIES": _non_negative_integer(configuration.get("companies"), "COMPANIES"),
	}
	modules = {str(code).strip().upper(): True for code in configuration.get("module_codes", []) if str(code).strip()}
	for override in value.get("overrides", []):
		if not isinstance(override, dict):
			continue
		kind = str(override.get("kind", "")).upper()
		key = str(override.get("key", "")).strip().upper()
		if kind == "RESOURCE" and key in RESOURCE_KEYS:
			resources[key] = _non_negative_integer(override.get("value"), key)
		elif kind == "MODULE" and key and isinstance(override.get("value"), bool):
			modules[key] = override["value"]
	_validate_module_dependencies(modules)
	return EffectiveEntitlements(tenant_id, 0, MODE_OBSERVE, resources, modules, canonical, fingerprint)


def _required_text(value: Any, field: str) -> str:
	text = str(value or "").strip().lower()
	if not text:
		raise EntitlementContractError(f"missing {field}")
	return text


def _parse_mode(value: Any) -> str:
	mode = str(value or MODE_OBSERVE).strip().upper()
	if mode not in VALID_MODES:
		raise EntitlementContractError("invalid entitlement mode")
	return mode


def _non_negative_integer(value: Any, key: str) -> int:
	if isinstance(value, bool):
		raise EntitlementContractError(f"invalid {key} limit")
	if isinstance(value, str) and value.isdigit():
		value = int(value)
	if not isinstance(value, int) or value < 0:
		raise EntitlementContractError(f"invalid {key} limit")
	return value


def _validate_required_resources(resources: dict[str, int]) -> None:
	missing = RESOURCE_KEYS - resources.keys()
	if missing:
		raise EntitlementContractError(f"missing entitlement resources: {','.join(sorted(missing))}")


def _validate_module_dependencies(modules: dict[str, bool]) -> None:
	for module, dependencies in MODULE_DEPENDENCIES.items():
		if not modules.get(module, False):
			continue
		missing = sorted(dependency for dependency in dependencies if not modules.get(dependency, False))
		if missing:
			raise EntitlementContractError(f"module {module} requires: {','.join(missing)}")
