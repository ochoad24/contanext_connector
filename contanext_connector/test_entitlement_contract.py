from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from contanext_connector.entitlement_contract import (
	EntitlementContractError,
	MODE_ENFORCE,
	MODE_OBSERVE,
	parse_contract,
	validate_transition,
)


class EntitlementContractTest(unittest.TestCase):
	def test_parses_v1_contract(self):
		value = parse_contract(
			{
				"schema_version": "1.0",
				"tenant_id": "tenant.contanext.test",
				"sequence": 9,
				"mode": MODE_ENFORCE,
				"expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
				"values": [
					{"kind": "RESOURCE", "key": "USERS", "value": 3},
					{"kind": "RESOURCE", "key": "COMPANIES", "value": 1},
					{"kind": "MODULE", "key": "ACCOUNTING", "value": True},
				],
			},
			"tenant.contanext.test",
		)
		self.assertEqual(value.sequence, 9)
		self.assertEqual(value.resources, {"USERS": 3, "COMPANIES": 1})
		self.assertEqual(value.modules, {"ACCOUNTING": True})

	def test_legacy_snapshot_starts_in_observation(self):
		value = parse_contract(
			{
				"general": {"domain": "tenant.contanext.test"},
				"plan": {},
				"configuration": {"users": 3, "companies": 1, "module_codes": ["ACCOUNTING"]},
				"initial_admin": {},
				"overrides": [{"kind": "MODULE", "key": "ACCOUNTING", "value": False}],
			},
			"tenant.contanext.test",
		)
		self.assertEqual(value.mode, MODE_OBSERVE)
		self.assertFalse(value.modules["ACCOUNTING"])

	def test_rejects_tenant_mismatch_expiration_and_rollback(self):
		base = {
			"schema_version": "1.0",
			"tenant_id": "tenant.contanext.test",
			"sequence": 2,
			"values": [
				{"kind": "RESOURCE", "key": "USERS", "value": 3},
				{"kind": "RESOURCE", "key": "COMPANIES", "value": 1},
			],
		}
		with self.assertRaises(EntitlementContractError):
			parse_contract(base, "other.contanext.test")
		candidate = parse_contract(base, "tenant.contanext.test")
		with self.assertRaises(EntitlementContractError):
			validate_transition(3, "", candidate)

	def test_equal_sequence_is_idempotent_but_not_mutable(self):
		payload = {
			"schema_version": "1.0",
			"tenant_id": "tenant.contanext.test",
			"sequence": 2,
			"values": [
				{"kind": "RESOURCE", "key": "USERS", "value": 3},
				{"kind": "RESOURCE", "key": "COMPANIES", "value": 1},
			],
		}
		candidate = parse_contract(payload, "tenant.contanext.test")
		self.assertEqual(validate_transition(2, candidate.fingerprint, candidate), "UNCHANGED")
		with self.assertRaises(EntitlementContractError):
			validate_transition(2, "0" * 64, candidate)

	def test_rejects_missing_module_dependency(self):
		with self.assertRaisesRegex(EntitlementContractError, "MANUFACTURING requires"):
			parse_contract(
				{
					"schema_version": "1.0",
					"tenant_id": "tenant.contanext.test",
					"sequence": 3,
					"values": [
						{"kind": "RESOURCE", "key": "USERS", "value": 3},
						{"kind": "RESOURCE", "key": "COMPANIES", "value": 1},
						{"kind": "MODULE", "key": "MANUFACTURING", "value": True},
						{"kind": "MODULE", "key": "INVENTORY", "value": False},
					],
				},
				"tenant.contanext.test",
			)


if __name__ == "__main__":
	unittest.main()
