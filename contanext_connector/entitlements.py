from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

import frappe

from contanext_connector.entitlement_contract import MODE_ENFORCE, EffectiveEntitlements


RESERVED_USERS = {"Administrator", "Guest", "contanext_sync@system.local"}
MODULE_ROLES = {
	"FISCAL_GUATEMALA": {"Accounts Manager", "GT Books Manager"},
	"ACCOUNTING": {"Accounts Manager", "Report Manager"},
	"SALES": {"Sales Manager"},
	"PURCHASING": {"Purchase Manager"},
	"INVENTORY": {"Stock Manager", "Item Manager"},
	"ASSETS": {"Accounts Manager"},
	"CRM": {"Sales Manager"},
	"PROJECTS": {"Projects Manager"},
	"MANUFACTURING": {"Manufacturing Manager", "Stock Manager"},
	"POINT_OF_SALE": {"Sales Manager", "Accounts Manager", "Stock Manager"},
}
CONTROLLED_ROLES = set().union(*MODULE_ROLES.values())
MODULE_WORKSPACES = {
	"FISCAL_GUATEMALA": {"Fiscal Guatemala", "Libros de Guatemala"},
	"ACCOUNTING": {"Accounting", "Financial Reports", "Payments"},
	"SALES": {"Selling", "Invoicing"},
	"PURCHASING": {"Buying"},
	"INVENTORY": {"Stock"},
	"ASSETS": {"Assets"},
	"CRM": {"CRM"},
	"PROJECTS": {"Projects"},
	"MANUFACTURING": {"Manufacturing"},
	"POINT_OF_SALE": {"Point of Sale"},
}
MODULE_DOCTYPES = {
	"FISCAL_GUATEMALA": {"GT Accounting Book Run", "GT Requirement Package", "GT Physical Count Import"},
	"ACCOUNTING": {"Account", "Journal Entry", "Payment Entry", "GL Entry", "Cost Center", "Budget"},
	"SALES": {"Customer", "Quotation", "Sales Order", "Delivery Note", "Sales Invoice"},
	"PURCHASING": {"Supplier", "Request for Quotation", "Purchase Order", "Purchase Receipt", "Purchase Invoice"},
	"INVENTORY": {"Item", "Warehouse", "Stock Entry", "Stock Reconciliation", "Stock Ledger Entry"},
	"ASSETS": {"Asset", "Asset Category", "Asset Movement", "Asset Repair", "Asset Capitalization"},
	"CRM": {"Lead", "Opportunity", "Prospect", "CRM Note"},
	"PROJECTS": {"Project", "Task", "Timesheet", "Project Template"},
	"MANUFACTURING": {"BOM", "Work Order", "Job Card", "Production Plan"},
	"POINT_OF_SALE": {"POS Profile", "POS Invoice", "POS Opening Entry", "POS Closing Entry"},
}
DOCTYPE_MODULE = {doctype: module for module, doctypes in MODULE_DOCTYPES.items() for doctype in doctypes}
WORKSPACE_MODULE = {workspace: module for module, workspaces in MODULE_WORKSPACES.items() for workspace in workspaces}


def load_effective_entitlements() -> EffectiveEntitlements | None:
	raw = frappe.db.get_default("contanext_effective_entitlements")
	if not raw:
		return None
	value = json.loads(raw)
	return EffectiveEntitlements(
		tenant_id=value["tenant_id"],
		sequence=int(value["sequence"]),
		mode=value["mode"],
		resources={key: int(limit) for key, limit in value["resources"].items()},
		modules={key: bool(enabled) for key, enabled in value["modules"].items()},
		canonical_payload="",
		fingerprint=value["fingerprint"],
	)


def validate_user(doc, method: str | None = None) -> None:
	if getattr(frappe.flags, "in_contanext_entitlement_sync", False):
		return
	entitlements = load_effective_entitlements()
	if not entitlements:
		return
	_validate_user_limit(doc, entitlements, force_new=method == "before_insert")
	_validate_user_roles(doc, entitlements)


def validate_company(doc, method: str | None = None) -> None:
	entitlements = load_effective_entitlements()
	if not entitlements or not doc.is_new():
		return
	with _entitlement_lock("companies"):
		used = len(frappe.db.sql("SELECT name FROM `tabCompany` FOR UPDATE"))
		limit = entitlements.resources["COMPANIES"]
		if used >= limit:
			_violation(entitlements, "COMPANIES", f"Company limit reached ({used}/{limit})")


def validate_module_operation(doc, method: str | None = None) -> None:
	entitlements = load_effective_entitlements()
	module = DOCTYPE_MODULE.get(doc.doctype)
	if entitlements and module and not entitlements.modules.get(module, False):
		_violation(entitlements, f"MODULE:{module}", f"Module {module} is not enabled for this tenant")


def has_module_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs):
	entitlements = load_effective_entitlements()
	if not entitlements:
		return True
	if doc.doctype == "Workspace":
		module = WORKSPACE_MODULE.get(doc.name) or WORKSPACE_MODULE.get(doc.get("label"))
	else:
		module = DOCTYPE_MODULE.get(doc.doctype)
	if module and not entitlements.modules.get(module, False):
		if entitlements.mode == MODE_ENFORCE:
			return False
		frappe.logger("contanext_connector").warning(
			{"event": "entitlement_permission_drift", "module": module, "doctype": doc.doctype, "name": doc.name}
		)
	return True


def module_permission_query(user: str | None = None, doctype: str | None = None) -> str:
	entitlements = load_effective_entitlements()
	module = DOCTYPE_MODULE.get(doctype or "")
	if entitlements and entitlements.mode == MODE_ENFORCE and module and not entitlements.modules.get(module, False):
		return "1=0"
	return ""


def get_entitlement_status() -> dict[str, Any]:
	entitlements = load_effective_entitlements()
	if not entitlements:
		return {"status": "UNCONFIGURED", "tenant_id": frappe.local.site}
	usage = {"USERS": _count_consuming_users(), "COMPANIES": int(frappe.db.count("Company"))}
	drift = _role_drift(entitlements)
	return {
		"status": "OVER_LIMIT" if any(usage[key] > entitlements.resources[key] for key in usage) else "ACTIVE",
		"tenant_id": entitlements.tenant_id,
		"sequence": entitlements.sequence,
		"mode": entitlements.mode,
		"fingerprint": entitlements.fingerprint,
		"usage": usage,
		"limits": entitlements.resources,
		"drift": drift,
		"disabled_modules": sorted(module for module, enabled in entitlements.modules.items() if not enabled),
	}


def reconcile_roles(entitlements: EffectiveEntitlements) -> list[dict[str, str]]:
	drift = _role_drift(entitlements)
	if entitlements.mode != MODE_ENFORCE:
		return drift
	by_user: dict[str, list[str]] = {}
	for item in drift:
		by_user.setdefault(item["user"], []).append(item["role"])
	for user_name, roles in by_user.items():
		user = frappe.get_doc("User", user_name)
		user.remove_roles(*roles)
	return drift


def observe_entitlements() -> None:
	status = get_entitlement_status()
	if status.get("status") == "UNCONFIGURED":
		return
	frappe.logger("contanext_connector").info({"event": "entitlement_observation", **status})


def filter_bootinfo(bootinfo) -> None:
	entitlements = load_effective_entitlements()
	if not entitlements:
		return
	disabled_modules = sorted(module for module, enabled in entitlements.modules.items() if not enabled)
	disabled_workspaces = {
		workspace for module in disabled_modules for workspace in MODULE_WORKSPACES.get(module, set())
	}
	bootinfo.contanext_entitlements = {
		"mode": entitlements.mode,
		"disabled_modules": disabled_modules,
		"disabled_workspaces": sorted(disabled_workspaces),
	}
	if entitlements.mode != MODE_ENFORCE:
		return
	for key in ("module_sidebars", "app_data", "dock", "desktop_icons"):
		if key in bootinfo:
			bootinfo[key] = _filter_navigation(bootinfo[key], disabled_workspaces)


def _filter_navigation(value, disabled_workspaces: set[str]):
	if isinstance(value, list):
		return [filtered for item in value if (filtered := _filter_navigation(item, disabled_workspaces)) is not None]
	if isinstance(value, dict):
		identity = {str(value.get(key, "")) for key in ("name", "label", "title", "workspace", "link_to")}
		if identity & disabled_workspaces:
			return None
		result = {}
		for key, item in value.items():
			filtered = _filter_navigation(item, disabled_workspaces)
			if filtered is not None:
				result[key] = filtered
		return result
	return value


def _validate_user_limit(doc, entitlements: EffectiveEntitlements, force_new: bool = False) -> None:
	if not _user_consumes(doc):
		return
	old = None if force_new or doc.is_new() else doc.get_doc_before_save()
	if old and _user_consumes(old):
		return
	with _entitlement_lock("users"):
		used = _count_consuming_users(exclude=doc.name)
		limit = entitlements.resources["USERS"]
		if used >= limit:
			_violation(entitlements, "USERS", f"System User limit reached ({used}/{limit})")


def _validate_user_roles(doc, entitlements: EffectiveEntitlements) -> None:
	roles = {row.role for row in (doc.get("roles") or [])}
	allowed = set()
	for module, enabled in entitlements.modules.items():
		if enabled:
			allowed.update(MODULE_ROLES.get(module, set()))
	unauthorized = sorted((roles & CONTROLLED_ROLES) - allowed)
	if unauthorized:
		_violation(entitlements, "ROLES", f"Roles not authorized by tenant entitlements: {', '.join(unauthorized)}")


def _role_drift(entitlements: EffectiveEntitlements) -> list[dict[str, Any]]:
	allowed = set()
	for module, enabled in entitlements.modules.items():
		if enabled:
			allowed.update(MODULE_ROLES.get(module, set()))
	rows = frappe.db.sql(
		"""SELECT parent, role FROM `tabHas Role`
		WHERE parenttype='User' AND role IN %(roles)s ORDER BY parent, role""",
		{"roles": tuple(sorted(CONTROLLED_ROLES))},
		as_dict=True,
	)
	return [{"user": row.parent, "role": row.role} for row in rows if row.role not in allowed]


def _count_consuming_users(exclude: str | None = None) -> int:
	conditions = ["u.enabled=1", "u.name NOT IN %(reserved)s"]
	params: dict[str, Any] = {"reserved": tuple(sorted(RESERVED_USERS))}
	if exclude:
		conditions.append("u.name<>%(exclude)s")
		params["exclude"] = exclude
	return len(
		frappe.db.sql(
			f"""SELECT u.name FROM `tabUser` u WHERE {' AND '.join(conditions)} AND
			(u.user_type='System User' OR EXISTS (
				SELECT 1 FROM `tabHas Role` hr JOIN `tabRole` r ON r.name=hr.role
				WHERE hr.parent=u.name AND hr.parenttype='User' AND COALESCE(r.desk_access,0)=1
			)) FOR UPDATE""",
			params,
		)
	)


def _user_consumes(doc) -> bool:
	identifier = doc.name or doc.get("email")
	if not bool(doc.enabled) or identifier in RESERVED_USERS:
		return False
	if doc.user_type == "System User":
		return True
	roles = [row.role for row in (doc.get("roles") or [])]
	return any(bool(frappe.db.get_value("Role", role, "desk_access")) for role in roles)


def _violation(entitlements: EffectiveEntitlements, key: str, message: str) -> None:
	frappe.logger("contanext_connector").warning(
		{"event": "entitlement_violation", "key": key, "mode": entitlements.mode, "message": message}
	)
	if entitlements.mode == MODE_ENFORCE:
		frappe.throw(message, title="ContaNext entitlement")


@contextmanager
def _entitlement_lock(resource: str) -> Iterator[None]:
	# MariaDB consistent reads can otherwise keep an older snapshot even after a
	# row-lock wait. A named connection lock remains held through commit/rollback,
	# and the DefaultValue row additionally fences code paths sharing a transaction.
	lock_name = f"contanext:{frappe.local.site}:{resource}"
	acquired = frappe.db.sql("SELECT GET_LOCK(%s, 15)", (lock_name,))[0][0]
	if acquired != 1:
		frappe.throw("Entitlement capacity is busy; retry the operation")
	released = False

	def release() -> None:
		nonlocal released
		if not released:
			released = True
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))

	frappe.db.after_commit.add(release)
	frappe.db.after_rollback.add(release)
	frappe.db.sql(
		"""SELECT defvalue FROM `tabDefaultValue`
		WHERE parent='__default' AND defkey=%s FOR UPDATE""",
		(f"contanext_entitlement_lock_{resource}",),
	)
	try:
		yield
	except Exception:
		raise
