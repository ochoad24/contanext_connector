from __future__ import annotations

import hashlib
import json

import frappe


def configure_tenant(country: str, time_zone: str, currency: str, language: str, admin_name: str, admin_email: str) -> dict[str, str]:
	"""Apply the allow-listed initial tenant settings without creating credentials."""
	country_name = frappe.db.get_value("Country", {"code": country.lower()}, "name")
	if not country_name:
		frappe.throw("Unsupported country code")
	settings = frappe.get_single("System Settings")
	settings.country = country_name
	settings.time_zone = time_zone
	settings.language = language.split("-", 1)[0]
	settings.save(ignore_permissions=True)

	global_defaults = frappe.get_single("Global Defaults")
	global_defaults.default_currency = currency
	global_defaults.save(ignore_permissions=True)

	first_name, _, last_name = admin_name.strip().partition(" ")
	if frappe.db.exists("User", admin_email):
		user = frappe.get_doc("User", admin_email)
		user.first_name = first_name
		user.last_name = last_name
		user.enabled = 1
		user.save(ignore_permissions=True)
	else:
		user = frappe.get_doc({
			"doctype": "User",
			"email": admin_email,
			"first_name": first_name,
			"last_name": last_name,
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		})
		user.insert(ignore_permissions=True)
	user.add_roles("System Manager")
	frappe.db.set_default("contanext_country", country)
	frappe.db.set_default("contanext_language", language)
	frappe.db.set_default("contanext_initial_admin", admin_email)
	frappe.db.commit()
	return {"status": "configured", "administrator": admin_email}


def sync_entitlements(snapshot: str) -> dict[str, str]:
	"""Persist exactly the immutable control-plane snapshot and its fingerprint."""
	value = json.loads(snapshot)
	if not isinstance(value, dict) or not {"general", "plan", "configuration", "initial_admin", "overrides"}.issubset(value):
		frappe.throw("Invalid entitlement snapshot")
	canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
	frappe.db.set_default("contanext_entitlements_snapshot", canonical)
	frappe.db.set_default("contanext_entitlements_fingerprint", fingerprint)
	frappe.db.commit()
	return {"status": "synchronized", "fingerprint": fingerprint}
