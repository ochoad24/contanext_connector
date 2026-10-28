from __future__ import annotations

import json
import os
import sys

import frappe
from frappe.installer import update_site_config

from contanext_connector.entitlement_contract import EntitlementContractError, parse_contract, validate_transition
from contanext_connector.entitlements import reconcile_roles


MODULE_MANAGER_ROLES = {
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


def configure_tenant(country: str, time_zone: str, currency: str, language: str, admin_name: str, admin_email: str) -> dict[str, str]:
	"""Apply allow-listed settings and invite the initial administrator."""
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

	_configure_outgoing_email()

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
	invitation_key = f"contanext_initial_admin_invited:{admin_email.lower()}"
	if not frappe.db.get_default(invitation_key):
		_send_initial_admin_invitation(user, admin_name)
		frappe.db.set_default(invitation_key, "1")
	frappe.db.set_default("contanext_country", country)
	frappe.db.set_default("contanext_language", language)
	frappe.db.set_default("contanext_initial_admin", admin_email)
	frappe.db.commit()
	return {"status": "configured", "administrator": admin_email}


def resend_initial_admin_invitation(admin_email: str) -> dict[str, str]:
	"""Resend the branded invitation only to the configured initial administrator."""
	admin_email = admin_email.strip().lower()
	if not admin_email or frappe.db.get_default("contanext_initial_admin") != admin_email:
		frappe.throw("Initial administrator invitation rejected")
	user = frappe.get_doc("User", admin_email)
	_send_initial_admin_invitation(user, user.full_name)
	frappe.db.set_default(f"contanext_initial_admin_invited:{admin_email}", "1")
	frappe.db.commit()
	return {"status": "sent", "administrator": admin_email}


def _send_initial_admin_invitation(user, admin_name: str) -> None:
	link = user._reset_password()
	login_url = frappe.utils.get_url("/login")
	message = frappe.render_template(
		"""
		<div style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;color:#17302a">
			<div style="background:#17302a;color:white;padding:24px;border-radius:14px 14px 0 0">
				<h1 style="margin:0;font-size:26px">Bienvenido a ContaNext</h1>
			</div>
			<div style="padding:28px;border:1px solid #dce2dc;border-top:0;border-radius:0 0 14px 14px">
				<p>Hola {{ admin_name | e }},</p>
				<p>Tu espacio <strong>{{ tenant | e }}</strong> está listo. Fuiste registrado como administrador inicial con el usuario <strong>{{ email | e }}</strong>.</p>
				<p>Configura tu contraseña para activar el acceso:</p>
				<p style="margin:26px 0"><a href="{{ activation_link | e }}" style="display:inline-block;background:#17302a;color:white;text-decoration:none;padding:13px 20px;border-radius:9px;font-weight:bold">Configurar mi contraseña</a></p>
				<p style="font-size:13px;color:#65736e">El enlace es personal, de un solo uso y tiene vigencia limitada. Si expira, solicita uno nuevo desde la pantalla de inicio de sesión.</p>
				<p style="font-size:13px">Acceso posterior: <a href="{{ login_url | e }}">{{ login_url | e }}</a></p>
			</div>
		</div>
		""",
		{"admin_name": admin_name, "tenant": frappe.local.site, "email": user.email, "activation_link": link, "login_url": login_url},
	)
	frappe.sendmail(
		recipients=[user.email],
		subject="Bienvenido a ContaNext — configura tu acceso",
		message=message,
		now=True,
		reference_doctype="User",
		reference_name=user.name,
	)


def _configure_outgoing_email() -> None:
	address = os.environ.get("CONTANEXT_SMTP_ADDR", "").strip()
	host = os.environ.get("CONTANEXT_SMTP_HOST", "").strip()
	sender = os.environ.get("CONTANEXT_SMTP_FROM", "").strip()
	username = os.environ.get("CONTANEXT_SMTP_USER", "").strip()
	password = os.environ.get("CONTANEXT_SMTP_PASSWORD", "")
	if not address or not host or not sender:
		frappe.throw("Tenant SMTP configuration is unavailable")
	server, separator, port = address.rpartition(":")
	if not separator or not server or not port.isdigit():
		frappe.throw("Tenant SMTP address is invalid")
	require_tls = os.environ.get("CONTANEXT_SMTP_REQUIRE_TLS", "false").strip().lower() == "true"
	public_url_template = os.environ.get("CONTANEXT_TENANT_PUBLIC_URL_TEMPLATE", "").strip()
	if not public_url_template or "{domain}" not in public_url_template:
		frappe.throw("Tenant public URL template is unavailable")
	public_url = public_url_template.replace("{domain}", frappe.local.site)
	if not public_url.startswith(("http://", "https://")):
		frappe.throw("Tenant public URL is invalid")
	update_site_config("host_name", public_url)
	frappe.local.conf.host_name = public_url

	account_name = "ContaNext Outgoing"
	if frappe.db.exists("Email Account", account_name):
		account = frappe.get_doc("Email Account", account_name)
	else:
		account = frappe.new_doc("Email Account")
		account.email_account_name = account_name
	account.email_id = sender
	account.login_id_is_different = int(bool(username and username != sender))
	account.login_id = username if account.login_id_is_different else None
	account.password = password
	account.auth_method = "Basic"
	account.enable_incoming = 0
	account.enable_outgoing = 1
	account.default_outgoing = 1
	account.always_use_account_email_id_as_sender = 1
	account.smtp_server = host
	account.smtp_port = port
	account.use_tls = int(require_tls)
	account.use_ssl_for_outgoing = 0
	account.no_smtp_authentication = int(not username)
	account.awaiting_password = 0
	account.save(ignore_permissions=True)


def sync_entitlements(snapshot: str) -> dict[str, str]:
	"""Validate and persist a monotonic entitlement contract.

	Legacy wizard snapshots remain accepted once and start in OBSERVE mode. New
	control-plane payloads use schema 1.0 and a strictly monotonic sequence.
	"""
	try:
		contract = parse_contract(snapshot, frappe.local.site)
		current_sequence = int(frappe.db.get_default("contanext_entitlements_sequence") or -1)
		current_fingerprint = frappe.db.get_default("contanext_entitlements_fingerprint") or ""
		transition = validate_transition(current_sequence, current_fingerprint, contract)
	except (EntitlementContractError, json.JSONDecodeError, TypeError, ValueError) as exc:
		frappe.throw(str(exc), title="Invalid entitlement snapshot")
	value = json.loads(contract.canonical_payload)
	frappe.flags.in_contanext_entitlement_sync = True
	try:
		roles = _apply_initial_admin_roles(value) if _configured_initial_admin(value) else []
		drift = reconcile_roles(contract)
	finally:
		frappe.flags.in_contanext_entitlement_sync = False
	frappe.db.set_default("contanext_entitlement_lock_users", "1")
	frappe.db.set_default("contanext_entitlement_lock_companies", "1")
	frappe.db.set_default("contanext_entitlements_snapshot", contract.canonical_payload)
	frappe.db.set_default("contanext_effective_entitlements", json.dumps(contract.as_dict(), sort_keys=True, separators=(",", ":")))
	frappe.db.set_default("contanext_entitlements_sequence", str(contract.sequence))
	frappe.db.set_default("contanext_entitlements_fingerprint", contract.fingerprint)
	frappe.db.commit()
	return {
		"status": "unchanged" if transition == "UNCHANGED" else "synchronized",
		"fingerprint": contract.fingerprint,
		"sequence": str(contract.sequence),
		"mode": contract.mode,
		"administrator_roles": ",".join(roles),
		"role_drift_count": str(len(drift)),
	}


def entitlement_status() -> dict:
	"""Return non-secret consumption and drift telemetry to the restricted worker."""
	from contanext_connector.entitlements import get_entitlement_status

	return get_entitlement_status()


def migration_inventory() -> dict:
	"""Return a read-only, non-secret inventory for a controlled legacy migration."""
	from contanext_connector.entitlements import MODULE_DOCTYPES, RESERVED_USERS

	role_rows = frappe.db.sql(
		"""SELECT DISTINCT hr.role FROM `tabHas Role` hr
		JOIN `tabUser` u ON u.name=hr.parent
		WHERE hr.parenttype='User' AND u.enabled=1 AND u.name NOT IN %(reserved)s
		AND (u.user_type='System User' OR EXISTS (
			SELECT 1 FROM `tabHas Role` desk_hr JOIN `tabRole` desk_role ON desk_role.name=desk_hr.role
			WHERE desk_hr.parent=u.name AND desk_hr.parenttype='User' AND COALESCE(desk_role.desk_access,0)=1
		)) ORDER BY hr.role""",
		{"reserved": tuple(sorted(RESERVED_USERS))},
		as_dict=True,
	)
	roles = [str(row.role) for row in role_rows]
	role_set = set(roles)
	modules = sorted(module for module, expected in MODULE_MANAGER_ROLES.items() if role_set & expected)
	module_records = {}
	for module, doctypes in MODULE_DOCTYPES.items():
		module_records[module] = sum(int(frappe.db.count(doctype)) for doctype in doctypes if frappe.db.table_exists(doctype))
	users = len(
		frappe.db.sql(
			"""SELECT u.name FROM `tabUser` u WHERE u.enabled=1 AND u.name NOT IN %(reserved)s AND
			(u.user_type='System User' OR EXISTS (
				SELECT 1 FROM `tabHas Role` hr JOIN `tabRole` r ON r.name=hr.role
				WHERE hr.parent=u.name AND hr.parenttype='User' AND COALESCE(r.desk_access,0)=1
			))""",
			{"reserved": tuple(sorted(RESERVED_USERS))},
		)
	)
	file_rows = frappe.db.sql(
		"""SELECT COALESCE(is_private,0) AS is_private, COUNT(*) AS file_count,
		COALESCE(SUM(COALESCE(file_size,0)),0) AS file_bytes
		FROM `tabFile` GROUP BY COALESCE(is_private,0)""",
		as_dict=True,
	)
	public_count = private_count = file_bytes = 0
	for row in file_rows:
		count = int(row.file_count or 0)
		file_bytes += int(row.file_bytes or 0)
		if int(row.is_private or 0):
			private_count += count
		else:
			public_count += count
	physical_public_count, physical_private_count, physical_bytes = 0, 0, 0
	for is_private, path in (
		(False, frappe.get_site_path("public", "files")),
		(True, frappe.get_site_path("private", "files")),
	):
		if not os.path.isdir(path):
			continue
		for root, _, names in os.walk(path, followlinks=False):
			for name in names:
				full_path = os.path.join(root, name)
				try:
					stat = os.stat(full_path, follow_symlinks=False)
				except OSError:
					continue
				if not os.path.isfile(full_path) or os.path.islink(full_path):
					continue
				physical_bytes += int(stat.st_size)
				if is_private:
					physical_private_count += 1
				else:
					physical_public_count += 1
	return {
		"tenant_id": frappe.local.site,
		"users": users,
		"companies": int(frappe.db.count("Company")),
		"modules": modules,
		"roles": roles,
		"observed_module_records": module_records,
		"public_file_count": max(public_count, physical_public_count),
		"private_file_count": max(private_count, physical_private_count),
		"file_bytes": max(file_bytes, physical_bytes),
	}


def configure_file_storage_from_stdin() -> dict[str, str]:
	"""Persist a worker-delivered, tenant-limited R2 credential encrypted by Frappe.

	The restricted worker writes this payload to stdin so credential material is
	not present in process arguments, API responses or audit metadata.
	"""
	from contanext_connector.file_storage import SETTINGS_DOCTYPE
	from contanext_connector.object_storage import R2Configuration

	raw = sys.stdin.buffer.read(16 * 1024 + 1)
	if not raw or len(raw) > 16 * 1024:
		frappe.throw("File storage configuration payload is invalid")
	try:
		value = json.loads(raw)
		allowed = {
			"tenant_id", "endpoint", "region", "bucket_name", "object_prefix",
			"files_quota_bytes", "access_key_id", "secret_access_key",
		}
		if set(value) != allowed or value["tenant_id"] != frappe.local.site:
			raise ValueError
		quota = int(value["files_quota_bytes"])
		if quota <= 0:
			raise ValueError
		configuration = R2Configuration(
			endpoint=str(value["endpoint"]),
			region=str(value["region"]),
			bucket=str(value["bucket_name"]),
			object_prefix=str(value["object_prefix"]),
			access_key_id=str(value["access_key_id"]),
			secret_access_key=str(value["secret_access_key"]),
		).validate()
	except (KeyError, TypeError, ValueError, json.JSONDecodeError):
		frappe.throw("File storage configuration payload is invalid")
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	settings.enabled = 0
	settings.endpoint = configuration.endpoint
	settings.region = configuration.region
	settings.bucket_name = configuration.bucket
	settings.object_prefix = configuration.object_prefix
	settings.files_quota_bytes = str(quota)
	settings.access_key_id = configuration.access_key_id
	settings.secret_access_key = configuration.secret_access_key
	settings.save(ignore_permissions=True)
	settings.enabled = 1
	settings.save(ignore_permissions=True)
	frappe.clear_cache(doctype=SETTINGS_DOCTYPE)
	frappe.db.commit()
	return {"status": "configured", "tenant_id": frappe.local.site}


def apply_initial_admin_roles() -> dict[str, str]:
	"""Repair roles idempotently from the entitlement snapshot already stored in the tenant."""
	snapshot = frappe.db.get_default("contanext_entitlements_snapshot")
	if not snapshot:
		frappe.throw("Tenant entitlement snapshot is unavailable")
	value = json.loads(snapshot)
	roles = _apply_initial_admin_roles(value)
	frappe.db.commit()
	return {"status": "configured", "administrator_roles": ",".join(roles)}


def _apply_initial_admin_roles(snapshot: dict) -> list[str]:
	admin_email = _configured_initial_admin(snapshot)
	if not admin_email or not frappe.db.exists("User", admin_email):
		frappe.throw("Initial administrator is unavailable")
	modules = {str(code).strip().upper() for code in snapshot.get("configuration", {}).get("module_codes", [])}
	for item in snapshot.get("values", []):
		if item.get("kind") == "MODULE" and item.get("value") is True:
			modules.add(str(item.get("key", "")).strip().upper())
	for override in snapshot.get("overrides", []):
		if override.get("kind") == "MODULE" and override.get("value") is False:
			modules.discard(str(override.get("key", "")).strip().upper())
	roles = {"System Manager"}
	for module in modules:
		roles.update(MODULE_MANAGER_ROLES.get(module, set()))
	roles = {role for role in roles if frappe.db.exists("Role", role)}
	user = frappe.get_doc("User", admin_email)
	current = {row.role for row in user.roles}
	missing = sorted(roles - current)
	if missing:
		user.add_roles(*missing)
	return sorted(roles)


def _configured_initial_admin(snapshot: dict) -> str:
	admin_email = str(snapshot.get("initial_admin", {}).get("email", "")).strip().lower()
	if not admin_email:
		admin_email = str(frappe.db.get_default("contanext_initial_admin") or "").strip().lower()
	return admin_email
