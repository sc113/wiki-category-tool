from __future__ import annotations

# ruff: noqa: E402
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QWidget
from wiki_cat_tool.gui.tabs.rename_tab import RenameTab
from wiki_cat_tool.workers.rename_worker import RenameWorker


class RenameDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.site = Mock(doc_subpage=('/doc',))
        self.pages, self.lookups, self.saved = {}, [], []
        with patch('wiki_cat_tool.workers.rename_worker.TemplateManager', return_value=Mock()):
            self.worker = RenameWorker('unused.tsv', '', '', 'en', 'wikipedia', 14,
                                       False, True, True, False, True, process_documentation=True)
        self.worker._debugf = Mock()
        self.worker._policy_prefix = lambda _namespace, _fallback: 'Category:'
        self.worker._build_summary = Mock(return_value='Category:Old → Category:New')
        self.worker._save_with_retry = Mock(side_effect=self.save)
        self.namespace_manager = Mock()
        self.namespace_manager._load_ns_info.return_value = {14: dict(all={'Category:', 'Категория:'})}
        self.main = self.page('Template:Example', '{{documentation}}')

    def page(self, title, text='', *, exists=True, redirect=False, namespace=10):
        page = Mock()
        page.title.return_value = title
        page.namespace.return_value = SimpleNamespace(id=namespace)
        page.exists.return_value, page.isRedirectPage.return_value = exists, redirect
        page.content_model = 'wikitext'
        page.text = text
        self.pages[title] = page
        return page

    def page_factory(self, _site, title):
        self.lookups.append(title)
        return self.pages.get(title) or self.page(title, exists=False)

    def save(self, page, text, summary, minor):
        self.saved.append((page.title(), text))
        page.text = text
        return True

    def move(self, titles=None):
        response = Mock(status_code=200)
        response.json.return_value = dict(query=dict(categorymembers=[dict(title=title, ns=10)
                                         for title in (titles or ['Template:Example'])]))
        api = Mock()
        api._build_api_url.return_value = 'https://example.org/w/api.php'
        with patch('wiki_cat_tool.core.api_client.WikimediaAPIClient', return_value=api), \
             patch('wiki_cat_tool.core.api_client.REQUEST_SESSION.get', return_value=response), \
             patch('wiki_cat_tool.core.api_client._rate_wait'), \
             patch('wiki_cat_tool.workers.rename_worker._ensure_title_with_ns', side_effect=lambda title, *_args: title), \
             patch('wiki_cat_tool.workers.rename_worker.pywikibot.Page', side_effect=self.page_factory), \
             patch('wiki_cat_tool.workers.rename_worker.pywikibot.Site', side_effect=AssertionError('Unexpected site lookup')), \
             patch('wiki_cat_tool.core.namespace_manager.get_namespace_manager', return_value=self.namespace_manager):
            self.worker._move_category_members(self.site, 'Category:Old', 'Category:New')

    def test_documentation_is_edited_and_main_template_is_preserved(self):
        doc = self.page('Template:Example/doc', '<includeonly>[[Category:Old|Keep key]]</includeonly>')
        self.move()
        self.assertEqual('{{documentation}}', self.main.text)
        self.assertEqual('<includeonly>[[Category:New|Keep key]]</includeonly>', doc.text)
        self.assertEqual(['Template:Example/doc'], [title for title, _text in self.saved])

    def test_disabled_option_keeps_previous_behavior_without_documentation_lookups(self):
        self.worker.process_documentation = False
        doc = self.page('Template:Example/doc', '[[Category:Old]]')
        self.move()
        self.assertEqual('[[Category:Old]]', doc.text)
        self.assertEqual([], self.saved)
        self.assertFalse(any(title.endswith('/doc') for title in self.lookups))

    def test_documentation_also_works_with_direct_transfer_disabled(self):
        self.worker.phase1_enabled = False
        self.main.text = '[[Category:Old]]\n{{documentation}}'
        doc = self.page('Template:Example/doc', '[[Category:Old]]')
        self.move()
        self.assertEqual('[[Category:Old]]\n{{documentation}}', self.main.text)
        self.assertEqual('[[Category:New]]', doc.text)
        self.assertEqual(1, len(self.saved))

    def test_main_template_and_documentation_are_both_updated_without_duplicate_doc_save(self):
        self.main.text = '[[Category:Old]]\n{{documentation}}'
        self.page('Template:Example/doc', '[[Category:Old]]')
        self.move(['Template:Example', 'Template:Example/doc'])
        self.assertEqual(['Template:Example', 'Template:Example/doc'], [title for title, _text in self.saved])

    def test_doc_member_before_parent_is_processed_once_even_without_direct_transfer(self):
        self.worker.phase1_enabled = False
        self.page('Template:Example/doc', '[[Category:Old]]')
        self.move(['Template:Example/doc', 'Template:Example'])
        self.assertEqual(['Template:Example/doc'], [title for title, _text in self.saved])
        self.assertNotIn('Template:Example/doc/doc', self.lookups)

    def test_documentation_examples_comments_and_escaped_category_links_are_preserved(self):
        samples = ['<!-- [[Category:Old]] -->', '<nowiki>[[Category:Old]]</nowiki>',
                   '<pre>[[Category:Old]]</pre>', '<source lang="wikitext">[[Category:Old]]</source>',
                   '<syntaxhighlight lang="wikitext">[[Category:Old]]</syntaxhighlight>', '[[:Category:Old]]']
        doc = self.page('Template:Example/doc', '\n'.join(samples) + '\n<includeonly>[[Category:Old|Key]]</includeonly>')
        self.move()
        self.assertEqual('\n'.join(samples) + '\n<includeonly>[[Category:New|Key]]</includeonly>', doc.text)
        self.assertEqual(1, len(self.saved))

    def test_localized_documentation_suffix_and_module_pages_are_supported(self):
        self.site.doc_subpage = ('/Documentation',)
        main = self.page('Module:Example', 'return {}', namespace=828)
        doc = self.page('Module:Example/Documentation', '<includeonly>[[Category:Old]]</includeonly>', namespace=828)
        self.move(['Module:Example'])
        self.assertEqual('return {}', main.text)
        self.assertEqual('<includeonly>[[Category:New]]</includeonly>', doc.text)

    def test_articles_and_documentation_subpages_are_not_followed_to_doc_doc(self):
        self.page('Article', 'text', namespace=0)
        self.page('Article/doc', '[[Category:Old]]', namespace=0)
        self.page('Template:Example/doc', 'text')
        self.page('Template:Example/doc/doc', '[[Category:Old]]')
        self.move(['Article', 'Template:Example/doc'])
        self.assertEqual([], self.saved)
        self.assertNotIn('Article/doc', self.lookups)
        self.assertNotIn('Template:Example/doc/doc', self.lookups)

    def test_bare_localized_suffix_stays_inside_the_documented_template_subpages(self):
        self.site.doc_subpage = ('Doku',)
        doc = self.page('Template:Example/Doku', '[[Category:Old]]')
        unrelated = self.page('Template:ExampleDoku', '[[Category:Old]]')
        self.move()
        self.assertEqual('[[Category:New]]', doc.text)
        self.assertEqual('[[Category:Old]]', unrelated.text)
        self.assertNotIn('Template:ExampleDoku', self.lookups)

    def test_redirect_parent_does_not_trigger_documentation_edits(self):
        self.main.isRedirectPage.return_value = True
        doc = self.page('Template:Example/doc', '[[Category:Old]]')
        self.move()
        self.assertEqual([], self.saved)
        self.assertEqual('[[Category:Old]]', doc.text)

    def test_documentation_redirect_and_missing_pages_are_skipped_without_following(self):
        doc = self.page('Template:Example/doc', '#REDIRECT [[Template:Other/doc]]', redirect=True)
        self.move()
        self.assertEqual([], self.saved)
        doc.getRedirectTarget.assert_not_called()
        self.assertEqual(0, self.worker.error_count)
        doc.exists.return_value = False
        self.move()
        self.assertEqual(0, self.worker.error_count)

    def test_read_errors_identify_the_documentation_page(self):
        doc = self.page('Template:Example/doc')
        doc.exists.side_effect = RuntimeError('read failed')
        self.move()
        self.assertEqual(1, self.worker.error_count)
        self.assertIn('Template:Example/doc', self.worker.error_pages)
        self.assertEqual([], self.saved)

    def test_lua_or_other_code_in_a_doc_subpage_is_not_rewritten_as_wikitext(self):
        doc = self.page('Template:Example/doc', 'return "[[Category:Old]]"')
        doc.content_model = 'Scribunto'
        self.move()
        self.assertEqual([], self.saved)
        self.assertEqual('return "[[Category:Old]]"', doc.text)

    def test_stop_after_first_doc_does_not_process_another_localized_doc(self):
        self.site.doc_subpage = ('/doc', '/Documentation')
        self.page('Template:Example/doc', '[[Category:Old]]')
        other = self.page('Template:Example/Documentation', '[[Category:Old]]')
        def save_and_stop(*args):
            result = self.save(*args)
            self.worker.request_stop()
            return result
        self.worker._save_with_retry.side_effect = save_and_stop
        self.move()
        self.assertEqual(1, len(self.saved))
        self.assertEqual('[[Category:Old]]', other.text)

    def test_documentation_only_mode_still_runs_category_content_transfer(self):
        self.worker.phase1_enabled = self.worker.find_in_templates = self.worker.move_category = False
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'categories.tsv'
            path.write_text('Category:Old\tCategory:New\n', encoding='utf-8-sig')
            self.worker.tsv_path = str(path)
            with patch('wiki_cat_tool.workers.rename_worker.pywikibot.Site', return_value=self.site), \
                 patch('wiki_cat_tool.workers.rename_worker.normalize_title_by_selection', side_effect=lambda title, *_args: title), \
                 patch.object(self.worker, '_move_category_members') as move:
                self.worker.run()
        move.assert_called_once_with(self.site, 'Category:Old', 'Category:New')


class RenameDocumentationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_separate_checkbox_has_help_and_is_passed_to_the_worker(self):
        parent = QWidget()
        parent._ui_lang, parent.current_lang, parent.current_family = 'ru', 'ru', 'wikipedia'
        parent.current_user, parent.current_password = 'User', 'unused'
        tab = RenameTab(parent)
        self.assertIn('/doc', tab.documentation_cb.text())
        self.assertFalse(tab.documentation_cb.isChecked())
        self.assertIn('nowiki', tab.documentation_cb.toolTip())
        tab.documentation_cb.setChecked(True)
        tab.phase1_enabled_cb.setChecked(False)
        tab.find_in_templates_cb.setChecked(False)
        tab.rename_ns_combo.clear()
        tab.rename_ns_combo.addItem('Категории', 14)
        with patch('wiki_cat_tool.gui.tabs.rename_tab.apply_pwb_config'), \
             patch('wiki_cat_tool.gui.tabs.rename_tab.RenameWorker') as factory:
            tab.start_rename()
        self.assertTrue(factory.call_args.kwargs['process_documentation'])
        self.assertFalse(factory.call_args.args[10])
        factory.return_value.start.assert_called_once()
        parent.close()
        parent.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)


if __name__ == '__main__':
    unittest.main()
