from __future__ import annotations

# ruff: noqa: E402
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wiki_cat_tool.core.update_checker import check_for_updates, parse_app_version
from wiki_cat_tool.constants import APP_VERSION, APP_DISPLAY_VERSION, display_app_version


class UpdateCheckerTests(unittest.TestCase):
    def test_release_display_uses_canonical_names_for_legacy_tags(self):
        self.assertEqual('2.2', APP_VERSION)
        self.assertEqual(APP_VERSION, APP_DISPLAY_VERSION)
        self.assertEqual('2.1', display_app_version('v2.10'))
        self.assertEqual('2.1.1', display_app_version('v2.10.1'))
        self.assertEqual('2.10.2', display_app_version('v2.10.2'))
        self.assertEqual('2.03', display_app_version('2.03'))

    def test_legacy_patch_versions_sort_before_2_1(self):
        ordered = ['2.0', '2.01', '2.04', '2.09', '2.10', '2.10.1', '2.2', '2.9', '2.10.0']
        parsed = [parse_app_version(value) for value in ordered]
        for older, newer in zip(parsed, parsed[1:]):
            self.assertLess(older, newer)
        for legacy, canonical in [('v2.09', '2.0.9'), ('2.10', '2.1.0'), ('v2.10.1', '2.1.1')]:
            with self.subTest(tag=legacy):
                self.assertEqual(parse_app_version(legacy), parse_app_version(canonical))

    def test_current_build_does_not_offer_legacy_release_as_an_update(self):
        response = Mock(status_code=200)
        with patch('wiki_cat_tool.core.update_checker.requests.get', return_value=response):
            for tag in ('2.09', '2.10', '2.10.1'):
                with self.subTest(tag=tag):
                    response.json.return_value = dict(tag_name=tag, html_url=f'https://example.org/{tag}')
                    self.assertIsNone(check_for_updates())

    def test_future_release_is_detected_and_same_release_is_skipped(self):
        response = Mock(status_code=200)
        with patch('wiki_cat_tool.core.update_checker.requests.get', return_value=response):
            response.json.return_value = dict(tag_name='v2.2.1', html_url='https://example.org/2.2.1')
            self.assertEqual(('2.2.1', 'https://example.org/2.2.1'), check_for_updates())
            for tag in ('2.2', 'v2.2.0'):
                with self.subTest(tag=tag):
                    response.json.return_value = dict(tag_name=tag, html_url=f'https://example.org/{tag}')
                    self.assertIsNone(check_for_updates())

    def test_normal_tag_updates_legacy_version_with_new_checker(self):
        response = Mock(status_code=200)
        response.json.return_value = dict(tag_name='2.2', html_url='https://example.org/2.2')
        with (
            patch('wiki_cat_tool.core.update_checker.APP_VERSION', '2.10.1'),
            patch('wiki_cat_tool.core.update_checker.requests.get', return_value=response),
        ):
            self.assertEqual(('2.2', 'https://example.org/2.2'), check_for_updates())


if __name__ == '__main__':
    unittest.main()
