from __future__ import annotations

import unittest
from unittest.mock import patch
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from contanext_connector.object_storage import R2Client, R2Configuration, normalize_prefix, object_key


class ObjectStorageTest(unittest.TestCase):
	def configuration(self) -> R2Configuration:
		return R2Configuration(
			endpoint="https://account.r2.cloudflarestorage.com",
			region="auto",
			bucket="cnx-0123456789abcdef0123456789abcdef",
			object_prefix="files/",
			access_key_id="access-key",
			secret_access_key="secret-key",
		)

	def test_object_keys_are_scoped_and_content_addressed(self):
		digest = "ab" * 32
		self.assertEqual(object_key("files/", False, digest), f"files/public/ab/{digest}")
		self.assertEqual(object_key("opaque/site/files/", True, digest), f"opaque/site/files/private/ab/{digest}")
		md5_digest = "cd" * 16
		self.assertEqual(object_key("files/", False, md5_digest), f"files/public/cd/{md5_digest}")
		for value in ("../files", "files//private", "/", "files/../../other"):
			with self.assertRaises(ValueError):
				normalize_prefix(value)

	def test_configuration_rejects_ssrf_and_invalid_buckets(self):
		for endpoint in (
			"http://account.r2.cloudflarestorage.com",
			"https://metadata.internal",
			"https://account.r2.cloudflarestorage.com/path",
			"https://user:password@account.r2.cloudflarestorage.com",
		):
			with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
				R2Configuration(**{**self.configuration().__dict__, "endpoint": endpoint}).validate()
		with self.assertRaises(ValueError):
			R2Configuration(**{**self.configuration().__dict__, "bucket": "Tenant Bucket"}).validate()

	def test_presigned_urls_are_short_lived_and_object_specific(self):
		client = R2Client(self.configuration())
		url = client.presigned_get_url(
			"files/private/ab/" + "ab" * 32,
			expires_seconds=120,
			now=datetime(2026, 10, 27, 12, 30, tzinfo=UTC),
		)
		parsed = urlsplit(url)
		query = parse_qs(parsed.query)
		self.assertEqual(parsed.hostname, "account.r2.cloudflarestorage.com")
		self.assertTrue(parsed.path.startswith("/cnx-0123456789abcdef0123456789abcdef/files/private/"))
		self.assertEqual(query["X-Amz-Expires"], ["120"])
		self.assertEqual(query["X-Amz-SignedHeaders"], ["host"])
		self.assertRegex(query["X-Amz-Signature"][0], r"^[0-9a-f]{64}$")
		with self.assertRaises(ValueError):
			client.presigned_get_url("files/private/object", expires_seconds=901)

	def test_keys_reject_traversal(self):
		client = R2Client(self.configuration())
		for key in ("../secret", "/absolute", "files//object", "files/./object"):
			with self.subTest(key=key), self.assertRaises(ValueError):
				client.presigned_get_url(key)
		with self.assertRaisesRegex(ValueError, "tenant scope"):
			client.presigned_get_url("other-tenant/files/private/object")

	def test_listing_is_restricted_to_configured_prefix(self):
		client = R2Client(self.configuration())
		page = b"""<?xml version="1.0" encoding="UTF-8"?>
		<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
		<IsTruncated>false</IsTruncated><Contents><Key>files/public/ab/object</Key><Size>42</Size></Contents>
		</ListBucketResult>"""
		with patch.object(client, "_request", return_value=page) as request:
			self.assertEqual(client.list_objects("files/"), [{"key": "files/public/ab/object", "size": 42}])
			self.assertEqual(request.call_args.kwargs["query"]["prefix"], "files/")
		outside = page.replace(b"files/public", b"other/public")
		with patch.object(client, "_request", return_value=outside), self.assertRaisesRegex(Exception, "escaped"):
			client.list_objects("files/")


if __name__ == "__main__":
	unittest.main()
