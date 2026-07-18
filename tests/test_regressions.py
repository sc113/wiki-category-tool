from __future__ import annotations

# The test suite is runnable both from the package directory and its parent.
# ruff: noqa: E402

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from wiki_cat_tool.core.api_client import APIRequestError, WikimediaAPIClient
from wiki_cat_tool.core.namespace_manager import NamespaceManager
import wiki_cat_tool.core.redundant_category_logic as redundant_logic
from wiki_cat_tool.core.template_manager import TemplateManager
from wiki_cat_tool.workers.base_worker import BaseWorker
from wiki_cat_tool.workers.category_content_sync_worker import (
    CategoryContentSyncWorker,
)
import wiki_cat_tool.workers.create_worker as create_worker_module
from wiki_cat_tool.workers.create_worker import _format_summary as format_create_summary
from wiki_cat_tool.workers.parse_worker import ParseWorker
import wiki_cat_tool.workers.rename_worker as rename_worker_module
from wiki_cat_tool.workers.replace_worker import _format_summary as format_replace_summary


class _NamespaceAPI:
    def get_namespace_info(self, _family: str, _lang: str) -> dict:
        return {}


class RegressionTests(unittest.TestCase):
    def test_category_reordering_preserves_include_scopes(self):
        original_aliases = redundant_logic.category_prefix_aliases
        original_prefix = redundant_logic.get_policy_prefix
        self.addCleanup(
            setattr,
            redundant_logic,
            "category_prefix_aliases",
            original_aliases,
        )
        self.addCleanup(
            setattr,
            redundant_logic,
            "get_policy_prefix",
            original_prefix,
        )
        redundant_logic.category_prefix_aliases = lambda _family, _lang: ("Category",)
        redundant_logic.get_policy_prefix = (
            lambda _family, _lang, _ns, _default: "Category:"
        )

        text = (
            "<noinclude>Before\n[[Category:Hidden]]\n"
            "[[Category:Broad]]</noinclude>\n"
            "Body\n[[Category:Precise]]\n[[Category:Root]]"
        )
        new_text, found, _names = redundant_logic.extract_and_filter_categories(
            text,
            {"Precise": {"Broad"}},
            {"Precise", "Broad", "Hidden", "Root"},
            "wikipedia",
            "en",
        )

        self.assertEqual({"Broad": "Precise"}, found)
        self.assertIn("<noinclude>Before\n\n[[Category:Hidden]]</noinclude>", new_text)
        self.assertNotIn("[[Category:Broad]]", new_text)
        self.assertTrue(new_text.endswith("[[Category:Precise]]\n[[Category:Root]]"))

    def test_cached_outer_template_rule_does_not_modify_nested_template(self):
        manager = TemplateManager.__new__(TemplateManager)
        manager._rules_file_path = None
        manager._rules_mtime = None
        manager.auto_skip_templates = set()
        manager.auto_confirm_direct_all = False
        manager.auto_skip_direct_all = False
        manager._display_names = {}
        key = manager._norm_tmpl_key("Outer", "wikipedia", "en")
        manager.template_auto_cache = {
            key: {
                "auto": "approve",
                "rules": [
                    {
                        "type": "named",
                        "param": "a",
                        "from": "b",
                        "to": "wrong",
                        "auto": "approve",
                    }
                ],
            }
        }

        text = "{{Outer|x={{Inner|a=b}}|y=z}}"
        result, changes = manager.apply_cached_template_rules(
            text, "wikipedia", "en"
        )

        self.assertEqual(text, result)
        self.assertEqual(0, changes)

    def test_namespace_selection_does_not_double_prefix_existing_namespace(self):
        manager = NamespaceManager(_NamespaceAPI())
        manager.ns_cache[("wikipedia", "en")] = {
            10: {"primary": "Template:", "all": {"template:"}},
            14: {"primary": "Category:", "all": {"category:"}},
        }
        self.assertEqual(
            "Template:Infobox",
            manager.normalize_title_by_selection(
                "Template:Infobox", "wikipedia", "en", 14
            ),
        )
        self.assertEqual(
            "Category:Physics",
            manager.normalize_title_by_selection(
                "Physics", "wikipedia", "en", "category"
            ),
        )

    def test_multi_digit_summary_placeholder_is_atomic(self):
        content = "\n".join(
            [
                "first",
                "second",
                "third",
                "fourth",
                "fifth",
                "sixth",
                "seventh",
                "eighth",
                "ninth",
                "tenth",
            ]
        )
        self.assertEqual("value=tenth", format_create_summary("value=$10", content))
        self.assertEqual("value=tenth", format_replace_summary("value=$10", content))

    def test_batch_api_failure_is_not_returned_as_empty_success(self):
        client = WikimediaAPIClient()
        client._rate_wait = Mock()
        client.session.post = Mock(side_effect=RuntimeError("network down"))

        with self.assertRaises(APIRequestError):
            client.fetch_contents_batch(
                ["Example"], "auto", lang="en", family="wikipedia", retries=2
            )

    def test_existing_category_accepts_aliases_and_underscore_titles(self):
        worker = CategoryContentSyncWorker.__new__(CategoryContentSyncWorker)
        worker.family = "wikipedia"
        worker.target_lang = "ru"
        worker._target_category_prefixes = ("Категория", "Category")

        self.assertTrue(
            worker._category_already_exists(
                "Text\n[[Category:Foo_bar|Sort key]]",
                "Категория:Foo bar",
            )
        )
        self.assertFalse(
            worker._category_already_exists(
                "Text\n[[:Category:Foo bar]]",
                "Категория:Foo bar",
            )
        )

    def test_stop_request_prevents_next_save(self):
        worker = BaseWorker("", "", "en", "wikipedia")
        page = Mock()
        worker._last_save_ts = time.time() + 30
        worker.request_stop()

        self.assertFalse(worker._save_with_retry(page, "text", "summary", False))
        page.save.assert_not_called()

    def test_two_column_rename_does_not_transfer_after_failed_move(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tsv_path = Path(tmp_dir) / "rename.tsv"
            tsv_path.write_text("Old\tNew\n", encoding="utf-8-sig")
            with patch.object(
                rename_worker_module, "TemplateManager", return_value=Mock()
            ):
                worker = rename_worker_module.RenameWorker(
                    str(tsv_path),
                    "",
                    "",
                    "en",
                    "wikipedia",
                    14,
                    True,
                    True,
                    True,
                    False,
                    True,
                )

            worker._move_page = Mock(return_value=False)
            worker._move_category_members = Mock()
            with (
                patch.object(rename_worker_module.pywikibot, "Site", return_value=Mock()),
                patch.object(
                    rename_worker_module,
                    "normalize_title_by_selection",
                    side_effect=lambda title, *_args: title,
                ),
            ):
                worker.run()

            worker._move_page.assert_called_once()
            self.assertEqual("", worker._move_page.call_args.args[3])
            worker._move_category_members.assert_not_called()

    def test_empty_existing_page_is_written_to_tsv(self):
        worker = ParseWorker(["Empty"], "unused.tsv", "auto", "en", "wikipedia")
        worker.writer = Mock()
        worker.output_file = Mock()

        self.assertTrue(worker._write_result_immediately(("Empty", [])))
        worker.writer.writerow.assert_called_once_with(["Empty", ""])
        worker.output_file.flush.assert_called_once()

    def test_create_worker_exposes_fatal_site_error(self):
        worker = create_worker_module.CreateWorker(
            "unused.tsv", "", "", "en", "wikipedia", "auto", "", False
        )
        with patch.object(
            create_worker_module.pywikibot,
            "Site",
            side_effect=RuntimeError("site unavailable"),
        ):
            worker.run()

        self.assertTrue(worker.failed)
        self.assertEqual("site unavailable", worker.failure_message)
        self.assertEqual(1, worker.stats["failed"])


if __name__ == "__main__":
    unittest.main()
