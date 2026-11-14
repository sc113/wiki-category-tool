"""Apply a previewed replacement map to the selected wiki pages."""

import html

import pywikibot
from PySide6.QtCore import Signal

from .base_worker import BaseWorker
from ..core.category_replacement import CategoryReplacements
from ..core.namespace_manager import normalize_title_by_selection


class CategoryReplacementWorker(BaseWorker):
    page_done = Signal()

    def __init__(self, *, titles, rules, username, password, lang, family, ns_selection,
                 reason, inherit_sort_key):
        super().__init__(username, password, lang, family)
        self.titles, self.rules, self.ns_selection = titles, rules, ns_selection
        self.reason, self.inherit_sort_key = reason, inherit_sort_key
        self.error_count = 0

    def run(self):
        try:
            site = pywikibot.Site(self.lang, self.family)
            site.login(user=self.username)
            namespace = site.namespaces[14]
            replacements = CategoryReplacements(
                self.rules, prefix=namespace.custom_name, aliases=tuple(namespace),
                first_letter=namespace.case == 'first-letter',
                inherit_sort_key=self.inherit_sort_key,
            )
        except Exception as exc:
            self._set_failure(exc)
            self.progress.emit(self._fmt('ui.operation_failed', error=exc))
            return

        skipped = 0
        seen = set()
        for index, title in enumerate(self.titles, 1):
            if self._stop:
                break
            self.progress.emit(self._fmt('log.redundant_worker.processing_header',
                                         index=index, total=len(self.titles), title=title))
            try:
                title = normalize_title_by_selection(
                    html.unescape(title.lstrip('\ufeff')), self.family, self.lang, self.ns_selection,
                )
                page = pywikibot.Page(site, title)
                canonical_title = page.title()
                if canonical_title in seen:
                    skipped += 1
                    continue
                seen.add(canonical_title)
                if not page.exists():
                    skipped += 1
                    self.progress.emit(self._t('log.redundant_worker.skip_page_missing'))
                    continue
                if page.content_model != 'wikitext':
                    skipped += 1
                    self.progress.emit(self._t('log.cat_replace.skip_content_model'))
                    continue
                original = page.text
                edited, changes = replacements.apply(original)
                if original == edited:
                    skipped += 1
                    self.progress.emit(self._t('log.cat_replace.no_changes'))
                    continue
                summary = replacements.summary(changes, self.reason)
                if self._save_with_retry(page, edited, summary, False):
                    self.progress.emit(self._fmt('log.redundant_worker.save_success', comment=summary))
                elif not self._stop:
                    self.error_count += 1
            except Exception as exc:
                self.error_count += 1
                self.progress.emit(self._fmt('log.redundant_worker.unexpected_error', error=exc))
            finally:
                self.page_done.emit()

        self.progress.emit(self._fmt('log.redundant_worker.summary', processed=self.saved_edits,
                                     skipped=skipped, errors=self.error_count))
