from __future__ import annotations

import frappe


def process_outbox() -> None:
	"""Extension point for a durable, allow-listed integration outbox."""
	if not frappe.conf.get("contanext_connector_enabled", False):
		return
	frappe.logger("contanext_connector").info("Connector enabled; no transport adapter configured")
