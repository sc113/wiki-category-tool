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
                 override_comment: str = '', title_regex: str = ''):
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
                    'dedupe_mode': response_data.get('dedupe_mode', 'keep_both')
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
                    
                    # Если отключено переименование категории — пропускаем сам move для категорий
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
                self.progress.emit(f"Страница <b>{html.escape(old_name)}</b> не найдена.")
                return
            if new_page.exists():
                self.progress.emit(f"Страница назначения <b>{html.escape(new_name)}</b> уже существует.")
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
                from ..core.api_client import REQUEST_SESSION, _rate_wait
                from ..constants import REQUEST_HEADERS
                import requests
                import urllib.parse
                
                api_url = f"https://{self.lang}.{self.family}.org/w/api.php"
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
                    self.progress.emit(f"ℹ️ Перенос содержимого категории <b>{html.escape(old_cat_full)}</b> → <b>{html.escape(new_cat_full)}</b>: {format_russian_pages_nominative(len(members_titles))}")
                except Exception:
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
                            if changes_made == 0 and (not self.find_in_templates or phase2_changes == 0):
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
                    # Помечаем частичные правки в подписи, если были
                    try:
                        if getattr(self, '_last_template_change_was_partial', False) and labels:
                            labels = [f"{x} [частично]" for x in labels]
                    except Exception:
                        pass
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
                else:
                    self.progress.emit(f"Не удалось обновить: <b>{html.escape(title)}</b>")
                    
                # Обновляем текст для дальнейшей обработки
                original_text = modified_text
            
            # Интерактивная обработка шаблонов с диалогами подтверждения
            modified_text, interactive_changes = self._process_templates_interactive(
                modified_text, old_cat_full, new_cat_full, title
            )
            
            if interactive_changes > 0:
                debug(f'Интерактивная обработка: {interactive_changes} изменений')
                changes_made += interactive_changes
                
                # Сохраняем изменения от интерактивной обработки (как в оригинале)
                # Интерактивные изменения в шаблонах — добавим конкретные шаблоны в summary
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
                    try:
                        tmpl_names = self._extract_changed_template_labels(original_text, modified_text)
                        try:
                            if getattr(self, '_last_template_change_was_partial', False) and tmpl_names:
                                tmpl_names = [f"{x} [частично]" for x in tmpl_names]
                        except Exception:
                            pass
                        suffix = f" ({', '.join(tmpl_names)})" if tmpl_names else ''
                        self.progress.emit(f'→ {new_cat_full} : "{title}" — {typ} перенесена{suffix}')
                    except Exception:
                        self.progress.emit(f'→ {new_cat_full} : "{title}" — перенесена')
                else:
                    try:
                        self.progress.emit(f"Ошибка сохранения <b>{html.escape(title)}</b>")
                    except Exception:
                        self.progress.emit(f"Ошибка сохранения {title}")
            
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
        rx = re.compile(r"\[\[\s*(?P<prefix>(" + alt_pat + r"))\s*:\s*" + re.escape(old_cat_name) + r"\s*(?:\|\s*(?P<sort>[^\]]*?))?\s*\]\]", re.IGNORECASE)

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
                # Токенизируем по пробелам и распространённым разделителям
                tokens_old = re.split(r"[\s:\-–—]+", old_s)
                tokens_new = re.split(r"[\s:\-–—]+", new_s)
                # Индекс первого различия по токенам
                diff_i = 0
                L = min(len(tokens_old), len(tokens_new))
                while diff_i < L and tokens_old[diff_i] == tokens_new[diff_i]:
                    diff_i += 1
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

                # Если прямых совпадений не найдено — пробуем частичные пары
                if not matched_this_param and partial_pairs:
                    try:
                        for old_sub, new_sub in partial_pairs:
                            old_sub = (old_sub or '').strip()
                            new_sub = (new_sub or '').strip()
                            if not old_sub or not new_sub:
                                continue
                            old_sub_enc = html.escape(old_sub, quote=True)
                            # Проверяем строгое равенство значению параметра (с учётом кавычек/экранирования)
                            if value_plain == old_sub or value_norm == old_sub or value_plain == old_sub_enc or value_norm == old_sub_enc:
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
                
                # Создаем предложение замены
                # Если в исходном значении первая буква была строчной и это позиционный/именованный
                # параметр-значение категории, повышаем регистр первой буквы в новом значении
                is_partial = (match_info.get('type') != 'direct')
                old_val = match_info.get('old_value') if not is_partial else (match_info.get('old_sub') or '')
                new_val = match_info.get('new_value') if not is_partial else (match_info.get('new_sub') or '')
                try:
                    if old_val and new_val and old_val[:1].islower():
                        new_val = new_val[:1].upper() + new_val[1:]
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
                        dedupe_mode = str(result.get('dedupe_mode', 'keep_both') or 'keep_both')
                        try:
                            # Обратная совместимость: keep_first/keep_second → left/right
                            if dedupe_mode == 'keep_first':
                                dedupe_mode = 'left'
                            elif dedupe_mode == 'keep_second':
                                dedupe_mode = 'right'
                            if dup_warning and dedupe_mode in ('left', 'right'):
                                # Удаляем дубликаты нового значения среди позиционных параметров
                                inner2 = final_template[2:-2]
                                parts2 = inner2.split('|') if inner2 else []
                                def _norm_val2(s: str) -> str:
                                    try:
                                        return (s or '').strip().strip('\"\'')
                                    except Exception:
                                        return (s or '').strip()
                                target_val = _norm_val2(new_val)
                                pos_list = []
                                for k, tok in enumerate(parts2[1:], 1):
                                    if '=' in tok:
                                        continue
                                    if _norm_val2(tok) == target_val and target_val != '':
                                        pos_list.append(k)
                                if len(pos_list) >= 2:
                                    if dedupe_mode == 'left':
                                        keep = pos_list[0]
                                    else:
                                        keep = pos_list[-1]
                                    to_remove = {p for p in pos_list if p != keep}
                                    rebuilt = [parts2[0]] + [tok for idx, tok in enumerate(parts2[1:], 1) if idx not in to_remove]
                                    final_template = '{{' + '|'.join(rebuilt) + '}}'
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
                            tmpl_label = self._format_template_label(template_name, is_partial)
                            self.progress.emit(f'→ {new_cat_full} : "{page_title}" — пропущено пользователем ({tmpl_label})')
                        except Exception:
                            self.progress.emit(f'→ {new_cat_full} : "{page_title}" — пропущено пользователем ({DEFAULT_EN_NS.get(10, 'Template:')}{template_name}{" [частично]" if is_partial else ""})')
                        continue
                    elif action == 'cancel':
                        debug(f'Пользователь отменил процесс')
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
                                     dup_warning: bool = False, dup_idx1: int = 0, dup_idx2: int = 0) -> dict:
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