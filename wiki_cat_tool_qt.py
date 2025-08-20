import sys
import os
import io
import csv
import json
import requests
import time
import urllib.parse
import webbrowser
import re
import ast
import ctypes
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import Lock

# Инициализация окружения для Pywikibot до его импорта (важно для сборки exe)
# 1) Отключаем требование наличия user-config.py при импорте
os.environ.setdefault('PYWIKIBOT_NO_USER_CONFIG', '1')
# 2) Указываем базовую директорию для конфигов рядом с exe/скриптом в подпапке configs
_startup_base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
_startup_cfg = os.path.join(_startup_base, 'configs')
os.environ['PYWIKIBOT_DIR'] = _startup_cfg
try:
    os.makedirs(_startup_cfg, exist_ok=True)
except Exception:
    pass

# В GUI-сборке у PyInstaller sys.stdout/sys.stderr могут быть None → print() падает.
# Подменяем на безопасный вывод в debug-лог.
class _GuiStdWriter(io.TextIOBase):
    def write(self, s: str) -> int:
        try:
            if s and s.strip():
                # импорт debug позже; используем ленивый вызов через globals
                dbg = globals().get('debug')
                if callable(dbg):
                    dbg(str(s).rstrip())
        except Exception:
            pass
        return len(s or '')
    def flush(self) -> None:
        pass

if getattr(sys, 'stdout', None) is None:
    sys.stdout = _GuiStdWriter()
if getattr(sys, 'stderr', None) is None:
    sys.stderr = _GuiStdWriter()

from PySide6.QtCore import Qt, QThread, Signal, QUrl, QEvent, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QLineEdit, QPushButton,
    QFileDialog, QTextEdit, QTabWidget, QVBoxLayout, QHBoxLayout, QRadioButton,
    QButtonGroup, QProgressBar, QMessageBox, QToolButton, QComboBox, QCheckBox,
    QSizePolicy, QDialog, QPlainTextEdit
)
from PySide6.QtGui import QDesktopServices

# Гарантируем, что Pywikibot использует конфиги из этой подпапки
def tool_base_dir() -> str:
    try:
        return os.path.dirname(__file__)
    except NameError:
        return os.getcwd()

os.environ['PYWIKIBOT_DIR'] = tool_base_dir()

import pywikibot
from pywikibot import config as pwb_config
from pywikibot.login import LoginManager
from pywikibot.comms import http as pywb_http

# hook pywikibot output to debug
def _pywb_log(msg, *a, **kw):
    debug('PYWIKIBOT: ' + str(msg))
pywikibot.output = _pywb_log
pywikibot.warning = _pywb_log
pywikibot.error = _pywb_log

write_lock = Lock()

# ---------- assets helper ---------- #
def asset_path(name: str) -> str:
    base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
    return os.path.join(base, name)

# ---------- HTTP session + rate limit (to reduce 429) ---------- #
REQUEST_SESSION = requests.Session()
REQUEST_HEADERS = {
    'User-Agent': 'WikiCatTool/1.0 (+github:local; email:none) requests',
    'Accept': 'application/json'
}
_RATE_LOCK = Lock()
_LAST_REQ_TS = 0.0
_MIN_INTERVAL = 0.12  # seconds between requests across all threads

def _rate_wait():
    global _LAST_REQ_TS
    with _RATE_LOCK:
        now = time.time()
        wait = max(0.0, (_LAST_REQ_TS + _MIN_INTERVAL) - now)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQ_TS = time.time()

def _rate_backoff(seconds: float | None = None):
    global _MIN_INTERVAL
    with _RATE_LOCK:
        add = float(seconds) if seconds is not None else 0.0
        # экспоненциальный рост с верхним пределом
        _MIN_INTERVAL = min(2.5, max(_MIN_INTERVAL * 1.5, add if add > 0 else _MIN_INTERVAL))
        debug(f"Rate backoff: MIN_INTERVAL={_MIN_INTERVAL:.2f}s")

# ---------- debug buffer ---------- #
DEBUG_BUFFER = []
DEBUG_VIEW = None  # will hold QTextEdit of the debug window

# Глобальный флаг для обхода проверки AWB (секретный режим)
BYPASS_AWB = False
BYPASS_TOKEN: str | None = None

def set_bypass_awb(flag: bool) -> None:
    global BYPASS_AWB
    BYPASS_AWB = bool(flag)
    debug(f"Bypass AWB set to {BYPASS_AWB}")

def is_bypass_awb() -> bool:
    return bool(BYPASS_AWB)

# Файл-ключ для обхода AWB в папке configs
def _bypass_key_path() -> str:
    return os.path.join(_dist_configs_dir(), 'awb_bypass.key')

def try_load_bypass_awb_from_file() -> bool:
    """Считывает токен обхода AWB из файла configs/awb_bypass.key (строка, без кавычек).

    Токен используется для разблокировки при вводе в окне Debug. Сам по себе файл
    обход не включает. Возвращает True, если токен успешно загружен и не пуст.
    """
    global BYPASS_TOKEN
    try:
        p = _bypass_key_path()
        if os.path.isfile(p):
            with open(p, 'r', encoding='utf-8') as f:
                token = (f.read() or '').strip().lower()
            BYPASS_TOKEN = token if token else None
            debug('Bypass token loaded from file' if BYPASS_TOKEN else 'Bypass token file is empty')
            return bool(BYPASS_TOKEN)
    except Exception:
        BYPASS_TOKEN = None
    return False

def try_load_bypass_awb_from_embedded() -> bool:
    """Пытается загрузить токен обхода AWB из встроенных источников:
    1) переменная окружения BYPASS_AWB_TOKEN
    2) необязательный модуль wikicat_tool._embedded_secrets с атрибутом BYPASS_AWB_TOKEN
    Возвращает True, если токен найден.
    """
    global BYPASS_TOKEN
    try:
        token = (os.environ.get('BYPASS_AWB_TOKEN', '') or '').strip().lower()
        if not token:
            # сначала пробуем модуль рядом со скриптом: _embedded_secrets.py
            try:
                import _embedded_secrets as _sec  # type: ignore
                token = (getattr(_sec, 'BYPASS_AWB_TOKEN', '') or '').strip().lower()
            except Exception:
                # затем пробуем как подпакет (если есть __init__.py)
                try:
                    from wikicat_tool import _embedded_secrets as _sec  # type: ignore
                    token = (getattr(_sec, 'BYPASS_AWB_TOKEN', '') or '').strip().lower()
                except Exception:
                    token = ''
        if token:
            BYPASS_TOKEN = token
            debug('Bypass token loaded from embedded source')
            return True
    except Exception:
        pass
    return False

def maybe_auto_activate_bypass(ui: 'MainWindow') -> None:
    """Если задана переменная окружения BYPASS_AWB_AUTO='1' или в модуле
    wikicat_tool._embedded_secrets установлен флаг AUTO_ACTIVATE=True, включает обход сразу."""
    try:
        auto_env = (os.environ.get('BYPASS_AWB_AUTO', '') or '').strip()
        auto = auto_env in {'1', 'true', 'yes'}
        if not auto:
            try:
                import _embedded_secrets as _sec  # type: ignore
                auto = bool(getattr(_sec, 'AUTO_ACTIVATE', False))
            except Exception:
                try:
                    from wikicat_tool import _embedded_secrets as _sec  # type: ignore
                    auto = bool(getattr(_sec, 'AUTO_ACTIVATE', False))
                except Exception:
                    auto = False
        if auto and not is_bypass_awb():
            set_bypass_awb(True)
            try:
                ui._set_awb_ui(True)
            except Exception:
                pass
            debug('Bypass AWB auto-activated')
    except Exception:
        pass

def debug(msg: str):
    ts = datetime.now().strftime('%H:%M:%S')
    DEBUG_BUFFER.append(f"[{ts}] {msg}")
    if DEBUG_VIEW is not None:
        DEBUG_VIEW.appendPlainText(DEBUG_BUFFER[-1])

# ---------- default edit summary ---------- #

def default_summary(lang: str) -> str:
    mapping = {
        'ru': 'Замена содержимого страницы на единообразное наполнение',
        'uk': 'Заміна вмісту сторінки на одноманітне наповнення',
        'be': 'Замена зместу старонкі на адзіную структуру',
        'en': 'Replacement of the page content with uniform filling',
        'fr': 'Remplacement du contenu pour cohérence',
        'es': 'Sustitución del contenido para uniformidad',
        'de': 'Ersetzung des Seiteninhalts für Konsistenz'
    }
    return mapping.get(lang, 'Consistency content replacement')

# ---------- default creation summary ---------- #
def default_create_summary(lang: str) -> str:
    mapping = {
        'ru': 'Создание новой категории с заготовленным содержимым',
        'uk': 'Створення нової категорії з уніфікованим наповненням',
        'be': 'Стварэнне новай катэгорыі з адзінай структурай',
        'en': 'Creation of a new category with prepared content',
        'fr': 'Création d\'une nouvelle catégorie avec contenu préparé',
        'es': 'Creación de una nueva categoría con contenido preparado',
        'de': 'Erstellung einer neuen Kategorie mit vorbereitetem Inhalt'
    }
    return mapping.get(lang, 'Category creation with prepared content')

# путь к файлу с авторизацией рядом с exe/скриптом
def cred_path():
    # Больше не используем credentials.txt; функция оставлена для совместимости, но не вызывается
    base = tool_base_dir()
    return os.path.join(base, 'credentials.txt')

# ---- pywikibot config helpers (single shared config) ---- #
def _dist_configs_dir() -> str:
    """Фактическая папка configs рядом с exe/скриптом (для записи файлов)."""
    base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else tool_base_dir()
    return os.path.join(base, 'configs')

def config_base_dir() -> str:
    # Используем env, если задана корректная папка; иначе — реальную папку рядом с exe
    cfg = os.environ.get('PYWIKIBOT_DIR')
    if cfg and os.path.isabs(cfg):
        return cfg
    return _dist_configs_dir()

def write_pwb_credentials(lang: str, username: str, password: str) -> None:
    cfg_dir = _dist_configs_dir()
    os.makedirs(cfg_dir, exist_ok=True)
    uc_path = os.path.join(cfg_dir, 'user-config.py')
    # Parse existing usernames mapping
    usernames_map: dict[str, str] = {}
    if os.path.isfile(uc_path):
        try:
            with open(uc_path, 'r', encoding='utf-8') as f:
                txt = f.read()
            for m in re.finditer(r"usernames\['wikipedia'\]\['([^']+)'\]\s*=\s*'([^']+)'", txt):
                usernames_map[m.group(1)] = m.group(2)
        except Exception:
            usernames_map = {}
    # Update or insert current language
    usernames_map[lang] = username
    # Build content in fixed order
    lines = [
        "family = 'wikipedia'",
        f"mylang = '{lang}'",
        "password_file = 'user-password.py'",
    ]
    for code in sorted(usernames_map.keys()):
        lines.append(f"usernames['wikipedia']['{code}'] = '{usernames_map[code]}'")
    debug(f"Write user-config.py → {uc_path}")
    with open(uc_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines) + "\n")
    # Write password file for the active account
    up_path = os.path.join(cfg_dir, 'user-password.py')
    debug(f"Write user-password.py → {up_path}")
    with open(up_path, 'w', encoding='utf-8') as f:
        f.write(repr((username, password)))

def apply_pwb_config(lang: str) -> str:
    cfg_dir = _dist_configs_dir()
    os.makedirs(cfg_dir, exist_ok=True)
    # ensure throttle control file exists to satisfy pywikibot throttling subsystem
    try:
        throttle_path = os.path.join(cfg_dir, 'throttle.ctrl')
        if not os.path.isfile(throttle_path):
            with open(throttle_path, 'w', encoding='utf-8') as _f:
                _f.write('')
    except Exception:
        pass
    os.environ['PYWIKIBOT_DIR'] = cfg_dir
    pwb_config.base_dir = cfg_dir
    pwb_config.family = 'wikipedia'
    pwb_config.mylang = lang
    pwb_config.password_file = os.path.join(cfg_dir, 'user-password.py')
    return cfg_dir

def cookies_exist(cfg_dir: str, username: str) -> bool:
    for cookie_name in (f'pywikibot-{username}.lwp', 'pywikibot.lwp'):
        if os.path.isfile(os.path.join(cfg_dir, cookie_name)):
            return True
    return False

def _normalize_username(name: str | None) -> str:
    if not name:
        return ''
    return name.strip().replace('_', ' ').casefold()

def _delete_all_cookies(cfg_dir: str, username: str | None = None) -> None:
    try:
        for name in os.listdir(cfg_dir):
            if name == 'pywikibot.lwp' or (name.startswith('pywikibot-') and name.endswith('.lwp')):
                try:
                    os.remove(os.path.join(cfg_dir, name))
                    debug(f"Удалён cookie: {name}")
                except Exception as e:
                    debug(f"Не удалось удалить cookie {name}: {e}")
    except Exception:
        pass

def verify_credentials_clientlogin(lang: str, username: str, password: str) -> tuple[str, str]:
    """Return ('pass'|'fail'|'unknown', message). Uses MediaWiki clientlogin.

    We treat explicit wrong username/password as 'fail'. Other statuses are 'unknown'.
    """
    api = f"https://{lang}.wikipedia.org/w/api.php"
    sess = requests.Session()
    try:
        r1 = sess.get(api, params={
            'action': 'query', 'meta': 'tokens', 'type': 'login', 'format': 'json'
        }, timeout=15)
        token = r1.json().get('query', {}).get('tokens', {}).get('logintoken')
        if not token:
            return 'unknown', 'Не удалось получить login-token'
        r2 = sess.post(api, data={
            'action': 'clientlogin', 'username': username, 'password': password,
            'loginreturnurl': 'https://example.com/', 'logintoken': token, 'format': 'json'
        }, timeout=15)
        cl = r2.json().get('clientlogin', {})
        status = cl.get('status')
        code = cl.get('messagecode', '')
        if status == 'PASS':
            return 'pass', 'OK'
        if status == 'FAIL' and code in {'wrongpassword', 'nosuchuser'}:
            return 'fail', cl.get('message', code)
        return 'unknown', cl.get('message', status or 'UNKNOWN')
    except Exception as e:
        return 'unknown', str(e)

def fetch_awb_lists(lang: str, timeout: int = 15) -> tuple[str, dict | None]:
    """Загружает JSON со страницы Wikipedia:AutoWikiBrowser/CheckPageJSON для данного языка.

    Возвращает кортеж (state, data):
      - state: 'ok' | 'missing' | 'error'
      - data: словарь с ключами 'enabledusers'/'enabledbots' при state='ok', иначе None
    """
    url = f"https://{lang}.wikipedia.org/wiki/Wikipedia:AutoWikiBrowser/CheckPageJSON"
    params = {'action': 'raw'}
    try:
        _rate_wait()
        r = REQUEST_SESSION.get(url, params=params, timeout=timeout, headers=REQUEST_HEADERS)
        if r.status_code == 404:
            debug(f"AWB CheckPage missing lang={lang}")
            return 'missing', None
        if r.status_code != 200:
            debug(f"AWB CheckPage fetch HTTP {r.status_code} lang={lang}")
            return 'error', None
        txt = (r.text or '').strip()
        try:
            data = json.loads(txt)
        except Exception as e:
            debug(f"AWB CheckPage JSON parse error: {e}")
            return 'error', None
        if not isinstance(data, dict):
            debug("AWB CheckPage content is not a JSON object")
            return 'error', None
        users = data.get('enabledusers') or []
        bots = data.get('enabledbots') or []
        if not isinstance(users, list) or not isinstance(bots, list):
            debug("AWB CheckPage lists have unexpected types")
            return 'error', None
        debug(f"AWB lists loaded: users={len(users)} bots={len(bots)} lang={lang}")
        return 'ok', {'enabledusers': users, 'enabledbots': bots}
    except Exception as e:
        debug(f"AWB CheckPage fetch error: {e}")
        return 'error', None

def is_user_awb_enabled(lang: str, username: str) -> tuple[bool, str]:
    """Проверяет, есть ли пользователь в списках AWB (люди или боты) на локальной вики.

    Возвращает (True, 'OK') если найден, иначе (False, сообщение об ошибке).
    """
    state, lists = fetch_awb_lists(lang)
    page_url = f"https://{lang}.wikipedia.org/wiki/Wikipedia:AutoWikiBrowser/CheckPageJSON"
    if state == 'missing':
        # Страница отсутствует на этой вики — разрешаем работу без ограничений
        return True, 'AWB CheckPage отсутствует на этой вики; доступ разрешён.'
    if state != 'ok' or not lists:
        return False, ("Не удалось загрузить список доступа AWB. Проверьте подключение или откройте страницу: "
                       f"{page_url}")
    target = _normalize_username((username or '').replace('_', ' '))
    enabled_users = { _normalize_username(n) for n in lists.get('enabledusers', []) if isinstance(n, str) }
    enabled_bots  = { _normalize_username(n) for n in lists.get('enabledbots', []) if isinstance(n, str) }
    if target in enabled_users or target in enabled_bots:
        return True, 'OK'
    return False, ("Учётная запись не включена в списки AWB (enabledusers/enabledbots) на этой вики. "
                   f"Проверьте страницу: {page_url}")

def reset_pywikibot_session(lang: str | None = None) -> None:
    try:
        pywb_http.session_reset()
    except Exception:
        pass
    try:
        sites = getattr(pywikibot, '_sites', None)
        if isinstance(sites, dict):
            if lang is None:
                sites.clear()
            else:
                # удалить все сайты выбранного языка в текущем семействе
                fam = getattr(pwb_config, 'family', 'wikipedia')
                for k in list(sites.keys()):
                    try:
                        if isinstance(k, tuple):
                            f, l = k
                            if f == fam and l == lang:
                                sites.pop(k, None)
                    except Exception:
                        pass
    except Exception:
        pass

# -------------------- backend helpers -------------------- #

def fetch_content(title: str, content_type: str, lang: str = 'ru', retries: int = 5, timeout: int = 6):
    debug(f"API GET content lang={lang} title={title}")
    url = f"https://{lang}.wikipedia.org/w/api.php"
    # допустимые префиксы для категории/шаблона на разных языках
    pref_map = {
        "2": (
            "Category:", "Категория:", "Категорія:", "Катэгорыя:",
            "Catégorie:", "Categoría:", "Kategorie:"
        ),
        "3": (
            "Template:", "Шаблон:",
            "Modèle:", "Plantilla:", "Vorlage:"
        )
    }

    if content_type in ("2", "3"):
        prefixes = pref_map[content_type]
        if any(title.lower().startswith(p.lower()) for p in prefixes):
            full = title
        else:
            full = prefixes[0] + title  # английский префикс по умолчанию
    else:
        full = title  # статьи без префикса
    params = {"action": "query", "prop": "revisions", "rvprop": "content", "titles": full, "format": "json"}
    for attempt in range(1, retries+1):
        try:
            _rate_wait()
            r = REQUEST_SESSION.get(url, params=params, timeout=timeout, headers=REQUEST_HEADERS)
            r.encoding = 'utf-8'
            if r.status_code == 429:
                debug(f"API ERR 429 (rate limit) for {title}; attempt {attempt}/{retries}")
                if attempt < retries:
                    _rate_backoff(0.6 * attempt)
                    continue
                return []
            if r.status_code != 200:
                debug(f"API ERR {r.status_code} for {title}")
                return []
            pages = r.json().get("query", {}).get("pages", {})
            for p in pages.values():
                if "missing" in p:
                    return []
                text = p.get("revisions", [{}])[0].get("*", "")
                return text.split("\n") if text else []
        except requests.exceptions.Timeout:
            if attempt < retries:
                time.sleep(2 ** attempt)
    return []

def write_row(title, lines, writer):
    if not lines:
        return
    with write_lock:
        writer.writerow([title, *lines])

# -------------------- worker threads -------------------- #

class ParseWorker(QThread):
    progress = Signal(str)

    def __init__(self, titles, out_path, ctype, lang):
        super().__init__()
        self.titles = titles
        self.out_path = out_path
        self.ctype = ctype
        self.lang = lang
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        with open(self.out_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f, delimiter='\t')
            max_workers = 8
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                in_flight = set()
                for title in self.titles:
                    if self._stop:
                        break
                    # ограничиваем количество одновременных задач
                    while len(in_flight) >= max_workers:
                        done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                        for fut in done:
                            try:
                                fut.result()
                            except Exception:
                                pass
                        if self._stop:
                            break
                    if self._stop:
                        break
                    in_flight.add(pool.submit(self.process, title, writer))

                # дождаться завершения оставшихся задач (их не больше max_workers)
                for fut in in_flight:
                    try:
                        fut.result()
                    except Exception:
                        pass
        # завершение потока вызовет встроенный сигнал QThread.finished автоматически

    def process(self, title, writer):
        if self._stop:
            return
        lines = fetch_content(title, self.ctype, lang=self.lang)
        if self._stop:
            return
        write_row(title, lines, writer)
        if self._stop:
            return
        if lines:
            self.progress.emit(f"{title}: {len(lines)} строк")
        else:
            self.progress.emit(f"{title}: не найдено")

class ReplaceWorker(QThread):
    progress = Signal(str)

    def __init__(self, tsv_path, username, password, lang, summary, minor: bool):
        super().__init__()
        self.tsv_path = tsv_path
        self.username = username
        self.password = password
        self.lang = lang
        self.summary = summary
        self.minor = minor
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        site = pywikibot.Site(self.lang, 'wikipedia')
        debug(f'Login attempt replace lang={self.lang}')
        if self.username and self.password:
            try:
                if not is_bypass_awb():
                    ok_awb, msg_awb = is_user_awb_enabled(self.lang, self.username)
                    if not ok_awb:
                        self.progress.emit(f"AWB доступ отсутствует: {msg_awb}")
                        return
                # Конфиг и пароль уже сохраняются во вкладке авторизации
                site.login(user=self.username)
            except Exception as e:
                self.progress.emit(f"Ошибка авторизации: {type(e).__name__}: {e}")
                return
        # Читаем как utf-8-sig, чтобы убрать BOM у первой ячейки
        with open(self.tsv_path, newline='', encoding='utf-8-sig') as f:
            reader = csv.reader(f, delimiter='\t')
            for row in reader:
                if self._stop:
                    break
                if len(row) < 2:
                    continue
                # Нормализуем заголовок и строки: убираем пробелы и возможный BOM
                raw_title = row[0] if row[0] is not None else ''
                title = raw_title.strip().lstrip('\ufeff')
                lines = [(s or '').lstrip('\ufeff') for s in row[1:]]
                # если пользователь уже указал префикс, не добавляем второй раз
                cat_prefixes = (
                    "category:", "категория:", "категорія:", "катэгорыя:",
                    "catégorie:", "categoría:", "kategorie:"
                )
                if any(title.lower().startswith(p) for p in cat_prefixes):
                    page = pywikibot.Page(site, title)
                else:
                    page = pywikibot.Page(site, f"Категория:{title}")
                if page.exists():
                    page.text = "\n".join(lines)
                    page.save(summary=self.summary, minor=self.minor)
                    self.progress.emit(f"{title}: записано {len(lines)} строк")
                else:
                    self.progress.emit(f"{title}: страница отсутствует")
        # завершение потока вызовет встроенный сигнал QThread.finished автоматически

class CreateWorker(QThread):
    progress = Signal(str)

    def __init__(self, tsv_path, username, password, lang, summary, minor: bool):
        super().__init__()
        self.tsv_path = tsv_path
        self.username = username
        self.password = password
        self.lang = lang
        self.summary = summary
        self.minor = minor
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        site = pywikibot.Site(self.lang, 'wikipedia')
        debug(f'Login attempt create lang={self.lang}')
        if self.username and self.password:
            try:
                if not is_bypass_awb():
                    ok_awb, msg_awb = is_user_awb_enabled(self.lang, self.username)
                    if not ok_awb:
                        self.progress.emit(f"AWB доступ отсутствует: {msg_awb}")
                        return
                site.login(user=self.username)
            except Exception as e:
                self.progress.emit(f"Ошибка авторизации: {type(e).__name__}: {e}")
                return
        try:
            # Читаем как utf-8-sig и снимаем BOM, чтобы первая строка не получала двойной префикс
            with open(self.tsv_path, newline='', encoding='utf-8-sig') as f:
                reader = csv.reader(f, delimiter='\t')
                for row in reader:
                    if self._stop:
                        break
                    if not row:
                        continue
                    raw_title = row[0] if row[0] is not None else ''
                    title = raw_title.strip().lstrip('\ufeff')
                    lines = [((s or '').lstrip('\ufeff')) for s in row[1:]]
                    cat_prefixes = (
                        'category:', 'категория:', 'категорія:', 'катэгорыя:',
                        'catégorie:', 'categoría:', 'kategorie:'
                    )
                    if any(title.lower().startswith(p) for p in cat_prefixes):
                        page = pywikibot.Page(site, title)
                    else:
                        page = pywikibot.Page(site, f"Категория:{title}")
                    if not page.exists():
                        page.text = "\n".join(lines)
                        page.save(summary=self.summary, minor=self.minor)
                        self.progress.emit(f"{title}: создано ({len(lines)} строк)")
                    else:
                        self.progress.emit(f"{title}: уже существует")
        except Exception as e:
            self.progress.emit(f"Ошибка: {e}")
        # завершение потока вызовет встроенный сигнал QThread.finished автоматически

class RenameWorker(QThread):
    progress = Signal(str)

    def __init__(self, tsv_path, username, password, lang, leave_cat_redirect: bool, leave_other_redirect: bool):
        super().__init__()
        self.tsv_path = tsv_path
        self.username = username
        self.password = password
        self.lang = lang
        self.leave_cat_redirect = leave_cat_redirect
        self.leave_other_redirect = leave_other_redirect
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        site = pywikibot.Site(self.lang, 'wikipedia')
        debug(f'Login attempt rename lang={self.lang}')
        # Авторизация, если указаны логин/пароль
        if self.username and self.password:
            try:
                if not is_bypass_awb():
                    ok_awb, msg_awb = is_user_awb_enabled(self.lang, self.username)
                    if not ok_awb:
                        self.progress.emit(f"AWB доступ отсутствует: {msg_awb}")
                        return
                site.login(user=self.username)
            except Exception as e:
                self.progress.emit(f"Ошибка авторизации: {type(e).__name__}: {e}")
                return
        try:
            import csv
            # Читаем как utf-8-sig и очищаем BOM/пробелы
            with open(self.tsv_path, newline='', encoding='utf-8-sig') as f:
                reader = csv.reader(f, delimiter='\t')
                for row in reader:
                    if self._stop:
                        break
                    if len(row) < 3:
                        self.progress.emit(f"Некорректная строка (требуется 3 столбца): {row}")
                        continue
                    old_name, new_name, reason = [((c or '').strip().lstrip('\ufeff')) for c in row[:3]]
                    is_category = old_name.lower().startswith(("категория:", "category:"))
                    leave_redirect = self.leave_cat_redirect if is_category else self.leave_other_redirect
                    self._move_page(site, old_name, new_name, reason, leave_redirect)
        except Exception as e:
            self.progress.emit(f"Ошибка работы с файлом TSV: {e}")
        finally:
            # завершение потока вызовет встроенный сигнал QThread.finished автоматически
            pass

    def _move_page(self, site: pywikibot.Site, old_name: str, new_name: str, reason: str, leave_redirect: bool):
        try:
            page = pywikibot.Page(site, old_name)
            new_page = pywikibot.Page(site, new_name)
            if not page.exists():
                self.progress.emit(f"Страница '{old_name}' не найдена.")
                return
            if new_page.exists():
                self.progress.emit(f"Страница назначения '{new_name}' уже существует.")
                return
            # noredirect=True означает НЕ оставлять редирект
            page.move(new_name, reason=reason, movetalk=True, noredirect=not leave_redirect)
            redir_status = "с редиректом" if leave_redirect else "без редиректа"
            self.progress.emit(f"Переименована '{old_name}' → '{new_name}' {redir_status}.")
        except Exception as e:
            self.progress.emit(f"Ошибка при переименовании '{old_name}' → '{new_name}': {e}")

# -------------------- login worker (avoid UI freeze) -------------------- #
class LoginWorker(QThread):
    success = Signal(str, str)  # username, lang
    failure = Signal(str)

    def __init__(self, username: str, password: str, lang: str):
        super().__init__()
        self.username = username
        self.password = password
        self.lang = lang

    def run(self):
        try:
            write_pwb_credentials(self.lang, self.username, self.password)
            cfg_dir = apply_pwb_config(self.lang)
            _delete_all_cookies(cfg_dir)
            reset_pywikibot_session(self.lang)
            # ограничим таймауты сетевых запросов pywikibot
            try:
                pwb_config.socket_timeout = 20  # seconds
            except Exception:
                pass
            site = pywikibot.Site(self.lang, 'wikipedia')
            try:
                site.logout()
            except Exception:
                pass
            site.login(user=self.username)
            try:
                _ = site.siteinfo.get('name')
            except Exception:
                pass
            usr = None
            try:
                usr = site.user()
            except Exception:
                usr = None
            if not usr:
                raise Exception('Сервер не вернул имя авторизованного пользователя')
            self.success.emit(self.username, self.lang)
        except Exception as e:
            self.failure.emit(f"{type(e).__name__}: {e}")

# -------------------- main window -------------------- #

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Wiki Category Tool')
        # используем иконку по умолчанию системы
        self.setMinimumSize(800, 600)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        # текущая сессия
        self.current_user = None
        self.current_lang = None
        # скрытый флаг обхода AWB: буфер для секрета
        self._secret_buffer = ''
        # признак временного закрепления окна сверху
        self._stay_on_top_active = False
        # ---- tabs ---- #
        self.init_auth_tab()
        # Попытка загрузить обход из встроенного секрета и/или файла-ключа
        try_load_bypass_awb_from_embedded()
        try_load_bypass_awb_from_file()
        # По флагу можно авто-включить обход
        maybe_auto_activate_bypass(self)
        self.init_parse_tab()
        self.init_replace_tab()
        self.init_create_tab()
        self.init_rename_tab()  # New tab for mass rename

    # ---- info button helper ---- #
    def _add_info_button(self, host_layout, text: str):
        """Insert an ℹ button aligned to the top-right of *host_layout*.

        Clicking the button shows *text* inside a modal information dialog.
        """
        btn = QToolButton()
        btn.setText('ℹ')
        btn.setAutoRaise(True)
        btn.setToolTip(text)  # quick hint on hover
        btn.clicked.connect(lambda _=None, t=text: QMessageBox.information(self, 'Справка', t))

        # Если это горизонтальный контейнер, ставим кнопку прямо в нём.
        if isinstance(host_layout, QHBoxLayout):
            host_layout.addStretch()
            host_layout.addWidget(btn)
        else:  # для вертикального — создаём отдельный верхний ряд
            row = QHBoxLayout()
            row.addStretch()
            row.addWidget(btn)
            host_layout.insertLayout(0, row)
        return btn

    # ---- tabs ---- #
    def init_auth_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.user_edit = QLineEdit(); self.user_edit.setPlaceholderText('Имя пользователя')
        self.pass_edit = QLineEdit(); self.pass_edit.setPlaceholderText('Пароль'); self.pass_edit.setEchoMode(QLineEdit.Password)
        # выбор языка проекта по центру
        layout_form = QVBoxLayout()
        layout_form.setAlignment(Qt.AlignHCenter)
        layout_form.setSpacing(4)
        # Верхний растягивающий блок для вертикального центрирования
        layout.addStretch(1)
        layout.addLayout(layout_form)
        # Нижний растягивающий блок
        layout.addStretch(2)
        layout.setContentsMargins(0, 10, 0, 10)

        # строка языка + справка
        lang_help = (
            'Можно вручную ввести любой код языка.\n'
            'Для указанных в списке языков автоматическое распознавание префиксов в считывании.\n'
            'Для остальных языков возможно только указание без префиксов с выбором пространства имён.'
        )
        row_lang = QHBoxLayout()
        row_lang.setAlignment(Qt.AlignHCenter)
        lang_label = QLabel('Язык вики:')
        row_lang.addWidget(lang_label)
        self.lang_combo = QComboBox(); self.lang_combo.setEditable(True)
        self.lang_combo.addItems(['ru', 'uk', 'be', 'en', 'fr', 'es', 'de'])
        self.lang_combo.setCurrentText('ru')
        self.lang_combo.setMaximumWidth(250)
        self.prev_lang = 'ru'
        self.lang_combo.currentTextChanged.connect(self._on_lang_change)
        row_lang.addWidget(self.lang_combo)
        # info button без растяжки — оставляем ряд компактным
        info_btn = QToolButton(); info_btn.setText('ℹ'); info_btn.setAutoRaise(True)
        info_btn.setToolTip(lang_help)
        info_btn.clicked.connect(lambda _=None: QMessageBox.information(self, 'Справка', lang_help))
        row_lang.addWidget(info_btn)
        layout_form.addLayout(row_lang)
        layout_form.setAlignment(row_lang, Qt.AlignHCenter)

        # поля логина/пароля под языком
        self.user_edit.setMinimumWidth(250)
        self.pass_edit.setMinimumWidth(250)
        layout_form.addWidget(self.user_edit, alignment=Qt.AlignHCenter)
        layout_form.addWidget(self.pass_edit, alignment=Qt.AlignHCenter)
        self.login_btn = QPushButton('Авторизоваться')
        self.login_btn.clicked.connect(self.save_creds)
        # Enter в полях логина/пароля запускает авторизацию
        try:
            self.user_edit.returnPressed.connect(self.save_creds)
            self.pass_edit.returnPressed.connect(self.save_creds)
        except Exception:
            pass
        self.status_label = QLabel('Авторизация (pywikibot)')
        # Разрешаем кликабельные ссылки в статусе
        try:
            self.status_label.setTextFormat(Qt.RichText)
            self.status_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
            self.status_label.setOpenExternalLinks(True)
        except Exception:
            pass
        # кнопка смены аккаунта — скрыта до успешной авторизации
        self.switch_btn = QPushButton('Сменить аккаунт')
        self.switch_btn.setVisible(False)
        self.switch_btn.clicked.connect(self.switch_account)
        layout_form.addWidget(self.login_btn, alignment=Qt.AlignHCenter)
        layout_form.addWidget(self.status_label, alignment=Qt.AlignHCenter)
        layout_form.addWidget(self.switch_btn, alignment=Qt.AlignHCenter)
        # уменьшить расстояние между кнопкой и статусом
        layout_form.setStretchFactor(self.login_btn, 0)
        # --- debug button at bottom-right ---
        dbg_btn = QPushButton('Debug'); dbg_btn.setFixedWidth(60)
        dbg_btn.clicked.connect(self.show_debug)
        layout.addWidget(dbg_btn, alignment=Qt.AlignRight | Qt.AlignBottom)
        self.tabs.addTab(tab, 'Авторизация')
        # Дополнительная подробная справка выводится при наведении/нажатии на кнопку ℹ рядом с полем "Язык вики".
        # Фильтр событий для секрета будет навешан на поле Debug-лога при его открытии

    # ---- creds helpers ---- #
    def _creds_ok(self):
        return bool(self.user_edit.text().strip() and self.pass_edit.text().strip())

    def _apply_cred_style(self, ok: bool):
        css_ok = 'background-color:#d4edda'
        css_def = ''
        for w in (self.user_edit, self.pass_edit):
            w.setStyleSheet(css_ok if ok else css_def)
        self.user_edit.setReadOnly(ok)
        self.pass_edit.setReadOnly(ok)
        self.lang_combo.setEnabled(not ok)
        self.login_btn.setVisible(not ok)
        self.switch_btn.setVisible(ok)
        self.status_label.setText('Авторизовано' if ok else 'Авторизация (pywikibot)')
        if ok:
            self.current_user = self.user_edit.text().strip()
            self.current_lang = (self.lang_combo.currentText() or 'ru').strip()

    def _set_awb_ui(self, has_awb: bool, note: str | None = None):
        """Включает/выключает доступ к операциям записи в зависимости от допуска AWB.
        Оставляет доступной вкладку считывания.
        """
        # Если включён режим обхода, считаем, что AWB есть
        if is_bypass_awb():
            has_awb = True
        lang = (self.current_lang or self.lang_combo.currentText() or 'ru').strip()
        awb_url = f"https://{lang}.wikipedia.org/wiki/Wikipedia:AutoWikiBrowser/CheckPageJSON"
        if has_awb:
            if is_bypass_awb():
                self.status_label.setText(f'Авторизовано · <a href="{awb_url}">AWB</a>: обход включён')
            else:
                # если страница AWB отсутствует — права не требуются
                text = 'есть'
                try:
                    if note and ('отсутств' in note.lower() or 'not exist' in note.lower()):
                        text = 'не требуются на этой вики'
                except Exception:
                    pass
                self.status_label.setText(f'Авторизовано · <a href="{awb_url}">AWB</a>: {text}')
        else:
            self.status_label.setText(f'Авторизовано. Требуется получить доступ к <a href="{awb_url}">AWB</a>')
        # Управление кнопками запуска опасных операций
        for btn_name in ('replace_btn', 'create_btn', 'rename_btn'):
            try:
                btn = getattr(self, btn_name, None)
                if btn is not None:
                    btn.setEnabled(has_awb)
            except Exception:
                pass

    def _after_login_success(self, u: str, l: str):
        self._apply_cred_style(True)
        try:
            if is_bypass_awb():
                ok_awb = True
            else:
                ok_awb, detail = is_user_awb_enabled(l, u)
        except Exception:
            ok_awb = False
        try:
            note = detail if not is_bypass_awb() else None
        except Exception:
            note = None
        # если включён bypass из файла — сразу обновим UI
        if is_bypass_awb():
            self._set_awb_ui(True)
        else:
            self._set_awb_ui(ok_awb, note)
        QMessageBox.information(self, 'OK', 'Авторизация прошла успешно.')
        # гарантируем возвращение окна на передний план (на случай кражи фокуса)
        try:
            self.raise_(); self.activateWindow()
        except Exception:
            pass
        # вернуть обычное поведение окон чуть позже, чтобы перекрыть возможный увод фокуса
        self._force_on_top(False, delay_ms=600)
        # повторно восстановить фокус с небольшими задержками
        self._bring_to_front_sequence()
        self.login_btn.setEnabled(True)

    def save_creds(self):
        debug('Saving credentials')
        user = self.user_edit.text().strip()
        pwd = self.pass_edit.text().strip()
        lang = (self.lang_combo.currentText() or 'ru').strip()
        if not user or not pwd:
            QMessageBox.warning(self, 'Ошибка', 'Введите имя пользователя и пароль.')
            self._apply_cred_style(False)
            return
        # Запускаем логин в рабочем потоке, чтобы UI не завис при сетевых/блокирующих ошибках (в т.ч. IP-запрет)
        self.login_btn.setEnabled(False)
        # На время авторизации удерживаем окно поверх других (Windows может красть фокус)
        self._force_on_top(True)
        try:
            self.raise_(); self.activateWindow()
        except Exception:
            pass
        worker = LoginWorker(user, pwd, lang)
        # на успех
        worker.success.connect(self._after_login_success)
        # на провал
        worker.failure.connect(lambda msg: [
            self.status_label.setText('Ошибка авторизации'),
            QMessageBox.critical(self, 'Ошибка авторизации', f'Не удалось авторизоваться: {msg}'),
            self._apply_cred_style(False),
            self.login_btn.setEnabled(True),
            # вернуть окно на передний план после модального окна
            (self.raise_(), self.activateWindow()),
            # снять режим поверх других окон
            self._force_on_top(False, delay_ms=600),
            self._bring_to_front_sequence()
        ])
        # удерживаем ссылку, чтобы поток не был собран GC
        self._login_worker = worker
        worker.start()

    def _force_on_top(self, enable: bool, delay_ms: int = 0) -> None:
        if delay_ms and delay_ms > 0:
            try:
                QTimer.singleShot(delay_ms, lambda: self._force_on_top(enable, 0))
                return
            except Exception:
                pass
        try:
            if enable == self._stay_on_top_active:
                if enable:
                    self.raise_(); self.activateWindow()
                return
            self._stay_on_top_active = bool(enable)
            was_visible = self.isVisible()
            self.setWindowFlag(Qt.WindowStaysOnTopHint, self._stay_on_top_active)
            if was_visible:
                # пере-применить флаг и удержать окно активным
                self.show()
                self.raise_(); self.activateWindow()
        except Exception:
            pass

    def _bring_to_front_sequence(self) -> None:
        """Многократное восстановление окна на передний план с задержками,
        чтобы перекрыть возможные асинхронные кражи фокуса."""
        try:
            def bring():
                try:
                    if self.isMinimized():
                        self.showNormal()
                    self.raise_(); self.activateWindow()
                    # Дополнительно — WinAPI на Windows
                    if sys.platform.startswith('win'):
                        try:
                            hwnd = int(self.winId())
                            user32 = ctypes.windll.user32
                            SW_SHOWNORMAL = 1
                            SWP_NOSIZE = 0x0001
                            SWP_NOMOVE = 0x0002
                            HWND_TOPMOST = -1
                            HWND_NOTOPMOST = -2
                            # показать и вывести на передний план
                            user32.ShowWindow(hwnd, SW_SHOWNORMAL)
                            # быстрый цикл topmost -> notopmost для всплытия над другими окнами
                            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
                            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
                            user32.SetForegroundWindow(hwnd)
                        except Exception:
                            pass
                except Exception:
                    pass
            # несколько попыток
            for delay in (0, 80, 200, 400, 800, 1500):
                QTimer.singleShot(delay, bring)
        except Exception:
            pass

    def load_creds(self):
        # При старте читаем общий конфиг и заполняем поля. Если есть активные куки — считаем, что уже авторизованы.
        cfg_dir = _dist_configs_dir()
        uc = os.path.join(cfg_dir, 'user-config.py')
        up = os.path.join(cfg_dir, 'user-password.py')
        cur_lang = None
        username_map = {}
        password = ''
        try:
            if os.path.isfile(uc):
                with open(uc, encoding='utf-8') as f:
                    txt = f.read()
                mlang = re.search(r"^\s*mylang\s*=\s*'([^']+)'\s*$", txt, re.M)
                if mlang:
                    cur_lang = mlang.group(1)
                    self.lang_combo.setCurrentText(cur_lang)
                for m in re.finditer(r"usernames\['wikipedia'\]\['([^']+)'\]\s*=\s*'([^']+)'", txt):
                    username_map[m.group(1)] = m.group(2)
            if os.path.isfile(up):
                with open(up, encoding='utf-8') as f:
                    try:
                        u, p = ast.literal_eval(f.read())
                        password = p
                        if cur_lang and cur_lang not in username_map and u:
                            username_map[cur_lang] = u
                    except Exception:
                        pass
        except Exception:
            pass
        # Заполнить поля
        if cur_lang and cur_lang in username_map:
            self.user_edit.setText(username_map[cur_lang])
        if password:
            self.pass_edit.setText(password)
        # Если есть и логин, и пароль, и обнаружены куки — считаем, что авторизация активна
        if cur_lang and (cur_lang in username_map) and password and cookies_exist(cfg_dir, username_map[cur_lang]):
            self._apply_cred_style(True)
            # Показать индикатор AWB-доступа и включить/выключить кнопки
            try:
                ok_awb, detail = is_user_awb_enabled(cur_lang, username_map[cur_lang])
                self._set_awb_ui(ok_awb, detail)
            except Exception:
                self._set_awb_ui(False)
        else:
            self._apply_cred_style(False)

    def init_parse_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        # help text previously used in tooltip
        parse_help = (
            '1. Укажите название корневой категории (без префикса) и нажмите «Получить» —'
            '   программа запросит ВСЕ её подкатегории через MediaWiki API выбранного языка'
            '   и автоматически вставит их в левый список в алфавитном порядке.\n'
            '\n'
            '2. Список можно редактировать вручную или загрузить готовый .txt-файл\n'
            '   (каждое название на новой строке).\n'
            '\n'
            '3. Вверху справа выберите тип содержимого:\n'
            '   • Категория   • Шаблон   • Статья\n'
            '   От выбора зависит, будет ли автоматически подставлен префикс'
            '   Category:Template: для нестандартных языков. Для стандартных'
            '   (предлагаемых на первой вкладке) локализованный префикс распознаётся автоматически.\n'
            '\n'
            '4. Укажите имя результирующего файла .tsv и нажмите «Начать считывание».\n'
            '   Будет создан TSV-файл вида: Title<TAB>line1<TAB>line2…'
        )
        # --- layouts: left (список) and right (настройки) ---
        h_main = QHBoxLayout()
        left = QVBoxLayout(); right = QVBoxLayout()
        # Wrap each side into QWidget for stretch control
        left_widget = QWidget(); left_widget.setLayout(left)
        right_widget = QWidget(); right_widget.setLayout(right)

        # --- row to fetch subcategories via Petscan (top of left) ---
        row_cat = QHBoxLayout()
        row_cat.addWidget(QLabel('Получить список подкатегорий:'))
        self.cat_edit = QLineEdit(); self.cat_edit.setPlaceholderText('Название категории (без префикса)'); self.cat_edit.setMinimumWidth(260)
        row_cat.addWidget(self.cat_edit)
        self.petscan_btn = QPushButton('Получить')
        self.petscan_btn.setToolTip('Клик — получить подкатегории через API.\nCtrl+клик — открыть Petscan с расширенными настройками')
        self.petscan_btn.clicked.connect(self.open_petscan)
        row_cat.addWidget(self.petscan_btn)
        left.addLayout(row_cat)

        # label above manual list
        left.addWidget(QLabel('<b>Список категорий для считывания содержимого:</b>'))

        # --- Manual list input ---
        self.manual_list = QTextEdit()
        self.manual_list.setPlaceholderText('По одному названию на строке')
        self.manual_list.setMinimumHeight(140)
        left.addWidget(self.manual_list, 1)

        # --- file picker для list.txt (под списком) ---
        # bold label for file input
        left.addWidget(QLabel('<b>или укажите список файлом:</b>'))

        h1 = QHBoxLayout()
        # кнопка подсказки ставится в строку с радиокнопками
        # (до добавления растяжки)
        # type radio
        types = QHBoxLayout()
        self.type_group = QButtonGroup()
        for text, val in [('Категория','2'), ('Шаблон','3'), ('Статья','1')]:
            rb = QRadioButton(text); rb.setProperty('ctype', val)
            self.type_group.addButton(rb); types.addWidget(rb)
        self.type_group.buttons()[0].setChecked(True)

        # file picker list.txt
        self.in_path = QLineEdit()
        self.in_path.setMinimumWidth(300)
        self.in_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        btn_browse_in = QPushButton('…'); btn_browse_in.clicked.connect(lambda: self.pick_file(self.in_path, '*.txt'))
        h1.addWidget(QLabel('Список страниц (.txt):'))
        h1.addWidget(self.in_path); h1.addWidget(btn_browse_in)
        left.addLayout(h1)

        # --- Right side (settings) ---
        # Row 1: radio buttons + info button
        types_row = QHBoxLayout()
        for rb in self.type_group.buttons():
            types_row.addWidget(rb)
        # Info button appended to same row
        self._add_info_button(types_row, parse_help)
        right.addLayout(types_row)

        # Row 2: result file picker
        h2 = QHBoxLayout()
        self.out_path = QLineEdit('categories.tsv')
        self.out_path.setMinimumWidth(250); self.out_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        btn_out = QPushButton('…'); btn_out.setFixedWidth(30)
        btn_out.clicked.connect(lambda: self.pick_save(self.out_path, '.tsv'))
        h2.addWidget(QLabel('Файл результата (.tsv):'))
        h2.addWidget(self.out_path)
        h2.addWidget(btn_out)
        right.addLayout(h2)

        # Row 3: parse buttons (start/stop)
        row_run = QHBoxLayout()
        self.parse_btn = QPushButton('Начать считывание'); self.parse_btn.clicked.connect(self.start_parse)
        self.parse_stop_btn = QPushButton('Остановить')
        self.parse_stop_btn.setEnabled(False)
        self.parse_stop_btn.clicked.connect(self.stop_parse)
        row_run.addWidget(self.parse_btn)
        row_run.addWidget(self.parse_stop_btn)
        row_run.addStretch()
        right.addLayout(row_run)
        # ширина старт/стоп в соотношении 3:1
        self._set_start_stop_ratio(self.parse_btn, self.parse_stop_btn, 3)

        # Row 4: log (fills remaining space)
        # progress bar
        self.parse_bar = QProgressBar(); self.parse_bar.setMaximum(1); self.parse_bar.setValue(0)
        right.addWidget(self.parse_bar)

        self.parse_log = QTextEdit(); self.parse_log.setReadOnly(True)
        right.addWidget(self.parse_log, 1)

        # open result button
        self.open_res_btn = QPushButton('Открыть результат')
        self.open_res_btn.clicked.connect(self.open_result_file)
        right.addWidget(self.open_res_btn)

        h_main.addWidget(left_widget, 1)
        h_main.addWidget(right_widget, 2)

        v.addLayout(h_main)

        self.tabs.addTab(tab, 'Считать')

    # ---- Petscan opener ---- #
    def open_petscan(self):
        debug(f"Fetch subcats btn pressed: cat={self.cat_edit.text().strip()}")
        category = self.cat_edit.text().strip()
        if not category:
            QMessageBox.warning(self, 'Ошибка', 'Введите название категории.')
            return
        lang = self.lang_combo.currentText().strip() or 'ru'

        # --- Ctrl+click → открыть Petscan URL в браузере ---
        mods = QApplication.keyboardModifiers()
        if mods & Qt.ControlModifier:
            cat_param = urllib.parse.quote_plus(category)
            petscan_url = (
                'https://petscan.wmcloud.org/?combination=subset&interface_language=en&ores_prob_from=&'
                'referrer_name=&ores_prob_to=&min_sitelink_count=&wikidata_source_sites=&templates_yes=&'
                'sortby=title&pagepile=&cb_labels_no_l=1&show_disambiguation_pages=both&language=' + lang +
                '&max_sitelink_count=&cb_labels_yes_l=1&outlinks_any=&common_wiki=auto&categories=' + cat_param +
                '&edits%5Bbots%5D=both&wikidata_prop_item_use=&ores_prediction=any&outlinks_no=&source_combination=&'
                'ns%5B14%5D=1&sitelinks_any=&cb_labels_any_l=1&edits%5Banons%5D=both&links_to_no=&search_wiki=&'
                'project=wikipedia&after=&wikidata_item=no&search_max_results=1000&langs_labels_no=&langs_labels_yes=&'
                'sortorder=ascending&templates_any=&show_redirects=both&active_tab=tab_output&wpiu=any&doit='
            )
            debug(f"Open Petscan URL: {petscan_url}")
            webbrowser.open_new_tab(petscan_url)
            self.petscan_btn.setEnabled(True)
            return

        # --- API режим --- формируем полное имя категории ---
        if re.match(r'(?i)^(категория|category):', category):
            cat_full = category
        else:
            cat_full = ('Категория:' if lang == 'ru' else 'Category:') + category

        api_url = f"https://{lang}.wikipedia.org/w/api.php"
        params = {
            'action': 'query',
            'list': 'categorymembers',
            'cmtitle': cat_full,
            'cmtype': 'subcat',
            'cmlimit': 'max',
            'format': 'json'
        }
        subcats = []
        try:
            while True:
                resp = requests.get(api_url, params=params, timeout=10).json()
                debug(f"API GET subcats len={len(resp.get('query',{}).get('categorymembers',[]))}")
                subcats.extend(m['title'] for m in resp.get('query', {}).get('categorymembers', []))
                if 'continue' in resp:
                    params.update(resp['continue'])
                else:
                    break
            if subcats:
                subcats_sorted = sorted(subcats, key=lambda s: s.casefold())
                self.manual_list.setPlainText('\n'.join(subcats_sorted))
                self.log(self.parse_log, f"Получено подкатегорий: {len(subcats_sorted)} (отсортировано)")
            else:
                self.log(self.parse_log, 'Подкатегории не найдены.')
        except Exception as e:
            self.log(self.parse_log, f"Ошибка API: {e}")
        debug("Subcat fetch finished")
        self.petscan_btn.setEnabled(True)

        # Если нужно открыть ссылку вручную, скопируйте URL из логов

    def init_replace_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        replace_help = (
            'TSV: Title<TAB>line1<TAB>line2…\n'
            'Title ДОЛЖЕН содержать полный префикс (например «Категория:...», «Template:...»).\n'
            'Строка TSV → одна страница: её имя + новый текст.\n'
            'При запуске существующий текст страницы полностью заменяется.\n'
            'Комментарий правки задаётся ниже; можно отметить как малую (minor).'
        )
        h = QHBoxLayout()
        self.tsv_path = QLineEdit('categories.tsv')
        self.tsv_path.setMinimumWidth(350)
        self.tsv_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        btn = QPushButton('…'); btn.clicked.connect(lambda: self.pick_file(self.tsv_path, '*.tsv'))
        h.addWidget(QLabel('Список для замен (.tsv):'))
        h.addWidget(self.tsv_path); h.addWidget(btn)
        # кнопка ℹ в строке выбора файла
        self._add_info_button(h, replace_help)
        # summary field
        sum_layout = QHBoxLayout()
        sum_layout.addWidget(QLabel('Комментарий к правкам:'))
        self.summary_edit = QLineEdit()
        sum_layout.addWidget(self.summary_edit)
        # начальное значение по умолчанию (ru)
        self.summary_edit.setText(default_summary('ru'))

        # Малая правка
        self.minor_checkbox = QCheckBox('Малая правка (minor edit)')
        sum_layout.addWidget(self.minor_checkbox)

        self.replace_btn = QPushButton('Начать запись')
        self.replace_btn.clicked.connect(self.start_replace)
        self.replace_stop_btn = QPushButton('Остановить')
        self.replace_stop_btn.setEnabled(False)
        self.replace_stop_btn.clicked.connect(self.stop_replace)
        self.rep_log = QTextEdit(); self.rep_log.setReadOnly(True)
        row_run = QHBoxLayout(); row_run.addWidget(self.replace_btn); row_run.addWidget(self.replace_stop_btn); row_run.addStretch()
        v.addLayout(h); v.addLayout(sum_layout); v.addLayout(row_run); v.addWidget(self.rep_log)
        self._set_start_stop_ratio(self.replace_btn, self.replace_stop_btn, 3)
        self.tabs.addTab(tab, 'Перезаписать')

    def init_create_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        create_help = (
            'TSV: Title<TAB>line1<TAB>line2…\n'
            'Title с полным префиксом («Категория:…», «Template:…»).\n'
            'Каждая строка TSV = новая страница; если она уже существует, будет пропущена.'
        )
        h = QHBoxLayout()
        self.tsv_path_create = QLineEdit('categories.tsv')
        self.tsv_path_create.setMinimumWidth(350)
        self.tsv_path_create.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        btn = QPushButton('…'); btn.clicked.connect(lambda: self.pick_file(self.tsv_path_create, '*.tsv'))
        h.addWidget(QLabel('Список для создания (.tsv):'))
        h.addWidget(self.tsv_path_create); h.addWidget(btn)
        # кнопка ℹ в строке выбора файла
        self._add_info_button(h, create_help)
        # summary field
        sum_layout = QHBoxLayout()
        sum_layout.addWidget(QLabel('Комментарий к правкам:'))
        self.summary_edit_create = QLineEdit()
        sum_layout.addWidget(self.summary_edit_create)
        self.summary_edit_create.setText(default_create_summary('ru'))
        # run buttons
        self.create_btn = QPushButton('Начать создание')
        self.create_btn.clicked.connect(self.start_create)
        self.create_stop_btn = QPushButton('Остановить')
        self.create_stop_btn.setEnabled(False)
        self.create_stop_btn.clicked.connect(self.stop_create)
        self.create_log = QTextEdit(); self.create_log.setReadOnly(True)
        row_run = QHBoxLayout(); row_run.addWidget(self.create_btn); row_run.addWidget(self.create_stop_btn); row_run.addStretch()
        v.addLayout(h); v.addLayout(sum_layout); v.addLayout(row_run); v.addWidget(self.create_log)
        self._set_start_stop_ratio(self.create_btn, self.create_stop_btn, 3)
        self.tabs.addTab(tab, 'Создать')

    def init_rename_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        rename_help = (
            'TSV: OldTitle<TAB>NewTitle<TAB>Reason\n'
            'OldTitle / NewTitle пишутся с полным префиксом («Категория:» / «Template:» / …).\n'
            'Скрипт выполняет переименование для каждой строки.\n'
            'Отдельные чекбоксы задают, оставлять ли редиректы для категорий и других типов страниц.'
        )
        # --- File picker for TSV ---
        h = QHBoxLayout()
        self.tsv_path_rename = QLineEdit('categories.tsv')
        self.tsv_path_rename.setMinimumWidth(350)
        self.tsv_path_rename.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        btn_browse = QPushButton('…'); btn_browse.clicked.connect(lambda: self.pick_file(self.tsv_path_rename, '*.tsv'))
        h.addWidget(QLabel('Список для переименования (.tsv):'))
        h.addWidget(self.tsv_path_rename); h.addWidget(btn_browse)
        # кнопка ℹ в строке выбора файла
        self._add_info_button(h, rename_help)
        v.addLayout(h)

        # --- Redirect options ---
        opts = QHBoxLayout()
        self.chk_redirect_cat = QCheckBox('Оставлять редирект для категорий')
        self.chk_redirect_cat.setChecked(False)  # соответствует скрипту по умолчанию
        self.chk_redirect_other = QCheckBox('Оставлять редирект для других страниц')
        self.chk_redirect_other.setChecked(True)
        opts.addWidget(self.chk_redirect_cat); opts.addWidget(self.chk_redirect_other)
        v.addLayout(opts)

        # --- Run buttons ---
        self.rename_btn = QPushButton('Начать переименование')
        self.rename_btn.clicked.connect(self.start_rename)
        self.rename_stop_btn = QPushButton('Остановить')
        self.rename_stop_btn.setEnabled(False)
        self.rename_stop_btn.clicked.connect(self.stop_rename)
        row_run = QHBoxLayout(); row_run.addWidget(self.rename_btn); row_run.addWidget(self.rename_stop_btn); row_run.addStretch()
        v.addLayout(row_run)
        self._set_start_stop_ratio(self.rename_btn, self.rename_stop_btn, 3)
        # Warning for users
        v.addWidget(QLabel('<b>ВНИМАНИЕ!</b> Переименовываются только названия категорий, содержимое нужно переносить вручную.' ))

        # --- Log widget ---
        self.rename_log = QTextEdit(); self.rename_log.setReadOnly(True)
        v.addWidget(self.rename_log)
        self.tabs.addTab(tab, 'Переименовать')

    # ---- secret code handler ---- #
    def eventFilter(self, obj, event):
        try:
            if event.type() == QEvent.KeyPress and DEBUG_VIEW is not None and obj is DEBUG_VIEW:
                # Считываем секрет только из поля лога Debug и только не авто-повтор
                if hasattr(event, 'isAutoRepeat') and not event.isAutoRepeat():
                    # Секрет из файла-ключа или переменной окружения BYPASS_AWB_TOKEN
                    token = (BYPASS_TOKEN or os.environ.get('BYPASS_AWB_TOKEN', '')).strip().lower()
                    if token and len(token) >= 3:
                        ch_raw = event.text() or ''
                        # Принимаем ровно один печатаемый символ; приводим к lower
                        if len(ch_raw) == 1 and ch_raw.isprintable() and not ch_raw.isspace():
                            ch = ch_raw.lower()
                            max_len = len(token)
                            self._secret_buffer = (self._secret_buffer + ch)[-max_len:]
                            if self._secret_buffer == token and not is_bypass_awb():
                                set_bypass_awb(True)
                                self._set_awb_ui(True)
                                debug('Bypass AWB activated via token')
                                QMessageBox.information(self, 'Режим обхода', 'Доступ к AWB: обход включён.')
                    else:
                        # Токен не задан — обход по клавишам невозможен
                        self._secret_buffer = ''
        except Exception:
            pass
        return super().eventFilter(obj, event)

    # ---- helpers ---- #
    def pick_file(self, edit: QLineEdit, pattern):
        path, _ = QFileDialog.getOpenFileName(self, 'Выберите файл', filter=f'Files ({pattern})')
        if path: edit.setText(path)

    def pick_save(self, edit: QLineEdit, default_ext):
        path, _ = QFileDialog.getSaveFileName(self, 'Куда сохранить', filter=f'*.{default_ext.lstrip(".")}')
        if path: edit.setText(path)

    def log(self, widget: QTextEdit, msg: str):
        debug(msg)
        # simple coloring: ошибки красным, успехи зелёным
        lower = msg.lower()
        color = None
        if 'ошибка' in lower or 'не найдено' in lower:
            color = 'red'
        elif 'уже существует' in lower:
            # тёмно-жёлтый для статуса "уже существует"
            color = '#b8860b'
        elif any(k in lower for k in ('записано', 'создано', 'переименована', 'готово')):
            # более тёмный зелёный для лучшей читаемости на светлой теме
            color = '#2e7d32'
        if color:
            widget.append(f"<span style='color:{color}'>{msg}</span>")
        else:
            widget.append(msg)

    def _set_start_stop_ratio(self, start_btn: QPushButton, stop_btn: QPushButton, ratio: int = 3):
        try:
            sh_start = start_btn.sizeHint().width()
            sh_stop = stop_btn.sizeHint().width()
            base = max(sh_start, ratio * sh_stop)
            stop_w = max(sh_stop, (base + ratio - 1) // ratio)
            start_w = ratio * stop_w
            stop_btn.setFixedWidth(stop_w)
            start_btn.setFixedWidth(start_w)
        except Exception:
            # fallback sizes
            stop_btn.setFixedWidth(100)
            start_btn.setFixedWidth(300)

    # ---- actions ---- #
    def start_parse(self):
        if not self.out_path.text():
            QMessageBox.warning(self, 'Ошибка', 'Укажите файл результата.')
            return

        titles = []
        # Приоритет: если указан файл — читаем из него; иначе берём из текстового поля
        in_file = self.in_path.text().strip()
        if in_file:
            try:
                with open(in_file, encoding='utf-8') as f:
                    titles = [l.strip() for l in f if l.strip()]
            except Exception as e:
                QMessageBox.critical(self, 'Ошибка', str(e)); return
        else:
            manual_lines = self.manual_list.toPlainText().splitlines()
            titles = [l.strip() for l in manual_lines if l.strip()]

        if not titles:
            QMessageBox.warning(self, 'Ошибка', 'Не указан ни файл со списком, ни текст списка.')
            return
        ctype = self.type_group.checkedButton().property('ctype')
        lang = self.lang_combo.currentText()
        # init progress bar
        self.parse_bar.setMaximum(len(titles))
        self.parse_bar.setValue(0)
        self.parse_btn.setEnabled(False); self.parse_log.clear()
        self.worker = ParseWorker(titles, self.out_path.text(), ctype, lang)
        self.worker.progress.connect(lambda m: [self._inc_parse_prog(), self.log(self.parse_log, m)])
        self.worker.finished.connect(self._on_parse_finished)
        self.parse_btn.setEnabled(False)
        self.parse_stop_btn.setEnabled(True)
        self.parse_log.clear()
        self.worker.start()

    def stop_parse(self):
        w = getattr(self, 'worker', None)
        if w and w.isRunning():
            w.request_stop()
            # мгновенная реакция UI
            try:
                self.parse_stop_btn.setEnabled(False)
                self.log(self.parse_log, 'Останавливаю...')
            except Exception:
                pass

    def _on_parse_finished(self):
        self.parse_btn.setEnabled(True)
        self.parse_stop_btn.setEnabled(False)
        msg = 'Остановлено!' if getattr(self, 'worker', None) and getattr(self.worker, '_stop', False) else 'Готово!'
        self.log(self.parse_log, msg)

    def _inc_parse_prog(self):
        self.parse_bar.setValue(self.parse_bar.value()+1)

    # ---- open result file ---- #
    def open_result_file(self):
        path = self.out_path.text().strip()
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, 'Файл не найден', 'Файл результата ещё не создан.')
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def start_replace(self):
        debug(f'Start replace: file={self.tsv_path.text()}')
        if not self.tsv_path.text():
            QMessageBox.warning(self, 'Ошибка', 'Укажите TSV.')
            return
        user, pwd = self.user_edit.text(), self.pass_edit.text()
        lang = self.lang_combo.currentText()
        apply_pwb_config(lang)
        # Блокируем запуск, если нет допуска AWB и не включён режим обхода
        if not is_bypass_awb():
            try:
                ok_awb, _ = is_user_awb_enabled(lang, user)
            except Exception:
                ok_awb = False
            if not ok_awb:
                QMessageBox.warning(self, 'Нет допуска AWB', 'Получите доступ к AWB или используйте режим считывания.')
                return
        summary = self.summary_edit.text().strip()
        if not summary:
            summary = default_summary(lang)
        minor = self.minor_checkbox.isChecked()
        self.replace_btn.setEnabled(False); self.replace_stop_btn.setEnabled(True); self.rep_log.clear()
        self.rworker = ReplaceWorker(self.tsv_path.text(), user, pwd, lang, summary, minor)
        self.rworker.progress.connect(lambda m: self.log(self.rep_log, m))
        self.rworker.finished.connect(self._on_replace_finished)
        self.rworker.start()

    def stop_replace(self):
        w = getattr(self, 'rworker', None)
        if w and w.isRunning():
            w.request_stop()

    def _on_replace_finished(self):
        self.replace_btn.setEnabled(True)
        self.replace_stop_btn.setEnabled(False)
        msg = 'Остановлено!' if getattr(self, 'rworker', None) and getattr(self.rworker, '_stop', False) else 'Запись завершена!'
        self.log(self.rep_log, msg)

    def start_create(self):
        debug(f'Start create: file={self.tsv_path_create.text()}')
        if not self.tsv_path_create.text():
            QMessageBox.warning(self, 'Ошибка', 'Укажите TSV.')
            return
        user, pwd = self.user_edit.text(), self.pass_edit.text()
        lang = self.lang_combo.currentText()
        apply_pwb_config(lang)
        if not is_bypass_awb():
            try:
                ok_awb, _ = is_user_awb_enabled(lang, user)
            except Exception:
                ok_awb = False
            if not ok_awb:
                QMessageBox.warning(self, 'Нет допуска AWB', 'Получите доступ к AWB или используйте режим считывания.')
                return
        summary = self.summary_edit_create.text().strip()
        if not summary:
            summary = default_create_summary(lang)
        minor = False  # Малая правка не применяется при создании
        self.create_btn.setEnabled(False); self.create_stop_btn.setEnabled(True); self.create_log.clear()
        self.cworker = CreateWorker(self.tsv_path_create.text(), user, pwd, lang, summary, minor)
        self.cworker.progress.connect(lambda m: self.log(self.create_log, m))
        self.cworker.finished.connect(self._on_create_finished)
        self.cworker.start()

    def stop_create(self):
        w = getattr(self, 'cworker', None)
        if w and w.isRunning():
            w.request_stop()

    def _on_create_finished(self):
        self.create_btn.setEnabled(True)
        self.create_stop_btn.setEnabled(False)
        msg = 'Остановлено!' if getattr(self, 'cworker', None) and getattr(self.cworker, '_stop', False) else 'Создание завершено!'
        self.log(self.create_log, msg)

    def start_rename(self):
        debug(f'Start rename file={self.tsv_path_rename.text()}')
        if not self.tsv_path_rename.text():
            QMessageBox.warning(self, 'Ошибка', 'Укажите TSV-файл.')
            return
        user, pwd = self.user_edit.text(), self.pass_edit.text()
        lang = self.lang_combo.currentText()
        apply_pwb_config(lang)
        if not is_bypass_awb():
            try:
                ok_awb, _ = is_user_awb_enabled(lang, user)
            except Exception:
                ok_awb = False
            if not ok_awb:
                QMessageBox.warning(self, 'Нет допуска AWB', 'Получите доступ к AWB или используйте режим считывания.')
                return
        leave_cat = self.chk_redirect_cat.isChecked()
        leave_other = self.chk_redirect_other.isChecked()
        self.rename_btn.setEnabled(False); self.rename_stop_btn.setEnabled(True); self.rename_log.clear()
        self.mrworker = RenameWorker(self.tsv_path_rename.text(), user, pwd, lang, leave_cat, leave_other)
        self.mrworker.progress.connect(lambda m: self.log(self.rename_log, m))
        self.mrworker.finished.connect(self._on_rename_finished)
        self.mrworker.start()

    def stop_rename(self):
        w = getattr(self, 'mrworker', None)
        if w and w.isRunning():
            w.request_stop()

    def _on_rename_finished(self):
        self.rename_btn.setEnabled(True)
        self.rename_stop_btn.setEnabled(False)
        msg = 'Остановлено!' if getattr(self, 'mrworker', None) and getattr(self.mrworker, '_stop', False) else 'Переименование завершено!'
        self.log(self.rename_log, msg)

    # ---- react to language change ---- #
    def _on_lang_change(self, new_lang):
        edits = [
            (getattr(self, 'summary_edit', None), default_summary),
            (getattr(self, 'summary_edit_create', None), default_create_summary)
        ]
        for widget, func in edits:
            if widget is None:
                continue
            cur = widget.text().strip()
            if cur == '' or cur == func(self.prev_lang):
                widget.setText(func(new_lang))
        self.prev_lang = new_lang

    # ---- window close event ---- #
    def closeEvent(self, event):
        running_threads = []
        for attr in ('worker','rworker','cworker','mrworker'):
            t = getattr(self, attr, None)
            if t and t.isRunning():
                running_threads.append(attr)
        if running_threads:
            res = QMessageBox.question(self, 'Внимание', 'Некоторые операции ещё выполняются. Закрыть программу?')
            if res != QMessageBox.Yes:
                event.ignore()
                return
        super().closeEvent(event)

    # ---- debug window ---- #
    def show_debug(self):
        global DEBUG_VIEW
        if DEBUG_VIEW is not None and not DEBUG_VIEW.parent().isHidden():
            DEBUG_VIEW.parent().raise_(); DEBUG_VIEW.parent().activateWindow(); return

        dlg = QDialog(self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.setWindowTitle('Debug log')
        dlg.resize(700,500)
        v = QVBoxLayout(dlg)
        txt = QPlainTextEdit('\n'.join(DEBUG_BUFFER))
        txt.setReadOnly(True)
        v.addWidget(txt)
        DEBUG_VIEW = txt  # store for live updates
        # Перехватываем клавиши в окне Debug и поле вывода лога
        try:
            txt.installEventFilter(self)
        except Exception:
            pass

        def on_close():
            global DEBUG_VIEW
            DEBUG_VIEW = None
        dlg.destroyed.connect(on_close)

        btn_clear = QPushButton('Clear'); btn_close = QPushButton('Close')
        btn_clear.clicked.connect(lambda: [DEBUG_BUFFER.clear(), txt.clear()])
        btn_close.clicked.connect(dlg.close)
        h = QHBoxLayout(); h.addStretch(); h.addWidget(btn_clear); h.addWidget(btn_close)
        v.addLayout(h)
        dlg.show()

    # ---- account switching ---- #
    def switch_account(self):
        # Разблокировать поля и скрыть кнопку "Сменить аккаунт"
        lang = (self.lang_combo.currentText() or 'ru').strip()
        cfg_dir = config_base_dir()
        # Удаляем куки для текущего пользователя (если введён)
        user = self.user_edit.text().strip()
        _delete_all_cookies(cfg_dir)
        reset_pywikibot_session(None)
        # Сбрасываем текущую сессию в UI
        self.current_user = None
        self.current_lang = None
        self._apply_cred_style(False)
        # переинициализируем конфиг (создаст throttle.ctrl при необходимости)
        apply_pwb_config(lang)

# -------------------- main -------------------- #

def main():
    app = QApplication(sys.argv)
    try:
        import qdarktheme
        app.setStyleSheet(qdarktheme.load_stylesheet())
    except ImportError:
        pass
    w = MainWindow(); w.load_creds(); w.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main() 