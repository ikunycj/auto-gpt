# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountNoteTests(unittest.TestCase):
    def test_update_account_note_single_and_bulk(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            accounts_path.write_text('[{"id":1,"email":"a@test.com"},{"id":2,"email":"b@test.com"}]', encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts_path), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy_accounts.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                self.assertTrue(db.update_account_note(1, "备注A"))
                self.assertFalse(db.update_account_note(99, "不存在"))
                self.assertEqual(db.get_account(1)["note"], "备注A")
                self.assertTrue(db.get_account(1)["note_updated_at"])

                updated, skipped = db.update_accounts_note([1, 2, 99], "批量备注")
                self.assertEqual([x["id"] for x in updated], [1, 2])
                self.assertEqual(skipped, [{"id": 99, "reason": "账号不存在"}])
                self.assertEqual(db.get_account(1)["note"], "批量备注")
                self.assertEqual(db.get_account(2)["note"], "批量备注")


if __name__ == "__main__":
    unittest.main()
