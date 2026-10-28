from __future__ import annotations

import hashlib
import hmac
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime


class ObjectStorageError(RuntimeError):
	pass


_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_HASH_PATTERN = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{64})$")


def normalize_prefix(value: str) -> str:
	parts = value.strip().strip("/").split("/")
	if not parts or any(not part or part in {".", ".."} for part in parts):
		raise ValueError("Invalid object prefix")
	if any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts):
		raise ValueError("Invalid object prefix")
	return "/".join(parts) + "/"


def object_key(prefix: str, is_private: bool, content_hash: str) -> str:
	content_hash = content_hash.strip().lower()
	if not _HASH_PATTERN.fullmatch(content_hash):
		raise ValueError("Invalid content hash")
	visibility = "private" if is_private else "public"
	return f"{normalize_prefix(prefix)}{visibility}/{content_hash[:2]}/{content_hash}"


@dataclass(frozen=True)
class R2Configuration:
	endpoint: str
	region: str
	bucket: str
	object_prefix: str
	access_key_id: str
	secret_access_key: str

	def validate(self) -> "R2Configuration":
		endpoint = urllib.parse.urlsplit(self.endpoint)
		if (
			endpoint.scheme != "https"
			or endpoint.path not in {"", "/"}
			or endpoint.query
			or endpoint.fragment
			or not (endpoint.hostname or "").lower().endswith(".r2.cloudflarestorage.com")
		):
			raise ValueError("R2 endpoint is not an allowed Cloudflare endpoint")
		if endpoint.username or endpoint.password or endpoint.port not in {None, 443}:
			raise ValueError("R2 endpoint contains unsupported authority fields")
		if not _BUCKET_PATTERN.fullmatch(self.bucket):
			raise ValueError("Invalid R2 bucket")
		if not self.region or len(self.region) > 64:
			raise ValueError("Invalid R2 region")
		normalize_prefix(self.object_prefix)
		if not self.access_key_id or not self.secret_access_key:
			raise ValueError("R2 credentials are incomplete")
		return self


class R2Client:
	def __init__(self, configuration: R2Configuration, timeout: float = 30.0):
		self.configuration = configuration.validate()
		self.timeout = timeout

	def put(self, key: str, content: bytes, content_type: str | None = None) -> None:
		self._require_tenant_key(key)
		headers = {"content-type": content_type or "application/octet-stream"}
		self._request("PUT", key, body=content, headers=headers, expected={200})

	def get(self, key: str) -> bytes:
		self._require_tenant_key(key)
		return self._request("GET", key, expected={200})

	def head(self, key: str) -> bool:
		self._require_tenant_key(key)
		try:
			self._request("HEAD", key, expected={200})
			return True
		except ObjectStorageError as exc:
			if exc.args and exc.args[0] == "R2 object not found":
				return False
			raise

	def delete(self, key: str) -> None:
		self._require_tenant_key(key)
		self._request("DELETE", key, expected={204, 200})

	def copy(self, source_key: str, target_key: str) -> None:
		self._require_tenant_key(source_key)
		self._require_tenant_key(target_key)
		source = "/" + urllib.parse.quote(self.configuration.bucket, safe="") + "/" + urllib.parse.quote(source_key, safe="/")
		self._request("PUT", target_key, headers={"x-amz-copy-source": source}, expected={200})

	def list_objects(self, prefix: str) -> list[dict[str, int | str]]:
		prefix = normalize_prefix(prefix)
		if not prefix.startswith(normalize_prefix(self.configuration.object_prefix)):
			raise ValueError("Object prefix escapes tenant scope")
		continuation = ""
		objects: list[dict[str, int | str]] = []
		while True:
			query = {"list-type": "2", "prefix": prefix}
			if continuation:
				query["continuation-token"] = continuation
			payload = None
			for attempt in range(3):
				try:
					payload = self._request("GET", "", query=query, expected={200})
					break
				except ObjectStorageError as exc:
					if "InternalError" not in str(exc) or attempt == 2:
						raise
					time.sleep(0.25 * (attempt + 1))
			assert payload is not None
			try:
				root = ET.fromstring(payload)
			except ET.ParseError:
				raise ObjectStorageError("R2 listing response is invalid") from None
			namespace = ""
			if root.tag.startswith("{"):
				namespace = root.tag.split("}", 1)[0] + "}"
			for item in root.findall(f"{namespace}Contents"):
				key = item.findtext(f"{namespace}Key") or ""
				size = item.findtext(f"{namespace}Size") or "0"
				if not key.startswith(prefix):
					raise ObjectStorageError("R2 listing escaped the tenant prefix")
				objects.append({"key": key, "size": int(size)})
			truncated = (root.findtext(f"{namespace}IsTruncated") or "false").lower() == "true"
			if not truncated:
				return objects
			continuation = root.findtext(f"{namespace}NextContinuationToken") or ""
			if not continuation:
				raise ObjectStorageError("R2 listing continuation is missing")

	def presigned_get_url(self, key: str, expires_seconds: int = 120, now: datetime | None = None) -> str:
		self._require_tenant_key(key)
		if expires_seconds < 1 or expires_seconds > 900:
			raise ValueError("Signed URL expiration must be between 1 and 900 seconds")
		now = (now or datetime.now(UTC)).astimezone(UTC)
		date = now.strftime("%Y%m%d")
		amz_date = now.strftime("%Y%m%dT%H%M%SZ")
		scope = f"{date}/{self.configuration.region}/s3/aws4_request"
		query = {
			"X-Amz-Algorithm": "AWS4-HMAC-SHA256",
			"X-Amz-Credential": f"{self.configuration.access_key_id}/{scope}",
			"X-Amz-Date": amz_date,
			"X-Amz-Expires": str(expires_seconds),
			"X-Amz-SignedHeaders": "host",
		}
		path = self._path(key)
		canonical_query = urllib.parse.urlencode(sorted(query.items()), quote_via=urllib.parse.quote)
		canonical_request = "\n".join(("GET", path, canonical_query, f"host:{self._host()}\n", "host", "UNSIGNED-PAYLOAD"))
		string_to_sign = "\n".join(("AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()))
		query["X-Amz-Signature"] = hmac.new(self._signing_key(date), string_to_sign.encode(), hashlib.sha256).hexdigest()
		return self.configuration.endpoint.rstrip("/") + path + "?" + urllib.parse.urlencode(
			sorted(query.items()), quote_via=urllib.parse.quote
		)

	def _request(
		self,
		method: str,
		key: str,
		body: bytes = b"",
		headers: dict[str, str] | None = None,
		query: dict[str, str] | None = None,
		expected: set[int] | None = None,
	) -> bytes:
		now = datetime.now(UTC)
		date = now.strftime("%Y%m%d")
		amz_date = now.strftime("%Y%m%dT%H%M%SZ")
		payload_hash = hashlib.sha256(body).hexdigest()
		request_headers = {key.lower(): value.strip() for key, value in (headers or {}).items()}
		request_headers.update({"host": self._host(), "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date})
		signed_header_names = sorted(request_headers)
		canonical_headers = "".join(f"{name}:{request_headers[name]}\n" for name in signed_header_names)
		canonical_query = urllib.parse.urlencode(sorted((query or {}).items()), quote_via=urllib.parse.quote)
		canonical_request = "\n".join(
			(method, self._path(key), canonical_query, canonical_headers, ";".join(signed_header_names), payload_hash)
		)
		scope = f"{date}/{self.configuration.region}/s3/aws4_request"
		string_to_sign = "\n".join(
			("AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest())
		)
		signature = hmac.new(self._signing_key(date), string_to_sign.encode(), hashlib.sha256).hexdigest()
		request_headers["authorization"] = (
			f"AWS4-HMAC-SHA256 Credential={self.configuration.access_key_id}/{scope}, "
			f"SignedHeaders={';'.join(signed_header_names)}, Signature={signature}"
		)
		url = self.configuration.endpoint.rstrip("/") + self._path(key)
		if canonical_query:
			url += "?" + canonical_query
		request = urllib.request.Request(
			url,
			data=body if method in {"PUT", "POST"} else None,
			headers=request_headers,
			method=method,
		)
		try:
			with urllib.request.urlopen(request, timeout=self.timeout) as response:
				if expected and response.status not in expected:
					raise ObjectStorageError("R2 operation failed")
				return response.read()
		except urllib.error.HTTPError as exc:
			if exc.code == 404:
				raise ObjectStorageError("R2 object not found") from None
			error_code = ""
			try:
				root = ET.fromstring(exc.read(64 * 1024))
				error_code = (root.findtext("Code") or "").strip()
			except (ET.ParseError, OSError):
				pass
			suffix = f" ({error_code})" if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", error_code) else ""
			raise ObjectStorageError(f"R2 operation failed with status {exc.code}{suffix}") from None
		except (urllib.error.URLError, TimeoutError, OSError):
			raise ObjectStorageError("R2 operation failed") from None

	def _host(self) -> str:
		return urllib.parse.urlsplit(self.configuration.endpoint).hostname or ""

	def _require_tenant_key(self, key: str) -> None:
		if not key.startswith(normalize_prefix(self.configuration.object_prefix)):
			raise ValueError("Object key escapes tenant scope")

	def _path(self, key: str) -> str:
		if key == "":
			return "/" + urllib.parse.quote(self.configuration.bucket, safe="")
		if key.startswith("/") or any(part in {"", ".", ".."} for part in key.split("/")):
			raise ValueError("Invalid object key")
		return "/" + urllib.parse.quote(self.configuration.bucket, safe="") + "/" + urllib.parse.quote(key, safe="/")

	def _signing_key(self, date: str) -> bytes:
		date_key = hmac.new(("AWS4" + self.configuration.secret_access_key).encode(), date.encode(), hashlib.sha256).digest()
		region_key = hmac.new(date_key, self.configuration.region.encode(), hashlib.sha256).digest()
		service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
		return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()
