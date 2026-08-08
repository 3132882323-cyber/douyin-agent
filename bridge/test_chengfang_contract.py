import unittest

from chengfang_contract import assess_page_fingerprint, build_chengfang_contract_registry


class ChengfangContractTests(unittest.TestCase):
    def test_registry_is_empty_unverified_and_has_no_write_contract(self):
        registry = build_chengfang_contract_registry()
        self.assertFalse(registry["verified"])
        self.assertEqual([], registry["pages"])
        self.assertEqual([], registry["fields"])
        self.assertEqual([], registry["selectors"])
        self.assertEqual([], registry["write_capabilities"])

    def test_visible_labels_cannot_match_without_verified_fingerprint(self):
        result = assess_page_fingerprint({"visible_labels": ["乘方", "综合 ROI"]})
        self.assertFalse(result["matched"])
        self.assertFalse(result["verified"])
        self.assertEqual("NO_VERIFIED_PAGE_FINGERPRINT", result["reason"])


if __name__ == "__main__":
    unittest.main()
