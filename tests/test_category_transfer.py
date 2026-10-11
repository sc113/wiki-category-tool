from __future__ import annotations

# The test suite is runnable from both the package directory and its parent.
# ruff: noqa: E402

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QPlainTextEdit, QWidget, QTabWidget
import pywikibot

from wiki_cat_tool.core.api_client import APIRequestError
from wiki_cat_tool.core.category_transfer import (
    CategoryCreationRow, CategoryItemConflict, CategoryPair, CategoryTransferReader, TransferStopped,
    export_creation_tsv, merge_commands, plan_numbered_titles, read_creation_tsv,
)
from wiki_cat_tool.gui.tabs.category_interwiki_tab import CategoryInterwikiTab
from wiki_cat_tool.gui.tabs.category_transfer_tab import CategoryTransferTab
from wiki_cat_tool.workers.category_transfer_worker import (
    CategoryInterwikiLinkWorker, CategoryTextReadWorker, CategoryTransferCreateWorker, CategoryTransferReadWorker,
)


def page(title, *, ns=14, qid="", **extra):
    return {"title": title, "ns": ns, "pageprops": {"wikibase_item": qid}, **extra}


class FixtureReader(CategoryTransferReader):
    def __init__(self):
        super().__init__("en", "ru", "wikipedia", threading.Event())
        self.page_data = {"en": {}, "ru": {}}
        self.links = {}
        self.graph = {}
        self.page_calls = []
        self.reverse_links = {}
        self.item_entities = {}
        self.title_items = {}

    def site_info(self, lang):
        name = "Категория" if lang == "ru" else "Category"
        return {"general": {"wikiid": lang + "wiki"},
                "namespaces": {"0": {"name": ""}, "10": {"name": "Template", "canonical": "Template"},
                               "14": {"name": name, "canonical": "Category", "case": "first-letter"}},
                "namespacealiases": []}

    def pages(self, lang, titles, **kwargs):
        self.page_calls.append((lang, list(titles)))
        return {title: self.page_data[lang].get(title, page(title, missing=True)) for title in titles}

    def item_links(self, qids):
        return {qid: self.links.get(qid, "") for qid in qids}

    def members(self, title, kind):
        return list(self.graph.get((title, kind), []))

    def request(self, lang, params, **kwargs):
        if "sites" in params:
            return {"entities": {qid: {"id": qid, "sitelinks": {params["sites"]: {"title": title}}}
                                 for title in params["titles"].split("|")
                                 if (qid := self.title_items.get((params["sites"], title)))}}
        return {"entities": {qid: self.item_entities.get(qid, {
            "id": qid, "sitelinks": {"enwiki": {"title": self.reverse_links[qid]}} if qid in self.reverse_links else {}})
                             for qid in params["ids"].split("|")}}


class CategoryTransferReadTests(unittest.TestCase):
    def setUp(self):
        self.reader = FixtureReader()
        self.addCleanup(self.reader.close)

    def test_localized_prefixes_preserve_colons_in_category_names(self):
        self.assertEqual("Категория:YouTube: 2024", self.reader.category_title("Category:YouTube:_2024", "ru"))
        self.assertEqual("Категория:Пример", self.reader.category_title("категория:пример", "ru"))
        with self.assertRaises(ValueError):
            self.reader.category_title("Template:Example", "en")

    def test_discovery_is_breadth_first_and_avoids_cycles(self):
        for title in ("Category:Root", "Category:A", "Category:B", "Category:C"):
            self.reader.page_data["en"][title] = page(title)
        self.reader.graph.update({
            ("Category:Root", "subcat"): ["Category:A", "Category:B"],
            ("Category:A", "subcat"): ["Category:Root", "Category:C"],
            ("Category:B", "subcat"): ["Category:C"],
        })
        rows = self.reader.discover("Root", 2, 0, False, lambda row: None, lambda *args: None)
        self.assertEqual(["Category:A", "Category:B", "Category:C"], [row["source_title"] for row in rows])
        self.assertEqual([1, 1, 2], [row["depth"] for row in rows])

    def test_skip_level_prunes_existing_category_and_its_branch(self):
        for title in ("Category:Root", "Category:A", "Category:B", "Category:C", "Category:D"):
            self.reader.page_data["en"][title] = page(title, qid="Q1" if title.endswith(":B") else "")
        self.reader.page_data["ru"]["Категория:Б"] = page("Категория:Б")
        self.reader.links["Q1"] = "Категория:Б"
        self.reader.graph.update({
            ("Category:Root", "subcat"): ["Category:A"],
            ("Category:A", "subcat"): ["Category:B", "Category:C"],
            ("Category:B", "subcat"): ["Category:D"],
        })
        rows = self.reader.discover("Root", 3, 2, True, lambda row: None, lambda *args: None)
        self.assertEqual(["Category:Root", "Category:A", "Category:C"], [row["source_title"] for row in rows])

    def test_missing_category_is_not_a_successful_empty_scan(self):
        with self.assertRaises(ValueError):
            self.reader.discover("Missing", 1, 0, False, lambda row: None, lambda *args: None)

    def test_scan_deduplicates_articles_and_excludes_missing_redirect_non_articles(self):
        category = "Category:A"
        self.reader.page_data["en"][category] = page(category)
        titles = ["A", "B", "C", "D", "E", "Source redirect"]
        self.reader.graph[(category, "page")] = titles
        for number, title in enumerate(titles, 1):
            self.reader.page_data["en"][title] = page(title, ns=0, qid=f"Q{number}")
        self.reader.page_data["en"]["Source redirect"]["redirect"] = True
        self.reader.links.update({"Q1": "Одна", "Q2": "Одна", "Q3": "Нет", "Q4": "Редирект",
                                  "Q5": "Категория:Иная", "Q6": "Другая"})
        self.reader.page_data["ru"].update({"Одна": page("Одна", ns=0),
                                         "Редирект": page("Редирект", ns=0, redirect=True),
                                         "Категория:Иная": page("Категория:Иная"),
                                         "Другая": page("Другая", ns=0)})
        row = self.reader.scan({"source_title": category, "depth": 1})
        self.assertEqual(6, row["total"])
        self.assertEqual(1, row["potential"])
        self.assertEqual(["Одна"], row["target_articles"])
        self.reader.page_calls.clear()
        self.reader.scan({"source_title": category, "depth": 1})
        self.assertFalse(any(lang == "en" and any(title in titles for title in batch)
                             for lang, batch in self.reader.page_calls))

    def test_preview_distinguishes_missing_unlinked_and_already_linked(self):
        rows = []
        for suffix in ("Missing", "Unlinked", "Complete"):
            source = "Category:" + suffix
            target = "Категория:" + suffix
            self.reader.page_data["en"][source] = page(source, qid="Q1" if suffix == "Complete" else "")
            if suffix != "Missing":
                self.reader.page_data["ru"][target] = page(target, qid="Q1" if suffix == "Complete" else "")
            rows.append(CategoryCreationRow(source, target, "body" if suffix == "Missing" else ""))
        self.reader.links["Q1"] = "Категория:Complete"
        plan = self.reader.preview_creation(tuple(rows), lambda row: None)
        self.assertEqual(["ready_create", "ready_link", "already_linked"], [row["status"] for row in plan])

    def test_wikidata_title_lookup_recovers_a_recently_linked_category(self):
        self.reader.page_data["en"]["Category:A"] = page("Category:A", qid="Q1")
        self.reader.page_data["ru"]["Категория:А"] = page("Категория:А")
        self.reader.links["Q1"] = "Категория:А"
        self.reader.title_items[("ruwiki", "Категория:А")] = "Q1"
        result = self.reader.check_pairs((CategoryPair("Category:A", "Категория:А", "Q1"),))[0]
        self.assertEqual("already_linked", result["status"])
        self.assertEqual(("Q1", "Q1"), (result["source_qid"], result["target_qid"]))

    def test_preparation_repairs_bare_labels_and_skips_complete_metadata(self):
        self.reader.page_data["en"]["Category:A"] = page("Category:A", qid="Q1")
        self.reader.page_data["ru"]["Категория:А"] = page("Категория:А", qid="Q1")
        self.reader.links["Q1"] = "Категория:А"
        self.reader.item_entities["Q1"] = {"labels": {"en": {"value": "A"}, "ru": {"value": "Категория:А"}}}
        rows = (CategoryCreationRow("A", "А", ""),)
        result = self.reader.preview_creation(rows, Mock(), check_labels=True)[0]
        self.assertEqual("ready_labels", result["status"])
        self.assertTrue(result["detail"])
        self.reader.item_entities["Q1"]["labels"]["en"]["value"] = "Category:A"
        result = self.reader.preview_creation(rows, Mock(), check_labels=True)[0]
        self.assertEqual("already_linked", result["status"])
        self.assertEqual("already_linked", self.reader.check_pairs((CategoryPair("Category:A", "Категория:А", "Q1"),))[0]["status"])

    def test_preparation_conflict_retains_both_item_ids(self):
        self.reader.page_data["en"]["Category:A"] = page("Category:A", qid="Q1")
        self.reader.page_data["ru"]["Категория:А"] = page("Категория:А", qid="Q2")
        result = self.reader.preview_creation((CategoryCreationRow("A", "А", ""),), Mock())[0]
        self.assertEqual(("error", "Q1", "Q2"), (result["status"], result["source_qid"], result["target_qid"]))

    def test_title_lookup_api_failure_never_means_an_unlinked_category(self):
        self.reader.page_data["en"]["Category:A"] = page("Category:A")
        self.reader.request = Mock(side_effect=APIRequestError("offline"))
        with self.assertRaises(APIRequestError):
            self.reader.preview_creation((CategoryCreationRow("A", "А", "body"),), Mock())

    def test_preview_blocks_existing_sitelink_even_if_its_page_is_missing(self):
        self.reader.page_data["en"]["Category:A"] = page("Category:A", qid="Q1")
        self.reader.links["Q1"] = "Категория:Иная"
        row = CategoryCreationRow("A", "Новая", "body")
        plan = self.reader.preview_creation((row,), lambda row: None)
        self.assertEqual("error", plan[0]["status"])

    def test_blank_translation_is_filled_from_existing_wikidata_link(self):
        self.reader.page_data["en"]["Category:A"] = page("Category:A", qid="Q1")
        self.reader.page_data["ru"]["Категория:А"] = page("Категория:А", qid="Q1")
        self.reader.links["Q1"] = "Категория:А"
        plan = self.reader.preview_creation((CategoryCreationRow("A", "", ""),), lambda row: None)
        self.assertEqual("already_linked", plan[0]["status"])
        self.assertEqual("Категория:А", plan[0]["row"].target_title)
        self.assertEqual("Q1", plan[0]["qid"])

    def test_missing_translation_does_not_hide_valid_rows(self):
        self.reader.page_data["en"].update({"Category:A": page("Category:A"), "Category:B": page("Category:B")})
        plan = self.reader.preview_creation((CategoryCreationRow("A", "", ""),
                                             CategoryCreationRow("B", "Б", "body")), lambda row: None)
        self.assertEqual(["needs_translation", "ready_create"], [row["status"] for row in plan])

    def test_handoff_pair_is_rejected_when_item_changes(self):
        self.reader.page_data["en"]["Category:A"] = page("Category:A", qid="Q2")
        self.reader.page_data["ru"]["Категория:А"] = page("Категория:А", qid="Q2")
        self.reader.links["Q2"] = "Категория:А"
        result = self.reader.check_pairs((CategoryPair("Category:A", "Категория:А", "Q1"),))[0]
        self.assertEqual("error", result["status"])

    def test_handoff_pair_is_rejected_when_target_loses_link(self):
        self.reader.page_data["en"]["Category:A"] = page("Category:A", qid="Q1")
        self.reader.page_data["ru"]["Категория:А"] = page("Категория:А")
        self.reader.links["Q1"] = "Категория:А"
        self.assertEqual("error", self.reader.check_pairs((CategoryPair("Category:A", "Категория:А", "Q1"),))[0]["status"])

    def test_preview_checks_target_item_source_link_before_reusing_it(self):
        self.reader.page_data["en"]["Category:A"] = page("Category:A")
        self.reader.page_data["ru"]["Категория:Б"] = page("Категория:Б", qid="Q9")
        self.reader.reverse_links["Q9"] = "Category:Other"
        plan = self.reader.preview_creation((CategoryCreationRow("A", "Б", ""),), lambda row: None)
        self.assertEqual("error", plan[0]["status"])

    def test_preview_requires_body_only_for_categories_to_create(self):
        self.reader.page_data["en"]["Category:A"] = page("Category:A")
        plan = self.reader.preview_creation((CategoryCreationRow("A", "Б", ""),), lambda row: None)
        self.assertEqual("needs_content", plan[0]["status"])
        self.reader.page_data["ru"]["Категория:Б"] = page("Категория:Б")
        plan = self.reader.preview_creation((CategoryCreationRow("A", "Б", ""),), lambda row: None)
        self.assertEqual("ready_link", plan[0]["status"])

    def test_preview_rejects_normalized_duplicates_before_emitting_rows(self):
        callback = Mock()
        with self.assertRaises(ValueError):
            self.reader.preview_creation((CategoryCreationRow("a", "Б", "body"),
                                          CategoryCreationRow("Category:A", "В", "body")), callback)
        callback.assert_not_called()

    def test_cancellation_is_not_reported_as_an_empty_result(self):
        self.reader.stop_event.set()
        with self.assertRaises(TransferStopped):
            self.reader.scan({"source_title": "Category:A", "depth": 0})

    def test_api_reads_page_continuation_and_normalized_titles(self):
        reader = CategoryTransferReader("en", "ru", "wikipedia", threading.Event())
        self.addCleanup(reader.close)
        reader.request = Mock(side_effect=[
            {"query": {"normalized": [{"from": "a", "to": "A"}], "pages": [page("A", langlinks=[])]},
             "continue": {"llcontinue": "1|ru", "continue": "||"}},
            {"query": {"pages": [page("A", langlinks=[{"lang": "ru", "title": "А"}])]}}
        ])
        pages = reader.pages("en", ["a"])
        self.assertEqual("А", pages["a"]["langlinks"][0]["title"])
        self.assertEqual(2, reader.request.call_count)
        self.assertEqual("1|ru", reader.request.call_args[0][1]["llcontinue"])

    def test_large_creation_preview_bounds_encoded_urls_and_preserves_all_rows(self):
        import requests
        reader = CategoryTransferReader("en", "ru", "wikipedia",
                                        Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False)))
        self.addCleanup(reader.close)
        rows = tuple(CategoryCreationRow(f"Category:{year} establishments in England",
                                         f"Категория:Появились в {year} году в Англии",
                                         "{{Появились в году в стране}}") for year in range(1070, 1325))
        requests_seen = []
        def get(url, *, params, **kwargs):
            encoded_url = requests.Request("GET", url, params=params).prepare().url
            self.assertLessEqual(len(encoded_url.encode("ascii")), 6000)
            requests_seen.append(encoded_url)
            lang = "ru" if "ru.wikipedia" in url else "en"
            if params.get("meta") == "siteinfo":
                payload = {"query": self.reader.site_info(lang)}
            else:
                titles = params["titles"].split("|")
                self.assertLessEqual(len(titles), 50)
                payload = {"query": {"pages": [page(title, **({"missing": True} if lang == "ru" else {}))
                                               for title in titles]}}
            return Mock(status_code=200, json=Mock(return_value=payload))
        reader.api.session.get = Mock(side_effect=get)
        results = reader.preview_creation(rows, lambda row: None)
        self.assertEqual(len(rows), len(results))
        self.assertEqual(rows, tuple(result["row"] for result in results))
        self.assertTrue(all(result["status"] == "ready_create" for result in results))
        self.assertGreater(sum("ru.wikipedia" in url and "titles=" in url for url in requests_seen), 6)

    def test_http_error_diagnostics_do_not_include_the_entire_query_string(self):
        import requests
        reader = CategoryTransferReader("en", "ru", "wikipedia",
                                        Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False)))
        self.addCleanup(reader.close)
        response = Mock(status_code=414)
        response.raise_for_status.side_effect = requests.HTTPError(
            "414 URI Too Long for url: https://ru.wikipedia.org/w/api.php?titles=" + "%D0%9A" * 1500,
            response=response)
        reader.api.session.get = Mock(return_value=response)
        with self.assertRaises(APIRequestError) as caught:
            reader.request("ru", {"action": "query", "titles": "Категория:Пример"})
        self.assertIn("414", str(caught.exception))
        self.assertIn("https://ru.wikipedia.org/w/api.php", str(caught.exception))
        self.assertNotIn("titles=", str(caught.exception))
        self.assertLess(len(str(caught.exception)), 150)

    def test_api_errors_are_bounded_and_never_become_missing_pages(self):
        reader = CategoryTransferReader("en", "ru", "wikipedia", Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False)))
        self.addCleanup(reader.close)
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"error": {"code": "maxlag", "info": "busy"}}
        reader.api.session.get = Mock(return_value=response)
        with self.assertRaises(APIRequestError):
            reader.request("en", {"action": "query"})
        self.assertEqual(4, reader.api.session.get.call_count)

    def test_scan_worker_preserves_errors_for_retry(self):
        worker = CategoryTransferReadWorker(source_lang="en", target_lang="ru", family="wikipedia",
                                           categories=[{"source_title": "Category:A", "depth": 1}])
        worker.reader.scan = Mock(side_effect=APIRequestError("offline"))
        worker.run()
        self.assertEqual(1, worker.error_count)
        self.assertEqual("error", worker.results[0]["status"])
        self.assertIsNone(worker.results[0]["potential"])

    def test_http_429_honors_retry_after_and_reduces_following_request_rate(self):
        import requests
        event = Mock(is_set=Mock(return_value=False))
        ticks = [100.0]
        def wait(seconds):
            ticks[0] += seconds
            return False
        event.wait.side_effect = wait
        reader = CategoryTransferReader("en", "ru", "wikipedia", event)
        self.addCleanup(reader.close)
        limited = Mock(status_code=429, headers={"Retry-After": "120"})
        limited.raise_for_status.side_effect = requests.HTTPError("Too many requests", response=limited)
        successful = Mock(status_code=200, headers={})
        successful.raise_for_status.return_value = None
        successful.json.return_value = {"query": {"pages": []}}
        reader.api.session.get = Mock(side_effect=[limited, successful])
        notices = []
        reader.on_retry = notices.append
        with patch("wiki_cat_tool.core.category_transfer.time.monotonic", side_effect=lambda: ticks[0]):
            reader.request("en", {"action": "query", "titles": "Category:1718 establishments in England", "prop": "info|pageprops"})
        self.assertGreaterEqual(ticks[0], 220.0)
        self.assertGreater(reader._request_delay, reader.delay)
        self.assertEqual(2, reader.api.session.get.call_count)
        self.assertTrue(any("120" in notice for notice in notices))
        params = reader.api.session.get.call_args.kwargs["params"]
        self.assertEqual("Category:1718 establishments in England", params["titles"])
        self.assertEqual("info|pageprops", params["prop"])

    def test_retry_after_accepts_http_dates(self):
        response = Mock(headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        with patch("wiki_cat_tool.core.category_transfer.time.time", return_value=1445412450):
            self.assertEqual(30.0, CategoryTransferReader._retry_after(response))

    def test_exhausted_retries_keep_server_cooldown_for_the_next_row(self):
        import requests
        ticks = [100.0]
        event = Mock(is_set=Mock(return_value=False))
        def wait(seconds):
            ticks[0] += seconds
            return False
        event.wait.side_effect = wait
        reader = CategoryTransferReader("en", "ru", "wikipedia", event)
        self.addCleanup(reader.close)
        limited = Mock(status_code=429, headers={"Retry-After": "120"})
        limited.raise_for_status.side_effect = requests.HTTPError("429", response=limited)
        successful = Mock(status_code=200, headers={})
        successful.json.return_value = {"query": {}}
        reader.api.session.get = Mock(side_effect=[limited] * 4 + [successful])
        with patch("wiki_cat_tool.core.category_transfer.time.monotonic", side_effect=lambda: ticks[0]):
            with self.assertRaises(APIRequestError):
                reader.request("en", {"action": "query", "titles": "Category:A"})
            failed_at = ticks[0]
            reader.request("en", {"action": "query", "titles": "Category:B"})
        self.assertGreaterEqual(ticks[0] - failed_at, 120.0)
        self.assertEqual(5, reader.api.session.get.call_count)

    def test_stop_interrupts_server_cooldown_before_another_request(self):
        import requests
        event = Mock(is_set=Mock(return_value=False))
        event.wait.side_effect = [False, True]
        reader = CategoryTransferReader("en", "ru", "wikipedia", event)
        self.addCleanup(reader.close)
        response = Mock(status_code=429, headers={"Retry-After": "120"})
        response.raise_for_status.side_effect = requests.HTTPError("429", response=response)
        reader.api.session.get = Mock(return_value=response)
        with self.assertRaises(TransferStopped):
            reader.request("en", {"action": "query"})
        self.assertEqual(1, reader.api.session.get.call_count)


class NumberedTitleTests(unittest.TestCase):
    def test_series_keeps_filled_titles_and_ignores_other_categories(self):
        rows = (CategoryCreationRow("Category:1718 establishments in England", "", "body"),
                CategoryCreationRow("Category:1754 establishments in England", "Категория:Готовое название", ""),
                CategoryCreationRow("Category:1754 establishments in Scotland", "", ""),
                CategoryCreationRow("Category:1754–1755 establishments in England", "", ""))
        plan = plan_numbered_titles(rows, "Category:{число} establishments in England", "Категория:Основаны в {число} году")
        self.assertEqual(["number_fill", "number_kept", "number_unmatched", "number_unmatched"],
                         [proposal.status for proposal in plan])
        self.assertEqual("Категория:Основаны в 1718 году", plan[0].target_title)
        self.assertEqual(rows[1].target_title, plan[1].target_title)
        self.assertEqual("", rows[0].target_title)

    def test_literal_source_pattern_and_leading_zeros_are_preserved(self):
        rows = (CategoryCreationRow("Category:Series_(0010)+", "", ""),
                CategoryCreationRow("Category:Series 0011+", "", ""))
        plan = plan_numbered_titles(rows, "Category:Series_({number})+", "Серия {number}")
        self.assertEqual("Серия 0010", plan[0].target_title)
        self.assertEqual("number_unmatched", plan[1].status)

    def test_selected_rows_check_conflicts_against_the_entire_list(self):
        rows = (CategoryCreationRow("Category:A 2010", "", ""),
                CategoryCreationRow("Category:B 2010", "Категория:Серия 2010", ""),
                CategoryCreationRow("Category:A 2011", "", ""))
        plan = plan_numbered_titles(rows, "Category:A {число}", "Категория:Серия {число}", {0})
        self.assertEqual(1, len(plan))
        self.assertEqual("number_conflict", plan[0].status)

    def test_colliding_generated_titles_are_both_excluded(self):
        rows = (CategoryCreationRow("Category:A 2010", "", ""),
                CategoryCreationRow("Category:A_2010", "", ""))
        plan = plan_numbered_titles(rows, "Category:A {number}", "Серия {number}")
        self.assertEqual(["number_conflict", "number_conflict"], [proposal.status for proposal in plan])

    def test_incomplete_or_unsafe_templates_never_produce_a_plan(self):
        rows = (CategoryCreationRow("Category:A 2010", "", ""),)
        for source, target in (("Category:A", "Серия {число}"),
                               ("Category:A {число}", "Серия"),
                               ("{число} {число}", "Серия {число}"),
                               ("Category:A {число}", "[[Серия {число}]]")):
            with self.subTest(source=source, target=target), self.assertRaises(ValueError):
                plan_numbered_titles(rows, source, target)


class CreationTsvTests(unittest.TestCase):
    def test_unicode_bom_quotes_and_content_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.tsv"
            path.write_text('Category:A\tКатегория:Б\t"line with\ta tab"\tsecond\n', encoding="utf-8-sig")
            rows = read_creation_tsv(str(path))
            self.assertEqual("line with\ta tab\nsecond", rows[0].content)
            path.write_text("Other\tИное\tnew", encoding="utf-8")
            self.assertEqual("Category:A", rows[0].source_title)

    def test_export_keeps_existing_translations_for_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.tsv"
            export_creation_tsv(str(path), [{"source_title": "Category:A", "target_title": "Категория:Б"}])
            rows = read_creation_tsv(str(path))
            self.assertEqual("Категория:Б", rows[0].target_title)
            self.assertEqual("", rows[0].content)


def fake_category(title, site, exists=False):
    category = Mock()
    category.site = site
    category._exists = exists
    category.title.side_effect = lambda **kwargs: title.split(":", 1)[1] if kwargs.get("with_ns") is False else title
    category.exists.side_effect = lambda: category._exists
    category.isRedirectPage.return_value = False
    category.namespace.return_value.id = 14
    category.section.return_value = None
    def save(**kwargs):
        category._exists = True
    category.save.side_effect = save
    return category


class CategoryCreationTests(unittest.TestCase):
    def setUp(self):
        self.row = CategoryCreationRow("Category:A", "Категория:Б", "body")
        self.worker = CategoryTransferCreateWorker(rows=(self.row,), username="User", password="unused",
                                                   source_lang="en", target_lang="ru", family="wikipedia", summary="summary")
        self.worker._save_min_interval = 0
        self.source_site = Mock(lang="en")
        self.target_site = Mock(lang="ru")
        self.source_site.dbName.return_value = "enwiki"
        self.target_site.dbName.return_value = "ruwiki"
        self.repo = Mock()
        self.source_site.data_repository.return_value = self.repo
        self.target_site.data_repository.return_value = self.repo
        self.source = fake_category(self.row.source_title, self.source_site, True)
        self.target = fake_category(self.row.target_title, self.target_site)

    def run_worker(self):
        with patch("wiki_cat_tool.workers.category_transfer_worker.pywikibot.Site", side_effect=[self.source_site, self.target_site]), \
             patch.object(self.worker, "_category", side_effect=[self.source, self.target]), \
             patch.object(self.worker, "_login_sites"):
            self.worker.run()

    def test_missing_category_is_create_only_then_linked(self):
        self.worker._resolve_item = Mock(return_value=None)
        self.worker._link_categories = Mock(return_value=("Q1", "linked"))
        self.run_worker()
        self.target.save.assert_called_once()
        self.assertTrue(self.target.save.call_args.kwargs["createonly"])
        self.assertEqual(1, self.worker.stats["created"])
        self.assertEqual(["Категория:Б"], self.worker.linked_categories)

    def test_existing_category_is_not_overwritten_and_can_be_linked(self):
        self.target._exists = True
        self.target.text = "existing wikitext"
        self.worker._resolve_item = Mock(return_value=None)
        self.worker._link_categories = Mock(return_value=("Q1", "linked"))
        self.run_worker()
        self.target.save.assert_not_called()
        self.assertEqual("existing wikitext", self.target.text)
        self.assertEqual(1, self.worker.stats["exists"])
        self.assertEqual(1, self.worker.stats["linked"])

    def test_sitelink_conflict_blocks_page_creation(self):
        self.worker._resolve_item = Mock(side_effect=ValueError("conflicting sitelink"))
        self.run_worker()
        self.target.save.assert_not_called()
        self.assertEqual(1, self.worker.stats["failed"])

    def test_wikidata_failure_keeps_created_page_out_of_sync_list(self):
        self.worker._resolve_item = Mock(return_value=None)
        self.worker._link_categories = Mock(side_effect=RuntimeError("Wikidata unavailable"))
        results = []
        self.worker.category_result.connect(results.append)
        self.run_worker()
        self.assertEqual(1, self.worker.stats["created"])
        self.assertEqual([], self.worker.linked_categories)
        self.assertEqual("unlinked", results[0]["status"])

    def test_stop_after_page_creation_reports_pending_link(self):
        self.worker._resolve_item = Mock(return_value=None)
        self.worker._link_categories = Mock()
        def save_and_stop(**kwargs):
            self.target._exists = True
            self.worker.request_stop()
        self.target.save.side_effect = save_and_stop
        results = []
        self.worker.category_result.connect(results.append)
        self.run_worker()
        self.worker._link_categories.assert_not_called()
        self.assertEqual("unlinked", results[0]["status"])

    def test_new_item_has_both_sitelinks_labels_and_category_type_in_one_edit(self):
        self.worker._resolve_item = Mock(return_value=None)
        item = Mock()
        item.getID.return_value = "Q42"
        claim = Mock()
        claim.toJSON.return_value = {"mainsnak": {"property": "P31", "datavalue": {"value": "Q4167836"}}}
        type_item = Mock()
        with patch("wiki_cat_tool.workers.category_transfer_worker.pywikibot.ItemPage", side_effect=[item, type_item]), \
             patch("wiki_cat_tool.workers.category_transfer_worker.pywikibot.Claim", return_value=claim) as claim_type:
            qid, action = self.worker._link_categories(self.source, self.target, self.repo)
        self.assertEqual("Q42", qid)
        self.assertEqual("linked", action)
        claim_type.assert_called_once_with(self.repo, "P31")
        claim.setTarget.assert_called_once_with(type_item)
        item.editEntity.assert_called_once()
        data = item.editEntity.call_args[0][0]
        self.assertEqual({"enwiki", "ruwiki"}, set(data["sitelinks"]))
        self.assertEqual({"en": "Category:A", "ru": "Категория:Б"}, data["labels"])
        self.assertEqual([claim.toJSON.return_value], data["claims"])

    def test_existing_item_keeps_other_sitelinks_and_labels(self):
        item = Mock(sitelinks={"enwiki": Mock(title="Category:A"), "dewiki": Mock(title="Kategorie:A")},
                    labels={"en": "Existing label", "de": "A"})
        item.getID.return_value = "Q1"
        self.worker._resolve_item = Mock(return_value=item)
        self.worker._link_categories(self.source, self.target, self.repo)
        data = item.editEntity.call_args[0][0]
        self.assertEqual({"ruwiki"}, set(data["sitelinks"]))
        self.assertEqual({"ru": "Категория:Б"}, data["labels"])
        self.assertNotIn("claims", data)

    def test_no_duplicate_wikidata_edit_when_everything_is_already_linked(self):
        item = Mock(sitelinks={"enwiki": Mock(title="Category:A"), "ruwiki": Mock(title="Категория:Б")},
                    labels={"en": "Category:A", "ru": "Категория:Б"})
        self.worker._resolve_item = Mock(return_value=item)
        self.worker._link_categories(self.source, self.target, self.repo)
        item.editEntity.assert_not_called()

    def test_existing_bare_labels_are_repaired_with_english_edit_summary(self):
        item = Mock(sitelinks={"enwiki": Mock(), "ruwiki": Mock()}, labels={"en": "A", "ru": "Б", "de": "A"})
        self.worker.summary = "Создание категории: русский комментарий"
        self.worker._resolve_item = Mock(return_value=item)
        _, action = self.worker._link_categories(self.source, self.target, self.repo)
        data = item.editEntity.call_args.args[0]
        self.assertEqual({"en": "Category:A", "ru": "Категория:Б"}, data["labels"])
        self.assertEqual({}, data["sitelinks"])
        self.assertTrue(item.editEntity.call_args.kwargs["summary"].isascii())
        self.assertEqual("labels_updated", action)

    def test_label_only_edit_is_not_reported_or_counted_as_a_new_link(self):
        self.target._exists = True
        item = Mock(sitelinks={"enwiki": Mock(), "ruwiki": Mock()}, labels={"en": "Category:A", "ru": "Б"})
        item.getID.return_value = "Q42"
        self.worker._resolve_item = Mock(return_value=item)
        results = []
        self.worker.category_result.connect(results.append)
        self.run_worker()
        self.assertEqual("labels_updated", results[0]["status"])
        self.assertEqual((0, 1, 0), (self.worker.stats["linked"], self.worker.stats["labels"], self.worker.stats["created"]))
        self.assertEqual([self.row.target_title], self.worker.linked_categories)
        self.target.save.assert_not_called()
        self.assertEqual({"labels": {"ru": "Категория:Б"}, "sitelinks": {}}, item.editEntity.call_args.args[0])
        self.assertEqual("Update Wikimedia category labels", item.editEntity.call_args.kwargs["summary"])

    def test_one_preparation_run_creates_links_and_skips_completed_pairs(self):
        rows = tuple(CategoryCreationRow(f"Category:{name}", f"Категория:{name}", "body") for name in ("A", "B", "C"))
        sources = [fake_category(row.source_title, self.source_site, True) for row in rows]
        targets = [fake_category(row.target_title, self.target_site, index != 0) for index, row in enumerate(rows)]
        complete = Mock(sitelinks={"enwiki": Mock(), "ruwiki": Mock()}, labels={"en": rows[2].source_title, "ru": rows[2].target_title})
        complete.getID.return_value = "Q3"
        self.worker.rows = rows
        self.worker._resolve_item = Mock(side_effect=lambda source, target: complete if source is sources[2] else None)
        self.worker._link_categories = Mock(side_effect=[("Q1", "linked"), ("Q2", "linked")])
        results = []
        self.worker.category_result.connect(results.append)
        categories = [category for pair in zip(sources, targets) for category in pair]
        with patch("wiki_cat_tool.workers.category_transfer_worker.pywikibot.Site", side_effect=[self.source_site, self.target_site]), \
             patch.object(self.worker, "_category", side_effect=categories), patch.object(self.worker, "_login_sites"):
            self.worker.run()
        targets[0].save.assert_called_once()
        targets[1].save.assert_not_called()
        targets[2].save.assert_not_called()
        self.assertEqual(2, self.worker._link_categories.call_count)
        self.assertEqual(["created", "linked", "already_linked"], [result["status"] for result in results])
        self.assertEqual({"created": 1, "exists": 1, "linked": 2, "labels": 0, "already": 1, "failed": 0}, self.worker.stats)
        self.assertEqual([row.target_title for row in rows], self.worker.linked_categories)

    def test_lookup_errors_do_not_become_absent_wikidata_items(self):
        with patch("wiki_cat_tool.workers.category_transfer_worker.pywikibot.ItemPage.fromPage", side_effect=RuntimeError("network")):
            with self.assertRaises(RuntimeError):
                self.worker._item_for(self.source)

    @staticmethod
    def namespace_link(title, prefix, namespace=14):
        # Exercise the real SiteLink API without contacting a wiki. .title is
        # namespace-free, whereas canonical_title() returns the full title.
        link = object.__new__(pywikibot.SiteLink)
        link.title = title
        link._namespace = namespace
        link._site = Mock()
        link._site.namespace.return_value = prefix
        return link

    def test_existing_category_sitelink_with_bare_title_can_be_reused(self):
        link = self.namespace_link("A", "Category")
        item = Mock(sitelinks={"enwiki": link}, labels={"en": "A"})
        item.getID.return_value = "Q1"
        with patch.object(self.worker, "_item_for", side_effect=[item, None]):
            self.assertIs(item, self.worker._resolve_item(self.source, self.target))
        with patch.object(self.worker, "_item_for", side_effect=[item, None]):
            self.worker._link_categories(self.source, self.target, self.repo)
        self.assertEqual({"ruwiki": {"site": "ruwiki", "title": "Категория:Б"}},
                         item.editEntity.call_args.args[0]["sitelinks"])

    def test_same_bare_title_in_mainspace_remains_a_conflict(self):
        link = self.namespace_link("A", "", namespace=0)
        item = Mock(sitelinks={"enwiki": link})
        with patch.object(self.worker, "_item_for", side_effect=[item, None]):
            with self.assertRaises(ValueError):
                self.worker._resolve_item(self.source, self.target)
        item.editEntity.assert_not_called()
        self.target.save.assert_not_called()

    def test_localized_category_namespace_is_preserved_when_resolving_links(self):
        item = Mock(sitelinks={"enwiki": self.namespace_link("A", "Category"),
                              "ruwiki": self.namespace_link("Б", "Категория")})
        item.getID.return_value = "Q1"
        with patch.object(self.worker, "_item_for", side_effect=[item, item]):
            self.assertIs(item, self.worker._resolve_item(self.source, self.target))


class CategoryTransferUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.parent = QWidget()
        self.parent._ui_lang = "ru"
        self.parent.current_lang = "ru"
        self.parent.current_family = "wikipedia"
        self.tab = CategoryTransferTab(self.parent)
        self.addCleanup(self.parent.deleteLater)

    def add_rows(self):
        self.tab._set_busy(True)
        for title, status, potential in (("Empty", "scanned", 0), ("Error", "error", None),
                                         ("Not scanned", "loaded", None), ("Useful", "scanned", 3)):
            self.tab._apply_row({"source_title": "Category:" + title, "depth": 1,
                                 "status": status, "potential": potential})
        self.tab._set_busy(False)

    def test_clear_empty_preserves_errors_and_unscanned_categories(self):
        self.add_rows()
        self.tab.clear_empty()
        self.assertEqual({"Category:Error", "Category:Not scanned", "Category:Useful"},
                         {row["source_title"] for row in self.tab._rows()})

    def test_retry_scan_only_includes_failed_rows(self):
        self.add_rows()
        with patch("wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryTransferReadWorker") as worker_type, \
             patch.object(self.tab, "_start_read"):
            self.tab.retry_errors()
        self.assertEqual(["Category:Error"], [row["source_title"] for row in worker_type.call_args.kwargs["categories"]])
        self.assertEqual(4, self.tab.table.rowCount())

    def test_transfer_log_preserves_titles_file_paths_and_api_parameters(self):
        self.parent.translate_ui_text = lambda text: text.replace("Category", "Категория").replace("page", "страница")
        raw = "Category:1718 establishments: https://en.wikipedia.org/w/api.php?prop=info|pageprops|categoryinfo"
        self.tab._write_log(raw)
        self.assertIn(raw, self.tab.log.toPlainText())
        from wiki_cat_tool.utils import DEBUG_BUFFER
        self.assertIn(raw, DEBUG_BUFFER[-1])

    def test_first_stage_actions_preserve_second_stage_input_and_verified_plan(self):
        rows = (CategoryCreationRow("Category:Saved", "Категория:Сохранено", "saved body"),)
        self.tab._fill_creation_table(rows)
        self.tab._creation_rows = rows
        self.tab._linked_categories = [rows[0].target_title]
        self.tab._preview_context = self.tab._linked_context = self.tab._creation_context()
        self.add_rows()
        self.tab.clear_empty()
        self.tab.table.selectRow(0)
        self.tab.delete_selected()
        with patch("wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryTransferReadWorker"), \
             patch.object(self.tab, "_start_read"):
            self.tab.scan_categories()
            self.tab.root_edit.setText("Category:New root")
            self.tab.load_categories()
        self.tab.source_lang.setText("de")
        self.assertEqual(rows, self.tab._table_creation_rows())
        self.assertEqual(rows, self.tab._creation_rows)
        self.assertEqual("en", self.tab.links_source_lang.text())
        self.assertTrue(self.tab.create_btn.isEnabled())
        self.assertTrue(self.tab.sync_btn.isEnabled())

    def test_changing_creation_language_or_global_target_preserves_input_but_invalidates_writes(self):
        rows = (CategoryCreationRow("Category:Saved", "Категория:Сохранено", "saved body"),)
        self.tab._fill_creation_table(rows, "ready_create")
        self.tab._creation_rows = rows
        self.tab._preview_context = self.tab._creation_context()
        self.tab.links_source_lang.setText("de")
        self.assertEqual(rows, self.tab._table_creation_rows())
        self.assertFalse(self.tab.create_btn.isEnabled())
        self.assertEqual("en", self.tab.source_lang.text())
        self.tab.update_language("uk")
        self.assertEqual(rows, self.tab._table_creation_rows())
        self.assertEqual(("de", "uk", "wikipedia"), self.tab._creation_context())
        self.assertEqual("editing", self.tab.creation_table.item(0, 5).data(Qt.UserRole))

    def test_preview_uses_creation_page_language_and_request_pause(self):
        rows = (CategoryCreationRow("Kategorie:A", "Категория:А", "body"),)
        self.tab._fill_creation_table(rows)
        self.tab._table_dirty = True
        self.tab.links_source_lang.setText("de")
        self.tab.links_delay_spin.setValue(1.2)
        with patch("wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryTransferPreviewWorker") as worker_type:
            self.tab.preview_creation()
        self.assertEqual("de", worker_type.call_args.kwargs["source_lang"])
        self.assertEqual(1.2, worker_type.call_args.kwargs["delay"])
        self.assertEqual(rows, worker_type.call_args.kwargs["rows"])
        self.tab._set_busy(False)

    def test_scan_report_round_trip_preserves_counts_and_second_stage_list(self):
        self.add_rows()
        rows = (CategoryCreationRow("Category:Saved", "Категория:Сохранено", "body"),)
        self.tab._fill_creation_table(rows)
        expected = self.tab._rows()
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "report.tsv")
            with patch("wiki_cat_tool.gui.tabs.category_transfer_tab.QFileDialog.getSaveFileName", return_value=(path, "")):
                self.tab.export_report()
            self.tab.table.setRowCount(0)
            with patch("wiki_cat_tool.gui.tabs.category_transfer_tab.QFileDialog.getOpenFileName", return_value=(path, "")):
                self.tab.import_report()
        keys = ("source_title", "depth", "total", "potential", "status")
        self.assertEqual([{key: row.get(key) for key in keys} for row in expected],
                         [{key: row.get(key) for key in keys} for row in self.tab._rows()])
        self.assertEqual(rows, self.tab._table_creation_rows())

    def test_failed_tsv_open_preserves_the_visible_list(self):
        rows = (CategoryCreationRow("Category:Saved", "Категория:Сохранено", "body"),)
        self.tab._fill_creation_table(rows)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.tsv"
            path.write_text("no tabs here", encoding="utf-8")
            with patch("wiki_cat_tool.gui.tabs.category_transfer_tab.QFileDialog.getOpenFileName", return_value=(str(path), "")), \
                 patch.object(self.tab, "_warn") as warning:
                self.tab.choose_tsv()
            warning.assert_called_once()
        self.assertEqual(rows, self.tab._table_creation_rows())

    def test_wikidata_cell_opens_a_link_and_depth_stepper_increments_and_decrements(self):
        self.tab._apply_row(dict(source_title="Category:A", target_title="", qid="Q1", depth=1, status="loaded"))
        item = self.tab.table.item(0, 2)
        self.assertTrue(item.font().underline())
        with patch("wiki_cat_tool.gui.tabs.category_transfer_tab.QDesktopServices.openUrl") as open_url:
            self.tab.table.cellClicked.emit(0, 2)
        self.assertEqual("https://www.wikidata.org/wiki/Q1", open_url.call_args.args[0].toString())
        self.tab.depth_spin.setValue(4)
        self.tab.depth_plus_btn.click()
        self.assertEqual(5, self.tab.depth_spin.value())
        self.tab.depth_minus_btn.click()
        self.assertEqual(4, self.tab.depth_spin.value())

    def show_creation_rows(self, rows):
        self.tab._fill_creation_table(rows)
        self.tab._set_busy(False)
        self.tab.creation_stage.setWindowFlag(Qt.Window)
        self.tab.creation_stage.resize(900, 550)
        self.tab.creation_stage.show()
        self.addCleanup(self.tab.creation_stage.close)
        self.app.processEvents()
        return self.tab.creation_table

    def test_delete_removes_selected_rows_but_editors_keep_character_deletion(self):
        rows = tuple(CategoryCreationRow(f"Category:{index}", f"Категория:{index}", "abc") for index in range(3))
        table = self.show_creation_rows(rows)
        table.setCurrentCell(1, 1)
        QTest.keyClick(table, Qt.Key_Delete)
        self.assertEqual((rows[0], rows[2]), self.tab._table_creation_rows())
        table.setCurrentCell(0, 2)
        table.editItem(table.item(0, 2))
        self.app.processEvents()
        editor = table.findChild(QPlainTextEdit)
        cursor = editor.textCursor()
        cursor.setPosition(0)
        editor.setTextCursor(cursor)
        QTest.keyClick(editor, Qt.Key_Delete)
        self.assertEqual("bc", editor.toPlainText())
        self.assertEqual(2, table.rowCount())
        QTest.keyClick(editor, Qt.Key_Return, Qt.ControlModifier)

    def test_bulk_content_dialog_fills_all_empty_cells_and_preserves_existing_text(self):
        rows = (CategoryCreationRow("Category:A", "Категория:А", ""),
                CategoryCreationRow("Category:B", "Категория:Б", "existing"),
                CategoryCreationRow("Category:C", "Категория:В", ""))
        table = self.show_creation_rows(rows)
        table.setCurrentCell(0, 2)
        self.tab._creation_rows = rows
        self.tab._preview_context = self.tab._creation_context()
        value = "{{Template}}\n[[Категория:Родительская]]"
        def apply_text():
            dialog = self.app.activeModalWidget()
            dialog.findChild(QPlainTextEdit).setPlainText(value)
            box = dialog.findChild(QDialogButtonBox)
            next(button for button in box.buttons() if box.buttonRole(button) == QDialogButtonBox.AcceptRole).click()
        QTimer.singleShot(0, apply_text)
        self.tab.fill_content()
        result = self.tab._table_creation_rows()
        self.assertEqual([value, "existing", value], [row.content for row in result])
        self.assertEqual([row.target_title for row in rows], [row.target_title for row in result])
        self.assertFalse(self.tab.create_btn.isEnabled())
        self.assertIsNone(self.tab._preview_context)

    def test_fill_handle_drag_copies_text_and_double_click_fills_only_blanks_below(self):
        rows = tuple(CategoryCreationRow(f"Category:{i}", f"Категория:{i}", text)
                     for i, text in enumerate(("{{Template}}", "old", "", "", "keep")))
        table = self.show_creation_rows(rows)
        table.setCurrentCell(0, 2)
        self.app.processEvents()
        corner = table._handle_rect().center()
        target = table.visualRect(table.model().index(2, 2)).center()
        QTest.mousePress(table.viewport(), Qt.LeftButton, pos=corner)
        QTest.mouseMove(table.viewport(), target)
        QTest.mouseRelease(table.viewport(), Qt.LeftButton, pos=target)
        self.assertEqual(["{{Template}}"] * 3 + ["", "keep"], [row.content for row in self.tab._table_creation_rows()])
        table.setCurrentCell(0, 2)
        QTest.mouseDClick(table.viewport(), Qt.LeftButton, pos=table._handle_rect().center())
        self.assertEqual(["{{Template}}"] * 4 + ["keep"], [row.content for row in self.tab._table_creation_rows()])
        self.assertEqual([row.source_title for row in rows], [row.source_title for row in self.tab._table_creation_rows()])

    def test_content_fill_obeys_selected_rows_overwrite_and_busy_guards(self):
        rows = tuple(CategoryCreationRow(f"Category:{i}", f"Категория:{i}", "old") for i in range(3))
        self.tab._fill_creation_table(rows)
        self.tab._fill_content_rows([1], "new", only_empty=False)
        self.assertEqual(["old", "new", "old"], [row.content for row in self.tab._table_creation_rows()])
        self.tab._set_busy(True)
        self.tab._fill_content_range(0, 2, "busy", False)
        self.assertEqual(["old", "new", "old"], [row.content for row in self.tab._table_creation_rows()])
        self.tab._set_busy(False)

    def test_confirmation_is_localized_and_cancel_never_starts_creation(self):
        rows = (CategoryCreationRow("Category:A", "Категория:А", "body"),)
        self.tab._fill_creation_table(rows)
        self.tab._creation_rows = rows
        self.tab._preview_context = self.tab._creation_context()
        self.parent.current_user = "user"
        self.parent.current_password = "unused"
        labels = []
        def cancel_dialog():
            dialog = self.app.activeModalWidget()
            box = dialog.findChild(QDialogButtonBox)
            labels.extend(button.text() for button in box.buttons())
            next(button for button in box.buttons() if box.buttonRole(button) == QDialogButtonBox.RejectRole).click()
        QTimer.singleShot(0, cancel_dialog)
        with patch("wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryTransferCreateWorker") as worker:
            self.tab.start_creation()
        self.assertEqual({"Выполнить подготовку", "Отмена"}, set(labels))
        worker.assert_not_called()
        self.assertIn("категория в проекте Викимедиа", self.tab._t("confirm_items"))
        self.parent._ui_lang = "en"
        self.assertIn("Wikimedia category", self.tab._t("confirm_items"))

    def test_language_change_invalidates_preview_and_loaded_rows(self):
        self.add_rows()
        self.tab._creation_rows = (CategoryCreationRow("A", "Б", "body"),)
        self.tab._preview_context = self.tab._context()
        self.tab.update_language("uk")
        self.assertEqual(0, self.tab.table.rowCount())
        self.assertFalse(self.tab.create_btn.isEnabled())

    def test_english_labels_are_available(self):
        self.parent._ui_lang = "en"
        self.assertEqual("Category transfer", self.tab._t("title"))

    def test_preview_keeps_only_unfinished_rows_in_creation_plan(self):
        rows = [CategoryCreationRow("Category:" + name, "Категория:" + name, "body")
                for name in ("New", "Unlinked", "Done", "Conflict")]
        self.tab.preview_worker = Mock(failed=False, _stop=False, results=[
            {"row": rows[0], "status": "ready_create"}, {"row": rows[1], "status": "ready_link"},
            {"row": rows[2], "status": "already_linked"}, {"row": rows[3], "status": "error"},
        ])
        self.tab._preview_context = self.tab._context()
        self.tab._preview_finished()
        self.assertEqual(tuple(rows[:2]), self.tab._creation_rows)
        self.assertEqual([rows[2].target_title], self.tab._linked_categories)
        self.assertTrue(self.tab.create_btn.isEnabled())
        self.assertEqual("Выполнить подготовку", self.tab.create_btn.text())

    def test_label_update_enters_preparation_and_remains_available_for_sync(self):
        row = CategoryCreationRow("Category:A", "Категория:А", "")
        self.tab._fill_creation_table((row,))
        self.tab.preview_worker = Mock(failed=False, _stop=False, results=[
            {"row": row, "status": "ready_labels", "source_qid": "Q1", "target_qid": "Q1"}])
        self.tab._preview_context = self.tab._creation_context()
        self.tab._preview_finished()
        self.assertEqual((row,), self.tab._creation_rows)
        self.tab._set_busy(True)
        self.tab._creation_result({"source_title": row.source_title, "target_title": row.target_title,
                                   "qid": "Q1", "source_qid": "Q1", "target_qid": "Q1", "status": "labels_updated"})
        self.tab.create_worker = Mock(failed=False, _stop=False, linked_categories=[row.target_title],
                                     stats={"created": 0, "exists": 1, "linked": 0, "labels": 1, "already": 0, "failed": 0})
        with patch.object(self.tab, "_offer_sync", return_value=False):
            self.tab._creation_finished()
        self.assertEqual("labels_updated", self.tab.creation_table.item(0, 5).data(Qt.UserRole))
        self.assertEqual((), self.tab._creation_rows)
        self.assertEqual("Q1", self.tab._linked_pairs[row.target_title].qid)
        self.assertTrue(self.tab.sync_btn.isEnabled())

    def test_draft_rows_are_separate_from_errors_and_never_enter_the_write_plan(self):
        rows = [CategoryCreationRow("Category:" + name, "Категория:" + name if name != "Title" else "", "")
                for name in ("Title", "Body", "Linked")]
        results = [dict(index=index, row=row, status=status, detail="Fill missing field")
                   for index, (row, status) in enumerate(zip(rows, ("needs_translation", "needs_content", "already_linked")))]
        self.tab._set_busy(True)
        self.tab._fill_creation_table(rows)
        for result in results:
            self.tab._preview_result(result)
        self.tab.preview_worker = Mock(failed=False, _stop=False, results=results)
        self.tab._preview_context = self.tab._context()
        self.tab._preview_finished()
        self.assertEqual((), self.tab._creation_rows)
        self.assertFalse(self.tab.create_btn.isEnabled())
        self.assertTrue(self.tab.sync_btn.isEnabled())
        self.assertIn("заполнить: 2", self.tab.creation_summary.text())
        self.assertIn("ошибок: 0", self.tab.creation_summary.text())

    def test_incomplete_navigation_continues_after_editing_invalidates_the_preview(self):
        rows = (CategoryCreationRow("Category:A", "", ""),
                CategoryCreationRow("Category:B", "Категория:Б", ""))
        self.tab._set_busy(True)
        self.tab._fill_creation_table(rows)
        for index, (row, status) in enumerate(zip(rows, ("needs_translation", "needs_content"))):
            self.tab._preview_result(dict(index=index, row=row, status=status))
        self.tab._set_busy(False)
        self.tab.creation_table.setCurrentCell(0, 1)
        self.tab.creation_table.item(0, 1).setText("Категория:А")
        self.assertIsNone(self.tab._preview_context)
        with patch.object(self.tab.creation_table, "editItem"):
            self.tab.select_next_incomplete()
        self.assertEqual((1, 2), (self.tab.creation_table.currentRow(), self.tab.creation_table.currentColumn()))

    def test_numbered_titles_preserve_other_columns_and_invalidate_writes(self):
        rows = (CategoryCreationRow("Category:A 2010", "", "category text"),
                CategoryCreationRow("Category:A 2011", "Категория:Готово 2011", "existing text"))
        self.tab._fill_creation_table(rows)
        self.tab._creation_rows = rows
        self.tab._linked_categories = [rows[1].target_title]
        self.tab._preview_context = self.tab._linked_context = self.tab._context()
        self.tab._set_busy(False)
        plan = plan_numbered_titles(rows, "Category:A {число}", "Категория:Серия {число}")
        self.tab._apply_numbered_titles(plan)
        result = self.tab._table_creation_rows()
        self.assertEqual("Категория:Серия 2010", result[0].target_title)
        self.assertEqual(rows[0].source_title, result[0].source_title)
        self.assertEqual(rows[0].content, result[0].content)
        self.assertEqual(rows[1], result[1])
        self.assertFalse(self.tab.create_btn.isEnabled())
        self.assertFalse(self.tab.sync_btn.isEnabled())
        self.assertIsNone(self.tab._preview_context)
        self.assertTrue(self.tab._table_dirty)

    def test_failed_preview_preserves_input_and_does_not_leave_checking_statuses(self):
        rows = (CategoryCreationRow("Category:1398 establishments in England",
                                    "Категория:Появились в 1398 году в Англии",
                                    "{{Появились в году в стране}}"),)
        self.tab._set_busy(True)
        self.tab._fill_creation_table(rows, "checking")
        self.tab._preview_context = self.tab._context()
        huge_error = "414 URI Too Long for url: https://ru.wikipedia.org/w/api.php?titles=" + "%D0%9A" * 1500
        self.tab.preview_worker = Mock(failed=True, _stop=False, failure_message=huge_error, results=[])
        self.tab._preview_finished()
        self.assertEqual(rows, self.tab._table_creation_rows())
        self.assertEqual("editing", self.tab.creation_table.item(0, 5).data(Qt.UserRole))
        self.assertLessEqual(len(self.tab.progress_label.text()), 180)
        self.assertLessEqual(self.tab.progress_label.maximumHeight(), 42)
        self.assertNotIn("%D0%9A", self.tab.progress_label.text())
        self.assertIn(huge_error, self.tab.log.toPlainText())
        self.assertFalse(self.tab.create_btn.isEnabled())
        self.assertFalse(self.tab.sync_btn.isEnabled())
        self.assertIsNone(self.tab._preview_context)

    def test_partial_creation_keeps_failures_for_retry_and_prior_completed_categories_for_sync(self):
        success = CategoryCreationRow("Category:A", "Категория:А", "body")
        failed = CategoryCreationRow("Category:B", "Категория:Б", "body")
        self.tab._creation_rows = (success, failed)
        self.tab._preview_context = self.tab._context()
        self.tab._linked_context = self.tab._context()
        self.tab._existing_linked_categories = ["Категория:Ранее"]
        self.tab._creation_row_results = {success.source_title: {"status": "created"},
                                          failed.source_title: {"status": "unlinked"}}
        self.tab.create_worker = Mock(failed=False, _stop=False, linked_categories=[success.target_title],
                                     stats={"created": 2, "linked": 1, "exists": 0, "failed": 1})
        with patch.object(self.tab, "_offer_sync", return_value=False):
            self.tab._creation_finished()
        self.assertEqual((failed,), self.tab._creation_rows)
        self.assertEqual(["Категория:Ранее", "Категория:А"], self.tab._linked_categories)
        self.assertEqual(self.tab._linked_categories, self.tab._existing_linked_categories)
        self.assertTrue(self.tab.sync_btn.isEnabled())

    def test_sorted_table_updates_the_correct_category_after_scanning(self):
        self.add_rows()
        self.tab.table.sortItems(0, Qt.DescendingOrder)
        self.tab._set_busy(True)
        self.tab._apply_row({"source_title": "Category:Useful", "depth": 1,
                             "status": "scanned", "potential": 7, "total": 10})
        actual = next(row for row in self.tab._rows() if row["source_title"] == "Category:Useful")
        self.assertEqual(7, actual["potential"])
        self.assertEqual(4, self.tab.table.rowCount())

    def test_sync_handoff_prefills_actual_sync_controls_without_starting_writes(self):
        from wiki_cat_tool.gui.tabs.category_content_sync_tab import CategoryContentSyncTab
        sync = CategoryContentSyncTab(self.parent)
        sync.source_panel.file_edit.setText("old-list.txt")
        sync.source_panel.cat_edit.setText("Old root")
        sync.load_transfer_categories(["Категория:А", "Категория:Б"], "de")
        self.assertEqual("Категория:А\nКатегория:Б", sync.source_panel.manual_list.toPlainText())
        self.assertEqual("", sync.source_panel.file_edit.text())
        self.assertEqual("", sync.source_panel.cat_edit.text())
        self.assertEqual("de", sync.source_lang_edit.text())
        self.assertEqual(0, sync.article_depth_spin.value())
        self.assertTrue(sync.articles_checkbox.isChecked())
        self.assertFalse(sync.subcategories_checkbox.isChecked())
        self.assertFalse(sync._preview_ready)
        self.assertIsNone(sync.sync_worker)

    def test_next_stage_carries_the_curated_list_without_requiring_a_file(self):
        self.parent.tabs = QTabWidget(self.parent)
        self.parent.tabs.addTab(self.tab, "recon")
        self.parent.tabs.addTab(self.tab.creation_stage, "links")
        self.tab._fill_creation_table((CategoryCreationRow("Category:Old", "Категория:Старое", "old"),))
        self.add_rows()
        self.tab.clear_empty()
        self.tab.open_links()
        self.assertIs(self.tab.creation_stage, self.parent.tabs.currentWidget())
        self.assertEqual({"Category:Error", "Category:Not scanned", "Category:Useful"},
                         {row.source_title for row in self.tab._table_creation_rows()})
        self.assertTrue(self.tab._table_dirty)
        self.assertFalse(self.tab.create_btn.isEnabled())

    def test_editing_a_translation_invalidates_writes_and_sync_but_preserves_input(self):
        self.tab._fill_creation_table((CategoryCreationRow("Category:A", "Категория:А", "body"),))
        self.tab._creation_rows = self.tab._table_creation_rows()
        self.tab._linked_categories = ["Категория:А"]
        self.tab._preview_context = self.tab._linked_context = self.tab._context()
        self.tab._set_busy(False)
        self.tab.creation_table.item(0, 1).setText("Категория:Б")
        self.assertFalse(self.tab.create_btn.isEnabled())
        self.assertFalse(self.tab.sync_btn.isEnabled())
        self.assertEqual("Категория:Б", self.tab._table_creation_rows()[0].target_title)

    def test_sync_handoff_carries_source_target_and_qid_and_context(self):
        from wiki_cat_tool.gui.tabs.category_content_sync_tab import CategoryContentSyncTab
        sync = CategoryContentSyncTab(self.parent)
        self.parent.category_content_sync_tab = sync
        self.parent.tabs = QTabWidget(self.parent)
        self.parent.tabs.addTab(self.tab, "recon")
        self.parent.tabs.addTab(self.tab.creation_stage, "links")
        self.parent.tabs.addTab(sync, "sync")
        pair = CategoryPair("Category:A", "Категория:А", "Q1")
        self.tab._linked_pairs = {pair.target_title: pair}
        self.tab._linked_categories = [pair.target_title]
        self.tab._linked_context = self.tab._context()
        self.tab.open_sync()
        self.assertIs(sync, self.parent.tabs.currentWidget())
        self.assertEqual((pair,), sync._active_transfer_pairs([pair.target_title], "en", "ru", "wikipedia"))
        self.assertEqual((), sync._active_transfer_pairs([pair.target_title], "de", "ru", "wikipedia"))
        self.assertIsNone(sync.sync_worker)
        signature = sync._current_signature()
        sync._transfer_pairs = (CategoryPair(pair.source_title, pair.target_title, "Q2"),)
        self.assertNotEqual(signature, sync._current_signature())

    def test_log_and_progress_follow_the_visible_stage(self):
        self.tab._show_stage(self.tab.creation_stage)
        self.assertIs(self.tab.footer.parent(), self.tab.creation_stage)
        self.tab._show_stage(self.tab)
        self.assertIs(self.tab.footer.parent(), self.tab)

    def test_untranslated_tsv_can_be_loaded_and_edited_before_checking(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plan.tsv"
            path.write_text("Category:A\t\t\nCategory:B\t\t\n", encoding="utf-8")
            self.tab.tsv_path.setText(str(path))
            self.tab.import_tsv()
            self.assertEqual(2, self.tab.creation_table.rowCount())
            self.assertTrue(self.tab._table_dirty)
            self.assertFalse(self.tab.create_btn.isEnabled())

    def test_failed_sync_preview_never_enables_writes(self):
        from wiki_cat_tool.gui.tabs.category_content_sync_tab import CategoryContentSyncTab
        sync = CategoryContentSyncTab(self.parent)
        sync._prefill_preview_rows(target_categories=["Категория:А"], target_lang="ru", family="wikipedia")
        sync.preview_worker = Mock(_stop=False, failed=True, failure_message="offline")
        sync._on_preview_finished()
        self.assertFalse(sync._preview_ready)
        self.assertFalse(sync.start_btn.isEnabled())

    def test_sidebar_groups_transfer_pages_and_preserves_the_sync_page(self):
        from wiki_cat_tool.gui.main_window import MainWindow
        old_style = self.app.styleSheet()
        self.addCleanup(self.app.setStyleSheet, old_style)
        with patch.object(MainWindow, "_load_ui_settings"), patch.object(MainWindow, "_save_ui_settings"), \
             patch.object(MainWindow, "_install_messagebox_i18n"):
            window = MainWindow()
        self.addCleanup(window.deleteLater)
        window.tabs.clear()
        for index in range(11):
            window.tabs.addTab(QWidget(), str(index))
        window._rebuild_sidebar_nav()
        group = window.transfer_nav_group
        indexes = [index for index, button in window._nav_buttons.items() if button.parent() is group]
        self.assertEqual([8, 9, 6], indexes)
        self.assertIsNot(window._nav_buttons[10].parent(), group)
        for index in indexes:
            window._nav_buttons[index].click()
            self.assertEqual(index, window.tabs.currentIndex())
        window._sidebar_collapsed = True
        window._apply_sidebar_collapse_mode()
        self.assertTrue(window.transfer_nav_title.isHidden())

    def test_preparation_displays_both_qids_without_changing_tsv_columns(self):
        row = CategoryCreationRow("Category:A", "Категория:А", "body")
        self.tab._fill_creation_table((row,))
        self.tab._set_busy(True)
        self.tab._preview_result(dict(index=0, row=row, source_qid="Q1", target_qid="Q2", status="error", error="conflict"))
        self.tab._set_busy(False)
        self.assertEqual((row,), self.tab._table_creation_rows())
        for col, qid in ((3, "Q1"), (4, "Q2")):
            item = self.tab.creation_table.item(0, col)
            self.assertEqual(qid, item.text())
            self.assertTrue(item.font().underline())
            self.assertFalse(item.flags() & Qt.ItemIsEditable)
        with patch("wiki_cat_tool.gui.tabs.category_transfer_tab.QDesktopServices.openUrl") as open_url:
            self.tab._open_creation_wd(0, 4)
        self.assertEqual("https://www.wikidata.org/wiki/Q2", open_url.call_args.args[0].toString())

    def test_language_refresh_translates_a_visible_preparation_table(self):
        row = CategoryCreationRow("Category:A", "Категория:А", "body")
        self.tab._fill_creation_table((row,), "ready_link")
        for lang, header in (("en", "Source category"), ("ru", "Исходная категория")):
            self.parent._ui_lang = lang
            self.tab.refresh_localized_texts()
            self.assertEqual(header, self.tab.creation_table.horizontalHeaderItem(0).text())
            self.assertEqual(self.tab._t("ready_link"), self.tab.creation_table.item(0, 5).text())
            self.assertEqual((row,), self.tab._table_creation_rows())

    def test_sync_table_uses_source_target_wikidata_order_and_retains_target_key(self):
        from wiki_cat_tool.gui.tabs.category_content_sync_tab import CategoryContentSyncTab
        sync = CategoryContentSyncTab(self.parent)
        pair = CategoryPair("Category:A", "Категория:А", "Q1")
        sync.load_transfer_categories([pair.target_title], "en", pairs=(pair,))
        sync._prefill_preview_rows(target_categories=[pair.target_title], target_lang="ru", family="wikipedia")
        self.assertEqual(pair.source_title, sync.preview_table.item(0, 0).text())
        sync._append_preview_row(dict(phase="details", target_title=pair.target_title, row_key=pair.target_title.casefold(),
                                      source_title=pair.source_title, qid="Q1", qid_url="https://www.wikidata.org/wiki/Q1",
                                      status="error", error="Changed pair"))
        self.assertEqual([pair.source_title, pair.target_title, "Q1"], [sync.preview_table.item(0, col).text() for col in range(3)])
        self.assertEqual([pair.target_title], sync._categories_from_preview_table())
        self.assertEqual(pair.target_title.casefold(), sync._row_key_for_row(0))
        self.assertEqual("error", sync.preview_table.item(0, 5).data(sync._status_code_role()))
        self.assertEqual("Changed pair", sync.preview_table.item(0, 5).toolTip())


class TransferAutofillTests(unittest.TestCase):
    def test_auto_detects_english_ordinals_and_converts_to_arabic_or_roman(self):
        rows = tuple(CategoryCreationRow('Category:' + title, '', '')
                     for title in ('1st-century art', '20th-century art', 'XXI-century art'))
        for token, numbers in (('{число}', ('1', '20', '21')), ('{римская}', ('I', 'XX', 'XXI'))):
            plan = plan_numbered_titles(rows, '', 'Категория:Искусство ' + token + ' века')
            self.assertEqual(['number_fill'] * 3, [entry.status for entry in plan])
            self.assertEqual(['Категория:Искусство ' + number + ' века' for number in numbers],
                             [entry.target_title for entry in plan])

    def test_ambiguous_numbers_and_noncanonical_roman_are_skipped(self):
        rows = tuple(CategoryCreationRow('Category:' + title, '', '')
                     for title in ('Wars of 1914–1918', 'IIII-century art', 'Art'))
        self.assertEqual(['number_unmatched'] * 3,
                         [entry.status for entry in plan_numbered_titles(rows, '', 'Категория:{число}')])

    def test_roman_range_never_produces_invalid_numerals(self):
        rows = tuple(CategoryCreationRow(f'Category:{number} art', '', '') for number in (0, 3999, 4000))
        plan = plan_numbered_titles(rows, '', 'Категория:{римская}')
        self.assertEqual(['number_unmatched', 'number_fill', 'number_unmatched'], [entry.status for entry in plan])
        self.assertEqual('Категория:MMMCMXCIX', plan[1].target_title)

    def test_exact_pattern_can_limit_a_mixed_list_with_ordinal_input(self):
        rows = (CategoryCreationRow('Category:20th-century art', '', ''),
                CategoryCreationRow('Category:20th-century buildings', '', ''))
        plan = plan_numbered_titles(rows, 'Category:{number}-century art', 'Категория:Искусство {roman} века')
        self.assertEqual(['number_fill', 'number_unmatched'], [entry.status for entry in plan])
        self.assertEqual('Категория:Искусство XX века', plan[0].target_title)

    def test_merge_commands_deduplicate_and_keep_oldest_connected_item(self):
        pairs = [('Q1234', 'Q123'), ('Q123', 'Q5'), ('Q5', 'Q1234'),
                 ('Q2345', 'Q234'), ('Q234', 'Q234'), ('bad', 'Q1')]
        code = merge_commands([dict(source_qid=source, target_qid=target) for source, target in pairs])
        self.assertEqual('MERGE\tQ123\tQ5\nMERGE\tQ1234\tQ5\nMERGE\tQ2345\tQ234', code)

    def test_long_conflict_chains_keep_oldest_item_without_recursive_overflow(self):
        conflicts = [dict(source_qid=f'Q{number}', target_qid=f'Q{number - 1}') for number in range(1550, 1, -1)]
        commands = merge_commands(conflicts).splitlines()
        self.assertEqual(1549, len(commands))
        self.assertTrue(all(command.endswith('\tQ1') for command in commands))


class InterwikiReadTests(unittest.TestCase):
    def setUp(self):
        self.reader = FixtureReader()
        self.addCleanup(self.reader.close)
        self.row = CategoryCreationRow('Category:A', 'Категория:Б', '')
        self.reader.request = Mock(side_effect=lambda lang, params, **kwargs: {
            'entities': {qid: {'sitelinks': {}} for qid in params.get('ids', '').split('|') if qid}})

    def preview(self, source_qid='', target_qid='', **target_extra):
        self.reader.page_data['en']['Category:A'] = page('Category:A', qid=source_qid)
        self.reader.page_data['ru']['Категория:Б'] = page('Категория:Б', qid=target_qid, **target_extra)
        return self.reader.preview_interwiki((self.row,), Mock())[0]

    def test_preview_distinguishes_all_item_combinations(self):
        for source, target, status in (('', '', 'ready_item'), ('Q1', '', 'ready_link'),
                                      ('', 'Q2', 'ready_link'), ('Q1', 'Q1', 'already_linked'),
                                      ('Q1', 'Q2', 'different_items')):
            with self.subTest(source=source, target=target):
                result = self.preview(source, target)
                self.assertEqual((status, source, target),
                                 (result['status'], result['source_qid'], result['target_qid']))

    def test_missing_redirect_or_noncategory_targets_are_not_created(self):
        for properties in ({'missing': True}, {'redirect': True}, {'ns': 0}):
            with self.subTest(properties=properties):
                self.assertEqual('error', self.preview(**properties)['status'])

    def test_occupied_sitelink_and_missing_entity_block_linking(self):
        for entity in ({'sitelinks': {'ruwiki': {'title': 'Категория:Другая'}}}, {'missing': True}):
            self.reader.request.return_value = {'entities': {'Q1': entity}}
            self.reader.request.side_effect = None
            self.assertEqual('error', self.preview('Q1')['status'])

    def test_normalized_duplicates_reject_the_whole_list_before_results(self):
        callback = Mock()
        with self.assertRaises(ValueError):
            self.reader.preview_interwiki((self.row, CategoryCreationRow('A', 'В', '')), callback)
        callback.assert_not_called()

    def test_api_failure_never_becomes_an_absent_item(self):
        self.reader.request.side_effect = APIRequestError('unavailable')
        self.reader.page_data['en']['Category:A'] = page('Category:A', qid='Q1')
        self.reader.page_data['ru']['Категория:Б'] = page('Категория:Б')
        with self.assertRaises(APIRequestError):
            self.reader.preview_interwiki((self.row,), Mock())


class InterwikiWriteTests(unittest.TestCase):
    def setUp(self):
        self.row = CategoryCreationRow('Category:A', 'Категория:Б', '')
        self.worker = CategoryInterwikiLinkWorker(rows=(self.row,), username='ActiveProfile', password='unused',
                                                  source_lang='en', target_lang='ru', family='wikipedia', summary='link')
        self.worker._save_min_interval = 0
        self.source_site, self.target_site, self.repo = Mock(lang='en'), Mock(lang='ru'), Mock()
        for site, name in ((self.source_site, 'enwiki'), (self.target_site, 'ruwiki')):
            site.data_repository.return_value = self.repo
            site.dbName.return_value = name
        self.source = fake_category(self.row.source_title, self.source_site, True)
        self.target = fake_category(self.row.target_title, self.target_site, True)
        self.results = []
        self.worker.category_result.connect(self.results.append)

    @staticmethod
    def item(qid, site='', title=''):
        link = Mock()
        link.canonical_title.return_value = title
        result = Mock(sitelinks={site: link} if site else {}, labels={})
        result.getID.return_value = qid
        return result

    def run_worker(self):
        previous = pywikibot.config.usernames['wikidata'].get('wikidata')
        with patch('wiki_cat_tool.workers.category_transfer_worker.pywikibot.Site',
                   side_effect=[self.source_site, self.target_site]) as sites, \
             patch.object(self.worker, '_category', side_effect=[self.source, self.target]), \
             patch.object(self.worker, '_login_sites'):
            self.worker.run()
        self.assertEqual('ActiveProfile', pywikibot.config.usernames['wikidata']['wikidata'])
        self.assertEqual(['ActiveProfile', 'ActiveProfile'], [call.kwargs['user'] for call in sites.call_args_list])
        pywikibot.config.usernames['wikidata']['wikidata'] = previous
        self.source.save.assert_not_called()
        self.target.save.assert_not_called()

    def test_different_items_are_reported_without_edits(self):
        source, target = self.item('Q1234'), self.item('Q123')
        self.worker._item_for = Mock(side_effect=[source, target])
        self.run_worker()
        source.editEntity.assert_not_called()
        target.editEntity.assert_not_called()
        self.assertEqual('different_items', self.results[0]['status'])
        self.assertEqual('MERGE\tQ1234\tQ123', merge_commands(self.worker.conflicts))

    def test_same_item_is_skipped(self):
        item = self.item('Q1')
        self.worker._item_for = Mock(return_value=item)
        self.run_worker()
        item.editEntity.assert_not_called()
        self.assertEqual(1, self.worker.stats['already'])

    def test_source_or_target_item_is_reused_with_both_missing_labels(self):
        for source_side in (True, False):
            self.setUp()
            item = self.item('Q1', 'enwiki' if source_side else 'ruwiki',
                             'Category:A' if source_side else 'Категория:Б')
            pair = [item, None] if source_side else [None, item]
            self.worker._item_for = Mock(side_effect=pair + pair)
            self.run_worker()
            data = item.editEntity.call_args.args[0]
            self.assertEqual({'ruwiki' if source_side else 'enwiki'}, set(data['sitelinks']))
            self.assertEqual({'en': 'Category:A', 'ru': 'Категория:Б'}, data['labels'])
            self.assertNotIn('claims', data)
            self.assertEqual('linked', self.results[0]['status'])

    def test_neither_item_exists_creates_one_atomic_category_item(self):
        self.worker._item_for = Mock(return_value=None)
        item, claim = self.item('Q42'), Mock()
        claim.toJSON.return_value = {'mainsnak': {'property': 'P31'}}
        with patch('wiki_cat_tool.workers.category_transfer_worker.pywikibot.ItemPage', side_effect=[item, Mock()]), \
             patch('wiki_cat_tool.workers.category_transfer_worker.pywikibot.Claim', return_value=claim):
            self.run_worker()
        data = item.editEntity.call_args.args[0]
        self.assertEqual({'enwiki', 'ruwiki'}, set(data['sitelinks']))
        self.assertEqual([claim.toJSON.return_value], data['claims'])
        item.editEntity.assert_called_once()
        self.assertEqual(1, self.worker.stats['items'])

    def test_changed_items_are_reported_during_revalidation(self):
        self.worker._item_for = Mock(side_effect=[self.item('Q1'), None])
        self.worker._link_categories = Mock(side_effect=CategoryItemConflict('A', 'Б', 'Q3', 'Q2'))
        self.run_worker()
        self.assertEqual('MERGE\tQ3\tQ2', merge_commands(self.worker.conflicts))
        self.assertEqual(0, self.worker.stats['failed'])

    def test_missing_page_blocks_item_edit(self):
        self.target._exists = False
        self.worker._item_for = Mock()
        self.run_worker()
        self.worker._item_for.assert_not_called()
        self.assertEqual('error', self.results[0]['status'])


class SourceTextTests(unittest.TestCase):
    def test_revision_content_is_requested_in_main_slots(self):
        reader = CategoryTransferReader('en', 'ru', 'wikipedia', threading.Event())
        self.addCleanup(reader.close)
        reader.request = Mock(return_value={'query': {'pages': [page('Category:A')]}})
        reader.pages('en', ['Category:A'], props='info|revisions')
        params = reader.request.call_args.args[1]
        self.assertEqual(('content', 'main'), (params['rvprop'], params['rvslots']))

    def test_source_text_preserves_row_indices_and_skips_suppressed_content(self):
        worker = CategoryTextReadWorker(rows=((4, 'A'), (9, 'B'), (12, 'C')), source_lang='en', target_lang='ru', family='wikipedia')
        worker.reader.close()
        reader = worker.reader = FixtureReader()
        reader.page_data['en'] = {'Category:A': page('Category:A', revisions=[{'slots': {'main': {'content': '{{Source}}'}}}]),
                                  'Category:B': page('Category:B', revisions=[{'slots': {'main': {'texthidden': True}}}])}
        payloads = []
        worker.content_ready.connect(payloads.append)
        worker.run()
        self.assertEqual([{'index': 4, 'content': '{{Source}}'}], payloads)
        self.assertEqual({'loaded': 1, 'failed': 2}, worker.stats)

    def test_credentials_write_binds_active_profile_to_runtime_and_file(self):
        from wiki_cat_tool.core.pywikibot_config import PywikibotConfigManager
        manager = PywikibotConfigManager()
        with tempfile.TemporaryDirectory() as folder, patch.object(manager, '_dist_configs_dir', return_value=folder), \
             patch.dict(pywikibot.config.usernames['wikidata']), patch.dict(pywikibot.config.usernames['wikipedia']):
            for username in ('FirstUser', "Second'User"):
                manager.write_pwb_credentials('ru', username, 'unused')
                config = (Path(folder) / 'user-config.py').read_text(encoding='utf-8')
                namespace = {'usernames': {'wikipedia': {}, 'wikidata': {}}}
                exec(compile(config, 'temporary-user-config.py', 'exec'), namespace)
                self.assertEqual(username, namespace['usernames']['wikidata']['wikidata'])
                self.assertEqual(username, pywikibot.config.usernames['wikidata']['wikidata'])


class InterwikiUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.parent = QWidget()
        self.parent._ui_lang = 'ru'
        self.parent.current_lang = 'ru'
        self.parent.current_family = 'wikipedia'
        self.tab = CategoryInterwikiTab(self.parent)
        self.rows = (CategoryCreationRow('Category:20th-century art', 'Категория:Искусство XX века', 'ignored'),)
        self.tab.load_pairs(self.rows, 'en')
        self.addCleanup(self.parent.deleteLater)

    def result(self, status='ready_link', source='Q20', target=''):
        return dict(index=0, row=self.tab._rows()[0], status=status, source_qid=source, target_qid=target)

    def finish_preview(self, result):
        self.tab._preview_context = self.tab._context()
        self.tab.preview_worker = Mock(failed=False, _stop=False, results=[result])
        self.tab._preview_result(result)
        self.tab._preview_finished()

    def test_manual_paste_delete_and_edit_preserve_input_but_invalidate_writes(self):
        self.finish_preview(self.result())
        self.assertTrue(self.tab.link_btn.isEnabled())
        self.tab.table.item(0, 1).setText('Категория:Новый перевод')
        self.assertFalse(self.tab.link_btn.isEnabled())
        self.app.clipboard().setText('Category:21st-century art\tКатегория:Искусство XXI века')
        self.tab.paste_pairs()
        self.assertEqual(2, self.tab.table.rowCount())
        self.tab.table.setCurrentCell(1, 0)
        QTest.keyClick(self.tab.table, Qt.Key_Delete)
        self.assertEqual((CategoryCreationRow(self.rows[0].source_title, 'Категория:Новый перевод', ''),), self.tab._rows())

    def test_failed_preview_keeps_titles_and_never_enables_linking(self):
        self.tab.preview_worker = Mock(failed=True, _stop=False, failure_message='unavailable', results=[])
        self.tab._preview_finished()
        self.assertEqual(self.rows[0].source_title, self.tab._rows()[0].source_title)
        self.assertFalse(self.tab.link_btn.isEnabled())

    def test_context_change_preserves_titles_and_invalidates_conflicts_and_writes(self):
        self.finish_preview(self.result('different_items', 'Q20', 'Q1'))
        self.assertTrue(self.tab.report_btn.isEnabled())
        self.tab.source_lang.setText('de')
        self.assertFalse(self.tab.link_btn.isEnabled())
        self.assertFalse(self.tab.report_btn.isEnabled())
        self.assertEqual(self.rows[0].target_title, self.tab._rows()[0].target_title)

    def test_failed_revalidation_removes_previous_success_from_sync_handoff(self):
        self.finish_preview(self.result('already_linked', 'Q20', 'Q20'))
        self.assertTrue(self.tab.sync_btn.isEnabled())
        self.tab._row_result(0, self.result('different_items', 'Q20', 'Q1'))
        self.tab._set_busy(False)
        self.assertFalse(self.tab.sync_btn.isEnabled())

    def test_next_sync_carries_only_linked_pairs_without_starting_writes(self):
        self.finish_preview(self.result('already_linked', 'Q20', 'Q20'))
        sync = self.parent.category_content_sync_tab = Mock(preview_worker=None, sync_worker=None)
        self.parent.tabs = Mock()
        self.tab.open_sync()
        sync.load_transfer_categories.assert_called_once_with([self.rows[0].target_title], 'en',
            pairs=(CategoryPair(self.rows[0].source_title, self.rows[0].target_title, 'Q20'),))
        self.parent.tabs.setCurrentWidget.assert_called_once_with(sync)

    def test_merge_export_uses_real_tabs_and_older_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'merge.tsv'
            with patch('wiki_cat_tool.gui.tabs.category_interwiki_tab.QFileDialog.getSaveFileName', return_value=(str(path), '')):
                self.tab._save_merge(merge_commands([dict(source_qid='Q1234', target_qid='Q123')]))
            self.assertEqual('MERGE\tQ1234\tQ123\n', path.read_text(encoding='utf-8'))

    def test_cancel_never_starts_linking_worker(self):
        self.finish_preview(self.result())
        self.parent.current_user, self.parent.current_password = 'User', 'unused'
        with patch.object(self.tab, '_confirm_linking', return_value=False), \
             patch('wiki_cat_tool.gui.tabs.category_interwiki_tab.CategoryInterwikiLinkWorker') as worker:
            self.tab.start_linking()
        worker.assert_not_called()

    def test_first_row_copy_and_source_loading_keep_stage_one_independent(self):
        transfer = CategoryTransferTab(self.parent)
        rows = (CategoryCreationRow('Category:2001 art', 'Категория:Искусство 2001', '{{Exact}}\n[[Category:A]]'),
                CategoryCreationRow('Category:2002 art', '', 'old'))
        transfer._fill_creation_table(rows)
        transfer._creation_rows = rows
        transfer.copy_first_content()
        self.assertEqual(rows[0].content, transfer.creation_table.item(1, 2).text())
        self.assertEqual((), transfer._creation_rows)
        transfer.creation_table.item(1, 2).setText('')
        with patch('wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryTextReadWorker') as worker:
            transfer.load_source_content()
        self.assertEqual(((1, 'Category:2002 art'),), worker.call_args.kwargs['rows'])
        self.assertEqual('en', worker.call_args.kwargs['source_lang'])
        transfer._source_content_ready({'index': 1, 'content': '{{English}}'})
        self.assertEqual('{{English}}', transfer.creation_table.item(1, 2).text())
        self.assertEqual(rows[0].target_title, transfer.creation_table.item(0, 1).text())

    def test_switching_ui_language_translates_autofill_help_and_keeps_user_template(self):
        transfer = CategoryTransferTab(self.parent)
        transfer.title_template_edit.setText('Категория:Искусство {римская} века')
        self.parent._ui_lang = 'en'
        transfer._show_stage(transfer.creation_stage)
        self.assertIn('{roman}', transfer.autofill_title_help.text())
        self.assertEqual('Категория:Искусство {римская} века', transfer.title_template_edit.text())

    def test_creation_handoff_preserves_creation_text(self):
        transfer = self.parent.category_transfer_tab = CategoryTransferTab(self.parent)
        self.parent.category_interwiki_tab, self.parent.tabs = self.tab, Mock()
        transfer._fill_creation_table(self.rows)
        transfer.open_interwiki()
        self.assertEqual('', self.tab._rows()[0].content)
        self.assertEqual('ignored', transfer.creation_table.item(0, 2).text())


class CategoryTransferSyncTests(unittest.TestCase):
    def setUp(self):
        from wiki_cat_tool.workers.category_content_sync_worker import CategoryContentSyncWorker
        self.reader = FixtureReader()
        self.addCleanup(self.reader.close)
        self.pair = CategoryPair("Category:A", "Категория:А", "Q1")
        self.worker = CategoryContentSyncWorker(
            target_categories=[self.pair.target_title], category_pairs=(self.pair,),
            username="user", password="pass", source_lang="en", target_lang="ru", family="wikipedia",
            process_articles=True, process_subcategories=False, article_depth=0,
        )

    def test_sync_never_falls_back_to_similar_title_after_transferred_link_disappears(self):
        self.reader.page_data["en"]["Category:A"] = page("Category:A", qid="Q1")
        self.reader.page_data["ru"]["Категория:А"] = page("Категория:А")
        with patch("wiki_cat_tool.workers.category_content_sync_worker.CategoryTransferReader", return_value=self.reader), \
             patch("wiki_cat_tool.workers.category_content_sync_worker.pywikibot.Category") as category:
            source, _ = self.worker._resolve_source_category_with_retry(self.pair.target_title, Mock(), Mock())
        self.assertIsNone(source)
        category.assert_not_called()

    def test_preview_rejects_changed_pair_without_guessing_another_source(self):
        from wiki_cat_tool.workers.category_content_sync_worker import CategoryContentSyncPreviewWorker
        self.reader.page_data["en"]["Category:A"] = page("Category:A", qid="Q1")
        self.reader.page_data["ru"]["Категория:А"] = page("Категория:А")
        worker = CategoryContentSyncPreviewWorker(
            target_categories=[self.pair.target_title], category_pairs=(self.pair,),
            source_lang="en", target_lang="ru", family="wikipedia",
            process_articles=True, process_subcategories=False, article_depth=0)
        self.addCleanup(worker.api_client.session.close)
        worker._api_get_json = Mock()
        with patch("wiki_cat_tool.workers.category_content_sync_worker.CategoryTransferReader", return_value=self.reader):
            rows = worker._build_preview_rows_batch(batch_titles=[self.pair.target_title],
                                                    source_site=Mock(), target_site=Mock())
        self.assertEqual(self.pair.source_title, rows[0]["source_title"])
        self.assertEqual("error", rows[0]["status"])
        self.assertTrue(rows[0]["error"])
        worker._api_get_json.assert_not_called()

    def test_standalone_preview_reads_source_when_wikipedia_item_props_lag(self):
        from wiki_cat_tool.workers.category_content_sync_worker import CategoryContentSyncPreviewWorker
        worker = CategoryContentSyncPreviewWorker(target_categories=[self.pair.target_title], source_lang="en", target_lang="ru",
                                                  family="wikipedia", process_articles=True, process_subcategories=False, article_depth=0)
        self.addCleanup(worker.api_client.session.close)
        source_site, target_site = Mock(), Mock()
        source_site.hostname.return_value = "en.wikipedia.org"
        target_site.hostname.return_value = "ru.wikipedia.org"
        def response(url, params):
            if params["action"] == "query":
                return {"query": {"pages": [page(self.pair.target_title)]}}, 200
            return {"entities": {"Q1": {"id": "Q1", "sitelinks": {"enwiki": {"title": self.pair.source_title},
                                                                       "ruwiki": {"title": self.pair.target_title}}}}}, 200
        worker._api_get_json = Mock(side_effect=response)
        rows = worker._build_preview_rows_batch(batch_titles=[self.pair.target_title], source_site=source_site, target_site=target_site)
        self.assertEqual(("Q1", self.pair.source_title), (rows[0]["qid"], rows[0]["source_title"]))
        title_request = worker._api_get_json.call_args_list[1].args[1]
        self.assertEqual("ruwiki", title_request["sites"])
        self.assertEqual(self.pair.target_title, title_request["titles"])

    def test_sync_preview_api_failure_is_an_error_instead_of_an_empty_result(self):
        from wiki_cat_tool.workers.category_content_sync_worker import CategoryContentSyncPreviewWorker
        worker = CategoryContentSyncPreviewWorker(target_categories=[self.pair.target_title], source_lang="en", target_lang="ru",
                                                  family="wikipedia", process_articles=True, process_subcategories=False, article_depth=0)
        self.addCleanup(worker.api_client.session.close)
        reader = Mock(request=Mock(side_effect=APIRequestError("offline")))
        with patch("wiki_cat_tool.workers.category_content_sync_worker.CategoryTransferReader", return_value=reader):
            # Class constants must retain their real value despite replacing the constructor.
            from wiki_cat_tool.workers import category_content_sync_worker as module
            module.CategoryTransferReader._MAX_QUERY_URL_BYTES = 6000
            with self.assertRaises(APIRequestError):
                worker._api_get_json("https://ru.wikipedia.org/w/api.php", {"action": "query", "titles": self.pair.target_title})

    def test_standalone_sync_can_resolve_the_existing_same_name_category(self):
        self.worker.category_pairs = {}
        self.worker._get_linked_page_title_retry = Mock(return_value=None)
        target = Mock()
        target.title.return_value = "A"
        source = Mock()
        source.title.return_value = "Category:A"
        source.exists.return_value = True
        source.isRedirectPage.return_value = False
        with patch("wiki_cat_tool.workers.category_content_sync_worker.pywikibot.Category", side_effect=[target, source]):
            resolved, _ = self.worker._resolve_source_category_with_retry("Категория:A", Mock(), Mock())
        self.assertEqual("Category:A", resolved)

    def test_sync_reuses_discovery_matching_and_saves_duplicate_target_only_once(self):
        for name in ("One", "Duplicate", "Missing"):
            self.reader.page_data["en"][name] = page(name, ns=0, qid=name)
        self.reader.links.update({"One": "Одна", "Duplicate": "Одна", "Missing": "Нет"})
        self.reader.page_data["ru"]["Одна"] = page("Одна", ns=0)
        members = [Mock(title=Mock(return_value=name)) for name in ("One", "Duplicate", "Missing")]
        target = Mock()
        target.exists.return_value = True
        target.isRedirectPage.return_value = False
        target.get.return_value = "Article"
        target.title.return_value = "Одна"
        self.worker._save_with_retry = Mock(return_value=True)
        self.worker._category_already_exists = Mock(return_value=False)
        self.worker._build_default_summary = Mock(return_value="summary")
        with patch("wiki_cat_tool.workers.category_content_sync_worker.CategoryTransferReader", return_value=self.reader), \
             patch("wiki_cat_tool.workers.category_content_sync_worker.pywikibot.Page", return_value=target):
            stats = self.worker._transfer_articles("Category:A", "Категория:А", Mock(), Mock(),
                                                  source_members=members, row_key="test")
        self.assertEqual(1, stats["success"])
        self.assertEqual(2, stats["skipped"])
        self.worker._save_with_retry.assert_called_once()

    def test_sync_does_not_save_articles_when_matching_api_fails(self):
        self.reader.article_matches = Mock(side_effect=APIRequestError("offline"))
        self.worker._save_with_retry = Mock()
        with patch("wiki_cat_tool.workers.category_content_sync_worker.CategoryTransferReader", return_value=self.reader):
            stats = self.worker._transfer_articles("Category:A", "Категория:А", Mock(), Mock(),
                                                  source_members=[Mock(title=Mock(return_value="One"))], row_key="test")
        self.assertEqual(1, stats["error"])
        self.worker._save_with_retry.assert_not_called()

    def test_sync_preserves_colons_in_transferred_category_titles(self):
        from wiki_cat_tool.workers.category_content_sync_worker import _normalize_category_name
        self.assertEqual("Категория:A: B", _normalize_category_name("Категория:A: B", "wikipedia", "ru"))


if __name__ == "__main__":
    unittest.main()
