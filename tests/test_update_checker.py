from __future__ import annotations

# ruff: noqa: E402
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from packaging.version import Version

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wiki_cat_tool.core.update_checker import check_for_updates, parse_app_version
from wiki_cat_tool.constants import APP_VERSION, APP_DISPLAY_VERSION, display_app_version


class UpdateCheckerTests(unittest.TestCase):
    def test_internal_tag_is_newer_under_both_legacy_comparisons(self):
        self.assertEqual('2.10', APP_VERSION)
        self.assertEqual('2.1', APP_DISPLAY_VERSION)
        for previous in ('2.03', '2.04'):
            self.assertGreater(Version(APP_VERSION), Version(previous))
            self.assertGreater([int(part) for part in APP_VERSION.split('.')],
                               [int(part) for part in previous.split('.')])
        self.assertEqual('2.1', display_app_version('v2.10'))
        self.assertEqual('2.03', display_app_version('2.03'))

    def test_legacy_patch_versions_sort_before_2_1(self):
        ordered = ['2.0', '2.01', '2.02', '2.03', '2.04', '2.1', '2.1.1', '2.2', '2.10']
        parsed = [parse_app_version(value) for value in ordered]
        self.assertEqual(parsed, sorted(parsed))
        self.assertEqual(parse_app_version('v2.03'), parse_app_version('2.0.3'))
        self.assertEqual(parse_app_version('2.1'), parse_app_version('2.1.0'))

    def test_current_build_does_not_offer_legacy_release_as_an_update(self):
        response = Mock(status_code=200)
        response.json.return_value = dict(tag_name='2.03', html_url='https://example.org/2.03')
        with patch('wiki_cat_tool.core.update_checker.requests.get', return_value=response):
            self.assertIsNone(check_for_updates())

    def test_future_release_is_detected_and_same_release_is_skipped(self):
        response = Mock(status_code=200)
        with patch('wiki_cat_tool.core.update_checker.requests.get', return_value=response):
            response.json.return_value = dict(tag_name='v2.11', html_url='https://example.org/2.11')
            self.assertEqual(('2.11', 'https://example.org/2.11'), check_for_updates())
            response.json.return_value = dict(tag_name='2.10.0', html_url='https://example.org/2.10')
            self.assertIsNone(check_for_updates())


if __name__ == '__main__':
    unittest.main()
