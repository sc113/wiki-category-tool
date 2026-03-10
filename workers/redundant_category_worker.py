# -*- coding: utf-8 -*-
"""
Worker для удаления избыточных родительских категорий.
"""

import html
import traceback

import pywikibot
from PySide6.QtCore import Signal

from .base_worker import BaseWorker
from ..core.namespace_manager import normalize_title_by_selection
from ..core.redundant_category_logic import (
    REDUNDANT_MODE_DEDUP,
    REDUNDANT_MODE_PAIRS,
    analyze_page_text,
    build_comment,
    build_dedupe_comment,
    deduplicate_target_categories_in_text,
    load_redundant_category_rules,
)


class RedundantCategoryWorker(BaseWorker):
    """Пакетно удаляет из текста страницы широкие категории при наличии точных."""

    page_done = Signal()

    def __init__(
        self,
        titles: list[str],
        categories_path: str,
        username: str,
        password: str,
        lang: str,
        family: str,
        ns_selection: str | int,
        single_template: str,
        multi_template: str,
        pair_template: str,
    ):
        super().__init__(username, password, lang, family)
        self.titles = list(titles or [])
        self.categories_path = categories_path
        self.ns_sel = ns_selection
        self.single_template = single_template
        self.multi_template = multi_template
        self.pair_template = pair_template

    def run(self):
        site = pywikibot.Site(self.lang, self.family)
        if self.username and self.password:
            try:
                site.login(user=self.username)
            except Exception as exc:
                self.progress.emit(
                    self._fmt('log.redundant_worker.auth_error', error_type=type(exc).__name__, error=exc))
                return

        try:
            mode, precise_to_broad_map, dedupe_categories = load_redundant_category_rules(
                self.categories_path, self.family, self.lang)
        except Exception as exc:
            self.progress.emit(
                self._fmt('log.redundant_worker.file_read_error', path=self.categories_path, error=exc))
            return

        if mode == REDUNDANT_MODE_PAIRS:
            if not precise_to_broad_map:
                self.progress.emit(
                    self._fmt('log.redundant_worker.empty_pairs_file', path=self.categories_path)
                )
                return
        elif mode == REDUNDANT_MODE_DEDUP:
            if not dedupe_categories:
                self.progress.emit(
                    self._fmt('log.redundant_worker.empty_categories_file', path=self.categories_path)
                )
                return
        else:
            self.progress.emit(self._fmt('log.redundant_worker.unknown_mode', mode=mode))
            return

        total_titles = len(self.titles)
        self.progress.emit(self._t('log.redundant_worker.started'))
        self.progress.emit(
            self._fmt('log.redundant_worker.loaded_titles', count=total_titles))
        if mode == REDUNDANT_MODE_PAIRS:
            self.progress.emit(
                self._fmt(
                    'log.redundant_worker.loaded_precise_categories',
                    categories=len(precise_to_broad_map),
                    associations=sum(len(value) for value in precise_to_broad_map.values()),
                )
            )
        else:
            self.progress.emit(
                self._t('log.redundant_worker.dedupe_mode')
            )
            self.progress.emit(
                self._fmt('log.redundant_worker.loaded_dedupe_categories', count=len(dedupe_categories))
            )

        processed_count = 0
        skipped_count = 0
        error_count = 0

        for idx, source_title in enumerate(self.titles, start=1):
            if self._stop:
                break

            title = html.unescape((source_title or '').strip().lstrip('\ufeff'))
            norm_title = normalize_title_by_selection(
                title, self.family, self.lang, self.ns_sel)
            self.progress.emit(
                self._fmt('log.redundant_worker.processing_header', index=idx, total=total_titles, title=title))
            try:
                page = pywikibot.Page(site, norm_title)
                if not page.exists():
                    self.progress.emit(self._t('log.redundant_worker.skip_page_missing'))
                    skipped_count += 1
                    continue

                old_text = page.text
                if mode == REDUNDANT_MODE_PAIRS:
                    new_text, precise_to_broad_found, original_names = analyze_page_text(
                        page, precise_to_broad_map, self.family, self.lang)

                    if precise_to_broad_found:
                        for broad_norm, precise_norm in precise_to_broad_found.items():
                            original_broad = original_names.get(broad_norm, broad_norm)
                            original_precise = original_names.get(precise_norm, precise_norm)
                            self.progress.emit(
                                self._fmt(
                                    'log.redundant_worker.found_pair',
                                    precise=original_precise,
                                    broad=original_broad,
                                ))

                        if new_text != old_text:
                            comment = build_comment(
                                precise_to_broad_found,
                                original_names,
                                self.family,
                                self.lang,
                                self.single_template,
                                self.multi_template,
                                self.pair_template,
                            )
                            if comment:
                                ok = self._save_with_retry(
                                    page, new_text, comment, False)
                                if ok:
                                    self.progress.emit(
                                        self._fmt('log.redundant_worker.save_success', comment=comment))
                                    processed_count += 1
                                else:
                                    error_count += 1
                            else:
                                self.progress.emit(
                                    self._t('log.redundant_worker.skip_comment_missing'))
                                skipped_count += 1
                        else:
                            self.progress.emit(
                                self._t('log.redundant_worker.skip_pairs_no_text_change'))
                            skipped_count += 1
                    else:
                        self.progress.emit(
                            self._t('log.redundant_worker.skip_pairs_not_found'))
                        skipped_count += 1
                else:
                    new_text, removed_counts, original_names = deduplicate_target_categories_in_text(
                        old_text,
                        dedupe_categories,
                        self.family,
                        self.lang,
                    )
                    if removed_counts:
                        for category_norm, removed_total in removed_counts.items():
                            display_name = original_names.get(category_norm, category_norm)
                            self.progress.emit(
                                self._fmt(
                                    'log.redundant_worker.found_duplicates',
                                    category=display_name,
                                    count=removed_total,
                                )
                            )
                        if new_text != old_text:
                            comment = build_dedupe_comment(self.lang)
                            ok = self._save_with_retry(page, new_text, comment, False)
                            if ok:
                                self.progress.emit(
                                    self._fmt('log.redundant_worker.save_success', comment=comment))
                                processed_count += 1
                            else:
                                error_count += 1
                        else:
                            self.progress.emit(
                                self._t('log.redundant_worker.skip_duplicates_no_text_change'))
                            skipped_count += 1
                    else:
                        self.progress.emit(
                            self._t('log.redundant_worker.skip_duplicates_not_found'))
                        skipped_count += 1
            except Exception as exc:
                self.progress.emit(
                    self._fmt('log.redundant_worker.unexpected_error', error=exc))
                self.progress.emit(traceback.format_exc().rstrip())
                error_count += 1
            finally:
                try:
                    self.page_done.emit()
                except Exception:
                    pass

        if self._stop:
            self.progress.emit(self._t('log.redundant_worker.stopped'))
        else:
            self.progress.emit(self._t('log.redundant_worker.completed'))
        self.progress.emit(
            self._fmt(
                'log.redundant_worker.summary',
                processed=processed_count,
                skipped=skipped_count,
                errors=error_count,
            ))
