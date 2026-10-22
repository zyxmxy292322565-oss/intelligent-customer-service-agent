"""Policy knowledge-base administration is protected at the backend boundary."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from auth.jwt import create_access_token


ADMIN_KEY = "test-only-admin-key-with-more-than-32-characters"


class PolicyAdminApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.settings = patch(
            "auth.admin_dependency.get_settings",
            return_value=SimpleNamespace(admin_api_key=ADMIN_KEY),
        )
        self.settings.start()
        self.addCleanup(self.settings.stop)

    @staticmethod
    def admin_headers():
        return {"X-Admin-Key": ADMIN_KEY}

    def test_admin_can_list_documents_and_jwt_cannot_access_them(self):
        records = [{
            "ID": 7,
            "文件名": "耳机售后政策.pdf",
            "文件hash": "hash-7",
            "文件类型": "pdf",
            "分类": "product_policy",
            "商品范围": "earphone",
            "版本": "2026.10",
            "创建人": "admin_workbench",
            "文本块数量": 9,
            "上传时间": "2026-10-13 10:00:00",
            "更新时间": "2026-10-13 10:00:00",
        }]
        with patch("main.list_policy_documents", return_value=records) as listing:
            denied = self.client.get(
                "/admin/policy/documents",
                headers={"Authorization": f"Bearer {create_access_token(1)}"},
            )
            allowed = self.client.get("/admin/policy/documents", headers=self.admin_headers())

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        document = allowed.json()["data"]["documents"][0]
        self.assertEqual(document["id"], 7)
        self.assertEqual(document["category"], "product_policy")
        self.assertEqual(document["chunk_count"], 9)
        listing.assert_called_once()

    def test_admin_upload_proxies_file_and_management_metadata(self):
        rag_result = {"success": True, "file_hash": "hash-8", "chunk_count": 3, "index_count": 21,
                      "deleted_cache_count": 2}
        with patch("main.upload_policy_document", return_value=rag_result) as upload:
            response = self.client.post(
                "/admin/policy/documents",
                headers=self.admin_headers(),
                files={"file": ("policy.txt", b"\xe9\x80\x80\xe8\xb4\xa7\xe6\x94\xbf\xe7\xad\x96", "text/plain")},
                data={"category": "return_policy", "product_scope": "earphone", "version": "2026.10"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["chunk_count"], 3)
        self.assertEqual(upload.call_args.kwargs["filename"], "policy.txt")
        self.assertEqual(upload.call_args.kwargs["category"], "return_policy")
        self.assertEqual(upload.call_args.kwargs["product_scope"], "earphone")
        self.assertEqual(upload.call_args.kwargs["version"], "2026.10")
        self.assertEqual(upload.call_args.kwargs["created_by"], "admin_workbench")

    def test_admin_delete_uses_document_id_and_preserves_rag_result(self):
        rag_result = {"success": True, "deleted_chunk_count": 3, "deleted_cache_count": 2,
                      "rag_status": {"index_count": 18}}
        with patch("main.delete_policy_document", return_value=rag_result) as deletion:
            response = self.client.delete("/admin/policy/documents/7", headers=self.admin_headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["rag_status"]["index_count"], 18)
        deletion.assert_called_once_with(7)

    def test_invalid_policy_metadata_is_rejected_before_rag_call(self):
        with patch("main.upload_policy_document") as upload:
            response = self.client.post(
                "/admin/policy/documents",
                headers=self.admin_headers(),
                files={"file": ("policy.txt", b"content", "text/plain")},
                data={"category": "unknown", "product_scope": "earphone", "version": "2026.10"},
            )
        self.assertEqual(response.status_code, 422)
        upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
