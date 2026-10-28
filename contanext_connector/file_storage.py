from __future__ import annotations

import hashlib
import mimetypes
import os
from contextlib import contextmanager
from typing import Iterator

import frappe
from frappe.core.doctype.file.file import FILE_ENCODING_OPTIONS, OLE_FILE_SIGNATURE, File
from frappe.core.doctype.file.utils import get_content_hash
from frappe.utils import cint
from werkzeug.exceptions import HTTPException
from werkzeug.utils import redirect

from contanext_connector.object_storage import ObjectStorageError, R2Client, R2Configuration, object_key


SETTINGS_DOCTYPE = "ContaNext File Storage Settings"
SIGNED_URL_SECONDS = 120


class ContaNextFile(File):
	def get_content(self, encodings=None) -> bytes | str:
		if not _uses_object_storage(self):
			return super().get_content(encodings=encodings)
		if self.get("content"):
			return super().get_content(encodings=encodings)
		try:
			content = _client().get(_key_for_doc(self))
		except ObjectStorageError:
			if os.path.isfile(super().get_full_path()):
				return super().get_content(encodings=encodings)
			raise
		self._content = content
		if encodings is None:
			encodings = FILE_ENCODING_OPTIONS
		if not content.startswith(OLE_FILE_SIGNATURE):
			for encoding in encodings:
				try:
					self._content = content.decode(encoding)
					break
				except UnicodeDecodeError:
					continue
		return self._content

	def exists_on_disk(self):
		if not _uses_object_storage(self):
			return super().exists_on_disk()
		return _client().head(_key_for_doc(self)) or super().exists_on_disk()

	def validate_file_on_disk(self):
		if _uses_object_storage(self):
			if not self.exists_on_disk():
				frappe.throw(f"File {self.file_url} does not exist", IOError)
			return True
		return super().validate_file_on_disk()

	def generate_content_hash(self):
		if _uses_object_storage(self):
			if not self.content_hash:
				frappe.throw("Object-backed files require a content hash")
			return
		return super().generate_content_hash()

	def handle_is_private_changed(self):
		if not _uses_object_storage(self):
			return super().handle_is_private_changed()
		old = self.get_doc_before_save()
		if not old or cint(old.is_private) == cint(self.is_private):
			return
		client = _client()
		old_key = object_key(_settings().object_prefix, bool(old.is_private), self.content_hash)
		new_key = _key_for_doc(self)
		with _quota_lock():
			_check_quota(0 if _object_is_referenced(self.content_hash, self.is_private, exclude=self.name) else self.file_size)
			if not client.head(new_key):
				client.copy(old_key, new_key)
		if not _object_is_referenced(self.content_hash, old.is_private, exclude=self.name):
			frappe.db.after_commit.add(lambda: client.delete(old_key))
		self.file_url = _file_url(self.file_name, bool(self.is_private))


def write_file(*args, **kwargs):
	"""Frappe calls this hook with either a File document or legacy arguments."""
	if len(args) == 1 and isinstance(args[0], File):
		doc = args[0]
		if not storage_enabled():
			return doc.save_file_on_filesystem()
		content = _as_bytes(doc._content)
		return _store(doc.file_name, content, doc.content_type, bool(doc.is_private), doc=doc)

	if len(args) < 2:
		raise TypeError("write_file requires a File document or filename and content")
	filename, content = args[0], _as_bytes(args[1])
	if not storage_enabled():
		from frappe.utils.file_manager import save_file_on_filesystem

		return save_file_on_filesystem(filename, content, **kwargs)
	return _store(filename, content, kwargs.get("content_type"), bool(kwargs.get("is_private")))


def before_write_file(file_size: int) -> None:
	if storage_enabled() and int(file_size) < 0:
		frappe.throw("Invalid file size")


def delete_file_data_content(doc, only_thumbnail=False):
	if not _uses_object_storage(doc):
		return doc.delete_file_from_filesystem(only_thumbnail=only_thumbnail)
	if only_thumbnail:
		return
	key = _key_for_doc(doc)
	client = _client()
	if not client.head(key):
		return doc.delete_file_from_filesystem(only_thumbnail=only_thumbnail)

	def remove_object() -> None:
		try:
			client.delete(key)
		except Exception:
			frappe.logger("contanext_connector").exception(
				{"event": "file_object_delete_failed", "file": doc.name, "object_key_hash": hashlib.sha256(key.encode()).hexdigest()}
			)
		try:
			doc.delete_file_from_filesystem(only_thumbnail=only_thumbnail)
		except Exception:
			frappe.logger("contanext_connector").exception(
				{"event": "file_local_fallback_delete_failed", "file": doc.name}
			)

	frappe.db.after_commit.add(remove_object)


def serve_object_file() -> None:
	"""Redirect missing local file routes to a short-lived object-specific R2 URL."""
	if not storage_enabled() or not getattr(frappe.local, "request", None):
		return
	path = frappe.local.request.path
	if not path.startswith(("/files/", "/private/files/")):
		return
	file_url = path.split("?", 1)[0]
	row = frappe.db.get_value(
		"File",
		{"file_url": file_url, "is_folder": 0},
		["name", "is_private", "content_hash"],
		as_dict=True,
	)
	if not row or not row.content_hash:
		return
	doc = frappe.get_doc("File", row.name)
	if not _client().head(_key_for_doc(doc)):
		return
	if bool(row.is_private):
		doc.check_permission("read")
		frappe.utils.response.make_access_log(
			doctype="File", document=doc.name, file_type=os.path.splitext(doc.file_name or "")[-1][1:]
		)
	elif frappe.local.request.method not in {"GET", "HEAD"}:
		frappe.throw("Method not allowed", frappe.PermissionError)
	location = _client().presigned_get_url(_key_for_doc(doc), expires_seconds=SIGNED_URL_SECONDS)
	raise HTTPException(response=redirect(location, code=302))


def storage_enabled() -> bool:
	try:
		return bool(cint(frappe.get_cached_doc(SETTINGS_DOCTYPE).enabled))
	except Exception:
		return False


def migrate_local_files(dry_run: bool = True, after: str = "", limit: int = 100) -> dict:
	"""Copy local File blobs to R2 idempotently and verify their SHA-256."""
	limit = max(1, min(int(limit), 1000))
	rows = frappe.get_all(
		"File",
		filters={"is_folder": 0, "content_hash": ("is", "set"), "name": (">", after or "")},
		fields=["name", "file_name", "file_url", "is_private", "content_hash", "file_size"],
		order_by="name asc",
		limit=limit,
	)
	client = _client()
	result = {"dry_run": bool(dry_run), "scanned": 0, "copied": 0, "verified": 0, "missing_local": 0, "failed": 0, "last_name": after or ""}
	for row in rows:
		result["scanned"] += 1
		result["last_name"] = row.name
		key = object_key(_settings().object_prefix, bool(row.is_private), row.content_hash)
		try:
			if client.head(key):
				if get_content_hash(client.get(key)) != row.content_hash:
					raise ValueError("remote checksum mismatch")
				result["verified"] += 1
				continue
			path = frappe.get_site_path("private" if row.is_private else "public", "files", os.path.basename(row.file_url))
			if not os.path.isfile(path):
				result["missing_local"] += 1
				continue
			with open(path, "rb") as source:
				content = source.read()
			if get_content_hash(content) != row.content_hash:
				raise ValueError("local checksum mismatch")
			if not dry_run:
				client.put(key, content, mimetypes.guess_type(row.file_name)[0])
				remote_content = client.get(key)
				if (
					get_content_hash(remote_content) != row.content_hash
					or hashlib.sha256(remote_content).digest() != hashlib.sha256(content).digest()
				):
					raise ValueError("uploaded checksum mismatch")
				result["copied"] += 1
				result["verified"] += 1
		except Exception:
			result["failed"] += 1
			frappe.logger("contanext_connector").exception({"event": "file_migration_item_failed", "file": row.name})
	return result


def reconcile_file_storage(delete_orphans: bool = False) -> dict:
	"""Compare File metadata with R2 and report usage without mutating records."""
	settings = _settings()
	client = _client(settings)
	objects = client.list_objects(settings.object_prefix)
	remote = {str(item["key"]): int(item["size"]) for item in objects}
	rows = frappe.get_all(
		"File",
		filters={"is_folder": 0, "content_hash": ("is", "set")},
		fields=["name", "is_private", "content_hash", "file_size"],
	)
	expected: dict[str, int] = {}
	missing: list[str] = []
	size_mismatch: list[str] = []
	for row in rows:
		key = object_key(settings.object_prefix, bool(row.is_private), row.content_hash)
		expected[key] = max(expected.get(key, 0), int(row.file_size or 0))
		if key not in remote:
			missing.append(row.name)
		elif remote[key] != int(row.file_size or 0):
			size_mismatch.append(row.name)
	orphans = sorted(set(remote) - set(expected))
	logical_used_bytes = sum(remote[key] for key in expected if key in remote)
	physical_used_bytes = sum(remote.values())
	deleted_orphans = 0
	if delete_orphans:
		for key in orphans:
			client.delete(key)
			deleted_orphans += 1
		orphan_set = set(orphans)
		remote = {key: size for key, size in remote.items() if key not in orphan_set}
		orphans = []
		physical_used_bytes = sum(remote.values())
	result = {
		"status": "OK" if not missing and not size_mismatch and not orphans else "DRIFT",
		"used_bytes": logical_used_bytes,
		"physical_used_bytes": physical_used_bytes,
		"object_count": len(remote),
		"expected_object_count": len(expected),
		"missing_files": sorted(missing),
		"size_mismatch_files": sorted(size_mismatch),
		"orphan_object_count": len(orphans),
		"deleted_orphan_count": deleted_orphans,
		"orphan_key_hashes": [hashlib.sha256(key.encode()).hexdigest() for key in orphans[:100]],
	}
	settings.reconciled_used_bytes = str(logical_used_bytes)
	settings.last_reconciled_at = frappe.utils.now_datetime()
	settings.save(ignore_permissions=True)
	frappe.clear_cache(doctype=SETTINGS_DOCTYPE)
	return result


def scheduled_reconcile_file_storage() -> None:
	if not storage_enabled():
		return
	try:
		result = reconcile_file_storage()
		frappe.logger("contanext_connector").info({"event": "file_storage_reconciled", **result})
	except Exception:
		frappe.logger("contanext_connector").exception({"event": "file_storage_reconciliation_failed"})


def _store(filename: str, content: bytes, content_type: str | None, is_private: bool, doc=None) -> dict[str, str]:
	settings = _settings()
	digest = get_content_hash(content)
	filename = _unique_filename(filename, digest, is_private)
	key = object_key(settings.object_prefix, is_private, digest)
	with _quota_lock():
		if not _object_is_referenced(digest, is_private):
			_check_quota(len(content))
		client = _client(settings)
		if not client.head(key):
			client.put(key, content, content_type or mimetypes.guess_type(filename)[0])
	if doc is not None:
		doc.file_name = filename
		doc.content_hash = digest
		doc.file_size = len(content)
		doc.file_url = _file_url(filename, is_private)
		frappe.db.after_rollback.add(lambda: _delete_unreferenced_after_rollback(key, digest, is_private))
	return {"file_name": os.path.basename(filename), "file_url": _file_url(filename, is_private)}


def _delete_unreferenced_after_rollback(key: str, digest: str, is_private: bool) -> None:
	if _object_is_referenced(digest, is_private):
		return
	try:
		_client().delete(key)
	except Exception:
		frappe.logger("contanext_connector").exception({"event": "file_object_rollback_cleanup_failed"})


def _check_quota(additional_bytes: int) -> None:
	rows = frappe.db.sql(
		"""SELECT field,value FROM `tabSingles`
		WHERE doctype=%s AND field IN ('files_quota_bytes','reconciled_used_bytes') FOR UPDATE""",
		(SETTINGS_DOCTYPE,),
	)
	values = {field: value for field, value in rows}
	quota = int(values.get("files_quota_bytes") or 0)
	used = max(_database_usage(), int(values.get("reconciled_used_bytes") or 0))
	if quota <= 0 or additional_bytes < 0 or used + additional_bytes > quota:
		frappe.throw(
			f"File storage quota reached ({used}/{quota} bytes used)",
			title="ContaNext storage quota",
		)


def _database_usage() -> int:
	rows = frappe.db.sql(
		"""SELECT content_hash,is_private,file_size FROM `tabFile`
		WHERE is_folder=0 AND content_hash IS NOT NULL AND content_hash<>'' FOR UPDATE"""
	)
	objects: dict[tuple[str, int], int] = {}
	for content_hash, is_private, file_size in rows:
		key = (content_hash, int(is_private or 0))
		objects[key] = max(objects.get(key, 0), int(file_size or 0))
	return sum(objects.values())


def _object_is_referenced(digest: str, is_private: bool | int, exclude: str | None = None) -> bool:
	filters = {"content_hash": digest, "is_private": cint(is_private), "is_folder": 0}
	if exclude:
		filters["name"] = ("!=", exclude)
	return bool(frappe.db.exists("File", filters))


def _settings():
	# Storage policy can change while long-lived web and worker processes retain
	# document cache. Quota enforcement must always read the committed singleton.
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	if not cint(settings.enabled):
		frappe.throw("ContaNext file object storage is disabled")
	return settings


def _client(settings=None) -> R2Client:
	settings = settings or _settings()
	return R2Client(
		R2Configuration(
			endpoint=settings.endpoint,
			region=settings.region,
			bucket=settings.bucket_name,
			object_prefix=settings.object_prefix,
			access_key_id=settings.get_password("access_key_id", raise_exception=False),
			secret_access_key=settings.get_password("secret_access_key", raise_exception=False),
		)
	)


def _key_for_doc(doc) -> str:
	return object_key(_settings().object_prefix, bool(doc.is_private), doc.content_hash)


def _uses_object_storage(doc) -> bool:
	return bool(storage_enabled() and not doc.is_folder and doc.content_hash and doc.file_url and doc.file_url.startswith(("/files/", "/private/files/")))


def _file_url(filename: str, is_private: bool) -> str:
	filename = os.path.basename(filename)
	return ("/private/files/" if is_private else "/files/") + filename


def _unique_filename(filename: str, digest: str, is_private: bool) -> str:
	filename = os.path.basename(filename)
	prefix = "/private/files/" if is_private else "/files/"
	if not frappe.db.exists("File", {"file_url": prefix + filename}):
		return filename
	stem, extension = os.path.splitext(filename)
	candidate = f"{stem}-{digest[-6:]}{extension}"
	index = 1
	while frappe.db.exists("File", {"file_url": prefix + candidate}):
		candidate = f"{stem}-{digest[-6:]}-{index}{extension}"
		index += 1
	return candidate


def _as_bytes(content: bytes | str) -> bytes:
	return content.encode() if isinstance(content, str) else bytes(content)


@contextmanager
def _quota_lock() -> Iterator[None]:
	lock_name = f"contanext:{frappe.local.site}:file-storage-quota"
	acquired = frappe.db.sql("SELECT GET_LOCK(%s, 30)", (lock_name,))[0][0]
	if acquired != 1:
		frappe.throw("File storage capacity is busy; retry the upload")
	released = False

	def release() -> None:
		nonlocal released
		if not released:
			released = True
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))

	frappe.db.after_commit.add(release)
	frappe.db.after_rollback.add(release)
	try:
		yield
	except Exception:
		release()
		raise
