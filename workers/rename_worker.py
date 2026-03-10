"""
Rename worker for renaming pages and moving category members in Wikimedia projects.
"""

import csv
import html
from threading import Event
from PySide6.QtCore import Signal
import pywikibot

from .base_worker import BaseWorker
from ..core.namespace_manager import normalize_title_by_selection, title_has_ns_prefix, _ensure_title_with_ns

from ..core.template_manager import TemplateManager
from ..core.localization import translate_runtime
from ..constants import DEFAULT_EN_NS
from ..utils import format_russian_pages_nominative
from ..utils import align_first_letter_case
from ..utils import debug


def _chars(*codes: int) -> str:
    return ''.join(chr(code) for code in codes)


_CYR_II = _chars(1080, 1080)
_CYR_IYA_LOWER = _chars(1080, 1103)
_CYR_IYA_UPPER = _chars(1048, 1071)
_CYR_LE = _chars(1083, 1077)
_CYR_SOFT = _chars(1100)
_CYR_GE = _chars(1075, 1077)
_CYR_KE = _chars(1082, 1077)
_CYR_HE = _chars(1093, 1077)
_CYR_G_UP = _chars(1043)
_CYR_G_LOW = _chars(1075)
_CYR_K_UP = _chars(1050)
_CYR_K_LOW = _chars(1082)
_CYR_H_UP = _chars(1061)
_CYR_H_LOW = _chars(1093)
_CYR_A_UP = _chars(1040)
_CYR_A_LOW = _chars(1072)
_CYR_AE = _chars(1072, 1077)
_CYR_OE = _chars(1086, 1077)
_CYR_SHORT_I_LOW = _chars(1081)
_CYR_SHORT_I_UP = _chars(1049)
_CYR_E = _chars(1077)
_CYR_I = _chars(1080)
_CYR_OY = _chars(1086, 1081)
_CYR_AYA = _chars(1072, 1103)
_CYR_VOWELS = _chars(1072, 1077, 1105, 1080, 1086, 1091, 1099, 1101, 1102, 1103) + 'AEIOUY' + _chars(1040, 1054, 1069, 1048, 1059, 1067, 1045, 1025, 1070, 1071)
_CYR_WORD_PATTERN = '[' + _chars(1072) + '-' + _chars(1103) + _chars(1105) + _chars(1040) + '-' + _chars(1071) + _chars(1025) + 'a-zA-Z\\-]+'


class RenameWorker(BaseWorker):
    """
    Worker для переименования страниц и переноса содержимого категорий.
    
    Поддерживает:
    - Переименование страниц с созданием перенаправлений
    - Перенос содержимого категорий (прямые ссылки и шаблоны)
    - Интеграцию с TemplateManager для обработки шаблонов
    - Диалоги проверки изменений в шаблонах
    """
    
    template_review_request = Signal(object)
    review_response = Signal(object)
    # Структурированные события для UI
    log_event = Signal(object)
    # Прогресс по TSV: инициализация общего количества и инкремент по строкам
    tsv_progress_init = Signal(int)
    tsv_progress_inc = Signal()
    # Внутренний прогресс по участникам текущей категории
    inner_progress_init = Signal(int)
    inner_progress_inc = Signal()
    inner_progress_reset = Signal()
    
    def __init__(self, tsv_path, username, password, lang, family, ns_selection: str | int, 
                 leave_cat_redirect: bool, leave_other_redirect: bool, move_members: bool, 
                 find_in_templates: bool, phase1_enabled: bool, move_category: bool = True,
                 override_comment: str = '', title_regex: str = '', use_locatives: bool = False):
        """
        Инициализация RenameWorker.
        
        Args:
            tsv_path: Путь к TSV файлу с данными для переименования
            username: Имя пользователя для авторизации
            password: Пароль для авторизации
            lang: Код языка
            family: Семейство проекта
            ns_selection: Выбор пространства имен
            leave_cat_redirect: Оставлять перенаправления для категорий
            leave_other_redirect: Оставлять перенаправления для остальных страниц
            move_members: Переносить содержимое категорий
            find_in_templates: Искать в шаблонах
            phase1_enabled: Включить фазу 1 (прямые ссылки)
            move_category: Переименовывать саму категорию
        """
        super().__init__(username, password, lang, family)
        self.tsv_path = tsv_path
        self.ns_sel = ns_selection
        self.leave_cat_redirect = leave_cat_redirect
        self.leave_other_redirect = leave_other_redirect
        self.move_members = move_members
        self.move_category = move_category
        self.find_in_templates = find_in_templates
        self.phase1_enabled = phase1_enabled
        # Фильтр заголовков (регулярное выражение Python)
        self.title_regex = (title_regex or '').strip()
        try:
            import re as _re
            self._title_regex_compiled = _re.compile(self.title_regex) if self.title_regex else None
        except Exception:
            self._title_regex_compiled = None
        
        # Пользовательский комментарий, который переопределяет комментарий из TSV
        self.override_comment = (override_comment or '').strip()
        
        # Текущий комментарий строки TSV (используется в операциях переноса содержимого)
        self._current_row_reason: str = ''
        
        # Template manager for handling template rules
        self.template_manager = TemplateManager()
        # Включение эвристики локативов
        self.use_locatives = bool(use_locatives)
        
        # Dialog communication
        self._prompt_events: dict[int, Event] = {}
        self._prompt_results: dict[int, str] = {}
        self._req_seq = 0
        
        try:
            self.review_response.connect(self._on_review_response)
        except Exception:
            pass

    def _policy_prefix(self, ns_id: int, fallback: str) -> str:
        """Возвращает локализованный префикс для пространства имён с фолбэком."""
        try:
            from ..core.namespace_manager import get_namespace_manager
            ns_manager = get_namespace_manager()
            return ns_manager.get_policy_prefix(self.family, self.lang, ns_id, DEFAULT_EN_NS.get(ns_id, fallback))
        except Exception:
            return fallback

    def _tr(self, key: str, default: str = '') -> str:
        return translate_runtime(key, default)

    def _tf(self, key: str, default: str = '', **kwargs) -> str:
        text = self._tr(key, default)
        try:
            return text.format(**kwargs)
        except Exception:
            return text

    def _emitf(self, key: str, default: str = '', **kwargs) -> None:
        self.progress.emit(self._tf(key, default, **kwargs))

    def _debugf(self, key: str, default: str = '', **kwargs) -> None:
        debug(self._tf(key, default, **kwargs))

    def _kind_label(self, kind: str) -> str:
        return self._tr(f'log.rename_worker.kind.{kind}', kind.title())

    def _kind_moved_text(self, kind: str) -> str:
        return self._tr(f'log.rename_worker.kind_moved.{kind}', f'{self._kind_label(kind)} moved')

    def _template_categories_label(self) -> str:
        return f"{self._policy_prefix(10, DEFAULT_EN_NS.get(10, 'Template:'))}{self._tr('log.rename_worker.template_categories_name', 'Categories')}"

    def _auto_note_text(self, count: int) -> str:
        if int(count or 0) == 1:
            return self._tr('log.rename_worker.auto_applied_single', 'automatically')
        return self._tf(
            'log.rename_worker.auto_applied_many',
            'automatically ({count} changes)',
            count=count,
        )

    def _format_template_label(self, template_name: str, partial: bool = False) -> str:
        """Формирует локализованную метку шаблона с учётом политики NS-10.

        Пример: локализованный префикс (NS-10) для текущего проекта/языка.
        """
        try:
            tm = self.template_manager
        except Exception:
            tm = None
        try:
            base = tm._strip_tmpl_prefix(template_name, self.family, self.lang) if tm else template_name
            prefix = self._policy_prefix(10, DEFAULT_EN_NS.get(10, 'Template:'))
            label = f"{prefix}{base}" if base else f"{prefix}{template_name}"
        except Exception:
            label = f"{DEFAULT_EN_NS.get(10, 'Template:')}{template_name}"
        if partial:
            try:
                label = f"{label}{self._tr('log.rename_worker.partial_tag', ' [partial]')}"
            except Exception:
                pass
        return label

    def _page_kind(self, page: pywikibot.Page) -> str:
        """Возвращает тип объекта для сообщений лога: 'категория' | 'статья' | 'страница'."""
        try:
            nsid = page.namespace().id
            return 'category' if nsid == 14 else 'article'
        except Exception:
            return 'page'

    def _build_summary(self, old_full: str, new_full: str, mode: str = 'move', template_label: str = '') -> str:
        """Собрать комментарий к правке в требуемом формате.

        mode: 'move' | 'phase1' | 'template'
        - move: [[Old]] → [[New]] — reason
        - phase1: [[OldCat]] → [[NewCat]] — reason
        - template: [[OldCat]] → [[NewCat]] (категоризация через [[Шаблон:Имя]], [[Шаблон:Имя2]]…) — reason
        """
        reason_text = self.override_comment or (self._current_row_reason or '')
        base = f"[[{old_full}]] → [[{new_full}]]"
        if mode == 'template':
            # Поддержка нескольких шаблонов: «t1, t2» → [[t1]], [[t2]]
            if template_label:
                try:
                    labels = [s.strip() for s in str(template_label).split(',') if (s or '').strip()]
                except Exception:
                    labels = []
            else:
                labels = []
            if not labels:
                try:
                    labels = [f"{self._policy_prefix(10, DEFAULT_EN_NS.get(10, 'Template:'))}{self._tr('log.rename_worker.summary.template_name_fallback', 'Name')}"]
                except Exception:
                    labels = [f"{DEFAULT_EN_NS.get(10, 'Template:')}{self._tr('log.rename_worker.summary.template_name_fallback', 'Name')}"]
            formatted = ', '.join(f"[[{lbl}]]" for lbl in labels)
            base = f"{base}{self._tf('log.rename_worker.summary.template_via', ' (categorization via {templates})', templates=formatted)}"
        if reason_text:
            return f"{base} — {reason_text}"
        return base

    def _extract_changed_template_labels(self, before_text: str, after_text: str) -> list[str]:
        """Вернуть список имён шаблонов (с локальным префиксом), в которых изменились параметры.

        Метод ищет различающиеся фрагменты {{...}} между до/после и извлекает
        названия шаблонов, нормализуя их к локальному префиксу пространства 10.
        """
        try:
            import re as _re
        except Exception:
            _re = None
        if not _re:
            return []
        try:
            before_chunks = set(_re.findall(r'\{\{([^{}]+?)\}\}', before_text or '', _re.DOTALL))
            after_chunks = set(_re.findall(r'\{\{([^{}]+?)\}\}', after_text or '', _re.DOTALL))
            changed = [c for c in after_chunks if c not in before_chunks]
        except Exception:
            changed = []
        labels: list[str] = []
        if not changed:
            return labels
        try:
            prefix = self._policy_prefix(10, DEFAULT_EN_NS.get(10, 'Template:'))
        except Exception:
            prefix = DEFAULT_EN_NS.get(10, 'Template:')
        try:
            tm = self.template_manager
        except Exception:
            tm = None
        for chunk in changed:
            try:
                name = (chunk.split('|', 1)[0] or '').strip()
                base = tm._strip_tmpl_prefix(name, self.family, self.lang) if tm else name
                if base:
                    labels.append(f"{prefix}{base}")
            except Exception:
                continue
        # Удаляем дубликаты, сохраняя порядок
        try:
            labels = list(dict.fromkeys(labels))
        except Exception:
            pass
        return labels

    def _on_review_response(self, response_data):
        """Handle response from template review dialog."""
        try:
            self._debugf('log.rename_worker.dialog.response_received', 'Dialog response received: {payload}', payload=response_data)
            
            req_id = response_data.get('req_id')
            if req_id in self._prompt_results:
                # Сохраняем полный ответ
                self._prompt_results[req_id] = {
                    'action': response_data.get('result', 'cancel'),
                    'auto_confirm': response_data.get('auto_confirm', False),
                    'auto_skip': response_data.get('auto_skip', False),
                    'edited_template': response_data.get('edited_template', ''),
                    # Если dedupe_mode не передан (диалог без дублей) — оставляем неустановленным (None)
                    'dedupe_mode': (response_data.get('dedupe_mode') if response_data.get('dedupe_mode') else None)
                }
                self._debugf(
                    'log.rename_worker.dialog.result_saved',
                    'Saved result for req_id {req_id}: {result}',
                    req_id=req_id,
                    result=self._prompt_results[req_id],
                )
                
                # Уведомляем ожидающий поток
                if req_id in self._prompt_events:
                    self._prompt_events[req_id].set()
                    self._debugf('log.rename_worker.dialog.event_set', 'Event set for req_id {req_id}', req_id=req_id)
            else:
                self._debugf('log.rename_worker.dialog.unknown_request', 'Unknown req_id: {req_id}', req_id=req_id)
        except Exception as e:
            self._debugf('log.rename_worker.dialog.response_error', 'Error in _on_review_response: {error}', error=e)

    def run(self):
        """Основной метод выполнения переименования."""
        site = pywikibot.Site(self.lang, self.family)
        debug(f'Login attempt rename lang={self.lang}')

        if self.username and self.password:
            try:
                site.login(user=self.username)
            except Exception as e:
                self._emitf(
                    'log.rename_worker.auth_error',
                    'Authorization error: {error_type}: {error}',
                    error_type=type(e).__name__,
                    error=e,
                )
                return

        try:
            # Читаем как utf-8-sig и очищаем BOM/пробелы
            with open(self.tsv_path, newline='', encoding='utf-8-sig') as f:
                reader = csv.reader(f, delimiter='\t')
                rows = list(reader)
                # Инициализируем общий прогресс по числу строк файла
                try:
                    self.tsv_progress_init.emit(len(rows))
                except Exception:
                    pass
                for row in rows:
                    if self._stop:
                        break
                    if len(row) < 3:
                        self._emitf(
                            'log.rename_worker.invalid_row',
                            'Invalid row (3 columns required): {row}',
                            row=row,
                        )
                        try:
                            self.tsv_progress_inc.emit()
                        except Exception:
                            pass
                        continue
                    old_name_raw, new_name_raw, reason = [((c or '').strip().lstrip('\ufeff')) for c in row[:3]]
                    # Запомним комментарий текущей строки (для операций переноса содержимого)
                    self._current_row_reason = reason

                    # Нормализация имён по выбору пользователя
                    sel = self.ns_sel
                    is_category = False
                    try:
                        if isinstance(sel, str) and sel.strip().lower() == 'auto':
                            old_name = old_name_raw
                            new_name = new_name_raw
                            # Определяем категорию по фактическому префиксу
                            is_category = title_has_ns_prefix(self.family, self.lang, old_name, {14})
                        else:
                            ns_id = int(sel)
                            old_name = normalize_title_by_selection(old_name_raw, self.family, self.lang, ns_id)
                            new_name = normalize_title_by_selection(new_name_raw, self.family, self.lang, ns_id)
                            is_category = (ns_id == 14)
                    except Exception:
                        # На случай некорректного выбора — ведём себя как 'Авто'
                        old_name = old_name_raw
                        new_name = new_name_raw
                        is_category = title_has_ns_prefix(self.family, self.lang, old_name, {14})

                    # Если переименовываем категорию и перенос содержимого выключен
                    if is_category and not (self.move_members and (self.phase1_enabled or self.find_in_templates)):
                        try:
                            old_full_check = _ensure_title_with_ns(old_name, self.family, self.lang, 14, DEFAULT_EN_NS.get(14, 'Category:'))
                            if not pywikibot.Page(site, old_full_check).exists():
                                try:
                                    self._emitf(
                                        'log.rename_worker.category_missing_transfer_disabled_html',
                                        'Category <b>{title}</b> does not exist. Content transfer is disabled.',
                                        title=html.escape(old_full_check),
                                    )
                                except Exception:
                                    self._emitf(
                                        'log.rename_worker.category_missing_transfer_disabled',
                                        'Category {title} does not exist. Content transfer is disabled.',
                                        title=old_full_check,
                                    )
                                continue
                        except Exception:
                            pass

                    leave_redirect = self.leave_cat_redirect if is_category else self.leave_other_redirect
                    
                    # Если переименование категории выключено — пропускаем сам move для категорий
                    if is_category and not self.move_category:
                        try:
                            self._emitf(
                                'log.rename_worker.category_rename_skipped_html',
                                'Category rename skipped <b>{old}</b> -> <b>{new}</b>. Transferring content...',
                                old=html.escape(old_name),
                                new=html.escape(new_name),
                            )
                        except Exception:
                            pass
                    else:
                        self._move_page(site, old_name, new_name, reason, leave_redirect)
                    
                    # Если это категория и хотя бы одна фаза переноса включена — переносим участников
                    if is_category and self.move_members and (self.phase1_enabled or self.find_in_templates) and not self._stop:
                        try:
                            self._debugf(
                                'log.rename_worker.category_transfer_start_debug',
                                'Starting category content transfer: {old} -> {new}',
                                old=old_name,
                                new=new_name,
                            )
                            debug(f'move_members={self.move_members}, phase1_enabled={self.phase1_enabled}, find_in_templates={self.find_in_templates}')
                            self._move_category_members(site, old_name, new_name)
                        except Exception as e:
                            self._emitf(
                                'log.rename_worker.category_transfer_error_named',
                                "Category content transfer error for '{title}': {error}",
                                title=old_name,
                                error=e,
                            )
                    # Инкремент общего прогресса по строкам TSV
                    try:
                        self.tsv_progress_inc.emit()
                    except Exception:
                        pass
        except Exception as e:
            self._emitf('log.rename_worker.tsv_error', 'TSV file error: {error}', error=e)
        finally:
            # Финальные сообщения об окончании теперь пишет UI
            pass

    def _move_page(self, site: pywikibot.Site, old_name: str, new_name: str, reason: str, leave_redirect: bool):
        """
        Переименование страницы с retry логикой.
        
        Args:
            site: Объект сайта pywikibot
            old_name: Старое название страницы
            new_name: Новое название страницы
            reason: Причина переименования
            leave_redirect: Оставлять ли перенаправление
        """
        try:
            page = pywikibot.Page(site, old_name)
            new_page = pywikibot.Page(site, new_name)
            if not page.exists():
                # Сообщение должно корректно отражать тип объекта и позволять UI
                # определить его по префиксу. Для категории выводим только
                # заголовок с префиксом «Категория:…» без лишнего слова «Категория » перед ним.
                try:
                    typ = self._page_kind(page)
                except Exception:
                    typ = 'page'
                if typ == 'category':
                    try:
                        self._emitf(
                            'log.rename_worker.not_found_html',
                            '<b>{title}</b> not found.',
                            title=html.escape(old_name),
                        )
                    except Exception:
                        self._emitf(
                            'log.rename_worker.not_found_plain',
                            '{title} not found.',
                            title=old_name,
                        )
                else:
                    self._emitf(
                        'log.rename_worker.page_not_found_html',
                        'Page <b>{title}</b> not found.',
                        title=html.escape(old_name),
                    )
                return
            if new_page.exists():
                # Структурированное событие; текстовый лог используем только как фолбэк
                try:
                    self.log_event.emit({'type': 'destination_exists', 'title': new_name, 'status': 'info'})
                except Exception:
                    try:
                        self._emitf(
                            'log.rename_worker.destination_exists_html',
                            'Destination page <b>{title}</b> already exists.',
                            title=html.escape(new_name),
                        )
                    except Exception:
                        self._emitf(
                            'log.rename_worker.destination_exists_plain',
                            'Destination page {title} already exists.',
                            title=new_name,
                        )
                return

            # Сформируем комментарий к правке для операции переименования
            # В summary оставляем только дополнительный комментарий (без «Old → New»),
            # так как информация о переименовании отображается автоматически системой.
            comment_text = self.override_comment or (reason or '')
            move_summary = comment_text

            # Системное сообщение: начинаем переименование
            try:
                self._emitf(
                    'log.rename_worker.rename_started_html',
                    'Starting rename: <b>{old}</b> -> <b>{new}</b>',
                    old=html.escape(old_name),
                    new=html.escape(new_name),
                )
            except Exception:
                self._emitf(
                    'log.rename_worker.rename_started_plain',
                    'Starting rename: {old} -> {new}',
                    old=old_name,
                    new=new_name,
                )

            # Адаптивный retry для move операций
            for attempt in range(1, 4):
                try:
                    self._wait_before_save()
                    # Для актуальных версий pywikibot используется параметр noredirect
                    page.move(new_name, reason=move_summary, noredirect=(not leave_redirect))
                    self._decay_save_interval()

                    # Если пользователь просил не оставлять редирект, но он всё же остался,
                    # явно показываем это в логе (обычно из-за отсутствия suppressredirect).
                    if not leave_redirect:
                        try:
                            old_after = pywikibot.Page(site, old_name)
                            redirect_left = old_after.exists() and old_after.isRedirectPage()
                        except Exception:
                            redirect_left = False
                        if redirect_left:
                            try:
                                self.log_event.emit({
                                    'type': 'redirect_retained',
                                    'old_title': old_name,
                                    'new_title': new_name,
                                    'status': 'info'
                                })
                            except Exception:
                                try:
                                    self._emitf(
                                        'log.rename_worker.redirect_retained_html',
                                        'ℹ️ Redirect remained after rename: {old} -> {new} (possibly insufficient suppressredirect rights).',
                                        old=html.escape(old_name),
                                        new=html.escape(new_name),
                                    )
                                except Exception:
                                    self._emitf(
                                        'log.rename_worker.redirect_retained_plain',
                                        'ℹ️ Redirect remained after rename: {old} -> {new} (possibly insufficient suppressredirect rights).',
                                        old=old_name,
                                        new=new_name,
                                    )

                    # Сообщение после тире: оставляем только комментарий/причину, без "Old → New"
                    try:
                        tail = comment_text if comment_text else ''
                        msg = (
                            self._tf(
                                'log.rename_worker.rename_success_with_comment',
                                'Renamed successfully - {comment}',
                                comment=html.escape(tail),
                            )
                            if tail
                            else self._tr('log.rename_worker.rename_success', 'Renamed successfully')
                        )
                        self.progress.emit(msg)
                    except Exception:
                        self.progress.emit(self._tr('log.rename_worker.rename_success', 'Renamed successfully'))
                    return
                except Exception as e:
                    if self._is_rate_error(e) and attempt < 3:
                        self._increase_save_interval(attempt)
                        try:
                            self._emitf(
                                'log.rename_worker.rename_rate_limit',
                                'Rename rate limit: pause {wait:.2f}s · attempt {attempt}/3',
                                wait=self._save_min_interval,
                                attempt=attempt,
                            )
                        except Exception:
                            pass
                        continue
                    try:
                        self._emitf(
                            'log.rename_worker.rename_error_html',
                            'Rename error <b>{title}</b>: {error_type}: {error}',
                            title=html.escape(old_name),
                            error_type=type(e).__name__,
                            error=e,
                        )
                    except Exception:
                        self._emitf(
                            'log.rename_worker.rename_error_plain',
                            'Rename error {title}: {error_type}: {error}',
                            title=old_name,
                            error_type=type(e).__name__,
                            error=e,
                        )
                    return
        except Exception as e:
            try:
                self._emitf(
                    'log.rename_worker.rename_critical_error_html',
                    'Critical rename error <b>{title}</b>: {error}',
                    title=html.escape(old_name),
                    error=e,
                )
            except Exception:
                self._emitf(
                    'log.rename_worker.rename_critical_error_plain',
                    'Critical rename error {title}: {error}',
                    title=old_name,
                    error=e,
                )

    def _move_category_members(self, site: pywikibot.Site, old_name: str, new_name: str):
        """
        Перенос содержимого категории (прямые ссылки и шаблоны).
        
        Args:
            site: Объект сайта pywikibot
            old_name: Старое название категории
            new_name: Новое название категории
        """
        try:
            debug(f'_move_category_members: old_name={old_name}, new_name={new_name}')
            
            # Получаем полные названия категорий с префиксами
            old_cat_full = _ensure_title_with_ns(old_name, self.family, self.lang, 14, DEFAULT_EN_NS.get(14, 'Category:'))
            new_cat_full = _ensure_title_with_ns(new_name, self.family, self.lang, 14, DEFAULT_EN_NS.get(14, 'Category:'))
            
            self._debugf(
                'log.rename_worker.category_transfer_titles_resolved',
                'After _ensure_title_with_ns: old_cat_full={old}, new_cat_full={new}',
                old=old_cat_full,
                new=new_cat_full,
            )
            
            old_cat_page = pywikibot.Page(site, old_cat_full)
            self._debugf('log.rename_worker.category_exists_check', 'Checking category existence: {title}', title=old_cat_full)
            
            # Проверяем существование категории (информативно, не блокируем процесс)
            try:
                category_exists = old_cat_page.exists()
                self._debugf('log.rename_worker.category_exists_result', 'Category exists: {exists}', exists=category_exists)
            except Exception as e:
                self._debugf('log.rename_worker.category_exists_error', 'Category existence check error: {error}', error=e)
                category_exists = True

            # Получаем список страниц в категории через MediaWiki API (как в оригинале)
            try:
                from ..core.api_client import REQUEST_SESSION, _rate_wait, WikimediaAPIClient
                from ..constants import REQUEST_HEADERS
                import requests
                import urllib.parse
                
                api_client = WikimediaAPIClient()
                api_url = api_client._build_api_url(self.family, self.lang)
                params = {
                    'action': 'query',
                    'list': 'categorymembers',
                    'cmtitle': old_cat_full,
                    'cmlimit': 'max',
                    'cmprop': 'title|ns',
                    'format': 'json'
                }
                members_titles: list[str] = []
                
                self._debugf(
                    'log.rename_worker.category_fetch_members',
                    'Fetching category members via API (with continuations)',
                )
                while True:
                    if self._stop:
                        break
                    _rate_wait()
                    r = REQUEST_SESSION.get(api_url, params=params, timeout=15, headers=REQUEST_HEADERS)
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code} while requesting {api_url}")
                    data = r.json()
                    chunk = [m.get('title') for m in (data.get('query', {}).get('categorymembers', []) or []) if m.get('title')]
                    members_titles.extend(chunk)
                    if 'continue' in data:
                        params.update(data['continue'])
                    else:
                        break
                
                self._debugf(
                    'log.rename_worker.category_members_found',
                    'Found {count} in category',
                    count=format_russian_pages_nominative(len(members_titles)),
                )
                if not members_titles:
                    self._emitf(
                        'log.rename_worker.category_empty_html',
                        'Category <b>{title}</b> is empty.',
                        title=html.escape(old_cat_full),
                    )
                    try:
                        self.inner_progress_reset.emit()
                    except Exception:
                        pass
                    return
                
                try:
                    # Структурированное событие для UI: старая категория в «Категория», число страниц — в «Источник»
                    try:
                        cnt = len(members_titles)
                        cnt_str = format_russian_pages_nominative(cnt)
                        self.log_event.emit({'type': 'category_move_start', 'old_category': old_cat_full, 'new_category': new_cat_full, 'count': cnt, 'count_str': cnt_str, 'status': 'info'})
                    except Exception:
                        try:
                            self._emitf(
                                'log.rename_worker.category_transfer_info_html',
                                'ℹ️ Category content transfer <b>{old}</b> -> <b>{new}</b>: {count}',
                                old=html.escape(old_cat_full),
                                new=html.escape(new_cat_full),
                                count=format_russian_pages_nominative(len(members_titles)),
                            )
                        except Exception:
                            self._emitf(
                                'log.rename_worker.category_transfer_info_plain',
                                'ℹ️ Category content transfer {old} -> {new}: {count}',
                                old=old_cat_full,
                                new=new_cat_full,
                                count=format_russian_pages_nominative(len(members_titles)),
                            )
                except Exception:
                    self._emitf(
                        'log.rename_worker.category_transfer_info_plain',
                        'ℹ️ Category content transfer {old} -> {new}: {count}',
                        old=old_cat_full,
                        new=new_cat_full,
                        count=format_russian_pages_nominative(len(members_titles)),
                    )
                try:
                    self.inner_progress_init.emit(len(members_titles))
                except Exception:
                    pass
                
                backlog_seen: set[str] = set()
                
                for title in members_titles:
                    if self._stop:
                        break
                    try:
                        self.inner_progress_inc.emit()
                    except Exception:
                        pass
                    # Применяем фильтр по заголовку, если задан
                    try:
                        if self._title_regex_compiled is not None and not self._title_regex_compiled.search(title):
                            continue
                    except Exception:
                        # На случай непредвиденной ошибки с регулярным выражением — игнорируем фильтр
                        pass
                    try:
                        page = pywikibot.Page(site, title)
                        self._debugf('log.rename_worker.member_processing', 'Processing page: {title}', title=page.title())
                        changes_made = self._process_category_member(site, page, old_cat_full, new_cat_full)

                        # Немедленно запускаем фазу 2 (если включена) и фиксируем были ли изменения
                        phase2_changes = 0
                        if self.find_in_templates and title not in backlog_seen:
                            self._debugf(
                                'log.rename_worker.phase2_immediate_processing',
                                'Phase 2 (immediate): processing page {title}',
                                title=title,
                            )
                            try:
                                _, phase2_changes = self._process_title_templates(site, title, old_cat_full, new_cat_full)
                            except Exception as e:
                                self._emitf(
                                    'log.rename_worker.template_processing_error',
                                    'Template processing error on page {title}: {error}',
                                    title=title,
                                    error=e,
                                )
                                self._debugf(
                                    'log.rename_worker.template_processing_error',
                                    'Template processing error on page {title}: {error}',
                                    title=title,
                                    error=e,
                                )
                            # Отмечаем как посещённую, чтобы не обрабатывать повторно
                            backlog_seen.add(title)

                        # Если ни фаза 1, ни фаза 2 не внесли изменений — добавим понятную строку в лог
                        try:
                            if changes_made == 0 and (not self.find_in_templates or (phase2_changes == 0 and not getattr(self, '_last_template_interactions', False))):
                                # Для корректной классификации как «шаблонной» операции
                                # укажем источник вида «<локальный префикс шаблона>…». Тогда в колонке «Тип» будет ✍️, а не 📝.
                                if self.find_in_templates:
                                    self._emitf(
                                        'log.rename_worker.skip_no_changes_with_source',
                                        '→ {category} : "{title}" - skipped, no changes ({source})',
                                        category=new_cat_full,
                                        title=title,
                                        source=self._template_categories_label(),
                                    )
                                else:
                                    self._emitf(
                                        'log.rename_worker.skip_no_changes',
                                        '→ {category} : "{title}" - skipped (no changes)',
                                        category=new_cat_full,
                                        title=title,
                                    )
                        except Exception:
                            pass
                    except Exception as e:
                        self._emitf(
                            'log.rename_worker.page_processing_error',
                            'Page processing error {title}: {error}',
                            title=title,
                            error=e,
                        )
                        self._debugf(
                            'log.rename_worker.page_processing_error',
                            'Page processing error {title}: {error}',
                            title=title,
                            error=e,
                        )
                
                # Фаза 2 через backlog не используется: интерактивная обработка выполняется немедленно при обходе members_titles
                if not self.find_in_templates:
                    self._debugf('log.rename_worker.phase2_disabled', 'Phase 2 disabled')
                elif self._stop:
                    self._debugf('log.rename_worker.process_stopped', 'Process stopped')
                
                self._debugf('log.rename_worker.category_processing_completed', 'Category processing completed')
                try:
                    self.inner_progress_reset.emit()
                except Exception:
                    pass
            except Exception as e:
                self._emitf(
                    'log.rename_worker.category_contents_error',
                    'Category content fetch error {title}: {error}',
                    title=old_cat_full,
                    error=e,
                )
                self._debugf(
                    'log.rename_worker.category_contents_error',
                    'Category content fetch error {title}: {error}',
                    title=old_cat_full,
                    error=e,
                )
                
        except Exception as e:
            self._emitf(
                'log.rename_worker.category_transfer_error',
                'Category content transfer error: {error}',
                error=e,
            )

    def _process_category_member(self, site: pywikibot.Site, page: pywikibot.Page, old_cat_full: str, new_cat_full: str) -> int:
        """
        Обработка одной страницы из категории (только фаза 1).
        
        Args:
            site: Объект сайта pywikibot
            page: Страница для обработки
            old_cat_full: Полное название старой категории
            new_cat_full: Полное название новой категории
            
        Returns:
            int: Количество внесенных изменений
        """
        try:
            if not page.exists():
                return 0

            self._debugf('log.rename_worker.phase1_processing', 'Processing page (phase 1): {title}', title=page.title())
            self._debugf('log.rename_worker.phase1_enabled', 'Phase 1 enabled: {enabled}', enabled=self.phase1_enabled)
                
            original_text = page.text
            modified_text = original_text
            changes_made = 0
            
            # Фаза 1: Прямые ссылки на категорию
            if self.phase1_enabled:
                modified_text, direct_changes = self._replace_category_links_in_text(
                    modified_text, self.family, self.lang, old_cat_full, new_cat_full
                )
                changes_made += direct_changes
                self._debugf('log.rename_worker.phase1_changes', 'Phase 1: {count} changes', count=direct_changes)
            
            # Сохраняем изменения если они есть (в стиле оригинала)
            if changes_made > 0 and modified_text != original_text:
                # Оригинал использует лаконичный summary и minor=True для фазы 1
                summary = self._build_summary(old_cat_full, new_cat_full, mode='phase1')
                ok = self._save_with_retry(page, modified_text, summary, True)
                if ok:
                    try:
                        typ = self._page_kind(page)
                    except Exception:
                        typ = 'page'
                    moved_text = self._kind_moved_text(typ)
                    try:
                        self._emitf(
                            'log.rename_worker.phase1_saved_html',
                            '▪️ {category} : "{title}" - {result}',
                            category=html.escape(new_cat_full),
                            title=html.escape(page.title()),
                            result=moved_text,
                        )
                    except Exception:
                        self._emitf(
                            'log.rename_worker.phase1_saved_plain',
                            '▪️ {category} : "{title}" - {result}',
                            category=new_cat_full,
                            title=page.title(),
                            result=moved_text,
                        )
                else:
                    try:
                        self._emitf(
                            'log.rename_worker.save_error_html',
                            'Save error <b>{title}</b>',
                            title=html.escape(page.title()),
                        )
                    except Exception:
                        self._emitf(
                            'log.rename_worker.save_error_plain',
                            'Save error {title}',
                            title=page.title(),
                        )
            
            return changes_made
            
        except Exception as e:
            self._emitf(
                'log.rename_worker.page_processing_error',
                'Page processing error {title}: {error}',
                title=page.title(),
                error=e,
            )
            return 0

    def _process_title_templates(self, site: pywikibot.Site, title: str, old_cat_full: str, new_cat_full: str) -> tuple[str, int]:
        """
        Обработка одной страницы по фазе 2 (поиск в параметрах шаблонов с диалогами подтверждения).
        Аналог функции _process_title_templates из оригинального скрипта.
        
        Args:
            site: Объект сайта pywikibot
            title: Название страницы
            old_cat_full: Полное название старой категории
            new_cat_full: Полное название новой категории
        """
        if self._stop:
            return ('', 0)
            
        try:
            page = pywikibot.Page(site, title)
            if not page.exists():
                return ('', 0)

            self._debugf('log.rename_worker.phase2_processing', 'Phase 2: processing templates on page {title}', title=title)
            
            original_text = page.text
            modified_text = original_text
            changes_made = 0
            
            self._debugf(
                'log.rename_worker.page_text_size',
                'Page text size: {count} characters',
                count=len(original_text),
            )
            
            # Сначала применяем кэшированные правила (автоматически)
            self._debugf('log.rename_worker.cached_rules_apply', 'Applying cached rules...')
            modified_text, cached_changes = self.template_manager.apply_cached_template_rules(
                modified_text, self.family, self.lang
            )
            
            if cached_changes > 0:
                self._debugf(
                    'log.rename_worker.cached_rules_applied',
                    'Cached rules applied: {count} changes',
                    count=cached_changes,
                )
                changes_made += cached_changes
                
                # Сохраняем изменения от кэшированных правил
                # В summary добавим конкретные имена шаблонов (через запятую)
                try:
                    labels = self._extract_changed_template_labels(original_text, modified_text)
                    label_str = ', '.join(labels)
                except Exception:
                    label_str = ''
                summary = self._build_summary(old_cat_full, new_cat_full, mode='template', template_label=label_str)
                ok = self._save_with_retry(page, modified_text, summary, True)
                if ok:
                    try:
                        typ = self._page_kind(page)
                    except Exception:
                        typ = 'page'
                    # Подготовим список шаблонов, изменённых кэш-правилами, для лога
                    try:
                        tmpl_names = self._extract_changed_template_labels(original_text, modified_text)
                        suffix = f" ({', '.join(tmpl_names)})" if tmpl_names else ''
                    except Exception:
                        suffix = ''
                    auto_note = self._auto_note_text(cached_changes)
                    self._emitf(
                        'log.rename_worker.template_saved_auto',
                        '→ {category} : "{title}" - {result} {auto_note}{suffix}',
                        category=new_cat_full,
                        title=title,
                        result=self._kind_moved_text(typ),
                        auto_note=auto_note,
                        suffix=suffix,
                    )
                    # Обновляем текст для дальнейшей обработки только при успешном сохранении
                    original_text = modified_text
                else:
                    # Если сохранение не удалось (например, LockedPageError) — не открываем диалог заново
                    # Отмечаем факт взаимодействия, чтобы верхний уровень не писал фолбэк «пропущено, без изменений»
                    try:
                        self._last_template_interactions = True
                    except Exception:
                        pass
                    return (original_text, 0)
            
            # Сброс признака «были ли какие-либо взаимодействия в диалогах» для текущей страницы
            try:
                self._last_template_interactions = False
                self._last_template_change_was_locative = False
                self._last_changed_template_name = None
            except Exception:
                pass
            # Интерактивная обработка шаблонов с диалогами подтверждения
            modified_text, interactive_changes = self._process_templates_interactive(
                modified_text, old_cat_full, new_cat_full, title
            )
            # Если пользователь нажал «Отмена» — прерываем без сохранений и логов успеха
            if self._stop:
                self._debugf(
                    'log.rename_worker.interactive_stopped_skip_save',
                    'Stopped by user during interactive processing - skipping save',
                )
                return (original_text, 0)
            
            if interactive_changes > 0:
                self._debugf(
                    'log.rename_worker.interactive_changes',
                    'Interactive processing: {count} changes',
                    count=interactive_changes,
                )
                changes_made += interactive_changes
                
                # Сохраняем изменения от интерактивной обработки (как в оригинале)
                # Интерактивные изменения в шаблонах — добавим конкретные шаблоны в summary
                try:
                    labels = self._extract_changed_template_labels(original_text, modified_text)
                    # Фолбэк: если ярлыков не удалось извлечь, но знаем последний шаблон — используем его (без меток)
                    if not labels:
                        try:
                            last_name = getattr(self, '_last_changed_template_name', '') or ''
                        except Exception:
                            last_name = ''
                        if last_name:
                            try:
                                pref = self._policy_prefix(10, DEFAULT_EN_NS.get(10, 'Template:'))
                            except Exception:
                                pref = DEFAULT_EN_NS.get(10, 'Template:')
                            labels = [f"{pref}{last_name}"]
                    label_str = ', '.join(labels)
                except Exception:
                    label_str = ''
                summary = self._build_summary(old_cat_full, new_cat_full, mode='template', template_label=label_str)
                ok = self._save_with_retry(page, modified_text, summary, True)
                if ok:
                    try:
                        typ = self._page_kind(page)
                    except Exception:
                        typ = 'page'
                    try:
                        tmpl_names = self._extract_changed_template_labels(original_text, modified_text)
                        if not tmpl_names:
                            try:
                                last_name = getattr(self, '_last_changed_template_name', '') or ''
                            except Exception:
                                last_name = ''
                            if last_name:
                                try:
                                    pref = self._policy_prefix(10, DEFAULT_EN_NS.get(10, 'Template:'))
                                except Exception:
                                    pref = DEFAULT_EN_NS.get(10, 'Template:')
                                tmpl_names = [f"{pref}{last_name}"]
                        suffix = f" ({', '.join(tmpl_names)})" if tmpl_names else ''
                        self._emitf(
                            'log.rename_worker.template_saved',
                            '→ {category} : "{title}" - {result}{suffix}',
                            category=new_cat_full,
                            title=title,
                            result=self._kind_moved_text(typ),
                            suffix=suffix,
                        )
                    except Exception:
                        self._emitf(
                            'log.rename_worker.template_saved_generic',
                            '→ {category} : "{title}" - moved',
                            category=new_cat_full,
                            title=title,
                        )
                else:
                    # Ошибка уже залогирована внутри _save_with_retry; не дублируем сообщение
                    try:
                        self._last_template_interactions = True
                    except Exception:
                        pass
            else:
                # Финальный шаг: эвристика «локативы», если включена и прямых/частичных совпадений не найдено
                try:
                    if self.find_in_templates and self.use_locatives and not self._stop:
                        modified_text2, loc_changes = self._process_locatives_heuristic(
                            modified_text, old_cat_full, new_cat_full, title
                        )
                        # Если пользователь отменил в режиме локативов — немедленно прерываем без сохранений
                        if self._stop:
                            self._debugf(
                                'log.rename_worker.locative_stopped_skip_save',
                                'Stopped by user during locative processing - skipping save',
                            )
                            return (original_text, changes_made)
                        if loc_changes > 0 and modified_text2 != modified_text:
                            changes_made += loc_changes
                            # Сохраняем изменения
                            try:
                                labels = self._extract_changed_template_labels(original_text, modified_text2)
                                if not labels:
                                    try:
                                        last_name = getattr(self, '_last_changed_template_name', '') or ''
                                    except Exception:
                                        last_name = ''
                                    if last_name:
                                        try:
                                            pref = self._policy_prefix(10, DEFAULT_EN_NS.get(10, 'Template:'))
                                        except Exception:
                                            pref = DEFAULT_EN_NS.get(10, 'Template:')
                                        labels = [f"{pref}{last_name}"]
                                label_str = ', '.join(labels)
                            except Exception:
                                label_str = ''
                            summary = self._build_summary(old_cat_full, new_cat_full, mode='template', template_label=label_str)
                            ok = self._save_with_retry(page, modified_text2, summary, True)
                            if ok:
                                try:
                                    typ = self._page_kind(page)
                                except Exception:
                                    typ = 'page'
                                try:
                                    tmpl_names = self._extract_changed_template_labels(original_text, modified_text2)
                                    if not tmpl_names:
                                        try:
                                            last_name = getattr(self, '_last_changed_template_name', '') or ''
                                        except Exception:
                                            last_name = ''
                                        if last_name:
                                            try:
                                                pref = self._policy_prefix(10, DEFAULT_EN_NS.get(10, 'Template:'))
                                            except Exception:
                                                pref = DEFAULT_EN_NS.get(10, 'Template:')
                                            tmpl_names = [f"{pref}{last_name}"]
                                    suffix = f" ({', '.join(tmpl_names)})" if tmpl_names else ''
                                    self._emitf(
                                        'log.rename_worker.template_saved',
                                        '→ {category} : "{title}" - {result}{suffix}',
                                        category=new_cat_full,
                                        title=title,
                                        result=self._kind_moved_text(typ),
                                        suffix=suffix,
                                    )
                                except Exception:
                                    self._emitf(
                                        'log.rename_worker.template_saved_generic',
                                        '→ {category} : "{title}" - moved',
                                        category=new_cat_full,
                                        title=title,
                                    )
                                original_text = modified_text2
                                modified_text = modified_text2
                            else:
                                try:
                                    self._last_template_interactions = True
                                except Exception:
                                    pass
                except Exception as _loc_e:
                    try:
                        self._debugf(
                            'log.rename_worker.locative_runtime_error',
                            'Locative heuristic error: {error}',
                            error=_loc_e,
                        )
                    except Exception:
                        pass
            
            # Если никаких изменений не было сделано
            if changes_made == 0:
                self._debugf(
                    'log.rename_worker.templates_not_found',
                    'No templates to change were found on page {title}',
                    title=title,
                )
                
        except Exception as e:
            self._emitf(
                'log.rename_worker.template_processing_error',
                'Template processing error on page {title}: {error}',
                title=title,
                error=e,
            )
            self._debugf(
                'log.rename_worker.template_processing_error',
                'Template processing error on page {title}: {error}',
                title=title,
                error=e,
            )
            try:
                # если удалось получить текст страницы — вернём его; иначе пустой
                return (locals().get('modified_text') or locals().get('original_text') or '', locals().get('changes_made') or 0)
            except Exception:
                return ('', 0)
        # Нормальный путь завершения
        try:
            return modified_text, changes_made
        except Exception:
            return ('', 0)

    # ==============================
    # Эвристика обработки локативов
    # ==============================
    def _loc_common_prefix(self, a: str, b: str) -> str:
        try:
            L = min(len(a), len(b))
            i = 0
            while i < L and a[i] == b[i]:
                i += 1
            return a[:i]
        except Exception:
            return ''

    def _loc_common_suffix(self, a: str, b: str) -> str:
        try:
            a2 = a[::-1]
            b2 = b[::-1]
            L = min(len(a2), len(b2))
            i = 0
            while i < L and a2[i] == b2[i]:
                i += 1
            return a[len(a)-i:] if i > 0 else ''
        except Exception:
            return ''

    def _loc_trim(self, s: str) -> str:
        try:
            import re as _re
            return (_re.sub(r"^[\s,;:–—\-·()\[\]]+|[\s,;:–—\-·()\[\]]+$", "", s or '') or '').strip()
        except Exception:
            return (s or '').strip()

    def _invert_locative_form(self, word: str) -> str:
        """Грубая эвристика обратного преобразования из предложного падежа к именительному.

        Параллельно срабатывает для пар: «Порт-Элизабете» → «Порт-Элизабет», «Грузии» → «Грузия»,
        «Варшаве» → «Варшава», «Сочи» → «Сочи» (без изменений).
        """
        try:
            w = (word or '').strip()
            if not w:
                return w
            lower = w.casefold()
            if lower.endswith(_CYR_II) and len(w) > 2:
                return w[:-2] + (_CYR_IYA_LOWER if w[-2:].islower() else _CYR_IYA_UPPER)
            if lower.endswith(_CYR_LE) and len(w) > 2:
                return w[:-1].rstrip(_CYR_E) + _CYR_SOFT
            vowels_set = set(_CYR_VOWELS)
            if len(w) >= 3 and (lower.endswith(_CYR_GE) or lower.endswith(_CYR_KE) or lower.endswith(_CYR_HE)):
                prev = w[-3]
                if prev in vowels_set:
                    base = w[:-2]
                    first = w[-2]
                    if lower.endswith(_CYR_GE):
                        cons = _CYR_G_UP if first.isupper() else _CYR_G_LOW
                    elif lower.endswith(_CYR_KE):
                        cons = _CYR_K_UP if first.isupper() else _CYR_K_LOW
                    else:
                        cons = _CYR_H_UP if first.isupper() else _CYR_H_LOW
                    a_char = _CYR_A_UP if w[-1].isupper() else _CYR_A_LOW
                    return base + cons + a_char
            if lower.endswith(_CYR_AE) and len(w) > 2:
                return w[:-1] + (_CYR_SHORT_I_LOW if w[-1].islower() else _CYR_SHORT_I_UP)
            if lower.endswith(_CYR_OE) and len(w) > 2:
                return w[:-1] + (_CYR_SHORT_I_LOW if w[-1].islower() else _CYR_SHORT_I_UP)
            vowels = set(_CYR_VOWELS)
            if lower.endswith(_CYR_E) and len(w) > 1 and (w[-2] not in vowels):
                return w[:-1]
            if lower.endswith(_CYR_I) and len(w) > 1 and (w[-2] not in vowels):
                return w[:-1] + _CYR_SOFT
            if lower.endswith(_CYR_OY) and len(w) > 2:
                return w[:-2] + _CYR_AYA
            return w
        except Exception:
            return word

    def _process_locatives_heuristic(self, text: str, old_cat_full: str, new_cat_full: str, page_title: str) -> tuple[str, int]:
        from ..utils import debug
        try:
            import re
        except Exception:
            re = None
        try:
            old_name = old_cat_full.split(':', 1)[-1] if ':' in old_cat_full else old_cat_full
            new_name = new_cat_full.split(':', 1)[-1] if ':' in new_cat_full else new_cat_full
            if not old_name or not new_name:
                return text, 0
            # Вычисляем различающиеся части
            # Токен‑дифф по словам: не режем буквы, только целые токены по пробелам
            import re as _re
            def _split_tokens(s: str) -> list[str]:
                try:
                    return [t for t in _re.split(r"\s+", (s or '').strip()) if t]
                except Exception:
                    return [(s or '').strip()] if (s or '').strip() else []
            old_t = _split_tokens(old_name)
            new_t = _split_tokens(new_name)
            i = 0
            L = min(len(old_t), len(new_t))
            while i < L and old_t[i] == new_t[i]:
                i += 1
            j = 0
            while (j < (len(old_t)-i)) and (j < (len(new_t)-i)) and (old_t[-1-j] == new_t[-1-j]):
                j += 1
            old_mid = self._loc_trim(' '.join(old_t[i:len(old_t)-j if j else None]))
            new_mid = self._loc_trim(' '.join(new_t[i:len(new_t)-j if j else None]))
            
            # ИСПРАВЛЕНИЕ: если old_mid пустой (расширение категории), используем последний токен старой категории
            # Например: "Родившиеся в Аксае" → "Родившиеся в Аксае (Дагестан)"
            # old_t=["Родившиеся","в","Аксае"], new_t=["Родившиеся","в","Аксае","(Дагестан)"]
            # old_mid="" → берем "Аксае", new_mid="(Дагестан)" → берем "Аксае (Дагестан)"
            if not old_mid and old_t:
                self._debugf(
                    'log.rename_worker.locative_old_mid_empty',
                    'Locatives: old_mid is empty, category expanded. Using the last token of the old category.',
                )
                # Берем последний значимый токен (обычно это географическое название)
                old_mid = self._loc_trim(old_t[-1])
                # Для новой категории берем последние N токенов (где N - количество добавленных токенов + 1)
                # ВАЖНО: не применяем _loc_trim к new_mid, чтобы сохранить скобки!
                added_tokens = len(new_t) - len(old_t)
                if added_tokens > 0:
                    new_mid = ' '.join(new_t[-(added_tokens+1):])
                else:
                    new_mid = new_t[-1]
                self._debugf(
                    'log.rename_worker.locative_adjusted_parts',
                    "Locatives: adjusted parts - old_mid='{old_mid}', new_mid='{new_mid}'",
                    old_mid=old_mid,
                    new_mid=new_mid,
                )
            
            if not old_mid or not new_mid:
                return text, 0
            
            # Инверсия локативов - нужно обрабатывать составные названия пословно
            # Например: "Аксае (Дагестан)" → нужно инвертировать только "Аксае" → "Аксай (Дагестан)"
            def invert_compound_locative(text: str) -> str:
                """Инвертирует локатив в составных названиях, обрабатывая каждое слово отдельно"""
                try:
                    import re as _re2
                    result = []
                    i = 0
                    while i < len(text):
                        # Пытаемся найти слово (буквы и дефисы)
                        match = _re2.match(_CYR_WORD_PATTERN, text[i:])
                        if match:
                            word = match.group(0)
                            # Инвертируем локатив для слова
                            result.append(self._invert_locative_form(word))
                            i += len(word)
                        else:
                            # Иначе это разделитель (пробел, скобка и т.д.) - берем один символ
                            result.append(text[i])
                            i += 1
                    return ''.join(result)
                except Exception as e:
                    self._debugf(
                        'log.rename_worker.locative_invert_error',
                        'Error in invert_compound_locative: {error}',
                        error=e,
                    )
                    return self._invert_locative_form(text)
            
            inv_old = invert_compound_locative(old_mid)
            inv_new = invert_compound_locative(new_mid)
            # Диагностика: если инверсий несколько (разные эвристики) — пока просто логируем возможность неоднозначности
            try:
                if inv_old != old_mid or inv_new != new_mid:
                    self._debugf(
                        'log.rename_worker.locative_inversion',
                        "Locatives: inversion '{old_mid}' -> '{inv_old}', '{new_mid}' -> '{inv_new}'",
                        old_mid=old_mid,
                        inv_old=inv_old,
                        new_mid=new_mid,
                        inv_new=inv_new,
                    )
            except Exception:
                pass
            if not inv_old or not inv_new or inv_old == inv_new:
                return text, 0
            self._debugf(
                'log.rename_worker.locative_diff',
                "Locatives: diff '{old_mid}' -> '{new_mid}', inversion '{inv_old}' -> '{inv_new}'",
                old_mid=old_mid,
                new_mid=new_mid,
                inv_old=inv_old,
                inv_new=inv_new,
            )
            
            # Ищем подходящий шаблон и параметр, где значение ровно inv_old
            import html as _html
            
            # Функция для поиска шаблонов с учетом вложенности
            def find_templates_nested(text: str) -> list[tuple[str, str]]:
                """Находит все шаблоны верхнего уровня с учетом вложенности.
                Возвращает список (full_match, content)"""
                templates = []
                i = 0
                while i < len(text):
                    if i < len(text) - 1 and text[i:i+2] == '{{':
                        start = i
                        depth = 1
                        j = i + 2
                        while j < len(text) - 1 and depth > 0:
                            if text[j:j+2] == '{{':
                                depth += 1
                                j += 2
                            elif text[j:j+2] == '}}':
                                depth -= 1
                                j += 2
                            else:
                                j += 1
                        if depth == 0:
                            end = j
                            full_match = text[start:end]
                            content = text[start+2:end-2]
                            templates.append((full_match, content))
                            i = end
                        else:
                            i += 1
                    else:
                        i += 1
                return templates
            
            try:
                templates = find_templates_nested(text)
                self._debugf(
                    'log.rename_worker.locative_templates_found',
                    'Locatives: found {count} templates on page',
                    count=len(templates),
                )
            except Exception:
                templates = []
            changes = 0
            modified_text = text
            for full_template, inner in templates:
                if self._stop:
                    break
                if '|' not in inner:
                    continue
                parts = inner.split('|')
                template_name = parts[0].strip()
                # Подсчёт числа непустых значений параметров (для автоприменения при единственном значении)
                try:
                    from ..utils import normalize_spaces_for_compare as _norm
                except Exception:
                    def _norm(x: str) -> str:
                        return (x or '').strip()
                non_empty_params = 0
                try:
                    for tok in parts[1:]:
                        val_tok = tok
                        if '=' in tok:
                            try:
                                val_tok = tok.split('=', 1)[1]
                            except Exception:
                                val_tok = tok
                        try:
                            val_plain = (val_tok or '').strip().strip('"\'')
                        except Exception:
                            val_plain = (val_tok or '').strip()
                        if _norm(val_plain) != '':
                            non_empty_params += 1
                except Exception:
                    non_empty_params = 0
                # Автопропуск отмеченных шаблонов
                try:
                    if self.template_manager.is_template_auto_skip(template_name, self.family, self.lang):
                        continue
                except Exception:
                    pass
                for i, param in enumerate(parts[1:], 1):
                    if self._stop:
                        break
                    param_clean = (param or '').strip()
                    # Разобрать name=value
                    try:
                        if '=' in param_clean:
                            _name, _val = param_clean.split('=', 1)
                            value_part = (_val or '').strip()
                        else:
                            value_part = param_clean
                    except Exception:
                        value_part = param_clean
                    try:
                        value_plain = value_part.strip().strip('"\'')
                    except Exception:
                        value_plain = value_part.strip()
                    
                    # ОТЛАДКА: логируем каждый параметр, если он похож на искомое значение
                    try:
                        if value_plain and inv_old and (
                            value_plain.lower() == inv_old.lower() or 
                            inv_old.lower() in value_plain.lower() or
                            value_plain.lower() in inv_old.lower()
                        ):
                            self._debugf(
                                'log.rename_worker.locative_check_param',
                                "  Locatives: checking parameter {index} of template {template}: '{value}' vs '{target}'",
                                index=i,
                                template=template_name,
                                value=value_plain,
                                target=inv_old,
                            )
                    except Exception:
                        pass
                    
                    # Проверяем точное совпадение с инверсией старого локатива
                    if value_plain != inv_old and value_part != inv_old:
                        # также проверим HTML-экранирование
                        try:
                            if value_plain != _html.escape(inv_old, quote=True):
                                continue
                        except Exception:
                            continue
                    # Построим предложение замены
                    old_val = inv_old
                    new_val = inv_new
                    try:
                        # Сохранить капитализацию первой буквы
                        if old_val and new_val:
                            of = old_val[:1]
                            nf = new_val[:1]
                            if of.islower() and nf.isupper():
                                new_val = nf.lower() + new_val[1:]
                            elif of.isupper() and nf.islower():
                                new_val = nf.upper() + new_val[1:]
                    except Exception:
                        pass
                    proposed_param = param_clean.replace(old_val, new_val, 1)
                    new_parts = parts.copy()
                    new_parts[i] = proposed_param
                    proposed_template = '{{' + '|'.join(new_parts) + '}}'
                    # Предупреждение о дублях позиционных параметров (как выше)
                    dup_warning = False
                    dup_idx1 = 0
                    dup_idx2 = 0
                    try:
                        is_positional = ('=' not in param_clean)
                        if is_positional:
                            target_val = (new_val or '').strip().strip('"\'')
                            inner2 = proposed_template[2:-2]
                            parts2 = inner2.split('|') if inner2 else []
                            pos_list = []
                            for j, tok in enumerate(parts2[1:], 1):
                                if '=' in tok:
                                    continue
                                if (tok or '').strip().strip('"\'') == target_val and target_val != '':
                                    pos_list.append(j)
                            if len(pos_list) >= 2:
                                dup_warning = True
                                dup_idx1 = pos_list[0]
                                dup_idx2 = pos_list[-1]
                    except Exception:
                        pass
                    # Всегда ручное подтверждение (локативы): массовые действия отключены
                    loc_mass_disabled = True
                    result = self._request_template_confirmation(
                        page_title=page_title,
                        template=full_template,
                        old_full=old_cat_full,
                        new_full=new_cat_full,
                        mode='locative',
                        proposed_template=proposed_template,
                        old_direct=old_val,
                        new_direct=new_val,
                        dup_warning=dup_warning,
                        dup_idx1=dup_idx1,
                        dup_idx2=dup_idx2,
                        disable_mass_actions=True
                    )
                    action = result.get('action', 'skip')
                    if action == 'apply':
                        edited_template = (result.get('edited_template') or '').strip()
                        final_template = edited_template or proposed_template
                        # Сохраняем правило без автофлага (auto=none)
                        try:
                            self.template_manager.update_template_cache_from_edit(
                                self.family, self.lang, full_template, final_template, 'none', result.get('dedupe_mode')
                            )
                        except Exception:
                            pass
                        try:
                            self._last_template_change_was_locative = True
                            self._last_changed_template_name = (template_name or '').strip()
                        except Exception:
                            pass
                        modified_text = modified_text.replace(full_template, final_template, 1)
                        changes += 1
                        break
                    elif action == 'skip':
                        # Лог «пропущено пользователем (Шаблон:Имя)» — как в ветке direct/partial
                        try:
                            tmpl_label = self._format_template_label(template_name, False)
                            self._emitf(
                                'log.rename_worker.skip_user',
                                '→ {category} : "{title}" - skipped by user ({source})',
                                category=new_cat_full,
                                title=page_title,
                                source=tmpl_label,
                            )
                        except Exception:
                            pass
                        try:
                            self._last_template_interactions = True
                        except Exception:
                            pass
                        continue
                    elif action == 'cancel':
                        try:
                            self._last_template_interactions = True
                        except Exception:
                            pass
                        # Логируем пропуск текущей статьи аналогично partial, с пометкой локативов
                        try:
                            tmpl_label = self._format_template_label(template_name, False)
                            self._emitf(
                                'log.rename_worker.skip_user',
                                '→ {category} : "{title}" - skipped by user ({source})',
                                category=new_cat_full,
                                title=page_title,
                                source=tmpl_label,
                            )
                        except Exception:
                            pass
                        # Жёсткая остановка процесса: поднимем флаг и вернём сразу
                        self._stop = True
                        try:
                            self._emitf(
                                'log.rename_worker.stopped_by_user',
                                'Process stopped by user.',
                            )
                        except Exception:
                            pass
                        return modified_text, changes
                if changes > 0:
                    break
            return modified_text, changes
        except Exception as e:
            self._debugf('log.rename_worker.locative_processing_error', 'Locative processing error: {error}', error=e)
            return text, 0

    def _replace_category_links_in_text(self, text: str, family: str, lang: str, old_cat_full: str, new_cat_full: str) -> tuple[str, int]:
        """
        Замена прямых ссылок на категорию в тексте.
        
        Args:
            text: Исходный текст
            family: Семейство проекта
            lang: Код языка
            old_cat_full: Полное название старой категории
            new_cat_full: Полное название новой категории
            
        Returns:
            Tuple[str, int]: Измененный текст и количество замен
        """
        # Замена категорий: сохраняем ключ сортировки после «|», если он указан
        import re
        
        changes = 0
        modified_text = text
        
        # Паттерн для поиска ссылок на категории
        old_cat_name = old_cat_full.split(':', 1)[-1] if ':' in old_cat_full else old_cat_full
        new_cat_name = new_cat_full.split(':', 1)[-1] if ':' in new_cat_full else new_cat_full
        
        # Локализованные префиксы для NS-14 из кэша (включая алиасы), без хардкода
        alt_pat = None
        try:
            from ..core.namespace_manager import get_namespace_manager
            ns_manager = get_namespace_manager()
            info = ns_manager._load_ns_info(self.family, self.lang) or {}
            cat_meta = info.get(14) or {}
            all_prefixes = list((cat_meta.get('all') or set()))
            # Удаляем двоеточие из конца, экранируем для regex и сортируем по длине
            alts = [re.escape(p[:-1] if p.endswith(':') else p) for p in all_prefixes if p]
            if not alts:
                # Фолбэк: английский префикс из констант
                alts = [re.escape((DEFAULT_EN_NS.get(14, 'Category:').rstrip(':')))]
            # Более длинные строки матчим первыми
            alts.sort(key=len, reverse=True)
            alt_pat = '|'.join(alts)
        except Exception:
            alt_pat = re.escape((DEFAULT_EN_NS.get(14, 'Category:').rstrip(':')))

        # Единый паттерн с локальными префиксами; игнор регистра для унификации
        # Учитываем невидимые символы и любые юникод‑пробелы вокруг разделителей
        try:
            from ..utils import build_ws_fuzzy_pattern
            name_pat = build_ws_fuzzy_pattern(old_cat_name)
        except Exception:
            name_pat = re.escape(old_cat_name)
        invis = r"[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF]*"
        spaces = r"[\s\u00A0\u202F\u1680\u2000-\u200A\u2007\u205F\u3000]"
        rx = re.compile(r"\[\[" + invis + spaces + r"*" + invis + r"(?P<prefix>(" + alt_pat + r"))" + invis + spaces + r"*" + invis + r":" + invis + spaces + r"*" + invis + name_pat + invis + spaces + r"*(?:\|" + invis + spaces + r"*(?P<sort>[^\]]*?))?" + invis + spaces + r"*" + invis + r"\]\]", re.IGNORECASE)

        def _repl(m: "re.Match") -> str:
            try:
                sort = m.group('sort')
            except Exception:
                sort = None
            try:
                new_pref = self._policy_prefix(14, DEFAULT_EN_NS.get(14, 'Category:'))
            except Exception:
                new_pref = (DEFAULT_EN_NS.get(14, 'Category:'))
            if sort is not None:
                return f"[[{new_pref}{new_cat_name}|{sort}]]"
            return f"[[{new_pref}{new_cat_name}]]"

        try:
            modified_text, count = rx.subn(_repl, modified_text)
            changes += count
        except Exception:
            pass
        
        return modified_text, changes

    def _process_templates_interactive(self, text: str, old_cat_full: str, new_cat_full: str, page_title: str) -> tuple[str, int]:
        """
        Интерактивная обработка шаблонов с диалогами подтверждения.
        
        Args:
            text: Исходный текст
            old_cat_full: Полное название старой категории
            new_cat_full: Полное название новой категории
            page_title: Название обрабатываемой страницы
            
        Returns:
            Tuple[str, int]: Измененный текст и количество изменений
        """
        # Извлекаем название категории без префикса для поиска в параметрах
        old_cat_name = old_cat_full.split(':', 1)[-1] if ':' in old_cat_full else old_cat_full
        new_cat_name = new_cat_full.split(':', 1)[-1] if ':' in new_cat_full else new_cat_full

        self._debugf(
            'log.rename_worker.interactive_start',
            'Interactive template processing for page: {title}',
            title=page_title,
        )
        self._debugf(
            'log.rename_worker.interactive_search_category',
            'Searching for category "{category}" in template parameters',
            category=old_cat_name,
        )
        self._debugf(
            'log.rename_worker.interactive_search_templates',
            "Searching templates with category '{category}' on page {title}",
            category=old_cat_name,
            title=page_title,
        )
        
        import re
        changes = 0
        modified_text = text
        # Сбросим флаг «последние изменения были частичными» для логирования
        try:
            self._last_template_change_was_partial = False
            self._last_template_change_was_locative = False
        except Exception:
            pass
        
        # Ищем шаблоны с параметрами
        template_pattern = r'\{\{([^{}]+?)\}\}'
        templates = list(re.finditer(template_pattern, text, re.DOTALL))
        
        self._debugf(
            'log.rename_worker.interactive_templates_found',
            'Templates found on page: {count}',
            count=len(templates),
        )

        # Кандидаты частичных замен из названий категорий
        def _generate_partial_pairs(_old: str, _new: str) -> list[tuple[str, str]]:
            pairs: list[tuple[str, str]] = []
            try:
                old_s = (_old or '').strip()
                new_s = (_new or '').strip()
                if not old_s or not new_s or old_s == new_s:
                    return pairs
                # Токенизация только по пробелам и двоеточию — дефисы считаем частью слова
                tokens_old = re.split(r"[\s:]+", old_s)
                tokens_new = re.split(r"[\s:]+", new_s)
                # Индекс первого различия по токенам
                diff_i = 0
                L = min(len(tokens_old), len(tokens_new))
                while diff_i < L and tokens_old[diff_i] == tokens_new[diff_i]:
                    diff_i += 1
                
                # СПЕЦИАЛЬНЫЙ СЛУЧАЙ: расширение категории (все старые токены совпадают)
                # Например: "Родившиеся в Аксае" → "Родившиеся в Аксае (Дагестан)"
                if diff_i >= len(tokens_old) and len(tokens_new) > len(tokens_old):
                    self._debugf(
                        'log.rename_worker.partial_pairs_expand',
                        'Partial pairs: category expansion (all old tokens match)',
                    )
                    # Берем последний токен старой категории и расширяем его
                    # "Аксае" → "Аксае (Дагестан)"
                    old_last = tokens_old[-1] if tokens_old else ""
                    # Для новой берем последние N+1 токенов (где N - количество добавленных)
                    added = len(tokens_new) - len(tokens_old)
                    new_extended = " ".join(tokens_new[-(added+1):]) if added > 0 else tokens_new[-1]
                    if old_last and new_extended and old_last != new_extended:
                        pairs.append((old_last, new_extended))
                        self._debugf(
                            'log.rename_worker.partial_pair_expand_item',
                            '  Expansion pair: "{old}" -> "{new}"',
                            old=old_last,
                            new=new_extended,
                        )
                    # Также добавляем пары с предлогом: "в Аксае" → "в Аксае (Дагестан)"
                    if len(tokens_old) >= 2:
                        old_with_prep = " ".join(tokens_old[-2:])
                        new_with_prep = " ".join(tokens_new[-(added+2):]) if len(tokens_new) >= added+2 else new_extended
                        if old_with_prep and new_with_prep and (old_with_prep, new_with_prep) not in pairs:
                            pairs.append((old_with_prep, new_with_prep))
                            self._debugf(
                                'log.rename_worker.partial_pair_with_prep',
                                '  Pair with preposition: "{old}" -> "{new}"',
                                old=old_with_prep,
                                new=new_with_prep,
                            )
                    return pairs

                # СПЕЦИАЛЬНЫЙ СЛУЧАЙ: сужение категории (новая короче старой, общий префикс совпадает)
                # Например: "Умершие в Константинополе (Византия) империя)" → "Умершие в Константинополе (Византия)"
                if diff_i >= len(tokens_new) and len(tokens_old) > len(tokens_new):
                    self._debugf(
                        'log.rename_worker.partial_pairs_narrow',
                        'Partial pairs: category narrowing (new title is shorter)',
                    )
                    removed = len(tokens_old) - len(tokens_new)
                    # Базовая пара по "хвосту": "(Византия) империя)" → "(Византия)"
                    old_tail = " ".join(tokens_old[-(removed+1):]) if removed > 0 else (tokens_old[-1] if tokens_old else "")
                    new_tail = tokens_new[-1] if tokens_new else ""
                    if old_tail and new_tail and old_tail != new_tail:
                        pairs.append((old_tail, new_tail))
                        self._debugf(
                            'log.rename_worker.partial_pair_narrow_item',
                            '  Narrowing pair: "{old}" -> "{new}"',
                            old=old_tail,
                            new=new_tail,
                        )
                    # Пара с контекстом: "Константинополе (Византия) империя)" → "Константинополе (Византия)"
                    if len(tokens_old) >= (removed + 2) and len(tokens_new) >= 2:
                        old_with_ctx = " ".join(tokens_old[-(removed+2):])
                        new_with_ctx = " ".join(tokens_new[-2:])
                        if old_with_ctx and new_with_ctx and (old_with_ctx, new_with_ctx) not in pairs:
                            pairs.append((old_with_ctx, new_with_ctx))
                            self._debugf(
                                'log.rename_worker.partial_pair_with_context',
                                '  Pair with context: "{old}" -> "{new}"',
                                old=old_with_ctx,
                                new=new_with_ctx,
                            )
                    return pairs
                
                # Пара 1: минимальная разница по токену
                if diff_i < len(tokens_old) and diff_i < len(tokens_new):
                    pairs.append((tokens_old[diff_i], tokens_new[diff_i]))
                # Пара 2: предыдущий токен + текущий (например, «США 1» → «США 2»)
                if diff_i > 0 and diff_i < len(tokens_old) and diff_i < len(tokens_new):
                    span_old = " ".join(tokens_old[diff_i-1:diff_i+1]).strip()
                    span_new = " ".join(tokens_new[diff_i-1:diff_i+1]).strip()
                    if span_old and span_new and (span_old, span_new) not in pairs:
                        pairs.append((span_old, span_new))
                # Пара 3: хвост от точки расхождения
                tail_old = " ".join(tokens_old[diff_i:]).strip()
                tail_new = " ".join(tokens_new[diff_i:]).strip()
                if tail_old and tail_new and (tail_old, tail_new) not in pairs:
                    pairs.append((tail_old, tail_new))
            except Exception:
                pass
            return pairs

        partial_pairs = _generate_partial_pairs(old_cat_name, new_cat_name)
        self._debugf(
            'log.rename_worker.partial_pairs_generated',
            'Generated partial replacement pairs: {count}',
            count=len(partial_pairs),
        )
        for idx, (old_p, new_p) in enumerate(partial_pairs, 1):
            self._debugf(
                'log.rename_worker.partial_pair_item',
                '  Pair {index}: "{old}" -> "{new}"',
                index=idx,
                old=old_p,
                new=new_p,
            )
        
        # Нормализация строк для сравнения: удаляем невидимые спецсимволы и нормализуем пробелы
        from ..utils import normalize_spaces_for_compare as _norm
        def _normalize_for_compare(s: str) -> str:
            return _norm(s)
        
        # Предрассчитанные нормализованные формы искомых названий категорий
        old_cat_name_norm = _normalize_for_compare(old_cat_name)
        old_cat_full_norm = _normalize_for_compare(old_cat_full)
        
        for match in templates:
            if self._stop:
                break
                
            template_content = match.group(1)
            full_template = match.group(0)
            
            if '|' not in template_content:
                continue
                
            # Ищем параметры, содержащие название категории
            parts = template_content.split('|')
            template_name = parts[0].strip()

            # Автопропуск для отмеченных шаблонов
            try:
                if self.template_manager.is_template_auto_skip(template_name, self.family, self.lang):
                    self._debugf(
                        'log.rename_worker.template_auto_skip',
                        'Template {template}: marked for auto-skip, skipping without dialog',
                        template=template_name,
                    )
                    # Лог в стиле оригинала (пропуск автоматически)
                    try:
                        tmpl_label = self._format_template_label(template_name)
                    except Exception:
                        tmpl_label = f"{DEFAULT_EN_NS.get(10, 'Template:')}{template_name}"
                    try:
                        self._emitf(
                            'log.rename_worker.skip_auto',
                            '→ {category} : "{title}" - skipped automatically ({source})',
                            category=new_cat_full,
                            title=page_title,
                            source=tmpl_label,
                        )
                    except Exception:
                        self._emitf(
                            'log.rename_worker.skip_auto',
                            '→ {category} : "{title}" - skipped automatically ({source})',
                            category=new_cat_full,
                            title=page_title,
                            source=tmpl_label,
                        )
                    continue
            except Exception:
                pass
            
            found_matches = []
            for i, param in enumerate(parts[1:], 1):
                param_clean = param.strip()
                param_norm = param_clean.strip()
                
                # Выделяем имя=значение (если именованный параметр)
                name_part = None
                value_part = param_clean
                value_norm = param_norm
                try:
                    if '=' in param_clean:
                        name_part, value_part = param_clean.split('=', 1)
                        name_part = name_part.strip()
                        value_norm = value_part.strip()
                except Exception:
                    name_part = None
                    value_part = param_clean
                    value_norm = param_norm

                # Значение без внешних кавычек
                try:
                    value_plain = value_norm.strip('"\'')
                except Exception:
                    value_plain = value_norm

                # Проверяем совпадения, учитывая HTML-экранирование (&quot; и др.)
                try:
                    old_cat_name_enc = html.escape(old_cat_name, quote=True)
                    old_cat_full_enc = html.escape(old_cat_full, quote=True)
                    new_cat_name_enc = html.escape(new_cat_name, quote=True)
                    new_cat_full_enc = html.escape(new_cat_full, quote=True)
                except Exception:
                    old_cat_name_enc = old_cat_name
                    old_cat_full_enc = old_cat_full
                    new_cat_name_enc = new_cat_name
                    new_cat_full_enc = new_cat_full

                matched_this_param = False
                def _append_match(
                    old_val: str,
                    new_val: str,
                    *,
                    _param_index: int = i,
                    _param_value: str = param_clean,
                    _matches=found_matches,
                ):
                    nonlocal matched_this_param
                    _matches.append({
                        'type': 'direct',
                        'param_index': _param_index,
                        'param_value': _param_value,
                        'old_value': old_val,
                        'new_value': new_val
                    })
                    matched_this_param = True

                # Подготовим значения для проверки «только первая буква может отличаться по регистру»
                pos_plain = param_norm.strip('"\'')
                def _eq_first_only(a: str, b: str) -> bool:
                    try:
                        if a == b:
                            return True
                        if not a or not b:
                            return False
                        return (a[:1].casefold() == b[:1].casefold()) and (a[1:] == b[1:])
                    except Exception:
                        return False

                # 1) Совпадение всей позиции (позиционный параметр)
                if param_norm == old_cat_name:
                    _append_match(old_cat_name, new_cat_name)
                elif param_norm == old_cat_full:
                    _append_match(old_cat_full, new_cat_full)
                elif old_cat_name_enc and param_norm == old_cat_name_enc:
                    _append_match(old_cat_name_enc, new_cat_name_enc)
                elif old_cat_full_enc and param_norm == old_cat_full_enc:
                    _append_match(old_cat_full_enc, new_cat_full_enc)
                # 1b) Позиционный параметр: допускаем различие только в первой букве по регистру
                elif _eq_first_only(pos_plain, old_cat_name):
                    _append_match(pos_plain, new_cat_name)
                elif _eq_first_only(pos_plain, old_cat_full):
                    _append_match(pos_plain, new_cat_full)
                # 2) Совпадение значения именованного параметра: name=VALUE
                elif value_norm == old_cat_name or value_plain == old_cat_name:
                    _append_match(old_cat_name, new_cat_name)
                elif value_norm == old_cat_full or value_plain == old_cat_full:
                    _append_match(old_cat_full, new_cat_full)
                elif old_cat_name_enc and (value_norm == old_cat_name_enc or value_plain == old_cat_name_enc):
                    _append_match(old_cat_name_enc, new_cat_name_enc)
                elif old_cat_full_enc and (value_norm == old_cat_full_enc or value_plain == old_cat_full_enc):
                    _append_match(old_cat_full_enc, new_cat_full_enc)
                # 2b) Именованный параметр: допускаем различие только в первой букве по регистру
                elif _eq_first_only(value_plain, old_cat_name):
                    _append_match(value_plain, new_cat_name)
                elif _eq_first_only(value_plain, old_cat_full):
                    _append_match(value_plain, new_cat_full)

                # 2c) Сопоставление после нормализации невидимых символов/пробелов
                else:
                    try:
                        value_plain_normed = _normalize_for_compare(value_plain)
                        value_norm_normed = _normalize_for_compare(value_norm)
                    except Exception:
                        value_plain_normed = value_plain
                        value_norm_normed = value_norm
                    if value_plain_normed == old_cat_name_norm or value_norm_normed == old_cat_name_norm:
                        _append_match(value_plain, new_cat_name)
                    elif value_plain_normed == old_cat_full_norm or value_norm_normed == old_cat_full_norm:
                        _append_match(value_plain, new_cat_full)

                # Если прямых совпадений не найдено — пробуем частичные пары
                if not matched_this_param and partial_pairs:
                    try:
                        # Вспомогательная проверка «подстрока с границами слова»
                        def _has_sub_with_boundaries(text: str, sub: str) -> bool:
                            try:
                                if not text or not sub:
                                    return False
                                idx = text.find(sub)
                                if idx == -1:
                                    return False
                                before = text[idx - 1] if idx > 0 else ''
                                after = text[idx + len(sub)] if (idx + len(sub)) < len(text) else ''
                                def _is_word_char(ch: str) -> bool:
                                    try:
                                        return ch.isalnum() or ch == '_'
                                    except Exception:
                                        return False
                                if before and _is_word_char(before):
                                    return False
                                if after and _is_word_char(after):
                                    return False
                                return True
                            except Exception:
                                # Фолбэк: простая подстрока
                                try:
                                    return sub in (text or '')
                                except Exception:
                                    return False

                        # Сначала рассматриваем более длинные подстроки, чтобы не портить контекст (напр. "Витории (Испания)" раньше, чем "Витории")
                        ppairs = sorted(partial_pairs, key=lambda p: len((p[0] or '').strip()), reverse=True)
                        for old_sub, new_sub in ppairs:
                            old_sub = (old_sub or '').strip()
                            new_sub = (new_sub or '').strip()
                            if not old_sub or not new_sub:
                                continue
                            old_sub_enc = html.escape(old_sub, quote=True)
                            # 2c.1) Строгое равенство значению параметра (с учётом кавычек/экранирования)
                            if (
                                value_plain == old_sub or value_norm == old_sub or
                                (old_sub_enc and (value_plain == old_sub_enc or value_norm == old_sub_enc))
                            ):
                                # Защита от повторной замены нужна только для «расширяющих» правил
                                # (old_sub -> new_sub, где new_sub содержит old_sub).
                                try:
                                    is_expanding = bool(old_sub and new_sub and old_sub != new_sub and (old_sub in new_sub))
                                    already_replaced = is_expanding and (
                                        (new_sub and new_sub in value_plain) or (new_sub and new_sub in value_norm)
                                    )
                                    if already_replaced:
                                        self._debugf(
                                            'log.rename_worker.partial_skip_equal_contains',
                                            '    Skip (strict match): value "{value}" already contains new substring "{substring}"',
                                            value=value_plain,
                                            substring=new_sub,
                                        )
                                        continue
                                except Exception:
                                    pass
                                
                                found_matches.append({
                                    'type': 'partial',
                                    'param_index': i,
                                    'param_value': param_clean,
                                    'old_sub': old_sub,
                                    'new_sub': new_sub
                                })
                                break
                            # 2c.2) Подстрочное совпадение внутри значения параметра
                            elif (
                                _has_sub_with_boundaries(value_plain, old_sub) or
                                _has_sub_with_boundaries(value_norm, old_sub) or
                                (old_sub_enc and (
                                    _has_sub_with_boundaries(value_plain, old_sub_enc) or
                                    _has_sub_with_boundaries(value_norm, old_sub_enc)
                                ))
                            ):
                                # Защита от повторной замены только для «расширения»:
                                # old_sub="в Аксае", new_sub="в Аксае (Дагестан)".
                                try:
                                    is_expanding = bool(old_sub and new_sub and old_sub != new_sub and (old_sub in new_sub))
                                    already_replaced = is_expanding and (
                                        (new_sub and new_sub in value_plain) or (new_sub and new_sub in value_norm)
                                    )
                                    if already_replaced:
                                        self._debugf(
                                            'log.rename_worker.partial_skip_contains',
                                            '    Skip: value "{value}" already contains new substring "{substring}"',
                                            value=value_plain,
                                            substring=new_sub,
                                        )
                                        continue
                                except Exception:
                                    pass
                                
                                found_matches.append({
                                    'type': 'partial',
                                    'param_index': i,
                                    'param_value': param_clean,
                                    'old_sub': old_sub,
                                    'new_sub': new_sub
                                })
                                break
                    except Exception:
                        pass
            
            # Если найдены совпадения, запрашиваем подтверждение через диалог
            for match_info in found_matches:
                if self._stop:
                    break
                    
                self._debugf(
                    'log.rename_worker.template_match_found',
                    'Found match in template {template}: {value}',
                    template=template_name,
                    value=match_info["param_value"],
                )
                
                # Попытка автоприменения по сохранённым правилам (auto=approve) без показа диалога
                # для случая частичного совпадения unnamed_single
                try:
                    auto_applied = False
                    if (match_info.get('type') == 'partial'):
                        tm = self.template_manager
                        # Сформировать ключ шаблона и найти правило
                        tmpl_key = tm._norm_tmpl_key(template_name, self.family, self.lang)
                        bucket = (tm.template_auto_cache or {}).get(tmpl_key) or {}
                        bucket_auto = str(bucket.get('auto') or '').strip().casefold()
                        rules = list(bucket.get('rules') or [])
                        # Ищем правило unnamed_single с from == old_sub
                        old_sub_val = (match_info.get('old_sub') or '').strip()
                        param_val = (match_info.get('param_value') or '').strip()
                        rule = None
                        from ..utils import normalize_spaces_for_compare as _norm
                        def _has_sub_with_boundaries(text: str, sub: str) -> bool:
                            try:
                                if not text or not sub:
                                    return False
                                idx = text.find(sub)
                                while idx != -1:
                                    before = text[idx - 1] if idx > 0 else ''
                                    after = text[idx + len(sub)] if (idx + len(sub)) < len(text) else ''
                                    def _is_word_char(ch: str) -> bool:
                                        try:
                                            return ch.isalnum() or ch == '_'
                                        except Exception:
                                            return False
                                    if (not before or not _is_word_char(before)) and (not after or not _is_word_char(after)):
                                        return True
                                    idx = text.find(sub, idx + 1)
                                return False
                            except Exception:
                                return False
                        for r in rules:
                            try:
                                if r.get('type') != 'unnamed_single':
                                    continue
                                rf = (r.get('from') or '').strip()
                                # Совпадение по «старой подстроке» ИЛИ по полному значению параметра
                                if _norm(rf) == _norm(old_sub_val) or _norm(rf) == _norm(param_val) or _has_sub_with_boundaries(param_val, rf):
                                    rule = r
                                    break
                            except Exception:
                                continue
                        rule_auto = str((rule or {}).get('auto') or '').strip().casefold() if rule else 'none'
                        if rule and (rule_auto == 'approve' or bucket_auto == 'approve'):
                            # Построим итоговый шаблон как при ручном подтверждении
                            is_partial = True
                            old_val = match_info.get('old_sub') or ''
                            new_val = match_info.get('new_sub') or ''
                            try:
                                if old_val and new_val:
                                    new_val = align_first_letter_case(old_val, new_val)
                            except Exception:
                                pass
                            proposed_param = match_info['param_value'].replace(old_val, new_val, 1)
                            new_parts = parts.copy()
                            new_parts[match_info['param_index']] = proposed_param
                            final_template = '{{' + '|'.join(new_parts) + '}}'
                            # Дедупликация позиционных параметров, если задана в правиле
                            try:
                                dedupe_mode = tm.normalize_dedupe_mode(rule.get('dedupe')) if rule else ''
                            except Exception:
                                dedupe_mode = ''
                            try:
                                if dedupe_mode in ('left', 'right'):
                                    final_template = tm.apply_positional_dedupe(final_template, new_val, dedupe_mode)
                            except Exception:
                                pass
                            # Применяем изменение
                            modified_text = modified_text.replace(full_template, final_template, 1)
                            changes += 1
                            try:
                                self._last_template_change_was_partial = True
                                self._last_changed_template_name = (template_name or '').strip()
                            except Exception:
                                pass
                            self._debugf(
                                'log.rename_worker.template_auto_apply',
                                'Auto-applied by rule (auto=approve) for template {template}',
                                template=template_name,
                            )
                            auto_applied = True
                    if auto_applied:
                        # Переходим к следующему шаблону без показа диалога
                        break
                except Exception:
                    # Любая ошибка автоприменения — игнор, пойдём обычным путём через диалог
                    pass

                # Создаем предложение замены
                # Требование: предлагать значение с точно такой же капитализацией,
                # как в исходном параметре. Если исходное значение начиналось с
                # заглавной — сохраняем заглавную; если со строчной — сохраняем строчную.
                is_partial = (match_info.get('type') != 'direct')
                old_val = match_info.get('old_value') if not is_partial else (match_info.get('old_sub') or '')
                new_val = match_info.get('new_value') if not is_partial else (match_info.get('new_sub') or '')
                try:
                    if old_val and new_val:
                        new_val = align_first_letter_case(old_val, new_val)
                except Exception:
                    pass
                proposed_param = match_info['param_value'].replace(old_val, new_val, 1)
                
                # Создаем предложение для всего шаблона
                new_parts = parts.copy()
                new_parts[match_info['param_index']] = proposed_param
                proposed_template = '{{' + '|'.join(new_parts) + '}}'
                
                # Проверяем, создаёт ли замена дубликат в позиционных параметрах
                dup_warning = False
                dup_idx1 = 0
                dup_idx2 = 0
                try:
                    # Дедупликация только для позиционного параметра
                    is_positional = ('=' not in match_info.get('param_value', ''))
                    if is_positional:
                        # Нормализованное новое значение для сравнения
                        def _norm_val(s: str) -> str:
                            try:
                                return (s or '').strip().strip('\"\'')
                            except Exception:
                                return (s or '').strip()
                        new_val_norm = _norm_val(new_val)
                        # Пройдём по всем позиционным параметрам после замены
                        for j, token in enumerate(new_parts[1:], 1):
                            try:
                                if j == match_info['param_index']:
                                    # Это уже заменённый параметр — сравниваем с остальными
                                    continue
                                if '=' in token:
                                    continue
                                if _norm_val(token) == new_val_norm and new_val_norm != '':
                                    dup_warning = True
                                    # Пара индексов, где обнаружены одинаковые значения
                                    dup_idx1 = min(j, match_info['param_index'])
                                    dup_idx2 = max(j, match_info['param_index'])
                                    break
                            except Exception:
                                continue
                except Exception:
                    dup_warning = False

                # Отправляем запрос на подтверждение
                try:
                    mode = 'partial' if is_partial else 'direct'
                    result = self._request_template_confirmation(
                        page_title=page_title,
                        template=full_template,
                        old_full=old_cat_full,
                        new_full=new_cat_full,
                        mode=mode,
                        proposed_template=proposed_template,
                        old_direct=(old_val if not is_partial else ''),
                        new_direct=(new_val if not is_partial else ''),
                        old_sub=(old_val if is_partial else ''),
                        new_sub=(new_val if is_partial else ''),
                        dup_warning=dup_warning,
                        dup_idx1=dup_idx1,
                        dup_idx2=dup_idx2
                    )
                    
                    action = result.get('action', 'skip')
                    self._debugf(
                        'log.rename_worker.confirmation_result',
                        'Confirmation dialog result: {action}',
                        action=action,
                    )
                    
                    if action == 'apply':
                        # Определяем итоговый вариант замены (учесть ручное редактирование)
                        edited_template = (result.get('edited_template') or '').strip()
                        final_template = edited_template or proposed_template
                        # Применим выбранный режим дедупликации к текущему финальному шаблону
                        dedupe_mode = result.get('dedupe_mode', None)
                        try:
                            # Нормализуем и применим дедупликацию через помощник TemplateManager
                            dedupe_mode = self.template_manager.normalize_dedupe_mode(dedupe_mode)
                            if dup_warning and dedupe_mode in ('left', 'right'):
                                final_template = self.template_manager.apply_positional_dedupe(final_template, new_val, dedupe_mode)
                        except Exception:
                            pass
                        # Фиксируем правило в кэше (для автоприменения в будущем)
                        # Если пользователь выбрал «Подтверждать все аналогичные», правило уже сохранено из UI — не дублируем.
                        try:
                            if not result.get('auto_confirm'):
                                rule_auto = 'none'
                                self.template_manager.update_template_cache_from_edit(
                                    self.family, self.lang, full_template, final_template, rule_auto, dedupe_mode
                                )
                        except Exception:
                            pass
                        # Устанавливаем флаг автоподтверждения для данного шаблона, если запрошено
                        try:
                            if result.get('auto_confirm'):
                                self.template_manager.set_template_auto_flag(template_name, self.family, self.lang, True)
                        except Exception:
                            pass
                        # Применяем изменение в тексте
                        modified_text = modified_text.replace(full_template, final_template, 1)
                        changes += 1
                        try:
                            if is_partial:
                                self._last_template_change_was_partial = True
                            self._last_changed_template_name = (template_name or '').strip()
                        except Exception:
                            pass
                        self._debugf(
                            'log.rename_worker.template_change_applied',
                            'Applied change in template {template}',
                            template=template_name,
                        )
                        # (лог названия шаблона не требуется, ярлык уже формируется при необходимости в других местах)
                        break  # Переходим к следующему шаблону
                    elif action == 'skip':
                        # Отметить шаблон на автопропуск, если запрошено
                        try:
                            if result.get('auto_skip'):
                                self.template_manager.set_template_skip_flag(template_name, self.family, self.lang, True)
                        except Exception:
                            pass
                        self._debugf(
                            'log.rename_worker.template_change_skipped',
                            'Skipped change in template {template}',
                            template=template_name,
                        )
                        # Лог в стиле оригинала (пропуск пользователем)
                        try:
                            tmpl_label = self._format_template_label(template_name, False)
                            src_label = tmpl_label
                            self._emitf(
                                'log.rename_worker.skip_user',
                                '→ {category} : "{title}" - skipped by user ({source})',
                                category=new_cat_full,
                                title=page_title,
                                source=src_label,
                            )
                        except Exception:
                            self._emitf(
                                'log.rename_worker.skip_user',
                                '→ {category} : "{title}" - skipped by user ({source})',
                                category=new_cat_full,
                                title=page_title,
                                source=f"{DEFAULT_EN_NS.get(10, 'Template:')}{template_name}",
                            )
                        # Отметим, что было взаимодействие (диалог) — чтобы не выводить общий «пропущено, без изменений»
                        try:
                            self._last_template_interactions = True
                        except Exception:
                            pass
                        continue
                    elif action == 'cancel':
                        self._debugf('log.rename_worker.user_cancelled', 'User cancelled the process')
                        # Отметим факт взаимодействия, чтобы внешний код не выводил
                        # общий фолбэк «пропущено, без изменений (Ш:Категории)»
                        try:
                            self._last_template_interactions = True
                        except Exception:
                            pass
                        # Лог: укажем шаблон и тип совпадения (полное/частичное), чтобы в
                        # «Источник» корректно показалась иконка #️⃣ для частичных совпадений
                        try:
                            tmpl_label = self._format_template_label(template_name, is_partial)
                            self._emitf(
                                'log.rename_worker.skip_user',
                                '→ {category} : "{title}" - skipped by user ({source})',
                                category=new_cat_full,
                                title=page_title,
                                source=tmpl_label,
                            )
                        except Exception:
                            pass
                        self._stop = True
                        self._emitf('log.rename_worker.stopped_by_user', 'Process stopped by user.')
                        return modified_text, changes
                    else:
                        self._debugf('log.rename_worker.unknown_action', 'Unknown action: {action}', action=action)
                        continue
                        
                except Exception as e:
                    self._debugf(
                        'log.rename_worker.confirmation_request_error',
                        'Confirmation request error: {error}',
                        error=e,
                    )
                    continue
        
        self._debugf(
            'log.rename_worker.interactive_completed',
            'Interactive processing completed: {count} changes',
            count=changes,
        )
        return modified_text, changes
    
    def _request_template_confirmation(self, page_title: str, template: str, old_full: str, new_full: str, 
                                     mode: str, proposed_template: str = '', old_direct: str = '', 
                                     new_direct: str = '', old_sub: str = '', new_sub: str = '',
                                     dup_warning: bool = False, dup_idx1: int = 0, dup_idx2: int = 0,
                                     disable_mass_actions: bool = False) -> dict:
        """
        Запрос подтверждения изменения в шаблоне через диалог.
        
        Returns:
            dict: Результат с ключом 'action' ('apply', 'skip', 'cancel')
        """
        try:
            from threading import Event
            
            self._req_seq += 1
            req_id = self._req_seq
            
            # Создаем событие для ожидания ответа
            ev = Event()
            self._prompt_events[req_id] = ev
            self._prompt_results[req_id] = {}
            
            # Отправляем запрос на показ диалога
            self.template_review_request.emit({
                'request_id': req_id,
                'page_title': page_title,
                'template': template,
                'old_full': old_full,
                'new_full': new_full,
                'mode': mode,
                'proposed_template': proposed_template,
                'old_sub': old_sub,
                'new_sub': new_sub,
                'old_direct': old_direct,
                'new_direct': new_direct,
                # Параметры предупреждения о дублях позиционных значений
                'dup_warning': bool(dup_warning),
                'dup_idx1': int(dup_idx1),
                'dup_idx2': int(dup_idx2),
                # Блокировка массовых действий (для многопараметричных локативов)
                'disable_mass_actions': bool(disable_mass_actions),
            })
            
            # Ждем ответа от диалога
            while not self._stop and not ev.wait(0.1):
                pass
                
            result = self._prompt_results.get(req_id, {}) or {}
            
            # Очищаем временные данные
            self._prompt_events.pop(req_id, None)
            self._prompt_results.pop(req_id, None)
            
            action = str(result.get('action') or '') or 'skip'
            result['action'] = action
            return result
            
        except Exception as e:
            self._debugf(
                'log.rename_worker.request_template_confirmation_error',
                'Error in _request_template_confirmation: {error}',
                error=e,
            )
            return {'action': 'skip'}
