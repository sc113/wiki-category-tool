from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from wiki_cat_tool.workers.category_fetch_worker import CategoryFetchWorker


class CategoryFetchTests(unittest.TestCase):
    def make_worker(self, depth=99):
        worker = CategoryFetchWorker(
            category="Category:Root", lang="en", family="wikipedia",
            depth=depth, mode="categories_only",
        )
        worker.api_client._rate_wait = Mock()
        worker.api_client._rate_backoff = Mock()
        worker._announce_expected_read_total = Mock()
        return worker

    @staticmethod
    def response(payload, status=200):
        return Mock(status_code=status, json=Mock(return_value=payload))

    def install_graph(self, worker, graph, count_overrides=None):
        overrides = count_overrides or {}
        calls = []

        def get(_url, *, params, **_kwargs):
            title = params.get("gcmtitle", params.get("cmtitle"))
            calls.append(title)
            members = []
            for child in graph.get(title, []):
                member = {"title": child, "ns": 14}
                if params.get("generator"):
                    count = overrides.get(child, len(graph.get(child, [])))
                    if count is not None:
                        member["categoryinfo"] = {"subcats": count}
                members.append(member)
            key = "pages" if params.get("generator") else "categorymembers"
            return self.response({"batchcomplete": True, "query": {key: members}})

        worker.api_client.session.get = Mock(side_effect=get)
        return calls

    def test_terminal_categories_are_kept_without_reading_their_children(self):
        worker = self.make_worker()
        calls = self.install_graph(worker, {
            "Category:Root": ["Category:Terminal", "Category:Branch"],
            "Category:Branch": ["Category:Nested terminal"],
        })
        results = []
        worker.result_ready.connect(lambda titles, stats: results.append((titles, stats)))

        worker.run()

        self.assertEqual(["Category:Root", "Category:Branch"], calls)
        self.assertEqual([([
            "Category:Branch", "Category:Nested terminal", "Category:Terminal",
        ], {"categories": 3, "non_categories": 0})], results)

    def test_depth_limits_keep_the_existing_direct_subcategory_semantics(self):
        graph = {
            "Category:Root": ["Category:Branch"],
            "Category:Branch": ["Category:Leaf"],
            "Category:Leaf": ["Category:Beyond"],
        }
        expected = [
            {"Category:Branch"},
            {"Category:Branch", "Category:Leaf"},
            {"Category:Branch", "Category:Leaf", "Category:Beyond"},
        ]
        for depth, expected_titles in enumerate(expected):
            with self.subTest(depth=depth):
                worker = self.make_worker(depth)
                calls = self.install_graph(worker, graph)
                titles, depths = worker._fetch_subcategory_tree(
                    category="Category:Root", lang="en", fam="wikipedia",
                    max_depth=depth,
                )
                self.assertEqual(expected_titles, set(titles))
                self.assertEqual(depth + 1, len(calls))
                self.assertEqual(depth + 1, max(depths.values()))
                last_params = worker.api_client.session.get.call_args.kwargs["params"]
                self.assertEqual("subcat", last_params["cmtype"])
                self.assertNotIn("prop", last_params)

    def test_cycles_and_shared_categories_keep_the_shortest_depth(self):
        worker = self.make_worker(2)
        calls = self.install_graph(worker, {
            "Category:Root": ["Category:Terminal", "Category:Long", "Category:Short"],
            "Category:Long": ["Category:Root", "Category:Middle"],
            "Category:Short": ["Category:Shared"],
            "Category:Middle": ["Category:Shared"],
            "Category:Shared": ["Category:Deep"],
        })

        titles, depths = worker._fetch_subcategory_tree(
            category="Category:Root", lang="en", fam="wikipedia", max_depth=2,
        )

        self.assertEqual({
            "Category:Terminal", "Category:Long", "Category:Short",
            "Category:Middle", "Category:Shared", "Category:Deep",
        }, set(titles))
        self.assertEqual(6, len(titles))
        self.assertEqual(2, depths["category:shared"])
        self.assertEqual(3, depths["category:deep"])
        self.assertEqual(1, calls.count("Category:Shared"))
        self.assertNotIn("Category:Terminal", calls)

    def test_missing_or_invalid_counts_do_not_skip_a_branch(self):
        worker = self.make_worker()
        counts = {
            "Category:Missing": None,
            "Category:String": "0",
            "Category:Negative": -1,
            "Category:Fraction": 0.5,
            "Category:Boolean": False,
            "Category:Terminal": 0,
        }
        graph = {"Category:Root": list(counts)}
        for title in counts:
            if title != "Category:Terminal":
                graph[title] = ["Category:Child of " + title.split(":", 1)[1]]
        calls = self.install_graph(worker, graph, counts)

        titles, _depths = worker._fetch_subcategory_tree(
            category="Category:Root", lang="en", fam="wikipedia", max_depth=99,
        )

        self.assertEqual(set(counts) - {"Category:Terminal"}, set(calls[1:]))
        self.assertIn("Category:Terminal", titles)
        self.assertEqual(11, len(titles))

    def test_property_continuation_updates_counts_without_duplicate_titles(self):
        worker = self.make_worker()
        worker.api_client.session.get = Mock(side_effect=[
            self.response({
                "query": {"pages": [
                    {"title": "Category:A"},
                    {"title": "Category:B", "categoryinfo": {"subcats": 1}},
                ]},
                "continue": {"cicontinue": "property", "gcmcontinue": "next", "continue": "||"},
            }),
            self.response({
                "query": {"pages": {
                    "1": {"title": "Category:A", "categoryinfo": {"subcats": 0}},
                }},
                "continue": {"gcmcontinue": "next", "continue": "||"},
            }),
            self.response({
                "query": {"pages": [
                    {"title": "Category:C", "categoryinfo": {"subcats": 0}},
                    {"title": "Category:B", "categoryinfo": {"subcats": 0}},
                ]},
            }),
            self.response({
                "query": {"pages": [
                    {"title": "Category:D", "categoryinfo": {"subcats": 0}},
                ]},
            }),
        ])

        titles, depths = worker._fetch_subcategory_tree(
            category="Category:Root", lang="en", fam="wikipedia", max_depth=99,
        )

        self.assertEqual(["Category:A", "Category:B", "Category:C", "Category:D"], titles)
        self.assertEqual(2, depths["category:d"])
        params = [call.kwargs["params"] for call in worker.api_client.session.get.call_args_list]
        self.assertIn("cicontinue", params[1])
        self.assertNotIn("cicontinue", params[2])
        self.assertEqual("Category:B", params[3]["gcmtitle"])

    def test_read_errors_report_failure_instead_of_an_empty_success(self):
        broken_json = self.response({})
        broken_json.json.side_effect = ValueError("invalid JSON")
        cases = [
            self.response({}, status=503),
            self.response({"error": {"code": "badvalue", "info": "Invalid generator"}}),
            broken_json,
        ]
        for response in cases:
            with self.subTest(response=response):
                worker = self.make_worker()
                worker.api_client.session.get = Mock(return_value=response)
                results, failures = [], []
                worker.result_ready.connect(lambda *args: results.append(args))
                worker.failed.connect(failures.append)

                worker.run()

                self.assertEqual([], results)
                self.assertEqual(1, len(failures))

    def test_failure_in_later_batch_does_not_publish_a_partial_success(self):
        worker = self.make_worker()
        worker.api_client.session.get = Mock(side_effect=[
            self.response({
                "query": {"pages": [{"title": "Category:Found", "categoryinfo": {"subcats": 0}}]},
                "continue": {"gcmcontinue": "next", "continue": "||"},
            }),
            self.response({}, status=503),
        ])
        results, failures = [], []
        worker.result_ready.connect(lambda *args: results.append(args))
        worker.failed.connect(failures.append)

        worker.run()

        self.assertEqual([], results)
        self.assertEqual(1, len(failures))

    def test_rate_limit_and_network_retries_preserve_the_request(self):
        worker = self.make_worker()
        worker.api_client.session.get = Mock(side_effect=[
            self.response({}, status=429),
            TimeoutError("temporary timeout"),
            self.response({"query": {"pages": [
                {"title": "Category:Terminal", "categoryinfo": {"subcats": 0}},
            ]}}),
        ])

        titles, _depths = worker._fetch_subcategory_tree(
            category="Category:Root", lang="en", fam="wikipedia", max_depth=99,
        )

        self.assertEqual(["Category:Terminal"], titles)
        self.assertEqual(2, worker.api_client._rate_backoff.call_count)
        params = [call.kwargs["params"] for call in worker.api_client.session.get.call_args_list]
        self.assertEqual([params[0]] * 3, params)

    def test_stop_during_rate_wait_does_not_start_another_request(self):
        worker = self.make_worker()
        worker.api_client._rate_wait.side_effect = worker.request_stop
        worker.api_client.session.get = Mock()
        results, failures = [], []
        worker.result_ready.connect(lambda *args: results.append(args))
        worker.failed.connect(failures.append)

        worker.run()

        worker.api_client.session.get.assert_not_called()
        self.assertEqual([], results)
        self.assertEqual([], failures)

    def test_stop_during_request_does_not_publish_failure_or_success(self):
        worker = self.make_worker()

        def get(*_args, **_kwargs):
            worker.request_stop()
            return self.response({}, status=503)

        worker.api_client.session.get = Mock(side_effect=get)
        results, failures = [], []
        worker.result_ready.connect(lambda *args: results.append(args))
        worker.failed.connect(failures.append)

        worker.run()

        self.assertEqual(1, worker.api_client.session.get.call_count)
        self.assertEqual([], results)
        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
