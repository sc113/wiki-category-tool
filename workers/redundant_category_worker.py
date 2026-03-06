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
    load_precise_to_broad_list_map,
    analyze_page_text,
    build_comment,
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
                    f'Ошибка авторизации: {type(exc).__name__}: {exc}')
                return

        try:
            precise_to_broad_map = load_precise_to_broad_list_map(
                self.categories_path, self.family, self.lang)
        except Exception as exc:
            self.progress.emit(
                f"Ошибка при чтении файла пар категорий '{self.categories_path}': {exc}")
            return

        if not precise_to_broad_map:
            self.progress.emit(
                f"Файл пар категорий '{self.categories_path}' пуст или не содержит валидных пар.")
            return

        total_titles = len(self.titles)
        self.progress.emit('Обработка запущена.')
        self.progress.emit(
            f"Загружено {total_titles} названий страниц для обработки.")
        self.progress.emit(
            f"Загружено {len(precise_to_broad_map)} уникальных точных категорий "
            f"с {sum(len(value) for value in precise_to_broad_map.values())} ассоциациями к широким категориям."
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
                f"========== Обработка {idx}/{total_titles}: '{title}' ==========")
            try:
                page = pywikibot.Page(site, norm_title)
                if not page.exists():
                    self.progress.emit('  ПРОПУСК: Страница не найдена.')
                    skipped_count += 1
                    continue

                old_text = page.text
                new_text, precise_to_broad_found, original_names = analyze_page_text(
                    page, precise_to_broad_map, self.family, self.lang)

                if precise_to_broad_found:
                    for broad_norm, precise_norm in precise_to_broad_found.items():
                        original_broad = original_names.get(broad_norm, broad_norm)
                        original_precise = original_names.get(precise_norm, precise_norm)
                        self.progress.emit(
                            f"  Найдена пара: '{original_precise}' (оставляем) -> '{original_broad}' (удаляем)")

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
                                    f'  УСПЕХ: Страница сохранена. Комментарий: "{comment}"')
                                processed_count += 1
                            else:
                                error_count += 1
                        else:
                            self.progress.emit(
                                '  ПРОПУСК: Не удалось создать комментарий.')
                            skipped_count += 1
                    else:
                        self.progress.emit(
                            '  ⏭️ ПРОПУСК: Пары найдены, но текст страницы не изменился.')
                        skipped_count += 1
                else:
                    self.progress.emit(
                        '  ⏭️ ПРОПУСК: Подходящих пар категорий не найдено.')
                    skipped_count += 1
            except Exception as exc:
                self.progress.emit(
                    f'  НЕПРЕДВИДЕННАЯ ОШИБКА обработки: {exc}')
                self.progress.emit(traceback.format_exc().rstrip())
                error_count += 1
            finally:
                try:
                    self.page_done.emit()
                except Exception:
                    pass

        if self._stop:
            self.progress.emit('========== Обработка остановлена ==========')
        else:
            self.progress.emit('========== Обработка завершена ==========')
        self.progress.emit(
            f'Итоги: Успешно обработано: {processed_count}, Пропущено: {skipped_count}, Ошибок: {error_count}.')
