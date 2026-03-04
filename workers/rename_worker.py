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
from ..constants import DEFAULT_EN_NS
from ..utils import format_russian_pages_nominative
from ..utils import align_first_letter_case


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
                label = f"{label} [частично]"
            except Exception:
                pass
        return label

    def _page_kind(self, page: pywikibot.Page) -> str:
        """Возвращает тип объекта для сообщений лога: 'категория' | 'статья' | 'страница'."""
        try:
            nsid = page.namespace().id
            return 'категория' if nsid == 14 else 'статья'
        except Exception:
            return 'страница'

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
                    labels = [f"{self._policy_prefix(10, DEFAULT_EN_NS.get(10, 'Template:'))}Название"]
                except Exception:
                    labels = [f"{DEFAULT_EN_NS.get(10, 'Template:')}Название"]
            formatted = ', '.join(f"[[{lbl}]]" for lbl in labels)
            base = f"{base} (категоризация через {formatted})"
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
            from ..utils import debug
            debug(f'Получен ответ от диалога: {response_data}')
            
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
                debug(f'Сохранен результат для req_id {req_id}: {self._prompt_results[req_id]}')
                
                # Уведомляем ожидающий поток
                if req_id in self._prompt_events:
                    self._prompt_events[req_id].set()
                    debug(f'Событие установлено для req_id {req_id}')
            else:
                debug(f'Неизвестный req_id: {req_id}')
        except Exception as e:
            debug(f'Ошибка в _on_review_response: {e}')

    def run(self):
        """Основной метод выполнения переименования."""
        site = pywikibot.Site(self.lang, self.family)
        from ..utils import debug
        debug(f'Login attempt rename lang={self.lang}')

        if self.username and self.password:
            try:
                site.login(user=self.username)
            except Exception as e:
                self.progress.emit(f"Ошибка авторизации: {type(e).__name__}: {e}")
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
                        self.progress.emit(f"Некорректная строка (требуется 3 столбца): {row}")
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
                                    self.progress.emit(f"Категория <b>{html.escape(old_full_check)}</b> не существует. Перенос содержимого отключён.")
                                except Exception:
                                    self.progress.emit(f"Категория {old_full_check} не существует. Перенос содержимого отключён.")
                                continue
                        except Exception:
                            pass

                    leave_redirect = self.leave_cat_redirect if is_category else self.leave_other_redirect
                    
                    # Если переименование категории выключено — пропускаем сам move для категорий
                    if is_category and not self.move_category:
                        try:
                            self.progress.emit(f"Пропущено переименование категории <b>{html.escape(old_name)}</b> → <b>{html.escape(new_name)}</b>. Переносим содержимое…")
                        except Exception:
                            pass
                    else:
                        self._move_page(site, old_name, new_name, reason, leave_redirect)
                    
                    # Если это категория и хотя бы одна фаза переноса включена — переносим участников
                    if is_category and self.move_members and (self.phase1_enabled or self.find_in_templates) and not self._stop:
                        try:
                            from ..utils import debug
                            debug(f'Начинаем перенос содержимого категории: {old_name} → {new_name}')
                            debug(f'move_members={self.move_members}, phase1_enabled={self.phase1_enabled}, find_in_templates={self.find_in_templates}')
                            self._move_category_members(site, old_name, new_name)
                        except Exception as e:
                            self.progress.emit(f"Ошибка переноса содержимого категории '{old_name}': {e}")
                    # Инкремент общего прогресса по строкам TSV
                    try:
                        self.tsv_progress_inc.emit()
                    except Exception:
                        pass
        except Exception as e:
            self.progress.emit(f"Ошибка работы с файлом TSV: {e}")
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
                    typ = 'страница'
                if typ == 'категория':
                    try:
                        self.progress.emit(f"<b>{html.escape(old_name)}</b> не найдена.")
                    except Exception:
                        self.progress.emit(f"{old_name} не найдена.")
                else:
                    self.progress.emit(f"Страница <b>{html.escape(old_name)}</b> не найдена.")
                return
            if new_page.exists():
                # Структурированное событие; текстовый лог используем только как фолбэк
                try:
                    self.log_event.emit({'type': 'destination_exists', 'title': new_name, 'status': 'info'})
                except Exception:
                    try:
                        self.progress.emit(f"Страница назначения <b>{html.escape(new_name)}</b> уже существует.")
                    except Exception:
                        self.progress.emit(f"Страница назначения {new_name} уже существует.")
                return

            # Сформируем комментарий к правке для операции переименования
            # В summary оставляем только дополнительный комментарий (без «Old → New»),
            # так как информация о переименовании отображается автоматически системой.
            comment_text = self.override_comment or (reason or '')
            move_summary = comment_text

            # Системное сообщение: начинаем переименование
            try:
                self.progress.emit(f"Начинаем переименование: <b>{html.escape(old_name)}</b> → <b>{html.escape(new_name)}</b>")
            except Exception:
                self.progress.emit(f"Начинаем переименование: {old_name} → {new_name}")

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
                                    self.progress.emit(
                                        f"ℹ️ После переименования перенаправление осталось: "
                                        f"{html.escape(old_name)} → {html.escape(new_name)} "
                                        f"(возможно, недостаточно прав suppressredirect)."
                                    )
                                except Exception:
                                    self.progress.emit(
                                        f"ℹ️ После переименования перенаправление осталось: "
                                        f"{old_name} → {new_name} "
                                        f"(возможно, недостаточно прав suppressredirect)."
                                    )

                    # Сообщение после тире: оставляем только комментарий/причину, без "Old → New"
                    try:
                        tail = comment_text if comment_text else ''
                        msg = f"Переименовано успешно — {html.escape(tail)}" if tail else "Переименовано успешно"
                        self.progress.emit(msg)
                    except Exception:
                        self.progress.emit("Переименовано успешно")
                    return
                except Exception as e:
                    if self._is_rate_error(e) and attempt < 3:
                        self._increase_save_interval(attempt)
                        try:
                            self.progress.emit(f"Лимит запросов при переименовании: пауза {self._save_min_interval:.2f}s · попытка {attempt}/3")
                        except Exception:
                            pass
                        continue
                    try:
                        self.progress.emit(f"Ошибка переименования <b>{html.escape(old_name)}</b>: {type(e).__name__}: {e}")
                    except Exception:
                        self.progress.emit(f"Ошибка переименования {old_name}: {type(e).__name__}: {e}")
                    return
        except Exception as e:
            try:
                self.progress.emit(f"Критическая ошибка переименования <b>{html.escape(old_name)}</b>: {e}")
            except Exception:
                self.progress.emit(f"Критическая ошибка переименования {old_name}: {e}")

    def _move_category_members(self, site: pywikibot.Site, old_name: str, new_name: str):
        """
        Перенос содержимого категории (прямые ссылки и шаблоны).
        
        Args:
            site: Объект сайта pywikibot
            old_name: Старое название категории
            new_name: Новое название категории
        """
        try:
            from ..utils import debug
            debug(f'_move_category_members: old_name={old_name}, new_name={new_name}')
            
            # Получаем полные названия категорий с префиксами
            old_cat_full = _ensure_title_with_ns(old_name, self.family, self.lang, 14, DEFAULT_EN_NS.get(14, 'Category:'))
            new_cat_full = _ensure_title_with_ns(new_name, self.family, self.lang, 14, DEFAULT_EN_NS.get(14, 'Category:'))
            
            debug(f'После _ensure_title_with_ns: old_cat_full={old_cat_full}, new_cat_full={new_cat_full}')
            
            old_cat_page = pywikibot.Page(site, old_cat_full)
            debug(f'Проверяем существование категории: {old_cat_full}')
            
            # Проверяем существование категории (информативно, не блокируем процесс)
            try:
                category_exists = old_cat_page.exists()
                debug(f'Категория существует: {category_exists}')
            except Exception as e:
                debug(f'Ошибка проверки существования категории: {e}')
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
                
                debug('Получаем список страниц в категории через API (с продолжениями)')
                while True:
                    if self._stop:
                        break
                    _rate_wait()
                    r = REQUEST_SESSION.get(api_url, params=params, timeout=15, headers=REQUEST_HEADERS)
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code} при запросе {api_url}")
                    data = r.json()
                    chunk = [m.get('title') for m in (data.get('query', {}).get('categorymembers', []) or []) if m.get('title')]
                    members_titles.extend(chunk)
                    if 'continue' in data:
                        params.update(data['continue'])
                    else:
                        break
                
                debug(f"Найдено {format_russian_pages_nominative(len(members_titles))} в категории")
                if not members_titles:
                    self.progress.emit(f"Категория <b>{html.escape(old_cat_full)}</b> пуста.")
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
                        # Фолбэк на текст, если событие не удалось отправить
                        try:
                            self.progress.emit(f"ℹ️ Перенос содержимого категории <b>{html.escape(old_cat_full)}</b> → <b>{html.escape(new_cat_full)}</b>: {format_russian_pages_nominative(len(members_titles))}")
                        except Exception:
                            self.progress.emit(f"ℹ️ Перенос содержимого категории {old_cat_full} → {new_cat_full}: {format_russian_pages_nominative(len(members_titles))}")
                except Exception:
                    # Если общий блок упал, попробуем простую текстовую строку
                    self.progress.emit(f"ℹ️ Перенос содержимого категории {old_cat_full} → {new_cat_full}: {format_russian_pages_nominative(len(members_titles))}")
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
                        debug(f'Обрабатываем страницу: {page.title()}')
                        changes_made = self._process_category_member(site, page, old_cat_full, new_cat_full)

                        # Немедленно запускаем фазу 2 (если включена) и фиксируем были ли изменения
                        phase2_changes = 0
                        if self.find_in_templates and title not in backlog_seen:
                            debug(f'Фаза 2 (немедленно): обрабатываем страницу {title}')
                            try:
                                _, phase2_changes = self._process_title_templates(site, title, old_cat_full, new_cat_full)
                            except Exception as e:
                                self.progress.emit(f"Ошибка обработки шаблонов на странице {title}: {e}")
                                debug(f'Ошибка обработки шаблонов на странице {title}: {e}')
                            # Отмечаем как посещённую, чтобы не обрабатывать повторно
                            backlog_seen.add(title)

                        # Если ни фаза 1, ни фаза 2 не внесли изменений — добавим понятную строку в лог
                        try:
                            if changes_made == 0 and (not self.find_in_templates or (phase2_changes == 0 and not getattr(self, '_last_template_interactions', False))):
                                # Для корректной классификации как «шаблонной» операции
                                # укажем источник вида «<локальный префикс шаблона>…». Тогда в колонке «Тип» будет ✍️, а не 📝.
                                if self.find_in_templates:
                                    self.progress.emit(f'→ {new_cat_full} : "{title}" — пропущено, без изменений ({self._policy_prefix(10, DEFAULT_EN_NS.get(10, 'Template:'))}Категории)')
                                else:
                                    self.progress.emit(f'→ {new_cat_full} : "{title}" — пропущено (без изменений)')
                        except Exception:
                            pass
                    except Exception as e:
                        self.progress.emit(f"Ошибка обработки страницы {title}: {e}")
                        debug(f'Ошибка обработки страницы {title}: {e}')
                
                # Фаза 2 через backlog не используется: интерактивная обработка выполняется немедленно при обходе members_titles
                if not self.find_in_templates:
                    debug('Фаза 2 отключена')
                elif self._stop:
                    debug('Процесс остановлен')
                
                debug('Обработка категории завершена')
                try:
                    self.inner_progress_reset.emit()
                except Exception:
                    pass
            except Exception as e:
                self.progress.emit(f"Ошибка получения содержимого категории {old_cat_full}: {e}")
                debug(f'Ошибка получения содержимого категории {old_cat_full}: {e}')
                
        except Exception as e:
            self.progress.emit(f"Ошибка переноса содержимого категории: {e}")

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
            
            from ..utils import debug
            debug(f'Обработка страницы (фаза 1): {page.title()}')
            debug(f'Фаза 1 включена: {self.phase1_enabled}')
                
            original_text = page.text
            modified_text = original_text
            changes_made = 0
            
            # Фаза 1: Прямые ссылки на категорию
            if self.phase1_enabled:
                modified_text, direct_changes = self._replace_category_links_in_text(
                    modified_text, self.family, self.lang, old_cat_full, new_cat_full
                )
                changes_made += direct_changes
                debug(f'Фаза 1: {direct_changes} изменений')
            
            # Сохраняем изменения если они есть (в стиле оригинала)
            if changes_made > 0 and modified_text != original_text:
                # Оригинал использует лаконичный summary и minor=True для фазы 1
                summary = self._build_summary(old_cat_full, new_cat_full, mode='phase1')
                ok = self._save_with_retry(page, modified_text, summary, True)
                if ok:
                    try:
                        typ = self._page_kind(page)
                    except Exception:
                        typ = 'страница'
                    try:
                        self.progress.emit(f"▪️ {html.escape(new_cat_full)} : \"{html.escape(page.title())}\" — {typ} перенесена")
                    except Exception:
                        self.progress.emit(f"▪️ {new_cat_full} : \"{page.title()}\" — {typ} перенесена")
                else:
                    try:
                        self.progress.emit(f"Ошибка сохранения <b>{html.escape(page.title())}</b>")
                    except Exception:
                        self.progress.emit(f"Ошибка сохранения {page.title()}")
            
            return changes_made
            
        except Exception as e:
            self.progress.emit(f"Ошибка обработки страницы {page.title()}: {e}")
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
        from ..utils import debug
        
        if self._stop:
            return ('', 0)
            
        try:
            page = pywikibot.Page(site, title)
            if not page.exists():
                return ('', 0)
                
            debug(f'Фаза 2: обработка шаблонов на странице {title}')
            
            original_text = page.text
            modified_text = original_text
            changes_made = 0
            
            debug(f'Размер текста страницы: {len(original_text)} символов')
            
            # Сначала применяем кэшированные правила (автоматически)
            debug(f'Применяем кэшированные правила...')
            modified_text, cached_changes = self.template_manager.apply_cached_template_rules(
                modified_text, self.family, self.lang
            )
            
            if cached_changes > 0:
                debug(f'Применены кэшированные правила: {cached_changes} изменений')
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
                        typ = 'страница'
                    # Подготовим список шаблонов, изменённых кэш-правилами, для лога
                    try:
                        tmpl_names = self._extract_changed_template_labels(original_text, modified_text)
                        suffix = f" ({', '.join(tmpl_names)})" if tmpl_names else ''
                    except Exception:
                        suffix = ''
                    auto_note = 'автоматически' if cached_changes == 1 else f'автоматически ({cached_changes} изменений)'
                    self.progress.emit(f'→ {new_cat_full} : "{title}" — {typ} перенесена {auto_note}{suffix}')
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
                debug('Остановлено пользователем во время интерактивной обработки — пропускаем сохранение')
                return (original_text, 0)
            
            if interactive_changes > 0:
                debug(f'Интерактивная обработка: {interactive_changes} изменений')
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
                        typ = 'страница'
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
                        self.progress.emit(f'→ {new_cat_full} : "{title}" — {typ} перенесена{suffix}')
                    except Exception:
                        self.progress.emit(f'→ {new_cat_full} : "{title}" — перенесена')
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
                            debug('Остановлено пользователем при обработке локативов — пропускаем сохранение')
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
                                    typ = 'страница'
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
                                    self.progress.emit(f'→ {new_cat_full} : "{title}" — {typ} перенесена{suffix}')
                                except Exception:
                                    self.progress.emit(f'→ {new_cat_full} : "{title}" — перенесена')
                                original_text = modified_text2
                                modified_text = modified_text2
                            else:
                                try:
                                    self._last_template_interactions = True
                                except Exception:
                                    pass
                except Exception as _loc_e:
                    try:
                        from ..utils import debug as _dbg
                        _dbg(f'Ошибка работы эвристики локативов: {_loc_e}')
                    except Exception:
                        pass
            
            # Если никаких изменений не было сделано
            if changes_made == 0:
                debug(f'На странице {title} не найдены шаблоны для изменения')
                
        except Exception as e:
            self.progress.emit(f"Ошибка обработки шаблонов на странице {title}: {e}")
            debug(f'Ошибка обработки шаблонов на странице {title}: {e}')
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
            # 1) "…ии" → "…ия"
            if lower.endswith('ии') and len(w) > 2:
                return w[:-2] + ('ия' if w[-2:].islower() else 'ИЯ')
            # 2) "…ле" → "…ль" (как в «Неаполь» → «в Неаполе»)
            if lower.endswith('ле') and len(w) > 2:
                return w[:-1].rstrip('е') + 'ь'
            # 3) Для топонимов на -ге/-ке/-хе и перед ними гласная → -га/-ка/-ха
            #    Примеры: Риге→Рига, Праге→Прага, Гцгебехе→Гцгебеха; но Гонконге→Гонконг (не меняем, перед 'ге' стоит согласная 'н')
            vowels_set = set('аеёиоуыэюяAEIOUYАОЭИУЫЕЁЮЯ')
            if len(w) >= 3 and (lower.endswith('ге') or lower.endswith('ке') or lower.endswith('хе')):
                prev = w[-3]
                if prev in vowels_set:
                    base = w[:-2]
                    first = w[-2]
                    # Сохраняем регистр согласной
                    if lower.endswith('ге'):
                        cons = 'Г' if first.isupper() else 'г'
                    elif lower.endswith('ке'):
                        cons = 'К' if first.isupper() else 'к'
                    else:
                        cons = 'Х' if first.isupper() else 'х'
                    a_char = 'А' if w[-1].isupper() else 'а'
                    return base + cons + a_char
            # 3.1) "…ае" → "…ай" (Аксае → Аксай, Шанхае → Шанхай)
            if lower.endswith('ае') and len(w) > 2:
                return w[:-1] + ('й' if w[-1].islower() else 'Й')
            # 3.2) "…ое" → "…ой" (для прилагательных)
            if lower.endswith('ое') and len(w) > 2:
                return w[:-1] + ('й' if w[-1].islower() else 'Й')
            # 3.3) "…ве/…пе/…ре/…те/..." общее правило: «…е» → удалить «е», если перед ним согласная
            vowels = set('аеёиоуыэюяAEIOUYАОЭИУЫЕЁЮЯ')
            if lower.endswith('е') and len(w) > 1 and (w[-2] not in vowels):
                return w[:-1]
            # 4) "…и" → «…ь» для слов на мягкий знак в именительном (частично верно)
            if lower.endswith('и') and len(w) > 1 and (w[-2] not in vowels):
                return w[:-1] + 'ь'
            # 5) Падежи прилагательных (минимум): «…ой» → «…ая»
            if lower.endswith('ой') and len(w) > 2:
                return w[:-2] + 'ая'
            # 6) Безопасный фолбэк: вернуть как есть
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
                debug(f"Локативы: old_mid пустой, расширение категории. Используем последний токен старой категории.")
                # Берем последний значимый токен (обычно это географическое название)
                old_mid = self._loc_trim(old_t[-1])
                # Для новой категории берем последние N токенов (где N - количество добавленных токенов + 1)
                # ВАЖНО: не применяем _loc_trim к new_mid, чтобы сохранить скобки!
                added_tokens = len(new_t) - len(old_t)
                if added_tokens > 0:
                    new_mid = ' '.join(new_t[-(added_tokens+1):])
                else:
                    new_mid = new_t[-1]
                debug(f"Локативы: скорректированные части - old_mid='{old_mid}', new_mid='{new_mid}'")
            
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
                        match = _re2.match(r'[а-яёА-ЯЁa-zA-Z\-]+', text[i:])
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
                    debug(f"Ошибка в invert_compound_locative: {e}")
                    return self._invert_locative_form(text)
            
            inv_old = invert_compound_locative(old_mid)
            inv_new = invert_compound_locative(new_mid)
            # Диагностика: если инверсий несколько (разные эвристики) — пока просто логируем возможность неоднозначности
            try:
                from ..utils import debug as _dbg
                if inv_old != old_mid or inv_new != new_mid:
                    _dbg(f"Локативы: инверсия '{old_mid}'→'{inv_old}', '{new_mid}'→'{inv_new}'")
            except Exception:
                pass
            if not inv_old or not inv_new or inv_old == inv_new:
                return text, 0
            debug(f"Локативы: diff '{old_mid}'→'{new_mid}', инверсия '{inv_old}'→'{inv_new}'")
            
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
                debug(f"Локативы: найдено {len(templates)} шаблонов на странице")
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
                            debug(f"  Локативы: проверяем параметр {i} шаблона {template_name}: '{value_plain}' vs '{inv_old}'")
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
                            self.progress.emit(f'→ {new_cat_full} : "{page_title}" — пропущено пользователем ({tmpl_label})')
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
                            self.progress.emit(f'→ {new_cat_full} : "{page_title}" — пропущено пользователем ({tmpl_label})')
                        except Exception:
                            pass
                        # Жёсткая остановка процесса: поднимем флаг и вернём сразу
                        self._stop = True
                        try:
                            self.progress.emit("Процесс остановлен пользователем.")
                        except Exception:
                            pass
                        return modified_text, changes
                if changes > 0:
                    break
            return modified_text, changes
        except Exception as e:
            from ..utils import debug
            debug(f'Ошибка обработки локативов: {e}')
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
        from ..utils import debug
        
        # Извлекаем название категории без префикса для поиска в параметрах
        old_cat_name = old_cat_full.split(':', 1)[-1] if ':' in old_cat_full else old_cat_full
        new_cat_name = new_cat_full.split(':', 1)[-1] if ':' in new_cat_full else new_cat_full
        
        debug(f'Интерактивная обработка шаблонов для страницы: {page_title}')
        debug(f'Поиск категории "{old_cat_name}" в параметрах шаблонов')
        debug(f"Поиск шаблонов с категорией '{old_cat_name}' на странице {page_title}")
        
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
        
        debug(f'Найдено шаблонов на странице: {len(templates)}')

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
                    debug(f'Частичные пары: расширение категории (все старые токены совпадают)')
                    # Берем последний токен старой категории и расширяем его
                    # "Аксае" → "Аксае (Дагестан)"
                    old_last = tokens_old[-1] if tokens_old else ""
                    # Для новой берем последние N+1 токенов (где N - количество добавленных)
                    added = len(tokens_new) - len(tokens_old)
                    new_extended = " ".join(tokens_new[-(added+1):]) if added > 0 else tokens_new[-1]
                    if old_last and new_extended and old_last != new_extended:
                        pairs.append((old_last, new_extended))
                        debug(f'  Пара расширения: "{old_last}" → "{new_extended}"')
                    # Также добавляем пары с предлогом: "в Аксае" → "в Аксае (Дагестан)"
                    if len(tokens_old) >= 2:
                        old_with_prep = " ".join(tokens_old[-2:])
                        new_with_prep = " ".join(tokens_new[-(added+2):]) if len(tokens_new) >= added+2 else new_extended
                        if old_with_prep and new_with_prep and (old_with_prep, new_with_prep) not in pairs:
                            pairs.append((old_with_prep, new_with_prep))
                            debug(f'  Пара с предлогом: "{old_with_prep}" → "{new_with_prep}"')
                    return pairs

                # СПЕЦИАЛЬНЫЙ СЛУЧАЙ: сужение категории (новая короче старой, общий префикс совпадает)
                # Например: "Умершие в Константинополе (Византия) империя)" → "Умершие в Константинополе (Византия)"
                if diff_i >= len(tokens_new) and len(tokens_old) > len(tokens_new):
                    debug(f'Частичные пары: сужение категории (новая короче старой)')
                    removed = len(tokens_old) - len(tokens_new)
                    # Базовая пара по "хвосту": "(Византия) империя)" → "(Византия)"
                    old_tail = " ".join(tokens_old[-(removed+1):]) if removed > 0 else (tokens_old[-1] if tokens_old else "")
                    new_tail = tokens_new[-1] if tokens_new else ""
                    if old_tail and new_tail and old_tail != new_tail:
                        pairs.append((old_tail, new_tail))
                        debug(f'  Пара сужения: "{old_tail}" → "{new_tail}"')
                    # Пара с контекстом: "Константинополе (Византия) империя)" → "Константинополе (Византия)"
                    if len(tokens_old) >= (removed + 2) and len(tokens_new) >= 2:
                        old_with_ctx = " ".join(tokens_old[-(removed+2):])
                        new_with_ctx = " ".join(tokens_new[-2:])
                        if old_with_ctx and new_with_ctx and (old_with_ctx, new_with_ctx) not in pairs:
                            pairs.append((old_with_ctx, new_with_ctx))
                            debug(f'  Пара с контекстом: "{old_with_ctx}" → "{new_with_ctx}"')
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
        debug(f'Сгенерировано пар для частичной замены: {len(partial_pairs)}')
        for idx, (old_p, new_p) in enumerate(partial_pairs, 1):
            debug(f'  Пара {idx}: "{old_p}" → "{new_p}"')
        
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
                    debug(f'Шаблон {template_name}: отмечен на автопропуск — пропускаем без диалога')
                    # Лог в стиле оригинала (пропуск автоматически)
                    try:
                        tmpl_label = self._format_template_label(template_name)
                    except Exception:
                        tmpl_label = f"{DEFAULT_EN_NS.get(10, 'Template:')}{template_name}"
                    try:
                        self.progress.emit(f'→ {new_cat_full} : "{page_title}" — пропущено автоматически ({tmpl_label})')
                    except Exception:
                        self.progress.emit(f'→ {new_cat_full} : "{page_title}" — пропущено автоматически ({tmpl_label})')
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
                def _append_match(old_val: str, new_val: str):
                    nonlocal matched_this_param
                    found_matches.append({
                        'type': 'direct',
                        'param_index': i,
                        'param_value': param_clean,
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
                                        debug(f'    Пропускаем (строгое равенство): значение "{value_plain}" уже содержит новую подстроку "{new_sub}"')
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
                                        debug(f'    Пропускаем: значение "{value_plain}" уже содержит новую подстроку "{new_sub}"')
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
                    
                debug(f'Найдено совпадение в шаблоне {template_name}: {match_info["param_value"]}')
                
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
                            debug(f'Автоприменено по правилу (auto=approve) для шаблона {template_name}')
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
                    debug(f'Результат диалога подтверждения: {action}')
                    
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
                        debug(f'Применено изменение в шаблоне {template_name}')
                        # (лог названия шаблона не требуется, ярлык уже формируется при необходимости в других местах)
                        break  # Переходим к следующему шаблону
                    elif action == 'skip':
                        # Отметить шаблон на автопропуск, если запрошено
                        try:
                            if result.get('auto_skip'):
                                self.template_manager.set_template_skip_flag(template_name, self.family, self.lang, True)
                        except Exception:
                            pass
                        debug(f'Пропущено изменение в шаблоне {template_name}')
                        # Лог в стиле оригинала (пропуск пользователем)
                        try:
                            tmpl_label = self._format_template_label(template_name, False)
                            src_label = tmpl_label
                            self.progress.emit(f'→ {new_cat_full} : "{page_title}" — пропущено пользователем ({src_label})')
                        except Exception:
                            self.progress.emit(f'→ {new_cat_full} : "{page_title}" — пропущено пользователем ({DEFAULT_EN_NS.get(10, 'Template:')}{template_name})')
                        # Отметим, что было взаимодействие (диалог) — чтобы не выводить общий «пропущено, без изменений»
                        try:
                            self._last_template_interactions = True
                        except Exception:
                            pass
                        continue
                    elif action == 'cancel':
                        debug(f'Пользователь отменил процесс')
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
                            self.progress.emit(f'→ {new_cat_full} : "{page_title}" — пропущено пользователем ({tmpl_label})')
                        except Exception:
                            pass
                        self._stop = True
                        self.progress.emit("Процесс остановлен пользователем.")
                        return modified_text, changes
                    else:
                        debug(f'Неизвестное действие: {action}')
                        continue
                        
                except Exception as e:
                    debug(f'Ошибка при запросе подтверждения: {e}')
                    continue
        
        debug(f'Интерактивная обработка завершена: {changes} изменений')
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
            from ..utils import debug
            debug(f'Ошибка в _request_template_confirmation: {e}')
            return {'action': 'skip'}
