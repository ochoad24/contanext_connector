from __future__ import annotations

import importlib
import json
import sys
import types
import unittest


class _DB:
	def __init__(self, mode: str):
		self.mode = mode

	def get_default(self, key):
		if key != "contanext_effective_entitlements":
			return None
		return json.dumps(
			{
				"tenant_id": "tenant.contanext.test",
				"sequence": 2,
				"mode": self.mode,
				"resources": {"USERS": 3, "COMPANIES": 1},
				"modules": {"ACCOUNTING": True, "MANUFACTURING": False},
				"fingerprint": "a" * 64,
			}
		)


class _Logger:
	def warning(self, value):
		pass

	def info(self, value):
		pass


class _Doc(dict):
	def __init__(self, doctype: str, name: str = "example"):
		super().__init__()
		self.doctype = doctype
		self.name = name

	def get(self, key, default=None):
		return super().get(key, default)


class EntitlementEnforcementTest(unittest.TestCase):
	def _module(self, mode: str):
		fake = types.ModuleType("frappe")
		fake.db = _DB(mode)
		fake.local = types.SimpleNamespace(site="tenant.contanext.test")
		fake.logger = lambda name: _Logger()
		fake.throw = lambda message, **kwargs: (_ for _ in ()).throw(RuntimeError(message))
		sys.modules["frappe"] = fake
		sys.modules.pop("contanext_connector.entitlements", None)
		return importlib.import_module("contanext_connector.entitlements")

	def test_disabled_module_is_observed_without_blocking(self):
		module = self._module("OBSERVE")
		doc = _Doc("Work Order")
		module.validate_module_operation(doc)
		self.assertTrue(module.has_module_permission(doc))

	def test_disabled_module_is_blocked_in_enforcement(self):
		module = self._module("ENFORCE")
		doc = _Doc("Work Order")
		with self.assertRaisesRegex(RuntimeError, "MANUFACTURING"):
			module.validate_module_operation(doc)
		self.assertFalse(module.has_module_permission(doc))

	def test_enabled_module_remains_accessible(self):
		module = self._module("ENFORCE")
		doc = _Doc("Journal Entry")
		module.validate_module_operation(doc)
		self.assertTrue(module.has_module_permission(doc))

	def test_enforcement_filters_disabled_workspace(self):
		module = self._module("ENFORCE")
		value = {"Manufacturing": {"name": "Manufacturing"}, "Accounting": {"name": "Accounting"}}
		self.assertEqual(module._filter_navigation(value, {"Manufacturing"}), {"Accounting": {"name": "Accounting"}})

	def test_list_query_is_denied_for_disabled_module(self):
		module = self._module("ENFORCE")
		self.assertEqual(module.module_permission_query(doctype="Work Order"), "1=0")
		self.assertEqual(module.module_permission_query(doctype="Journal Entry"), "")

	def test_every_catalog_module_has_server_and_workspace_coverage(self):
		module = self._module("ENFORCE")
		expected = {
			"FISCAL_GUATEMALA", "ACCOUNTING", "SALES", "PURCHASING", "INVENTORY",
			"ASSETS", "CRM", "PROJECTS", "MANUFACTURING", "POINT_OF_SALE",
		}
		self.assertEqual(set(module.MODULE_DOCTYPES), expected)
		self.assertEqual(set(module.MODULE_WORKSPACES), expected)
		for code in expected:
			self.assertTrue(module.MODULE_DOCTYPES[code], code)
			self.assertTrue(module.MODULE_WORKSPACES[code], code)


if __name__ == "__main__":
	unittest.main()
