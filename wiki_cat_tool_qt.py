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
import difflib
import html
import ast
import ctypes
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import Lock, Event


# Инициализация окружения для Pywikibot
_startup_base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
_startup_cfg = os.path.join(_startup_base, 'configs')
try:
    os.makedirs(_startup_cfg, exist_ok=True)
except Exception:
    pass
# Настройка конфигурации pywikibot
_user_cfg_path = os.path.join(_startup_cfg, 'user-config.py')
if os.path.isfile(_user_cfg_path):
    os.environ.pop('PYWIKIBOT_NO_USER_CONFIG', None)
else:
    os.environ['PYWIKIBOT_NO_USER_CONFIG'] = '1'

os.environ['PYWIKIBOT_DIR'] = _startup_cfg

# Перенаправление вывода для GUI-приложения
class _GuiStdWriter(io.TextIOBase):
    def write(self, s: str) -> int:
        try:
            if s and s.strip():
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

from PySide6.QtCore import Qt, QThread, Signal, QUrl, QEvent, QTimer, QPoint

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QLineEdit, QPushButton,
    QFileDialog, QTextEdit, QTabWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QProgressBar, QMessageBox, QToolButton, QComboBox, QCheckBox,
    QSizePolicy, QDialog, QPlainTextEdit, QGroupBox, QFrame
)
from PySide6.QtGui import QDesktopServices, QFont, QKeySequence, QShortcut

# Настройка базовой директории
def tool_base_dir() -> str:
    try:
        return os.path.dirname(__file__)
    except NameError:
        return os.getcwd()

if 'PYWIKIBOT_DIR' not in os.environ:
    os.environ['PYWIKIBOT_DIR'] = tool_base_dir()

import pywikibot
from pywikibot import config as pwb_config

from pywikibot.comms import http as pywb_http

# Перехват вывода pywikibot
def _pywb_log(msg, *_args, **_kwargs):
    debug('PYWIKIBOT: ' + str(msg))
pywikibot.output = _pywb_log
pywikibot.warning = _pywb_log
pywikibot.error = _pywb_log

write_lock = Lock()


# ===== HTTP Session + Rate Limiting =====
REQUEST_SESSION = requests.Session()
REQUEST_HEADERS = {
    'User-Agent': 'WikiCatTool/1.0 (+https://github.com/sc113/wiki-category-tool; contact:none) requests',
    'Accept': 'application/json'
}
RELEASES_URL = 'https://github.com/sc113/wiki-category-tool/releases'
GITHUB_API_RELEASES = 'https://api.github.com/repos/sc113/wiki-category-tool/releases'
APP_VERSION = "beta5"
_RATE_LOCK = Lock()
_LAST_REQ_TS = 0.0
_MIN_INTERVAL = 0.12

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

        _MIN_INTERVAL = min(2.5, max(_MIN_INTERVAL * 1.5, add if add > 0 else _MIN_INTERVAL))
        debug(f"Rate backoff: MIN_INTERVAL={_MIN_INTERVAL:.2f}s")

# ===== Debug Buffer =====
DEBUG_BUFFER = []
DEBUG_VIEW = None

# ===== AWB Bypass System =====
BYPASS_AWB = False
BYPASS_TOKEN: str | None = None
AWB_CHECKS_DISABLED = True  # TEMP: отключение проверок AWB

def set_bypass_awb(flag: bool) -> None:
    global BYPASS_AWB
    BYPASS_AWB = bool(flag)
    debug(f"Bypass AWB set to {BYPASS_AWB}")

def is_bypass_awb() -> bool:
    return True if AWB_CHECKS_DISABLED else bool(BYPASS_AWB)

def try_load_bypass_awb_from_embedded() -> bool:
    """Пытается загрузить токен обхода AWB только из модуля `_embedded_secrets`.
    Возвращает True, если токен найден.
    """
    global BYPASS_TOKEN
    try:
        token = ''
        # Сначала пробуем модуль рядом со скриптом: _embedded_secrets.py
        try:
            import _embedded_secrets as _sec
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
    """Если в модуле `_embedded_secrets` установлен флаг `AUTO_ACTIVATE=True`, включает обход сразу."""
    try:
        auto = False
        try:
            import _embedded_secrets as _sec
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

# ===== Default Edit Summary =====

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

# ===== Default Creation Summary =====
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

# ===== Project Helpers =====
def build_host(family: str, lang: str) -> str:
    fam = (family or 'wikipedia').strip()
    lng = (lang or 'ru').strip()
    if fam == 'commons':
        return 'commons.wikimedia.org'
    if fam == 'wikidata':
        return 'www.wikidata.org'
    if fam == 'meta':
        return 'meta.wikimedia.org'
    if fam == 'species':
        return 'species.wikimedia.org'
    if fam == 'incubator':
        return 'incubator.wikimedia.org'
    if fam == 'mediawiki':
        return 'www.mediawiki.org'
    if fam == 'wikifunctions':
        return 'www.wikifunctions.org'
    return f"{lng}.{fam}.org"

def build_api_url(family: str, lang: str) -> str:
    return f"https://{build_host(family, lang)}/w/api.php"

def build_project_awb_url(family: str, lang: str) -> str:
    return f"https://{build_host(family, lang)}/wiki/Project:AutoWikiBrowser/CheckPageJSON"

# ===== Namespace Helpers =====
NS_CACHE: dict[tuple[str, str], dict[int, dict[str, set[str] | str]]] = {}

# Предзаданные локальные префиксы для популярных языков/проектов
DEFAULT_NS_PREFIXES: dict[tuple[str, str], dict[int, dict[str, set[str] | str]]] = {}

# Общая справка о префиксах для tooltip
PREFIX_TOOLTIP = (
    "Для любого языка локальные префиксы автоматически\n"
    "подгружаются из API и кэшируются.\n\n"
    "«Авто» — не изменяет заголовки и распознаёт все префиксы\n"
    "При выборе конкретного пространства имён\n"
    "префикс подставляется на локальном языке\n\n"
    "Английские префиксы в исходных данных\n"
    "также корректно распознаются"
)

def _ns_cache_dir() -> str:
    base = _dist_configs_dir()
    path = os.path.join(base, 'apicache')
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path

def _ns_cache_file(family: str, lang: str) -> str:
    safe_f = re.sub(r"[^a-z0-9_-]+", "_", (family or '').lower())
    safe_l = re.sub(r"[^a-z0-9_-]+", "_", (lang or '').lower())
    return os.path.join(_ns_cache_dir(), f"ns_{safe_f}_{safe_l}.json")

 

def _load_ns_info(family: str, lang: str) -> dict[int, dict[str, set[str] | str]]:
    key = (family, lang)
    if key in NS_CACHE:
        return NS_CACHE[key]
    # Загрузка из дискового кэша
    cache_path = _ns_cache_file(family, lang)
    try:
        if os.path.isfile(cache_path):
            with open(cache_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                restored: dict[int, dict[str, set[str] | str]] = {}
                for sid, meta in raw.items():
                    try:
                        ns_id = int(sid)
                    except Exception:
                        continue
                    if not isinstance(meta, dict):
                        continue
                    prim = str(meta.get('primary') or '')
                    all_list = meta.get('all') or []
                    if isinstance(all_list, list):
                        restored[ns_id] = {'primary': prim, 'all': {str(x).lower() for x in all_list}}
                if restored:
                    NS_CACHE[key] = restored
                    try:
                        debug(f"NS cache hit: {family}/{lang} → {len(restored)} namespaces from {cache_path}")
                    except Exception:
                        pass
                    return restored
    except Exception:
        pass
    # Запрос к API и сохранение в кэш
    url = build_api_url(family, lang)
    params = {
        'action': 'query', 'meta': 'siteinfo', 'siprop': 'namespaces|namespacealiases', 'format': 'json'
    }
    prefixes_by_id: dict[int, dict[str, set[str] | str]] = {}
    try:
        debug(f"NS API fetch: {family}/{lang}")
        _rate_wait()
        r = REQUEST_SESSION.get(url, params=params, timeout=10, headers=REQUEST_HEADERS)
        data = r.json() if r.status_code == 200 else {}
        ns_map = data.get('query', {}).get('namespaces', {}) or {}
        aliases = data.get('query', {}).get('namespacealiases', []) or []
        for sid, meta in ns_map.items():
            try:
                ns_id = int(sid)
            except Exception:
                continue
            # Пропускаем пространства обсуждений (нечётные ns) и отрицательные ns
            if ns_id % 2 == 1 or ns_id < 0:
                continue
            names: set[str] = set()
            local_name = (meta.get('*') or '').strip()
            canon = (meta.get('canonical') or '').strip()
            if local_name:
                names.add(local_name + ':')
            if canon:
                names.add(canon + ':')
            prefixes_by_id[ns_id] = {
                'primary': (local_name or canon) + ':' if (local_name or canon) else '',
                'all': {p.lower() for p in names if p}
            }
        for a in aliases:
            try:
                ns_id = int(a.get('id'))
                if ns_id % 2 == 1 or ns_id < 0:
                    continue
                name = (a.get('*') or '').strip()
                if not name:
                    continue
                d = prefixes_by_id.setdefault(ns_id, {'primary': '', 'all': set()})
                d['all'] = set(d.get('all') or set())
                d['all'].add((name + ':').lower())
            except Exception:
                continue
    except Exception:
        prefixes_by_id = {}
    if prefixes_by_id:
        NS_CACHE[key] = prefixes_by_id
        # Сохранение дискового кэша
        try:
            to_dump = {str(k): {'primary': str(v.get('primary') or ''), 'all': sorted([s for s in (v.get('all') or set())])}
                       for k, v in prefixes_by_id.items()}
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(to_dump, f, ensure_ascii=False)
            try:
                debug(f"NS cached: {family}/{lang} → {len(prefixes_by_id)} namespaces → {cache_path}")
            except Exception:
                pass
        except Exception:
            pass
        return prefixes_by_id
    # Предзаданные префиксы (резервный вариант)
    preset = DEFAULT_NS_PREFIXES.get((family or '', lang or ''))
    if isinstance(preset, dict) and preset:
        NS_CACHE[key] = preset
        try:
            to_dump = {str(k): {'primary': str(v.get('primary') or ''), 'all': sorted([s for s in (v.get('all') or set())])}
                       for k, v in preset.items()}
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(to_dump, f, ensure_ascii=False)
            try:
                debug(f"NS fallback preset used: {family}/{lang} → {len(preset)} namespaces")
            except Exception:
                pass
        except Exception:
            pass
        return preset
    return {}

def get_primary_ns_prefix(family: str, lang: str, ns_id: int, default_en: str) -> str:
    info = _load_ns_info(family, lang)
    prim = (info.get(ns_id, {}).get('primary') or '') if isinstance(info.get(ns_id, {}), dict) else ''
    prim = str(prim)
    return prim if prim else (default_en if default_en.endswith(':') else default_en + ':')

def title_has_ns_prefix(family: str, lang: str, title: str, ns_ids: set[int]) -> bool:
    info = _load_ns_info(family, lang)
    prefixes: set[str] = set()
    for i in ns_ids:
        d = info.get(i) or {}
        allp = d.get('all') or set()
        if isinstance(allp, set):
            prefixes |= allp
    t = (title or '').lstrip('\ufeff')
    lower = t.casefold()
    return any(lower.startswith(p) for p in prefixes)

def _has_en_prefix(title: str, ns_id: int) -> bool:
    lower = (title or '').lstrip('\ufeff').casefold()

    base = (DEFAULT_EN_NS.get(ns_id) or '').strip()
    candidates: set[str] = set()
    if base:
        candidates.add(base.casefold() if base.endswith(':') else (base + ':').casefold())
    candidates |= set(EN_PREFIX_ALIASES.get(ns_id, set()))
    return any(lower.startswith(p) for p in candidates) if candidates else False

def has_prefix_by_policy(family: str, lang: str, title: str, ns_ids: set[int]) -> bool:
    # Сначала проверяем локальные префиксы
    if title_has_ns_prefix(family, lang, title, ns_ids):
        return True
    # Затем английские
    return any(_has_en_prefix(title, ns) for ns in ns_ids)

def get_policy_prefix(family: str, lang: str, ns_id: int, default_en: str) -> str:
    # Если локальный основной известен — используем его
    local = get_primary_ns_prefix(family, lang, ns_id, '')
    if local:
        return local if local.endswith(':') else local + ':'
    # Резервный вариант — английский
    return default_en if default_en.endswith(':') else default_en + ':'

def _ensure_title_with_ns(title: str, family: str, lang: str, ns_id: int, default_en: str) -> str:
    t = (title or '').lstrip('\ufeff').strip()
    if not t:
        return t
    if has_prefix_by_policy(family, lang, t, {ns_id}):
        return t
    prefix = get_policy_prefix(family, lang, ns_id, default_en)
    return f"{prefix}{t}"

def normalize_title_by_selection(title: str, family: str, lang: str, selection: str | int) -> str:
    """Нормализует заголовок по выбранному пространству имён.

    Теперь при указанном NS добавляется локальный основной префикс,
    полученный из API/JSON. Английские префиксы в исходных названиях
    распознаются и считаются валидными.
    """
    base_title = (title or '').lstrip('\ufeff')
    try:
        if isinstance(selection, str):
            sel = selection.strip().lower()
            if sel in {'', 'auto'}:
                return base_title
            alias_to_ns = {'cat': 14, 'category': 14, 'tpl': 10, 'template': 10, 'art': 0, 'article': 0}
            ns_id = alias_to_ns.get(sel, int(sel))
        else:
            ns_id = int(selection)
    except Exception:
        return base_title

    if ns_id == 0:
        return base_title
    # Подставляем локальный основной префикс (резерв: английский из DEFAULT_EN_NS)
    default_en = DEFAULT_EN_NS.get(ns_id, '')
    return _ensure_title_with_ns(base_title, family, lang, ns_id, default_en)

# ===== UI Helpers =====
def _common_ns_ids() -> list[int]:
    # Подсчитываем в скольких языках встречается каждый NS-ID
    counts: dict[int, int] = {}
    for (fam, lng), mapping in DEFAULT_NS_PREFIXES.items():
        if fam != 'wikipedia':
            continue
        for ns_id in mapping.keys():
            counts[ns_id] = counts.get(ns_id, 0) + 1
    common = sorted([ns for ns, cnt in counts.items() if cnt >= 3])
    return common

def _primary_label_for_ns(family: str, lang: str, ns_id: int) -> str:
    # Пытаемся взять локальный основной префикс, иначе английский
    try:
        prim = get_primary_ns_prefix(family, lang, ns_id, DEFAULT_EN_NS.get(ns_id, ''))
        return prim[:-1] if prim.endswith(':') else prim
    except Exception:
        base = DEFAULT_EN_NS.get(ns_id, '')
        return base[:-1] if base.endswith(':') else base

def _adjust_combo_popup_width(combo) -> None:
    try:
        view = combo.view()
        fm = view.fontMetrics() if hasattr(view, 'fontMetrics') else combo.fontMetrics()
        max_w = 0
        for i in range(combo.count()):
            text = combo.itemText(i) or ''
            w = fm.horizontalAdvance(text)
            if w > max_w:
                max_w = w

        max_w += 48
        try:
            view.setMinimumWidth(max_w)
        except Exception:
            pass
    except Exception:
        pass

def _populate_ns_combo(combo, family: str, lang: str) -> None:
    try:
        combo.clear()
    except Exception:
        pass

    combo.addItem('Авто', 'auto')
    combo.addItem('(нет) [0]', 0)


    info = _load_ns_info(family or 'wikipedia', lang or 'ru')
    if info:
        ns_ids = sorted(info.keys())
    else:

        ns_ids = _common_ns_ids()

    for ns_id in ns_ids:
        if ns_id == 0:
            continue
        label = f"{_primary_label_for_ns(family, lang, ns_id)} [{ns_id}]"
        combo.addItem(label, ns_id)
    _adjust_combo_popup_width(combo)


 

# ===== Pywikibot Config Helpers =====
def _dist_configs_dir() -> str:
    """Фактическая папка configs рядом с exe/скриптом (для записи файлов)."""
    base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else tool_base_dir()
    return os.path.join(base, 'configs')

def config_base_dir() -> str:
    cfg = os.environ.get('PYWIKIBOT_DIR')
    if cfg and os.path.isabs(cfg):
        return cfg
    return _dist_configs_dir()

def write_pwb_credentials(lang: str, username: str, password: str, family: str = 'wikipedia') -> None:
    cfg_dir = _dist_configs_dir()
    os.makedirs(cfg_dir, exist_ok=True)
    uc_path = os.path.join(cfg_dir, 'user-config.py')

    usernames_map: dict[str, str] = {}
    if os.path.isfile(uc_path):
        try:
            with open(uc_path, 'r', encoding='utf-8') as f:
                txt = f.read()
            fam_re = re.escape(family)
            for m in re.finditer(rf"usernames\['{fam_re}'\]\['([^']+)'\]\s*=\s*'([^']+)'", txt):
                usernames_map[m.group(1)] = m.group(2)
        except Exception:
            usernames_map = {}

    usernames_map[lang] = username

    lines = [
        f"family = '{family}'",
        f"mylang = '{lang}'",
        "password_file = 'user-password.py'",
    ]
    for code in sorted(usernames_map.keys()):
        lines.append(f"usernames['{family}']['{code}'] = '{usernames_map[code]}'")
    debug(f"Write user-config.py → {uc_path}")
    with open(uc_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines) + "\n")

    up_path = os.path.join(cfg_dir, 'user-password.py')
    debug(f"Write user-password.py → {up_path}")
    with open(up_path, 'w', encoding='utf-8') as f:
        f.write(repr((username, password)))

def apply_pwb_config(lang: str, family: str = 'wikipedia') -> str:
    cfg_dir = _dist_configs_dir()
    os.makedirs(cfg_dir, exist_ok=True)

    try:
        throttle_path = os.path.join(cfg_dir, 'throttle.ctrl')
        if not os.path.isfile(throttle_path):
            with open(throttle_path, 'w', encoding='utf-8') as _f:
                _f.write('')
    except Exception:
        pass

    os.environ['PYWIKIBOT_DIR'] = cfg_dir
    pwb_config.base_dir = cfg_dir
    pwb_config.family = family
    pwb_config.mylang = lang
    pwb_config.password_file = os.path.join(cfg_dir, 'user-password.py')
    # Ускоряем операции записи pywikibot: убираем внутренние задержки между правками.
    # Кнопки записи в UI доступны только при наличии допуска AWB/обходе, поэтому это безопасно.
    try:
        pwb_config.put_throttle = 0.0
        pwb_config.maxlag = 5
    except Exception:
        pass
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

 

def fetch_awb_lists(lang: str, timeout: int = 15, family: str = 'wikipedia') -> tuple[str, dict | None]:
    """Загружает JSON со страницы Wikipedia:AutoWikiBrowser/CheckPageJSON для данного языка.

    Возвращает кортеж (state, data):
      - state: 'ok' | 'missing' | 'error'
      - data: словарь с ключами 'enabledusers'/'enabledbots' при state='ok', иначе None
    """
    url = build_project_awb_url(family, lang)
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

def is_user_awb_enabled(lang: str, username: str, family: str = 'wikipedia') -> tuple[bool, str]:
    """Проверяет, есть ли пользователь в списках AWB (люди или боты) на локальной вики.

    Возвращает (True, 'OK') если найден, иначе (False, сообщение об ошибке).
    """
    state, lists = fetch_awb_lists(lang, family=family)
    page_url = build_project_awb_url(family, lang)
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

# ===== Backend Helpers =====

DEFAULT_EN_NS: dict[int, str] = {
    # Основные пространства имён
    0:    'Main:',
    2:    'User:',
    4:    'Wikipedia:',
    6:    'File:',
    8:    'MediaWiki:',
    10:   'Template:',
    12:   'Help:',
    14:   'Category:',
    100:  'Portal:',
    102:  'Incubator:',
    104:  'Project:',
    710:  'TimedText:',
    828:  'Module:',
    1728: 'Event:',
    # Редкие пространства имён
    118:  'Draft:',
    126:  'MOS:',
}

# Английские алиасы префиксов
EN_PREFIX_ALIASES: dict[int, set[str]] = {
    0: {'main:', 'article:'},
    2: {'user:'},
    4: {'wikipedia:'},
    6: {'file:'},
    8: {'mediawiki:'},
    10: {'template:'},
    12: {'help:'},
    14: {'category:'},
    100: {'portal:'},
    102: {'incubator:'},
    104: {'project:'},
    118: {'draft:'},
    126: {'mos:'},
    710: {'timedtext:'},
    828: {'module:'},
    1728: {'event:'},
}

def fetch_content(title: str, ns_selection: str | int, lang: str = 'ru', family: str = 'wikipedia', retries: int = 5, timeout: int = 6):
    debug(f"API GET content lang={lang} title={title}")
    url = build_api_url(family, lang)
    # Нормализация заголовка по выбранному пространству
    full = (title or '').lstrip('\ufeff')
    try:
        if isinstance(ns_selection, str) and ns_selection == 'auto':
            pass
        else:
            ns_id = int(ns_selection)
            if ns_id != 0:
                default_en = DEFAULT_EN_NS.get(ns_id, '')
                full = _ensure_title_with_ns(full, family, lang, ns_id, default_en)
    except Exception:
        pass
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

# ===== Worker Threads =====

class ParseWorker(QThread):
    progress = Signal(str)

    def __init__(self, titles, out_path, ns_sel, lang, family):
        super().__init__()
        self.titles = titles
        self.out_path = out_path
        self.ns_sel = ns_sel
        self.lang = lang
        self.family = family
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

                    # Ограничиваем количество одновременных задач
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

                # Дождаться завершения оставшихся задач
                for fut in in_flight:
                    try:
                        fut.result()
                    except Exception:
                        pass


    def process(self, title, writer):
        if self._stop:
            return
        lines = fetch_content(title, self.ns_sel, lang=self.lang, family=self.family)
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

    def __init__(self, tsv_path, username, password, lang, family, ns_selection: str, summary, minor: bool):
        super().__init__()
        self.tsv_path = tsv_path
        self.username = username
        self.password = password
        self.lang = lang
        self.family = family
        self.ns_sel = ns_selection
        self.summary = summary
        self.minor = minor
        self._stop = False

    def request_stop(self):
        self._stop = True

    # --- Внутренний троттлинг сохранений (ускорить, но избегать 429) ---
    def _init_save_ratelimit(self):
        # Базовый минимальный интервал между сохранениями; будет адаптироваться
        self._save_min_interval = 0.25
        self._last_save_ts = 0.0

    def _wait_before_save(self):
        now = time.time()
        to_wait = max(0.0, (self._last_save_ts + self._save_min_interval) - now)
        if to_wait > 0:
            time.sleep(to_wait)
        self._last_save_ts = time.time()

    def _increase_save_interval(self, attempt: int):
        # Увеличиваем интервал агрессивно при срабатывании лимитов
        target = max(self._save_min_interval * 1.5, 0.6 * attempt)
        self._save_min_interval = min(target, 2.5)

    def _decay_save_interval(self):
        # Плавно уменьшаем интервал при стабильной работе
        self._save_min_interval = max(0.2, self._save_min_interval * 0.9)

    def _is_rate_error(self, err: Exception) -> bool:
        msg = (str(err) or '').lower()
        return (
            '429' in msg or 'too many requests' in msg or 'ratelimit' in msg or
            'rate limit' in msg or 'maxlag' in msg or 'readonly' in msg
        )

    def _save_with_retry(self, page: 'pywikibot.Page', text: str, summary: str, minor: bool, retries: int = 6) -> bool:
        for attempt in range(1, retries + 1):
            try:
                self._wait_before_save()
                page.text = text
                page.save(summary=summary, minor=minor)
                self._decay_save_interval()
                return True
            except Exception as e:
                if self._is_rate_error(e) and attempt < retries:
                    self._increase_save_interval(attempt)
                    try:
                        self.progress.emit(f"Лимит запросов: пауза {self._save_min_interval:.2f}s · попытка {attempt}/{retries}")
                    except Exception:
                        pass
                    continue
                try:
                    self.progress.emit(f"Ошибка сохранения: {type(e).__name__}: {e}")
                except Exception:
                    pass
                return False
        return False

    def run(self):
        site = pywikibot.Site(self.lang, self.family)
        debug(f'Login attempt replace lang={self.lang}')
        if self.username and self.password:
            try:
                if not is_bypass_awb():
                    ok_awb, msg_awb = is_user_awb_enabled(self.lang, self.username, self.family)
                    if not ok_awb:
                        self.progress.emit(f"AWB доступ отсутствует: {msg_awb}")
                        return

                site.login(user=self.username)
            except Exception as e:
                self.progress.emit(f"Ошибка авторизации: {type(e).__name__}: {e}")
                return
        # Инициализируем адаптивный интервал между сохранениями
        self._init_save_ratelimit()

        # Читаем как utf-8-sig, чтобы убрать BOM у первой ячейки
        with open(self.tsv_path, newline='', encoding='utf-8-sig') as f:
            reader = csv.reader(f, delimiter='\t')
            for row in reader:
                if self._stop:
                    break
                if len(row) < 2:
                    continue

                raw_title = row[0] if row[0] is not None else ''
                # Нормализуем заголовок и строки: убираем пробелы и возможный BOM
                title = raw_title.strip().lstrip('\ufeff')
                lines = [(s or '').lstrip('\ufeff') for s in row[1:]]
                # Нормализуем по выбору пользователя (Авто/Категория/Шаблон/Статья)
                norm_title = normalize_title_by_selection(title, self.family, self.lang, self.ns_sel)
                page = pywikibot.Page(site, norm_title)
                if page.exists():
                    ok = self._save_with_retry(page, "\n".join(lines), self.summary, self.minor)
                    if ok:
                        self.progress.emit(f"{title}: записано {len(lines)} строк")
                    else:
                        self.progress.emit(f"{title}: не удалось сохранить после повторных попыток")
                else:
                    self.progress.emit(f"{title}: страница отсутствует")


class CreateWorker(QThread):
    progress = Signal(str)

    def __init__(self, tsv_path, username, password, lang, family, ns_selection: str, summary, minor: bool):
        super().__init__()
        self.tsv_path = tsv_path
        self.username = username
        self.password = password
        self.lang = lang
        self.family = family
        self.ns_sel = ns_selection
        self.summary = summary
        self.minor = minor
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        site = pywikibot.Site(self.lang, self.family)
        debug(f'Login attempt create lang={self.lang}')
        if self.username and self.password:
            try:
                if not is_bypass_awb():
                    ok_awb, msg_awb = is_user_awb_enabled(self.lang, self.username, self.family)
                    if not ok_awb:
                        self.progress.emit(f"AWB доступ отсутствует: {msg_awb}")
                        return
                site.login(user=self.username)
            except Exception as e:
                self.progress.emit(f"Ошибка авторизации: {type(e).__name__}: {e}")
                return
        # Локальный адаптивный троттлинг сохранений
        save_min_interval = 0.25
        last_save_ts = 0.0

        def wait_before_save():
            nonlocal last_save_ts
            now = time.time()
            to_wait = max(0.0, (last_save_ts + save_min_interval) - now)
            if to_wait > 0:
                time.sleep(to_wait)
            last_save_ts = time.time()

        def increase_interval(attempt: int):
            nonlocal save_min_interval
            save_min_interval = min(max(save_min_interval * 1.5, 0.6 * attempt), 2.5)

        def decay_interval():
            nonlocal save_min_interval
            save_min_interval = max(0.2, save_min_interval * 0.9)

        def is_rate_error(err: Exception) -> bool:
            msg = (str(err) or '').lower()
            return ('429' in msg or 'too many requests' in msg or 'ratelimit' in msg or 'rate limit' in msg or 'maxlag' in msg or 'readonly' in msg)

        def save_with_retry(page: 'pywikibot.Page', text: str, retries: int = 6) -> bool:
            for attempt in range(1, retries + 1):
                try:
                    wait_before_save()
                    page.text = text
                    page.save(summary=self.summary, minor=self.minor)
                    decay_interval()
                    return True
                except Exception as e:
                    if is_rate_error(e) and attempt < retries:
                        increase_interval(attempt)
                        try:
                            self.progress.emit(f"Лимит запросов: пауза {save_min_interval:.2f}s · попытка {attempt}/{retries}")
                        except Exception:
                            pass
                        continue
                    try:
                        self.progress.emit(f"Ошибка сохранения: {type(e).__name__}: {e}")
                    except Exception:
                        pass
                    return False
            return False

        try:
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

                    norm_title = normalize_title_by_selection(title, self.family, self.lang, self.ns_sel)
                    page = pywikibot.Page(site, norm_title)
                    if not page.exists():
                        ok = save_with_retry(page, "\n".join(lines))
                        if ok:
                            self.progress.emit(f"{title}: создано ({len(lines)} строк)")
                        else:
                            self.progress.emit(f"{title}: не удалось создать после повторных попыток")
                    else:
                        self.progress.emit(f"{title}: уже существует")
        except Exception as e:
            self.progress.emit(f"Ошибка: {e}")


class RenameWorker(QThread):
    progress = Signal(str)
    template_review_request = Signal(object)
    review_response = Signal(object)

    def __init__(self, tsv_path, username, password, lang, family, ns_selection: str | int, leave_cat_redirect: bool, leave_other_redirect: bool, move_members: bool, find_in_templates: bool, phase1_enabled: bool, move_category: bool = True):
        super().__init__()
        self.tsv_path = tsv_path
        self.username = username
        self.password = password
        self.lang = lang
        self.family = family
        self.ns_sel = ns_selection
        self.leave_cat_redirect = leave_cat_redirect
        self.leave_other_redirect = leave_other_redirect
        self.move_members = move_members
        # Переименовывать ли саму категорию (если строка — категория)
        self.move_category = move_category
        # Фаза 2 (поиск в параметрах шаблонов)
        self.find_in_templates = find_in_templates
        # Фаза 1 (прямые ссылки на категорию)
        self.phase1_enabled = phase1_enabled
        self._stop = False
        # Автоподтверждение прямых совпадений категории в параметрах шаблонов (фаза 2)
        self.auto_confirm_direct_all: bool = False

        self._prompt_events: dict[int, Event] = {}
        self._prompt_results: dict[int, str] = {}
        self._req_seq = 0
        try:
            self.review_response.connect(self._on_review_response)
        except Exception:
            pass

    def request_stop(self):
        self._stop = True

    def run(self):
        site = pywikibot.Site(self.lang, self.family)
        debug(f'Login attempt rename lang={self.lang}')

        if self.username and self.password:
            try:
                if not is_bypass_awb():
                    ok_awb, msg_awb = is_user_awb_enabled(self.lang, self.username, self.family)
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
                    old_name_raw, new_name_raw, reason = [((c or '').strip().lstrip('\ufeff')) for c in row[:3]]

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

                    # Если переименовываем категорию и перенос содержимого (обе фазы) выключен —
                    # при отсутствии самой категории просто сообщаем и переходим к следующей строке
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
                            self._move_category_members(site, old_name, new_name)
                        except Exception as e:
                            self.progress.emit(f"Ошибка переноса содержимого категории '{old_name}': {e}")
        except Exception as e:
            self.progress.emit(f"Ошибка работы с файлом TSV: {e}")
        finally:
    
            pass

    def _move_page(self, site: pywikibot.Site, old_name: str, new_name: str, reason: str, leave_redirect: bool):
        try:
            page = pywikibot.Page(site, old_name)
            new_page = pywikibot.Page(site, new_name)
            if not page.exists():
                self.progress.emit(f"Страница <b>{html.escape(old_name)}</b> не найдена.")
                return
            if new_page.exists():
                self.progress.emit(f"Страница назначения <b>{html.escape(new_name)}</b> уже существует.")
                return

            # Адаптивный бэкофф для move
            move_min_interval = 0.25
            last_move_ts = 0.0

            def wait_before_move():
                nonlocal last_move_ts
                now = time.time()
                to_wait = max(0.0, (last_move_ts + move_min_interval) - now)
                if to_wait > 0:
                    time.sleep(to_wait)
                last_move_ts = time.time()

            def increase_interval(attempt: int):
                nonlocal move_min_interval
                move_min_interval = min(max(move_min_interval * 1.5, 0.6 * attempt), 2.5)

            def is_rate_error(err: Exception) -> bool:
                msg = (str(err) or '').lower()
                return ('429' in msg or 'too many requests' in msg or 'ratelimit' in msg or 'rate limit' in msg or 'maxlag' in msg or 'readonly' in msg)

            for attempt in range(1, 6):
                try:
                    wait_before_move()
                    # noredirect=True означает НЕ оставлять редирект
                    page.move(new_name, reason=reason, movetalk=True, noredirect=not leave_redirect)
                    redir_status = "с редиректом" if leave_redirect else "без редиректа"
                    self.progress.emit(f"Переименована <b>{html.escape(old_name)}</b> → <b>{html.escape(new_name)}</b> {redir_status}.")
                    break
                except Exception as e:
                    if is_rate_error(e) and attempt < 6:
                        increase_interval(attempt)
                        try:
                            self.progress.emit(f"Лимит при переименовании: пауза {move_min_interval:.2f}s · попытка {attempt}/5")
                        except Exception:
                            pass
                        continue
                    raise
        except Exception as e:
            self.progress.emit(f"Ошибка при переименовании <b>{html.escape(old_name)}</b> → <b>{html.escape(new_name)}</b>: {html.escape(str(e))}")

    def _cat_prefixes(self, family: str, lang: str) -> set[str]:
        info = _load_ns_info(family, lang)
        prefs = set(info.get(14, {}).get('all') or set())
        prefs |= set(EN_PREFIX_ALIASES.get(14, set()))
        return {(p if str(p).endswith(':') else str(p) + ':').lower() for p in prefs}

    def _strip_cat_prefix(self, title: str, family: str, lang: str) -> str:
        t = (title or '').lstrip('\ufeff').strip()
        lower = t.casefold()
        for p in sorted(self._cat_prefixes(family, lang), key=len, reverse=True):
            if lower.startswith(p):
                return t[len(p):].lstrip()
        return t

    def _replace_category_links_in_text(self, text: str, family: str, lang: str, old_cat_full: str, new_cat_full: str) -> tuple[str, int]:
        old_base = self._strip_cat_prefix(old_cat_full, family, lang)
        new_base = self._strip_cat_prefix(new_cat_full, family, lang)
        prefixes = self._cat_prefixes(family, lang)
        name_group = '(?:' + '|'.join(re.escape(p[:-1]) for p in prefixes) + ')'

        def esc_title_pat(t: str) -> str:
            pat = re.escape(t)
            pat = pat.replace(r'\ ', r'[ _]+').replace(r'\_', r'[ _]+')
            return pat

        pat_old = esc_title_pat(old_base)
        rx = re.compile(r'\[\[\s*(' + name_group + r')\s*:\s*' + pat_old + r'\s*(\|[^]]*)?\]\]', re.IGNORECASE)

        count = 0
        def repl(m):
            nonlocal count
            count += 1
            pref = m.group(1)
            tail = m.group(2) or ''
            return f'[[{pref}:{new_base}{tail}]_]'.replace(']_', ']]')

        new_text, _ = rx.subn(repl, text)
        return new_text, count

    def _find_template_param_category(self, text: str, old_cat_full: str) -> list[str]:
        """Грубый поиск вхождений old_cat_full внутри шаблонов {{...}} как значения параметра.
        Возвращает список строк полных вызовов шаблонов, где найдено совпадение.
        """
        results: list[str] = []
        seen: set[str] = set()
        if not old_cat_full:
            return results
        start_pos = 0
        while True:
            idx = text.find(old_cat_full, start_pos)
            if idx == -1:
                break

            l = text.rfind('{{', 0, idx)
            r = text.find('}}', idx)
            if l != -1 and r != -1 and r > l:
                chunk = text[l:r+2]
                if '|' in chunk:
                    if chunk not in seen:
                        seen.add(chunk)
                        results.append(chunk)
            start_pos = idx + len(old_cat_full)
        return results

    # ====== Поиск и предложения «по частям» для параметров шаблонов ======
    def _compute_partial_pairs(self, old_full: str, new_full: str, family: str, lang: str) -> list[tuple[str, str]]:
        """Строит список пар (old_piece -> new_piece) на основе различий между базовыми
        именами категорий без префиксов. Пары упорядочены от более длинных к более коротким.
        """
        try:
            old_base = self._strip_cat_prefix(old_full, family, lang)
            new_base = self._strip_cat_prefix(new_full, family, lang)
            sm = difflib.SequenceMatcher(a=old_base, b=new_base)
            pairs: list[tuple[str, str]] = []
            # В первую очередь — полная пара «всё старое имя» → «всё новое имя»
            if old_base and new_base and old_base != new_base:
                pairs.append((old_base, new_base))

            def _token_at(s: str, idx: int, direction: int) -> str:
                # Возвращает соседнее слово слева (direction=-1) или справа (direction=+1)
                if not s:
                    return ''
                if direction < 0:
                    left = s[:idx]
                    m = re.search(r"(\S+)\s*$", left)
                    return m.group(1) if m else ''
                else:
                    right = s[idx:]
                    m = re.match(r"^\s*(\S+)", right)
                    return m.group(1) if m else ''

            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag in ('replace',):
                    base_old = old_base[i1:i2]
                    base_new = new_base[j1:j2]
                    if base_old and base_new:
                        pairs.append((base_old, base_new))
                        # Попробуем расширить влево на короткий предлог (в/во/на/к/с/из/у/о/об/от/по/за/для/при)
                        left_old = _token_at(old_base, i1, -1)
                        left_new = _token_at(new_base, j1, -1)
                        if left_old and len(left_old) <= 3 and left_new and len(left_new) <= 3:
                            pairs.append((f"{left_old} {base_old}", f"{left_new} {base_new}"))
                        # Попробуем расширить вправо на одно слово (напр. «левом районе»)
                        right_old = _token_at(old_base, i2, +1)
                        right_new = _token_at(new_base, j2, +1)
                        if right_old and right_new:
                            pairs.append((f"{base_old} {right_old}", f"{base_new} {right_new}"))
                        # Попробуем расширить и слева и справа (предлог + слово + следующее слово)
                        if left_old and len(left_old) <= 3 and left_new and len(left_new) <= 3 and right_old and right_new:
                            pairs.append((f"{left_old} {base_old} {right_old}", f"{left_new} {base_new} {right_new}"))
                elif tag == 'delete':
                    base_old = old_base[i1:i2]
                    if base_old:
                        pairs.append((base_old, ''))
                elif tag == 'insert':
                    base_new = new_base[j1:j2]
                    if base_new:
                        # Вставка — специфично, добавим пару пустого в новый текст как справочную
                        pairs.append(('', base_new))

            # Удалим дубликаты и отсортируем по длине убыванию старого фрагмента
            seen_pairs: set[tuple[str, str]] = set()
            uniq: list[tuple[str, str]] = []
            for p in pairs:
                if p not in seen_pairs and p[0] != p[1]:
                    seen_pairs.add(p)
                    uniq.append(p)
            # Дополнительно: дифф по словам, чтобы получать целые словоформы
            def _words(s: str) -> list[str]:
                # Делим на слова (буквы/числа/подчёркивания) и прочие токены
                return re.findall(r"[^\W_\d]+|\d+|_+|[^\s]", s, flags=re.UNICODE)
            ow = _words(old_base)
            nw = _words(new_base)
            if ow and nw:
                smw = difflib.SequenceMatcher(a=[w.casefold() for w in ow], b=[w.casefold() for w in nw])
                for tag, i1, i2, j1, j2 in smw.get_opcodes():
                    if tag in ('replace',):
                        so = ''.join(ow[i1:i2]).strip()
                        sn = ''.join(nw[j1:j2]).strip()
                        if so and sn and (so, sn) not in seen_pairs:
                            seen_pairs.add((so, sn))
                            uniq.append((so, sn))
            uniq.sort(key=lambda x: len(x[0] or ''), reverse=True)
            return uniq
        except Exception:
            return []

    def _compile_wordish(self, text: str) -> re.Pattern:
        r"""Компилирует регэксп для поиска фразы по границам слов с гибкими пробелами.
        Пробелы в тексте сопоставляются как \s+, добавляются границы (?<!\w) и (?!\w).
        """
        t = re.sub(r"[\s\u00A0]+", " ", (text or '').strip())
        esc = re.escape(t).replace(r"\ ", r"[\s\u00A0]+")
        return re.compile(rf"(?<!\w){esc}(?!\w)", re.IGNORECASE | re.UNICODE)

    def _norm_space_fold(self, s: str) -> str:
        try:
            return re.sub(r"[\s\u00A0]+", " ", (s or '').strip()).casefold()
        except Exception:
            return (s or '').strip().lower()

    def _replace_in_unnamed_params_once(self, template_chunk: str, old_sub: str, new_sub: str, replace_entire_token: bool = False) -> tuple[str, bool]:
        """Заменяет ОДНО вхождение в безымянных параметрах шаблона.
        Если replace_entire_token=True — целиком заменяет значение параметра на new_sub,
        иначе выполняет точечную подстановку old_sub→new_sub внутри параметра.
        Возвращает (новый_шаблон, были_ли_изменения).
        """
        try:
            if '{{' not in template_chunk or '}}' not in template_chunk or '|' not in template_chunk:
                return template_chunk, False
            inner = template_chunk[2:-2]
            parts = inner.split('|')
            if not parts:
                return template_chunk, False
            head = parts[0]
            tail = parts[1:]
            changed = False
            # Только по границам слов/фраз, с гибкими пробелами
            ci_old_word = self._compile_wordish(old_sub)
            indices: list[int] = []
            for idx, token in enumerate(tail):
                if '=' not in token and ci_old_word.search(token):
                    indices.append(idx)

            def _starts_with_short_word(tok: str) -> bool:
                # Универсально: слово 1–3 букв (любой язык, исключая цифры/подчёркивания), затем пробел
                t = tok.lstrip()
                return bool(re.match(r'^([^\W\d_]{1,3})\s', t, flags=re.UNICODE))

            # Сортируем: сначала параметры, начинающиеся с короткого слова (обычно предлоги), затем остальные
            indices.sort(key=lambda i: (0 if _starts_with_short_word(tail[i]) else 1, i))

            for idx in indices:
                token = tail[idx]
                if replace_entire_token:
                    # Не заменяем целиком, если новая подстрока пустая/короткая
                    # или параметр не совпадает полностью со старым фрагментом (после нормализации пробелов)
                    if not new_sub or len(old_sub.strip()) < 2:
                        pass
                    else:
                        norm = lambda s: re.sub(r"\s+", " ", (s or '').strip()).casefold()
                        if norm(token) == norm(old_sub):
                            tail[idx] = new_sub
                            changed = True
                            break
                if not changed:
                    new_token, n = ci_old_word.subn(new_sub, token, count=1)
                    if n > 0:
                        tail[idx] = new_token
                        changed = True
                        break
            if changed:
                new_inner = '|'.join([head] + tail)
                return '{{' + new_inner + '}}', True
            return template_chunk, False
        except Exception:
            return template_chunk, False

    def _find_template_param_partial(self, text: str, old_full: str, new_full: str, family: str, lang: str) -> list[dict]:
        """Ищет кандидатов в шаблонах, где в безымянных параметрах встречаются части старого названия.
        Возвращает список словарей: {'template', 'proposed_template', 'old_sub', 'new_sub'}
        """
        results: list[dict] = []
        try:
            old_base = self._strip_cat_prefix(old_full, family, lang)
            new_base = self._strip_cat_prefix(new_full, family, lang)
            pairs = self._compute_partial_pairs(old_full, new_full, family, lang)
            if not pairs:
                return results
            seen_templates: set[str] = set()
            # Находим грубо все куски шаблонов
            start = 0
            while True:
                l = text.find('{{', start)
                if l == -1:
                    break
                r = text.find('}}', l + 2)
                if r == -1:
                    break
                chunk = text[l:r+2]
                start = r + 2
                if '|' not in chunk:
                    continue
                if chunk in seen_templates:
                    continue
                # 0) Поиск точного значения категории в ИМЕНОВАННЫХ параметрах (с или без префикса)
                try:
                    inner = chunk[2:-2]
                    parts = inner.split('|')
                    head = parts[0]
                    params = parts[1:]
                    prefixes = self._cat_prefixes(family, lang)
                    def _norm(s: str) -> str:
                        return re.sub(r"\s+", " ", (s or '').strip()).replace('_', ' ').casefold()
                    norm_old_base = _norm(old_base)
                    for i, raw in enumerate(params):
                        if '=' not in raw:
                            continue
                        m = re.match(r"^(?P<left>\s*[^=]+?)(?P<eq>\s*=\s*)(?P<val>.*)$", raw)
                        if not m:
                            continue
                        val = m.group('val').strip()
                        val_no_pref = val
                        # Снимем возможный префикс пространства имён
                        val_lower = val.casefold()
                        for p in sorted(prefixes, key=len, reverse=True):
                            p_no_colon = p[:-1]
                            if val_lower.startswith(p_no_colon):
                                # допускаем варианты без двоеточия/со пробелами
                                m2 = re.match(rf"^{re.escape(p_no_colon)}\s*:\s*(.+)$", val, flags=re.IGNORECASE)
                                if m2:
                                    val_no_pref = m2.group(1).strip()
                                break
                        if _norm(val_no_pref) == norm_old_base:
                            # Предлагаем замену, сохраняя стиль (с префиксом или без)
                            new_val = val
                            # если было с префиксом, заменим только хвост
                            if val_no_pref != val:
                                new_val = re.sub(r"(:\s*)(.+)$", lambda mm: f"{mm.group(1)}{new_base}", val, count=1)
                            else:
                                new_val = new_base
                            new_param = f"{m.group('left')}{m.group('eq')}{new_val}"
                            new_inner = '|'.join([head] + params[:i] + [new_param] + params[i+1:])
                            proposed = '{{' + new_inner + '}}'
                            seen_templates.add(chunk)
                            results.append({
                                'template': chunk,
                                'proposed_template': proposed,
                                'old_sub': val,
                                'new_sub': new_val,
                            })
                            continue
                except Exception:
                    pass
                for old_sub, new_sub in pairs:
                    if not old_sub:
                        continue
                    pat = self._compile_wordish(old_sub)
                    if pat.search(chunk) or (self._norm_space_fold(old_sub) in self._norm_space_fold(chunk)):
                        # Сначала пробуем заменить целиком совпадающий параметр, если весь токен равен old_sub
                        new_chunk, changed = self._replace_in_unnamed_params_once(chunk, old_sub, new_sub or '', replace_entire_token=True)
                        if not changed:
                            # Если не вышло — fallback на точечную подстановку внутри параметра
                            new_chunk, changed = self._replace_in_unnamed_params_once(chunk, old_sub, new_sub or '', replace_entire_token=False)
                        if changed:
                            seen_templates.add(chunk)
                            results.append({
                                'template': chunk,
                                'proposed_template': new_chunk,
                                'old_sub': old_sub,
                                'new_sub': new_sub,
                            })
                            break
                # Дополнительно, если ничего не найдено по парам, пробуем упрощённое правило «предлог+слово → предлог+слово»
                if chunk not in seen_templates:
                    # извлекаем безымянные параметры
                    inner = chunk[2:-2]
                    parts = inner.split('|')
                    unnamed = [p for p in parts[1:] if '=' not in p]
                    # соберём все предлоговые фразы длиной 2 токена минимум
                    for tok in unnamed:
                        m = re.search(r"(^|\s)([^\W\d_]{1,3})\s+([^|]+)$", tok.strip(), flags=re.UNICODE)
                        if m:
                            short = m.group(2)
                            rest = m.group(3).strip()
                            # ищем соответствие старой/новой фразы по словам
                            for old_sub, new_sub in pairs:
                                if not old_sub or not new_sub:
                                    continue
                                # старое/новое должно начинаться с короткого слова
                                mo = re.match(rf"^([^\W\d_]{{1,3}})\s+(.+)$", old_sub.strip(), flags=re.UNICODE)
                                mn = re.match(rf"^([^\W\d_]{{1,3}})\s+(.+)$", new_sub.strip(), flags=re.UNICODE)
                                if not mo or not mn:
                                    continue
                                if mo.group(1).casefold() == short.casefold():
                                    # если хвосты совпадают по началу/слову — предлагаем замену целиком
                                    if self._norm_space_fold(rest).startswith(self._norm_space_fold(mo.group(2))[:1]):
                                        new_tail = f"{short} {mn.group(2)}"
                                        new_chunk, changed = self._replace_in_unnamed_params_once(chunk, tok, new_tail, replace_entire_token=True)
                                        if changed:
                                            seen_templates.add(chunk)
                                            results.append({
                                                'template': chunk,
                                                'proposed_template': new_chunk,
                                                'old_sub': tok,
                                                'new_sub': new_tail,
                                            })
                                            break
            return results
        except Exception:
            return results

    def _on_review_response(self, payload: object) -> None:
        try:
            data = payload or {}
            req_id = int(data.get('request_id'))
            ev = self._prompt_events.get(req_id)
            if ev is not None:
                # Сохраняем весь ответ (включая возможное edited_template)
                self._prompt_results[req_id] = data
                ev.set()
        except Exception:
            pass

    def _prompt_user_template_replace(self, page_title: str, template_str: str, old_full: str, new_full: str,
                                      proposed_template: str | None = None,
                                      old_sub: str | None = None,
                                      new_sub: str | None = None,
                                      old_direct: str | None = None,
                                      new_direct: str | None = None) -> dict:
        """Синхронно запрашивает у пользователя подтверждение.
        Возвращает словарь с полями: {'action': 'confirm'|'skip'|'cancel', 'edited_template'?: str}
        """
        try:
            self._req_seq += 1
            req_id = self._req_seq
            ev = Event()
            self._prompt_events[req_id] = ev
            self._prompt_results[req_id] = {}
            self.template_review_request.emit({
                'request_id': req_id,
                'page_title': page_title,
                'template': template_str,
                'old_full': old_full,
                'new_full': new_full,
                'mode': 'partial' if proposed_template is not None else 'direct',
                'proposed_template': proposed_template or '',
                'old_sub': old_sub or '',
                'new_sub': new_sub or '',
                'old_direct': old_direct or '',
                'new_direct': new_direct or '',
            })

            while not self._stop and not ev.wait(0.1):
                pass
            result = self._prompt_results.get(req_id, {}) or {}

            self._prompt_events.pop(req_id, None)
            self._prompt_results.pop(req_id, None)
            action = str(result.get('action') or '') or 'skip'
            result['action'] = action
            return result
        except Exception:
            return {'action': 'skip'}

    def _move_category_members(self, site: pywikibot.Site, old_name: str, new_name: str):
        family, lang = self.family, self.lang
        old_full = _ensure_title_with_ns(old_name, family, lang, 14, DEFAULT_EN_NS.get(14, 'Category:'))
        new_full = _ensure_title_with_ns(new_name, family, lang, 14, DEFAULT_EN_NS.get(14, 'Category:'))

        # Если страница категории отсутствует, но есть участники — сообщаем и всё равно начнём перенос
        try:
            try:
                old_page_exists = pywikibot.Page(site, old_full).exists()
            except Exception:
                old_page_exists = None
            if old_page_exists is False:
                api_check = build_api_url(family, lang)
                params_check = {
                    'action': 'query',
                    'list': 'categorymembers',
                    'cmtitle': old_full,
                    'cmlimit': '1',
                    'cmprop': 'ids',
                    'format': 'json'
                }
                _rate_wait()
                r_check = REQUEST_SESSION.get(api_check, params=params_check, timeout=20, headers=REQUEST_HEADERS)
                if r_check.status_code == 200:
                    data_check = r_check.json()
                    has_any = bool(data_check.get('query', {}).get('categorymembers') or [])
                    if has_any:
                        try:
                            self.progress.emit(f"Категория <b>{html.escape(old_full)}</b> не существует, но в ней есть содержимое — пытаемся перенести в <b>{html.escape(new_full)}</b>.")
                        except Exception:
                            self.progress.emit(f"Категория {old_full} не существует, но в ней есть содержимое — пытаемся перенести в {new_full}.")
        except Exception:
            pass

        api = build_api_url(family, lang)
        params = {
            'action': 'query',
            'list': 'categorymembers',
            'cmtitle': old_full,
            'cmlimit': 'max',
            'cmprop': 'title',
            'format': 'json'
        }
        moved_direct = 0
        moved_via_template = 0
        backlog: list[str] = []
        backlog_seen: set[str] = set()

        # Вспомогательная функция: обработка одной страницы по Фазе 2 (поиск в параметрах шаблонов)
        def _process_title_templates(title: str):
            nonlocal moved_via_template, write_min_interval, last_write_ts
            if self._stop:
                return
            try:
                page = pywikibot.Page(site, title)
                if not page.exists():
                    return
                txt = page.text
                visited = set()
                made_change = False
                direct_seen = False
                # Поиск «прямых» совпадений: значение параметра равно категории (полной или «голой»)
                def _find_equal_template_candidates(text_src: str, old_full_cat: str) -> list[tuple[str, str, str]]:
                    results: list[tuple[str, str, str]] = []
                    try:
                        if not old_full_cat:
                            return results
                        old_bare = old_full_cat.split(':', 1)[1] if ':' in old_full_cat else old_full_cat
                        new_bare = new_full.split(':', 1)[1] if ':' in new_full else new_full
                        patterns = [
                            (old_full_cat, new_full),
                            (old_bare, new_bare)
                        ]
                        for old_tok, new_tok in patterns:
                            start = 0
                            # Ищем в тексте вхождения значения параметра вида: "| old" или "| name = old"
                            rx = re.compile(r"\|\s*(?:[^|{}=\n]+\s*=\s*)?" + re.escape(old_tok) + r"\s*(?=\||}})", re.S)
                            while True:
                                m = rx.search(text_src, start)
                                if not m:
                                    break
                                idx = m.start()
                                l = text_src.rfind('{{', 0, idx)
                                r = text_src.find('}}', idx)
                                if l != -1 and r != -1 and r > l:
                                    chunk = text_src[l:r+2]
                                    if '|' in chunk and (chunk, old_tok, new_tok) not in results:
                                        results.append((chunk, old_tok, new_tok))
                                start = m.end()
                    except Exception:
                        pass
                    return results

                # 1) Прямые указания полной категории в шаблонах (только случаи равенства значения параметра)
                auto_applied = 0
                while not self._stop:
                    eq_list = _find_equal_template_candidates(txt, old_full)
                    # исключаем уже показанные кандидаты
                    eq_list = [t for t in eq_list if t[0] not in visited]
                    if not eq_list:
                        break
                    direct_seen = True
                    tmpl, old_token, new_token = eq_list[0]
                    visited.add(tmpl)
                    # Автоподтверждение прямых совпадений
                    if getattr(self, 'auto_confirm_direct_all', False):
                        try:
                            new_tmpl = tmpl.replace(old_token, new_token, 1)
                            new_txt = txt.replace(tmpl, new_tmpl, 1)
                            if new_txt != txt:
                                # сохранение с адаптивным бэкоффом
                                for attempt in range(1, 6):
                                    try:
                                        now2 = time.time()
                                        wait2 = max(0.0, (last_write_ts + write_min_interval) - now2)
                                        if wait2 > 0:
                                            time.sleep(wait2)
                                        page.text = new_txt
                                        page.save(summary=f"[[{old_full}]] → [[{new_full}]] (исправление категоризации через параметр шаблона)", minor=True)
                                        write_min_interval = max(0.2, write_min_interval * 0.9)
                                        last_write_ts = time.time()
                                        break
                                    except Exception as e:
                                        msg = (str(e) or '').lower()
                                        if any(x in msg for x in ('429', 'too many requests', 'ratelimit', 'rate limit', 'maxlag', 'readonly')) and attempt < 5:
                                            write_min_interval = min(max(write_min_interval * 1.5, 0.6 * attempt), 2.5)
                                            debug(f"Template save backoff: {write_min_interval:.2f}s · attempt {attempt}")
                                            continue
                                        raise
                                txt = new_txt
                                moved_via_template += 1
                                made_change = True
                                auto_applied += 1
                                try:
                                    nsid = page.namespace().id
                                    typ = 'категория' if nsid == 14 else 'статья'
                                except Exception:
                                    typ = 'страница'
                                self.progress.emit(f"→ {new_full} → {title}: {typ} перенесена")
                        except Exception as e:
                            self.progress.emit(f"{title}: ошибка правки шаблона: {e}")
                        continue
                    # Диалог подтверждения (с флажком автоподтверждения для полных совпадений)
                    result = self._prompt_user_template_replace(title, tmpl, old_full, new_full, old_direct=old_token, new_direct=new_token)
                    action = result.get('action') if isinstance(result, dict) else str(result)
                    if action == 'cancel':
                        self._stop = True
                        break
                    try:
                        if action == 'confirm' and isinstance(result, dict) and bool(result.get('auto_confirm_all')):
                            self.auto_confirm_direct_all = True
                    except Exception:
                        pass
                    if action == 'confirm':
                        try:
                            edited = str(result.get('edited_template') or '') if isinstance(result, dict) else ''
                            repl = edited if edited.strip() else tmpl.replace(old_token, new_token, 1)
                            new_tmpl = repl
                            new_txt = txt.replace(tmpl, new_tmpl, 1)
                            if new_txt != txt:
                                for attempt in range(1, 6):
                                    try:
                                        now2 = time.time()
                                        wait2 = max(0.0, (last_write_ts + write_min_interval) - now2)
                                        if wait2 > 0:
                                            time.sleep(wait2)
                                        page.text = new_txt
                                        page.save(summary=f"[[{old_full}]] → [[{new_full}]] (исправление категоризации через параметр шаблона)", minor=True)
                                        write_min_interval = max(0.2, write_min_interval * 0.9)
                                        last_write_ts = time.time()
                                        break
                                    except Exception as e:
                                        msg = (str(e) or '').lower()
                                        if any(x in msg for x in ('429', 'too many requests', 'ratelimit', 'rate limit', 'maxlag', 'readonly')) and attempt < 5:
                                            write_min_interval = min(max(write_min_interval * 1.5, 0.6 * attempt), 2.5)
                                            debug(f"Template save backoff: {write_min_interval:.2f}s · attempt {attempt}")
                                            continue
                                        raise
                                txt = new_txt
                                moved_via_template += 1
                                made_change = True
                                try:
                                    nsid = page.namespace().id
                                    typ = 'категория' if nsid == 14 else 'статья'
                                except Exception:
                                    typ = 'страница'
                                self.progress.emit(f"→ {new_full} → {title}: {typ} перенесена")
                        except Exception as e:
                            self.progress.emit(f"{title}: ошибка правки шаблона: {e}")

                # Если на странице были автоприменения (после включения галочки) — короткая сводка
                if auto_applied > 0 and not self._stop:
                    try:
                        self.progress.emit(f"Автоприменено {auto_applied} замен(ы) на странице {html.escape(title)}")
                    except Exception:
                        self.progress.emit(f"Автоприменено {auto_applied} замен(ы) на странице {title}")
                # 2) Поиск по частям — только если на странице не было ни одного полного совпадения
                if not self._stop and not made_change and not direct_seen:
                    partial_seen: set[str] = set()
                    while not self._stop:
                        cand_list = self._find_template_param_partial(txt, old_full, new_full, family, lang)
                        cand_list = [c for c in cand_list if c.get('template') not in partial_seen]
                        if not cand_list:
                            self.progress.emit(f"{title}: прямое указание категории в параметрах и совпадения по частям не найдены")
                            break
                        c0 = cand_list[0]
                        partial_seen.add(str(c0.get('template')))
                        result = self._prompt_user_template_replace(
                            title,
                            str(c0.get('template') or ''),
                            old_full,
                            new_full,
                            proposed_template=str(c0.get('proposed_template') or ''),
                            old_sub=str(c0.get('old_sub') or ''),
                            new_sub=str(c0.get('new_sub') or ''),
                        )
                        action = result.get('action') if isinstance(result, dict) else str(result)
                        if action == 'cancel':
                            self._stop = True
                            break
                        if action == 'confirm':
                            try:
                                edited = str(result.get('edited_template') or '') if isinstance(result, dict) else ''
                                replacement = edited if edited.strip() else str(c0.get('proposed_template') or '')
                                tmpl_old = str(c0.get('template') or '')
                                if replacement and tmpl_old and replacement != tmpl_old:
                                    new_txt = txt.replace(tmpl_old, replacement, 1)
                                    if new_txt != txt:
                                        for attempt in range(1, 6):
                                            try:
                                                now2 = time.time()
                                                wait2 = max(0.0, (last_write_ts + write_min_interval) - now2)
                                                if wait2 > 0:
                                                    time.sleep(wait2)
                                                page.text = new_txt
                                                page.save(summary=f"[[{old_full}]] → [[{new_full}]] (исправление категоризации через параметр шаблона)", minor=True)
                                                write_min_interval = max(0.2, write_min_interval * 0.9)
                                                last_write_ts = time.time()
                                                break
                                            except Exception as e:
                                                msg = (str(e) or '').lower()
                                                if any(x in msg for x in ('429', 'too many requests', 'ratelimit', 'rate limit', 'maxlag', 'readonly')) and attempt < 5:
                                                    write_min_interval = min(max(write_min_interval * 1.5, 0.6 * attempt), 2.5)
                                                    debug(f"Template partial save backoff: {write_min_interval:.2f}s · attempt {attempt}")
                                                    continue
                                                raise
                                        txt = new_txt
                                        moved_via_template += 1
                                        made_change = True
                                        try:
                                            nsid = page.namespace().id
                                            typ = 'категория' if nsid == 14 else 'статья'
                                        except Exception:
                                            typ = 'страница'
                                        self.progress.emit(f"→ {new_full} → {title}: {typ} перенесена (частичная замена)")
                            except Exception as e:
                                self.progress.emit(f"{title}: ошибка правки шаблона (частично): {e}")
            except Exception as e:
                self.progress.emit(f"{title}: ошибка обработки на ручной фазе: {e}")

        # Адаптивные интервалы для чтения и записи
        read_min_interval = 0.15
        last_read_ts = 0.0
        write_min_interval = 0.25
        last_write_ts = 0.0

        # Счётчик прогресса Фазы 1 (для отображения «живости» процесса)
        scanned_phase1_count = 0
        phase1_progress_logged = False

        while not self._stop:
            # локальный адаптивный троттлинг чтения списка участников
            now = time.time()
            to_wait = max(0.0, (last_read_ts + read_min_interval) - now)
            if to_wait > 0:
                time.sleep(to_wait)
            last_read_ts = time.time()
            # Стартовое сообщение о начале Фазы 1
            if self.phase1_enabled and not phase1_progress_logged:
                try:
                    self.progress.emit("Сканируем прямые вхождения категорий…")
                except Exception:
                    pass
                phase1_progress_logged = True

            r = REQUEST_SESSION.get(api, params=params, timeout=20, headers=REQUEST_HEADERS)
            if r.status_code == 429:
                read_min_interval = min(max(read_min_interval * 1.7, 0.6), 2.0)
                debug(f"Read members backoff: {read_min_interval:.2f}s")
                continue
            if r.status_code != 200:
                try:
                    self.progress.emit(f"Ошибка API при получении участников категории <b>{html.escape(old_full)}</b>: HTTP {r.status_code}")
                except Exception:
                    self.progress.emit(f"Ошибка API при получении участников категории {old_full}: HTTP {r.status_code}")
                break
            data = r.json()
            members = [m.get('title') for m in data.get('query', {}).get('categorymembers', []) if m.get('title')]

            # Режим «только по шаблонам»: начинаем обрабатывать сразу, без лишнего чтения страниц в Фазе 1
            if self.find_in_templates and not self.phase1_enabled:
                for title in members:
                    if self._stop:
                        break
                    _process_title_templates(title)
                if 'continue' in data:
                    params.update(data['continue'])
                    continue
                else:
                    break
            for title in members:
                if self._stop:
                    break
                try:
                    any_changed = False
                    targets = {title}
                    try:
                        page_obj = pywikibot.Page(site, title)
                        ns_id = page_obj.namespace().id
                    except Exception:
                        ns_id = None

                    if isinstance(ns_id, int) and ns_id in (10, 828):  # Template:, Module:
                        if title.endswith('/doc'):
                            base = title[:-4]
                            targets.add(base)
                        else:
                            targets.add(f"{title}/doc")

                    for t in targets:
                        if self._stop:
                            break
                        try:
                            page = pywikibot.Page(site, t)
                            if not page.exists():
                                continue
                            txt = page.text
                            new_txt, cnt = (txt, 0)
                            if self.phase1_enabled:
                                new_txt, cnt = self._replace_category_links_in_text(txt, family, lang, old_full, new_full)
                            if cnt > 0 and new_txt != txt:
                                # сохранение с адаптивным бэкоффом
                                saved = False
                                for attempt in range(1, 6):
                                    try:
                                        now2 = time.time()
                                        wait2 = max(0.0, (last_write_ts + write_min_interval) - now2)
                                        if wait2 > 0:
                                            time.sleep(wait2)
                                        page.text = new_txt
                                        page.save(summary=f"[[{old_full}]] → [[{new_full}]]", minor=True)
                                        write_min_interval = max(0.2, write_min_interval * 0.9)
                                        last_write_ts = time.time()
                                        saved = True
                                        break
                                    except Exception as e:
                                        msg = (str(e) or '').lower()
                                        if any(x in msg for x in ('429', 'too many requests', 'ratelimit', 'rate limit', 'maxlag', 'readonly')) and attempt < 5:
                                            write_min_interval = min(max(write_min_interval * 1.5, 0.6 * attempt), 2.5)
                                            debug(f"Move member save backoff: {write_min_interval:.2f}s · attempt {attempt}")
                                            continue
                                        raise
                                if not saved:
                                    continue
                                any_changed = True
                                moved_direct += 1

                                try:
                                    nsid = page.namespace().id
                                    typ = 'категория' if nsid == 14 else 'статья'
                                except Exception:
                                    typ = 'страница'
                                try:
                                    self.progress.emit(f"▪️ {html.escape(new_full)} → {html.escape(t)}: {typ} перенесена")
                                except Exception:
                                    self.progress.emit(f"▪️ {new_full} → {t}: {typ} перенесена")
                        except Exception as e:
                            self.progress.emit(f"{t}: ошибка переноса категории: {e}")
                    # Если правки по Фазе 1 не было, а Фаза 2 включена — добавляем в backlog
                    if not any_changed and self.find_in_templates and title not in backlog_seen:
                        backlog.append(title)
                        backlog_seen.add(title)
                except Exception as e:
                    self.progress.emit(f"{title}: ошибка обработки: {e}")

                # Обновление прогресса Фазы 1 каждые 10 проверенных страниц
                if self.phase1_enabled:
                    scanned_phase1_count += 1
                    if scanned_phase1_count % 10 == 0:
                        try:
                            self.progress.emit(f"🔹 Проверено {scanned_phase1_count}, прямых замен {moved_direct}")
                        except Exception:
                            pass
            if 'continue' in data:
                params.update(data['continue'])
            else:
                break

        if self._stop:
            return

        if backlog and self.find_in_templates:
            self.progress.emit(f"Не удалось перенести автоматически: {len(backlog)} страниц(ы). Требуются ручные действия.")

        # Фаза 2: поиск в параметрах шаблонов с подтверждением пользователя
        for title in (backlog if self.find_in_templates else []):
            if self._stop:
                break
            try:
                page = pywikibot.Page(site, title)
                if not page.exists():
                    continue
                txt = page.text
                visited = set()
                made_change = False
                # 1) Сначала пытаемся найти прямые указания полной категории в шаблонах
                while not self._stop:
                    candidates = self._find_template_param_category(txt, old_full)
                    candidates = [c for c in candidates if c not in visited]
                    if not candidates:
                        break
                    tmpl = candidates[0]
                    visited.add(tmpl)
                    # Если включено автоподтверждение для прямых совпадений — применяем без диалога
                    if getattr(self, 'auto_confirm_direct_all', False):
                        try:
                            new_tmpl = tmpl.replace(old_full, new_full)
                            new_txt = txt.replace(tmpl, new_tmpl, 1)
                            if new_txt != txt:
                                # сохранение с адаптивным бэкоффом
                                for attempt in range(1, 6):
                                    try:
                                        now2 = time.time()
                                        wait2 = max(0.0, (last_write_ts + write_min_interval) - now2)
                                        if wait2 > 0:
                                            time.sleep(wait2)
                                        page.text = new_txt
                                        page.save(summary=f"[[{old_full}]] → [[{new_full}]] (исправление категоризации через параметр шаблона)", minor=True)
                                        write_min_interval = max(0.2, write_min_interval * 0.9)
                                        last_write_ts = time.time()
                                        break
                                    except Exception as e:
                                        msg = (str(e) or '').lower()
                                        if any(x in msg for x in ('429', 'too many requests', 'ratelimit', 'rate limit', 'maxlag', 'readonly')) and attempt < 5:
                                            write_min_interval = min(max(write_min_interval * 1.5, 0.6 * attempt), 2.5)
                                            debug(f"Template save backoff: {write_min_interval:.2f}s · attempt {attempt}")
                                            continue
                                        raise
                                txt = new_txt
                                moved_via_template += 1
                                made_change = True
                                try:
                                    nsid = page.namespace().id
                                    typ = 'категория' if nsid == 14 else 'статья'
                                except Exception:
                                    typ = 'страница'
                                self.progress.emit(f"→ {new_full} → {title}: {typ} перенесена")
                        except Exception as e:
                            self.progress.emit(f"{title}: ошибка правки шаблона: {e}")
                        # Переходим к следующему кандидату
                        continue
                    result = self._prompt_user_template_replace(title, tmpl, old_full, new_full)
                    action = result.get('action') if isinstance(result, dict) else str(result)
                    if action == 'cancel':
                        self._stop = True
                        break
                    # Включаем авто-подтверждение для всех последующих прямых совпадений, если пользователь так решил
                    try:
                        if action == 'confirm' and isinstance(result, dict) and bool(result.get('auto_confirm_all')):
                            self.auto_confirm_direct_all = True
                    except Exception:
                        pass
                    if action == 'confirm':
                        try:
                            edited = str(result.get('edited_template') or '') if isinstance(result, dict) else ''
                            new_tmpl = edited if edited.strip() else tmpl.replace(old_full, new_full)
                            new_txt = txt.replace(tmpl, new_tmpl, 1)
                            if new_txt != txt:
                                # сохранение с адаптивным бэкоффом
                                for attempt in range(1, 6):
                                    try:
                                        now2 = time.time()
                                        wait2 = max(0.0, (last_write_ts + write_min_interval) - now2)
                                        if wait2 > 0:
                                            time.sleep(wait2)
                                        page.text = new_txt
                                        page.save(summary=f"[[{old_full}]] → [[{new_full}]] (исправление категоризации через параметр шаблона)", minor=True)
                                        write_min_interval = max(0.2, write_min_interval * 0.9)
                                        last_write_ts = time.time()
                                        break
                                    except Exception as e:
                                        msg = (str(e) or '').lower()
                                        if any(x in msg for x in ('429', 'too many requests', 'ratelimit', 'rate limit', 'maxlag', 'readonly')) and attempt < 5:
                                            write_min_interval = min(max(write_min_interval * 1.5, 0.6 * attempt), 2.5)
                                            debug(f"Template save backoff: {write_min_interval:.2f}s · attempt {attempt}")
                                            continue
                                        raise
                                txt = new_txt
                                moved_via_template += 1
                                made_change = True
                                try:
                                    nsid = page.namespace().id
                                    typ = 'категория' if nsid == 14 else 'статья'
                                except Exception:
                                    typ = 'страница'
                                self.progress.emit(f"→ {new_full} → {title}: {typ} перенесена")
                        except Exception as e:
                            self.progress.emit(f"{title}: ошибка правки шаблона: {e}")
                    # skip => продолжаем к следующему кандидату

                # 2) Если прямых указаний нет или пользователь всё пропустил — пробуем «по частям»
                if not self._stop and not made_change:
                    partial_seen: set[str] = set()
                    while not self._stop:
                        cand_list = self._find_template_param_partial(txt, old_full, new_full, family, lang)
                        # уберём уже показанные кандидаты
                        cand_list = [c for c in cand_list if c.get('template') not in partial_seen]
                        if not cand_list:
                            # прямых и частичных совпадений не найдено
                            self.progress.emit(f"{title}: прямое указание категории в параметрах и совпадения по частям не найдены")
                            break
                        c0 = cand_list[0]
                        partial_seen.add(str(c0.get('template')))
                        result = self._prompt_user_template_replace(
                            title,
                            str(c0.get('template') or ''),
                            old_full,
                            new_full,
                            proposed_template=str(c0.get('proposed_template') or ''),
                            old_sub=str(c0.get('old_sub') or ''),
                            new_sub=str(c0.get('new_sub') or ''),
                        )
                        action = result.get('action') if isinstance(result, dict) else str(result)
                        if action == 'cancel':
                            self._stop = True
                            break
                        if action == 'confirm':
                            try:
                                edited = str(result.get('edited_template') or '') if isinstance(result, dict) else ''
                                replacement = edited if edited.strip() else str(c0.get('proposed_template') or '')
                                tmpl_old = str(c0.get('template') or '')
                                if replacement and tmpl_old and replacement != tmpl_old:
                                    new_txt = txt.replace(tmpl_old, replacement, 1)
                                    if new_txt != txt:
                                        # сохранение с адаптивным бэкоффом
                                        for attempt in range(1, 6):
                                            try:
                                                now2 = time.time()
                                                wait2 = max(0.0, (last_write_ts + write_min_interval) - now2)
                                                if wait2 > 0:
                                                    time.sleep(wait2)
                                                page.text = new_txt
                                                page.save(summary=f"[[{old_full}]] → [[{new_full}]] (исправление категоризации через параметр шаблона)", minor=True)
                                                write_min_interval = max(0.2, write_min_interval * 0.9)
                                                last_write_ts = time.time()
                                                break
                                            except Exception as e:
                                                msg = (str(e) or '').lower()
                                                if any(x in msg for x in ('429', 'too many requests', 'ratelimit', 'rate limit', 'maxlag', 'readonly')) and attempt < 5:
                                                    write_min_interval = min(max(write_min_interval * 1.5, 0.6 * attempt), 2.5)
                                                    debug(f"Template partial save backoff: {write_min_interval:.2f}s · attempt {attempt}")
                                                    continue
                                                raise
                                        txt = new_txt
                                        moved_via_template += 1
                                        made_change = True
                                        try:
                                            nsid = page.namespace().id
                                            typ = 'категория' if nsid == 14 else 'статья'
                                        except Exception:
                                            typ = 'страница'
                                        self.progress.emit(f"→ {new_full} → {title}: {typ} перенесена (частичная замена)")
                            except Exception as e:
                                self.progress.emit(f"{title}: ошибка правки шаблона (частично): {e}")
            except Exception as e:
                self.progress.emit(f"{title}: ошибка обработки на ручной фазе: {e}")

        if self._stop:
            return

        # Итоговая проверка: сколько осталось в старой категории
        remaining = 0
        try:
            params2 = {
                'action': 'query',
                'list': 'categorymembers',
                'cmtitle': old_full,
                'cmlimit': 'max',
                'cmprop': 'ids',
                'format': 'json'
            }
            while True:
                _rate_wait()
                r2 = REQUEST_SESSION.get(api, params=params2, timeout=20, headers=REQUEST_HEADERS)
                if r2.status_code != 200:
                    break
                data2 = r2.json()
                remaining += len(data2.get('query', {}).get('categorymembers', []) or [])
                if 'continue' in data2:
                    params2.update(data2['continue'])
                else:
                    break
        except Exception:
            remaining = -1

        # Итоговая строка: компактно и наглядно; присутствует слово «готово» для зелёной подсветки в логе
        self.progress.emit(
            "✅ Готово:\n"
            f"— прямые замены: <b>{moved_direct}</b>,\n"
            f"— через параметры шаблонов: <b>{moved_via_template}</b>,\n"
            f"— осталось: <b>{remaining if remaining>=0 else 'неизвестно'}</b>."
        )

# ===== Login Worker =====
class LoginWorker(QThread):
    success = Signal(str, str, str)
    failure = Signal(str)

    def __init__(self, username: str, password: str, lang: str, family: str):
        super().__init__()
        self.username = username
        self.password = password
        self.lang = lang
        self.family = family

    def run(self):
        try:
            write_pwb_credentials(self.lang, self.username, self.password, self.family)
            cfg_dir = apply_pwb_config(self.lang, self.family)
            _delete_all_cookies(cfg_dir)
            reset_pywikibot_session(self.lang)
            # Ограничим таймауты сетевых запросов pywikibot
            try:
                pwb_config.socket_timeout = 20
            except Exception:
                pass
            site = pywikibot.Site(self.lang, self.family)
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
            self.success.emit(self.username, self.lang, self.family)
        except Exception as e:
            self.failure.emit(f"{type(e).__name__}: {e}")

# ===== Main Window =====

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Wiki Category Tool')

        # Стартовый размер как раньше, но можно сжимать до меньшего минимума
        self.resize(1200, 700)
        self.setMinimumSize(900, 540)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.current_user = None
        self.current_lang = None
        self._secret_buffer = ''
        self._stay_on_top_active = False
        # Запоминание флага «автоподтверждать прямые совпадения» между диалогами
        self._auto_confirm_direct_all_ui: bool = False

        self.init_auth_tab()
        try_load_bypass_awb_from_embedded()
        maybe_auto_activate_bypass(self)
        self.init_parse_tab()
        self.init_replace_tab()
        self.init_create_tab()
        self.init_rename_tab()


    def _add_info_button(self, host_layout, text: str, inline: bool = False):
        """Insert an ℹ button.

        When inline=True and host_layout is QHBoxLayout, the button is placed
        immediately after the previous widget. Otherwise, it is aligned to the
        right edge of the host layout.

        Clicking the button shows *text* inside a modal information dialog.
        """
        btn = QToolButton()
        btn.setText('❔')
        btn.setAutoRaise(True)
        btn.setToolTip(text)
        btn.clicked.connect(lambda _=None, t=text: QMessageBox.information(self, 'Справка', t))

        if isinstance(host_layout, QHBoxLayout):
            if inline:
                # Добавляем кнопку сразу за предыдущим виджетом, без растяжки
                try:
                    host_layout.addSpacing(6)
                except Exception:
                    pass
                host_layout.addWidget(btn, 0, Qt.AlignLeft)
            else:
                host_layout.addStretch()
                host_layout.addWidget(btn)
        else:
            row = QHBoxLayout()
            row.addStretch()
            row.addWidget(btn)
            host_layout.insertLayout(0, row)
        return btn


    def init_auth_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        try:
            tab.setStyleSheet("QWidget { font-size: 13px; } QLineEdit, QComboBox, QPushButton { min-height: 30px; }")
        except Exception:
            pass
        self.user_edit = QLineEdit(); self.user_edit.setPlaceholderText('Имя пользователя')
        self.pass_edit = QLineEdit(); self.pass_edit.setPlaceholderText('Пароль'); self.pass_edit.setEchoMode(QLineEdit.Password)

        layout_form = QVBoxLayout()
        layout_form.setAlignment(Qt.AlignHCenter)
        layout_form.setSpacing(10)

        layout.addStretch(1)
        layout.addLayout(layout_form)

        layout.addStretch(2)
        layout.setContentsMargins(0, 14, 0, 14)


        lang_help = (
            'Можно вручную ввести любой код языка.\n'
            'Для большинства языков локальные префиксы определяются автоматически через кэш/API.'
        )
        row_lang = QHBoxLayout()
        row_lang.setAlignment(Qt.AlignHCenter)
        try:
            row_lang.setSpacing(8)
        except Exception:
            pass
        lang_label = QLabel('Язык вики:')
        row_lang.addWidget(lang_label)
        self.lang_combo = QComboBox(); self.lang_combo.setEditable(True)
        self.lang_combo.addItems(['ru', 'uk', 'be', 'en', 'fr', 'es', 'de'])
        self.lang_combo.setCurrentText('ru')
        self.lang_combo.setMaximumWidth(250)
        self.prev_lang = 'ru'
        self.lang_combo.currentTextChanged.connect(self._on_lang_change)
        row_lang.addWidget(self.lang_combo)

        info_btn = QToolButton(); info_btn.setText('❔'); info_btn.setAutoRaise(True)
        info_btn.setToolTip(lang_help)
        info_btn.clicked.connect(lambda _=None: QMessageBox.information(self, 'Справка', lang_help))
        row_lang.addWidget(info_btn)
        layout_form.addLayout(row_lang)
        layout_form.setAlignment(row_lang, Qt.AlignHCenter)


        fam_help = (
            'Выберите проект: Wikipedia, Commons или иной (Wikibooks, Wiktionary, Wikiquote, Wikisource, '
            'Wikiversity, Wikidata, Wikifunctions, Wikivoyage, Wikinews, Meta, MediaWiki).\n\n'
            'Для Commons укажите язык "commons".\n\n'
            'Важно: работа вне Wikipedia не полностью протестирована и может быть частично ограничена.'
        )
        row_fam = QHBoxLayout()
        row_fam.setAlignment(Qt.AlignHCenter)
        try:
            row_fam.setSpacing(8)
        except Exception:
            pass
        fam_label = QLabel('Проект:')
        row_fam.addWidget(fam_label)
        self.family_combo = QComboBox(); self.family_combo.setEditable(False)

        primary = ['wikipedia', 'commons']
        others = sorted([
            'wikibooks', 'wiktionary', 'wikiquote',
            'wikisource', 'wikiversity', 'species',
            'wikidata', 'wikifunctions',
            'wikivoyage', 'wikinews',
            'meta', 'mediawiki'
        ])

        self.family_combo.addItems(primary)
        self.family_combo.insertSeparator(self.family_combo.count())
        self.family_combo.addItems(others)
        self.family_combo.setCurrentText('wikipedia')
        self.family_combo.setMaximumWidth(250)
        row_fam.addWidget(self.family_combo)
        fam_btn = QToolButton(); fam_btn.setText('❔'); fam_btn.setAutoRaise(True)
        fam_btn.setToolTip(fam_help)
        fam_btn.clicked.connect(lambda _=None: QMessageBox.information(self, 'Справка', fam_help))
        row_fam.addWidget(fam_btn)
        layout_form.addLayout(row_fam)
        layout_form.setAlignment(row_fam, Qt.AlignHCenter)

        try:
            self.family_combo.currentTextChanged.connect(lambda fam: [
                _populate_ns_combo(self.ns_combo_parse, fam, (self.lang_combo.currentText() or 'ru')),
                _populate_ns_combo(self.ns_combo_replace, fam, (self.lang_combo.currentText() or 'ru')),
                _populate_ns_combo(self.ns_combo_create, fam, (self.lang_combo.currentText() or 'ru')),
                _populate_ns_combo(self.ns_combo_rename, fam, (self.lang_combo.currentText() or 'ru')),
                _adjust_combo_popup_width(self.ns_combo_parse),
                _adjust_combo_popup_width(self.ns_combo_replace),
                _adjust_combo_popup_width(self.ns_combo_create),
                _adjust_combo_popup_width(self.ns_combo_rename)
            ])
        except Exception:
            pass


        self.user_edit.setMinimumWidth(280)
        self.pass_edit.setMinimumWidth(280)
        try:
            self.user_edit.setMinimumHeight(30)
            self.pass_edit.setMinimumHeight(30)
        except Exception:
            pass
        layout_form.addWidget(self.user_edit, alignment=Qt.AlignHCenter)
        layout_form.addWidget(self.pass_edit, alignment=Qt.AlignHCenter)
        self.login_btn = QPushButton('Авторизоваться')
        self.login_btn.clicked.connect(self.save_creds)

        try:
            self.user_edit.returnPressed.connect(self.save_creds)
            self.pass_edit.returnPressed.connect(self.save_creds)
        except Exception:
            pass
        self.status_label = QLabel('Авторизация (pywikibot)')

        try:
            self.status_label.setTextFormat(Qt.RichText)
            self.status_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
            self.status_label.setOpenExternalLinks(True)
        except Exception:
            pass

        self.switch_btn = QPushButton('Сменить аккаунт')
        self.switch_btn.setVisible(False)
        self.switch_btn.clicked.connect(self.switch_account)
        layout_form.addWidget(self.login_btn, alignment=Qt.AlignHCenter)
        layout_form.addWidget(self.status_label, alignment=Qt.AlignHCenter)
        layout_form.addWidget(self.switch_btn, alignment=Qt.AlignHCenter)

        layout_form.setStretchFactor(self.login_btn, 0)

        row_debug = QHBoxLayout()
        row_debug.addStretch()
        dbg_btn = QPushButton('Debug'); dbg_btn.setFixedWidth(60)
        dbg_btn.clicked.connect(self.show_debug)
        row_debug.addWidget(dbg_btn)
        upd_btn = QPushButton('Проверить обновления')
        upd_btn.clicked.connect(self.check_updates)
        row_debug.addWidget(upd_btn)
        layout.addLayout(row_debug)
        self.tabs.addTab(tab, 'Авторизация')



 

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

    def check_updates(self):
        try:
            debug('Проверка обновлений...')
            _rate_wait()
            r = REQUEST_SESSION.get(GITHUB_API_RELEASES, headers=REQUEST_HEADERS, timeout=10)
            if r.status_code != 200:
                debug(f'GitHub API status {r.status_code}')
                QMessageBox.information(self, 'Проверка обновлений', f'Не удалось проверить обновления. Текущая версия: {APP_VERSION}. Откроем страницу релизов.')
                QDesktopServices.openUrl(QUrl(RELEASES_URL))
                return
            data = r.json() or []
            if not isinstance(data, list) or not data:
                QMessageBox.information(self, 'Проверка обновлений', f'Пока нет опубликованных релизов. Текущая версия: {APP_VERSION}. Откроем страницу.')
                QDesktopServices.openUrl(QUrl(RELEASES_URL))
                return
            latest = None
            for rel in data:
                # пропускаем черновики
                if rel.get('draft'):
                    continue
                latest = rel
                break
            if not latest:
                QMessageBox.information(self, 'Проверка обновлений', f'Подходящих релизов не найдено. Текущая версия: {APP_VERSION}.')
                return
            tag = (latest.get('tag_name') or '').strip()
            name = (latest.get('name') or tag or 'Новый релиз')
            html_url = (latest.get('html_url') or RELEASES_URL)
            # Форматируем дату публикации
            published = (latest.get('published_at') or latest.get('created_at') or '').strip()
            date_str = ''
            if published:
                try:
                    # ISO 8601 → YYYY-MM-DD
                    date_str = (published.split('T', 1)[0])
                except Exception:
                    date_str = published
            # Сравнение версий: поддержка числовых (0.1, 1.2.3) и betaN
            def _num(ver: str):
                m = re.search(r"(\d+(?:\.\d+)+|\d+)", ver or '')
                return [int(x) for x in m.group(1).split('.')] if m else None
            def _beta(ver: str):
                m = re.search(r"beta\s*(\d+)", (ver or ''), re.I)
                return int(m.group(1)) if m else None

            local = (APP_VERSION or '').strip()
            remote = (tag or '').strip()
            ln, rn = _num(local), _num(remote)
            lb, rb = _beta(local), _beta(remote)

            def _cmp_lists(a, b):
                la, lb = len(a), len(b)
                n = max(la, lb)
                a = a + [0] * (n - la)
                b = b + [0] * (n - lb)
                if a == b:
                    return 0
                return 1 if a > b else -1

            cmp_res = None
            if ln and rn:
                cmp_res = _cmp_lists(ln, rn)
            elif lb is not None and rb is not None:
                cmp_res = -1 if lb < rb else (1 if lb > rb else 0)
            elif local and remote:
                # Фолбэк: строковое сравнение по равенству
                cmp_res = 0 if local == remote else None

            if cmp_res is None:
                # Не смогли корректно сравнить — просто предложим открыть страницу
                extra = f' ({date_str})' if date_str else ''
                msg = (
                    f'Найден релиз: {name}{extra}.\n'
                    f'Текущая версия: {APP_VERSION}\n'
                    f'Актуальная версия: {remote or name}\n'
                    f'Открыть страницу релизов?'
                )
                res = QMessageBox.question(self, 'Проверить обновления', msg, QMessageBox.Yes | QMessageBox.No)
                if res == QMessageBox.Yes:
                    QDesktopServices.openUrl(QUrl(html_url))
            elif cmp_res < 0:
                # remote > local
                extra = f' ({date_str})' if date_str else ''
                msg = (
                    f'Доступна новая версия: {name}{extra}.\n\n'
                    f'Текущая версия: {APP_VERSION}\n'
                    f'Актуальная версия: {remote or name}\n'
                    f'Открыть страницу релизов?'
                )
                res = QMessageBox.question(self, 'Проверить обновления', msg, QMessageBox.Yes | QMessageBox.No)
                if res == QMessageBox.Yes:
                    QDesktopServices.openUrl(QUrl(html_url))
            elif cmp_res > 0:
                # local > remote
                msg = (
                    f'У вас версия новее последнего релиза на GitHub.\n\n'
                    f'Текущая версия: {APP_VERSION}\n'
                    f'Актуальная версия: {remote or name}\n'
                )
                QMessageBox.information(self, 'Проверить обновления', msg)
            else:
                msg = (
                    f'У вас установлена актуальная версия.\n\n'
                    f'Текущая версия: {APP_VERSION}\n'
                    f'Актуальная версия: {remote or name}'
                )
                QMessageBox.information(self, 'Проверить обновления', msg)
        except Exception as e:
            debug(f'Ошибка проверки обновлений: {e}')
            QMessageBox.information(self, 'Проверка обновлений', f'Произошла ошибка. Текущая версия: {APP_VERSION}. Откроем страницу релизов.')
            QDesktopServices.openUrl(QUrl(RELEASES_URL))

    def _set_awb_ui(self, has_awb: bool, note: str | None = None):
        """Включает/выключает доступ к операциям записи в зависимости от допуска AWB.
        Оставляет доступной вкладку считывания.
        """

        # TEMP: при отключённых проверках считаем доступ всегда есть и скрываем ссылки/тексты AWB
        try:
            if AWB_CHECKS_DISABLED:
                has_awb = True
                self.status_label.setText('Авторизовано')
                for btn_name in ('replace_btn', 'create_btn', 'rename_btn'):
                    try:
                        btn = getattr(self, btn_name, None)
                        if btn is not None:
                            btn.setEnabled(True)
                    except Exception:
                        pass
                return
        except NameError:
            pass

        if is_bypass_awb():
            has_awb = True
        lang = (self.current_lang or self.lang_combo.currentText() or 'ru').strip()
        fam = (self.family_combo.currentText() or 'wikipedia').strip()
        awb_url = build_project_awb_url(fam, lang)
        if has_awb:
            if is_bypass_awb():
                self.status_label.setText(f'Авторизовано · <a href="{awb_url}">AWB</a>: обход включён')
            else:
                text = 'есть'
                try:
                    if note and ('отсутств' in note.lower() or 'not exist' in note.lower()):
                        text = 'не требуются на этой вики'
                except Exception:
                    pass
                self.status_label.setText(f'Авторизовано · <a href="{awb_url}">AWB</a>: {text}')
        else:
            self.status_label.setText(f'Авторизовано. Требуется получить доступ к <a href="{awb_url}">AWB</a>')

        for btn_name in ('replace_btn', 'create_btn', 'rename_btn'):
            try:
                btn = getattr(self, btn_name, None)
                if btn is not None:
                    btn.setEnabled(has_awb)
            except Exception:
                pass

    def _after_login_success(self, u: str, l: str, fam: str):
        self._apply_cred_style(True)
        try:
            if is_bypass_awb():
                ok_awb = True
            else:
                ok_awb, detail = is_user_awb_enabled(l, u, fam)
        except Exception:
            ok_awb = False
        try:
            note = detail if not is_bypass_awb() else None
        except Exception:
            note = None

        if is_bypass_awb():
            self._set_awb_ui(True)
        else:
            self._set_awb_ui(ok_awb, note)
        QMessageBox.information(self, 'OK', 'Авторизация прошла успешно.')

        try:
            self.raise_(); self.activateWindow()
        except Exception:
            pass

        self._force_on_top(False, delay_ms=600)

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
        fam = (self.family_combo.currentText() or 'wikipedia')
        worker = LoginWorker(user, pwd, lang, fam)
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
        cur_family = None
        username_map = {}
        password = ''
        try:
            if os.path.isfile(uc):
                with open(uc, encoding='utf-8') as f:
                    txt = f.read()
                mfam = re.search(r"^\s*family\s*=\s*'([^']+)'\s*$", txt, re.M)
                if mfam:
                    cur_family = mfam.group(1)
                    try:
                        self.family_combo.setCurrentText(cur_family)
                    except Exception:
                        pass
                mlang = re.search(r"^\s*mylang\s*=\s*'([^']+)'\s*$", txt, re.M)
                if mlang:
                    cur_lang = mlang.group(1)
                    self.lang_combo.setCurrentText(cur_lang)
                fam = cur_family or (self.family_combo.currentText() or 'wikipedia')
                fam_re = re.escape(fam)
                for m in re.finditer(rf"usernames\['{fam_re}'\]\['([^']+)'\]\s*=\s*'([^']+)'", txt):
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
        fam = cur_family or (self.family_combo.currentText() or 'wikipedia')
        if cur_lang and (cur_lang in username_map) and password and cookies_exist(cfg_dir, username_map[cur_lang]):
            self._apply_cred_style(True)

            try:
                ok_awb, detail = is_user_awb_enabled(cur_lang, username_map[cur_lang], fam)
                self._set_awb_ui(ok_awb, detail)
            except Exception:
                self._set_awb_ui(False)
        else:
            self._apply_cred_style(False)

    def init_parse_tab(self):
        tab = QWidget()
        main_layout = QVBoxLayout(tab)
        
        # Справочная информация (кратко и по делу)
        parse_help_left = (
            'Префиксы: «Авто» не меняет и не добавляет префиксы; Выбирать пространство имён из списка нужно когда в списке указаны только названия страниц, без префиксов.\n'
            'Английские префиксы в исходных названиях распознаются.\n'
            'Ctrl+клик по «Получить» — открыть PetScan для выбранной категории.'
        )
        parse_help_right = (
            'Результат сохраняется в .tsv (UTF‑8 с BOM).\n'
            'При лимитах API инструмент сам замедляется.'
        )
        
        # === ГОРИЗОНТАЛЬНОЕ РАЗДЕЛЕНИЕ ===
        h_main = QHBoxLayout()
        
        # === ЛЕВАЯ ПАНЕЛЬ: ВВОД ДАННЫХ ===
        left_group = QGroupBox("Настройка и ввод данных")
        left_group.setStyleSheet("QGroupBox { border: 1px solid lightgray; border-radius: 5px; margin-top: 10px; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; }")
        left_layout = QVBoxLayout(left_group)
        try:
            left_layout.setContentsMargins(8, 12, 8, 8)  # не двигаем (i) у верхней кромки
            left_layout.setSpacing(2)  # уменьшили зазор между (i) и строкой «Префиксы»
        except Exception:
            pass
        # (i) будет добавлена в строку «Префиксы» справа — без влияния на вертикальные отступы
        
        # Префиксы пространства имён
        prefix_layout = QHBoxLayout()
        prefix_label = QLabel('Префиксы:')
        prefix_label.setToolTip(PREFIX_TOOLTIP)
        prefix_layout.addWidget(prefix_label)
        self.ns_combo_parse = QComboBox()
        self.ns_combo_parse.setEditable(False)
        _populate_ns_combo(self.ns_combo_parse, (self.family_combo.currentText() or 'wikipedia'), (self.lang_combo.currentText() or 'ru'))
        prefix_layout.addWidget(self.ns_combo_parse)
        # Толкаем (i) к правому краю в этой же строке
        prefix_layout.addStretch()
        self._add_info_button(prefix_layout, parse_help_left, inline=True)
        left_layout.addLayout(prefix_layout)
        
        # Получение подкатегорий
        left_layout.addSpacing(6)
        left_layout.addWidget(QLabel('<b>Получить подкатегории:</b>'))
        petscan_input_layout = QHBoxLayout()
        self.cat_edit = QLineEdit()
        self.cat_edit.setPlaceholderText('Название корневой категории')
        petscan_input_layout.addWidget(self.cat_edit, 1)
        
        self.petscan_btn = QPushButton('Получить')
        self.petscan_btn.setToolTip('Клик — получить подкатегории через API.\nCtrl+клик — открыть Petscan с расширенными настройками')
        self.petscan_btn.clicked.connect(self.open_petscan)
        petscan_input_layout.addWidget(self.petscan_btn)
        left_layout.addLayout(petscan_input_layout)
        
        # Ручной ввод списка
        left_layout.addSpacing(6)
        lbl_left_top = QLabel('<b>Список категорий для считывания:</b>')
        left_layout.addWidget(lbl_left_top)
        self.manual_list = QTextEdit()
        self.manual_list.setPlaceholderText('По одному названию категории на строке')
        self.manual_list.setMinimumHeight(220)
        left_layout.addWidget(self.manual_list, 1)
        try:
            left_layout.setStretchFactor(self.manual_list, 1)
        except Exception:
            pass
        
        # Загрузка из файла
        left_layout.addWidget(QLabel('<b>Или загрузить из файла:</b>'))
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel('Файл (.txt):'))
        
        self.in_path = QLineEdit()
        self.in_path.setMinimumWidth(0)
        self._embed_button_in_lineedit(self.in_path, lambda: self.pick_file(self.in_path, '*.txt'))
        file_layout.addWidget(self.in_path, 1)
        
        btn_open_in = QPushButton('Открыть')
        btn_open_in.clicked.connect(lambda: self.open_from_edit(self.in_path))
        file_layout.addWidget(btn_open_in)
        left_layout.addLayout(file_layout)
        
        # === ПРАВАЯ ПАНЕЛЬ: ХОД СЧИТЫВАНИЯ ===
        right_group = QGroupBox("Ход считывания")
        right_group.setStyleSheet("QGroupBox { border: 1px solid lightgray; border-radius: 5px; margin-top: 10px; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; }")
        right_layout = QVBoxLayout(right_group)
        try:
            right_layout.setContentsMargins(8, 12, 8, 8)
            right_layout.setSpacing(8)
        except Exception:
            pass
        
        # i-кнопка справа будет прикреплена к рамке, без участия в layout
        
        # Файл сохранения
        save_layout = QHBoxLayout()
        save_layout.addWidget(QLabel('Сохранить в:'))
        
        self.out_path = QLineEdit('categories.tsv')
        self.out_path.setMinimumWidth(0)
        self._embed_button_in_lineedit(self.out_path, lambda: self.pick_save(self.out_path, '.tsv'))
        save_layout.addWidget(self.out_path, 1)
        
        btn_open_out = QPushButton('Открыть')
        btn_open_out.clicked.connect(lambda: self.open_from_edit(self.out_path))
        save_layout.addWidget(btn_open_out)
        # Толкаем (i) к правому краю верхней строки справа
        save_layout.addStretch()
        self._add_info_button(save_layout, parse_help_right, inline=True)
        right_layout.addLayout(save_layout)
        
        # Небольшой отступ и лог процесса (основная область)
        right_layout.addSpacing(6)
        lbl_right_top = QLabel('<b>Лог выполнения:</b>')
        # Заголовок
        right_layout.addWidget(lbl_right_top)
        # Контейнер для лога с кнопкой очистки в правом нижнем углу
        self.parse_log = QTextEdit(); self.parse_log.setReadOnly(True); self.parse_log.setMinimumHeight(220)
        parse_log_wrap = QWidget(); parse_log_grid = QGridLayout(parse_log_wrap)
        try:
            parse_log_grid.setContentsMargins(0, 0, 0, 0); parse_log_grid.setSpacing(0)
        except Exception:
            pass
        parse_log_grid.addWidget(self.parse_log, 0, 0)
        btn_clear_parse = QToolButton(); btn_clear_parse.setText('🧹'); btn_clear_parse.setAutoRaise(True); btn_clear_parse.setToolTip('<span style="font-size:12px">Очистить</span>')
        try:
            btn_clear_parse.setStyleSheet('font-size: 20px; padding: 0px;')
            btn_clear_parse.setFixedSize(32, 32)
            btn_clear_parse.setCursor(Qt.PointingHandCursor)
        except Exception:
            pass
        btn_clear_parse.clicked.connect(lambda: self.parse_log.clear())
        parse_log_grid.addWidget(btn_clear_parse, 0, 0, Qt.AlignBottom | Qt.AlignRight)
        right_layout.addWidget(parse_log_wrap, 1)
        try:
            right_layout.setStretchFactor(parse_log_wrap, 1)
        except Exception:
            pass
        
        # Прогресс-бар
        self.parse_bar = QProgressBar()
        self.parse_bar.setMaximum(1)
        self.parse_bar.setValue(0)
        right_layout.addWidget(self.parse_bar)
        try:
            # Синхронизируем нижние поля по высоте (лог справа и список слева)
            right_layout.setStretchFactor(self.parse_log, 1)
        except Exception:
            pass
        
        # Кнопки управления
        control_layout = QHBoxLayout()
        control_layout.addStretch()
        self.parse_btn = QPushButton('Начать считывание')
        self.parse_btn.clicked.connect(self.start_parse)
        control_layout.addWidget(self.parse_btn)
        self.parse_stop_btn = QPushButton('Остановить')
        self.parse_stop_btn.setEnabled(False)
        self.parse_stop_btn.clicked.connect(self.stop_parse)
        control_layout.addWidget(self.parse_stop_btn)
        control_layout.addStretch()
        right_layout.addLayout(control_layout)
        self._set_start_stop_ratio(self.parse_btn, self.parse_stop_btn, 3)
        
        # Добавление панелей в основной макет
        h_main.addWidget(left_group, 1)
        h_main.addWidget(right_group, 1)
        main_layout.addLayout(h_main)

        # Удалён код закрепления (i) как дочерних виджетов — используется упрощённый вариант в строках выше
        
        self.tabs.addTab(tab, 'Считать')


    def open_petscan(self):
        debug(f"Fetch subcats btn pressed: cat={self.cat_edit.text().strip()}")
        category = self.cat_edit.text().strip()
        if not category:
            QMessageBox.warning(self, 'Ошибка', 'Введите название категории.')
            return
        lang = self.lang_combo.currentText().strip() or 'ru'
        fam = (self.family_combo.currentText() or 'wikipedia').strip()

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
                f'project={fam}&after=&wikidata_item=no&search_max_results=1000&langs_labels_no=&langs_labels_yes=&'
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

        api_url = build_api_url(fam, lang)
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
                _rate_wait()
                r = REQUEST_SESSION.get(api_url, params=params, timeout=10, headers=REQUEST_HEADERS)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code} при запросе {api_url}")
                try:
                    resp = r.json()
                except Exception:
                    snippet = (r.text or '')[:200].replace('\n', ' ')
                    ct = r.headers.get('Content-Type')
                    raise RuntimeError(f"Не удалось распарсить JSON: Content-Type={ct}, тело[:200]={snippet!r}")
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
            'Форматирование исходного файла:\n'
            'TSV: Title<TAB>line1<TAB>line2…\n\n'
            'Одна строка — одна новая страница.\n\n'
            'Пустая колонка в файле добавляет новую строку.\n\n'
            'Указанные страницы будут найдены и в них выполнены замены текста согласно списку.\n\n'
            'Опции:\n'
            '• Малая правка (minor edit) — отметка малой правки для каждого действия.\n'
        )
        h = QHBoxLayout()
        self.tsv_path = QLineEdit('categories.tsv')
        self.tsv_path.setMinimumWidth(0)
        self.tsv_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._embed_button_in_lineedit(self.tsv_path, lambda: self.pick_file(self.tsv_path, '*.tsv'))
        h.addWidget(QLabel('Список для замен (.tsv):'))
        h.addWidget(self.tsv_path, 1)
        btn_open_tsv = QPushButton('Открыть'); btn_open_tsv.clicked.connect(lambda: self.open_from_edit(self.tsv_path))
        btn_open_tsv.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        h.addWidget(btn_open_tsv)
        # компактный выбор префикса (выпадающий список)
        prefix_label_replace = QLabel('Префиксы:')
        prefix_label_replace.setToolTip(PREFIX_TOOLTIP)
        h.addWidget(prefix_label_replace)
        self.ns_combo_replace = QComboBox(); self.ns_combo_replace.setEditable(False)
        _populate_ns_combo(self.ns_combo_replace, (self.family_combo.currentText() or 'wikipedia'), (self.lang_combo.currentText() or 'ru'))
        h.addWidget(self.ns_combo_replace)
        # кнопка ℹ в строке выбора файла
        self._add_info_button(h, replace_help)

        sum_layout = QHBoxLayout()
        sum_layout.addWidget(QLabel('Комментарий к правкам:'))
        self.summary_edit = QLineEdit()
        sum_layout.addWidget(self.summary_edit)

        self.summary_edit.setText(default_summary('ru'))


        # Малая правка
        self.minor_checkbox = QCheckBox('Малая правка (minor edit)')
        sum_layout.addWidget(self.minor_checkbox)

        self.replace_btn = QPushButton('Начать запись')
        self.replace_btn.clicked.connect(self.start_replace)
        self.replace_stop_btn = QPushButton('Остановить')
        self.replace_stop_btn.setEnabled(False)
        self.replace_stop_btn.clicked.connect(self.stop_replace)
        # Лог выполнения и кнопка очистки (заголовок внутри контейнера)
        self.rep_log = QTextEdit(); self.rep_log.setReadOnly(True)
        rep_wrap = QWidget(); rep_grid = QGridLayout(rep_wrap)
        try:
            rep_grid.setContentsMargins(0, 0, 0, 0); rep_grid.setSpacing(0)
        except Exception:
            pass
        rep_header = QLabel('<b>Лог выполнения:</b>')
        rep_grid.addWidget(rep_header, 0, 0)
        rep_grid.addWidget(self.rep_log, 1, 0)
        btn_clear_rep = QToolButton(); btn_clear_rep.setText('🧹'); btn_clear_rep.setAutoRaise(True); btn_clear_rep.setToolTip('<span style="font-size:12px">Очистить</span>')
        try:
            btn_clear_rep.setStyleSheet('font-size: 20px; padding: 0px;')
            btn_clear_rep.setFixedSize(32, 32)
            btn_clear_rep.setCursor(Qt.PointingHandCursor)
        except Exception:
            pass
        btn_clear_rep.clicked.connect(lambda: self.rep_log.clear())
        rep_grid.addWidget(btn_clear_rep, 1, 0, Qt.AlignBottom | Qt.AlignRight)
        # Перемещаем кнопки вправо вниз под лог
        row_run = QHBoxLayout(); row_run.addStretch(); row_run.addWidget(self.replace_btn); row_run.addWidget(self.replace_stop_btn)
        v.addLayout(h); v.addLayout(sum_layout); v.addWidget(rep_wrap, 1); v.addLayout(row_run)
        self._set_start_stop_ratio(self.replace_btn, self.replace_stop_btn, 3)
        self.tabs.addTab(tab, 'Перезаписать')

    def init_create_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        create_help = (
            'Форматирование исходного файла:\n'
            'TSV: Title<TAB>line1<TAB>line2…\n\n'
            'Одна строка — одна новая страница.\n\n'
            'Пустая колонка в файле добавляет новую строку.\n\n'
            'Будут созданы новые страницы с указанными названиями и содержимым.'
        )
        h = QHBoxLayout()
        self.tsv_path_create = QLineEdit('categories.tsv')
        self.tsv_path_create.setMinimumWidth(0)
        self.tsv_path_create.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._embed_button_in_lineedit(self.tsv_path_create, lambda: self.pick_file(self.tsv_path_create, '*.tsv'))
        h.addWidget(QLabel('Список для создания (.tsv):'))
        h.addWidget(self.tsv_path_create, 1)
        btn_open_tsv_create = QPushButton('Открыть'); btn_open_tsv_create.clicked.connect(lambda: self.open_from_edit(self.tsv_path_create))
        btn_open_tsv_create.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        h.addWidget(btn_open_tsv_create)
        # компактный выбор префикса (выпадающий список)
        prefix_label_create = QLabel('Префиксы:')
        prefix_label_create.setToolTip(PREFIX_TOOLTIP)
        h.addWidget(prefix_label_create)
        self.ns_combo_create = QComboBox(); self.ns_combo_create.setEditable(False)
        _populate_ns_combo(self.ns_combo_create, (self.family_combo.currentText() or 'wikipedia'), (self.lang_combo.currentText() or 'ru'))
        h.addWidget(self.ns_combo_create)
        # кнопка ℹ в строке выбора файла
        self._add_info_button(h, create_help)

        sum_layout = QHBoxLayout()
        sum_layout.addWidget(QLabel('Комментарий к правкам:'))
        self.summary_edit_create = QLineEdit()
        sum_layout.addWidget(self.summary_edit_create)
        self.summary_edit_create.setText(default_create_summary('ru'))

        self.create_btn = QPushButton('Начать создание')
        self.create_btn.clicked.connect(self.start_create)
        self.create_stop_btn = QPushButton('Остановить')
        self.create_stop_btn.setEnabled(False)
        self.create_stop_btn.clicked.connect(self.stop_create)
        # Лог выполнения и кнопка очистки (заголовок внутри контейнера)
        self.create_log = QTextEdit(); self.create_log.setReadOnly(True)
        create_wrap = QWidget(); create_grid = QGridLayout(create_wrap)
        try:
            create_grid.setContentsMargins(0, 0, 0, 0); create_grid.setSpacing(0)
        except Exception:
            pass
        create_header = QLabel('<b>Лог выполнения:</b>')
        create_grid.addWidget(create_header, 0, 0)
        create_grid.addWidget(self.create_log, 1, 0)
        btn_clear_create = QToolButton(); btn_clear_create.setText('🧹'); btn_clear_create.setAutoRaise(True); btn_clear_create.setToolTip('<span style="font-size:12px">Очистить</span>')
        try:
            btn_clear_create.setStyleSheet('font-size: 20px; padding: 0px;')
            btn_clear_create.setFixedSize(32, 32)
            btn_clear_create.setCursor(Qt.PointingHandCursor)
        except Exception:
            pass
        btn_clear_create.clicked.connect(lambda: self.create_log.clear())
        create_grid.addWidget(btn_clear_create, 1, 0, Qt.AlignBottom | Qt.AlignRight)
        # кнопки справа внизу
        row_run = QHBoxLayout(); row_run.addStretch(); row_run.addWidget(self.create_btn); row_run.addWidget(self.create_stop_btn)
        v.addLayout(h); v.addLayout(sum_layout); v.addWidget(create_wrap, 1); v.addLayout(row_run)
        self._set_start_stop_ratio(self.create_btn, self.create_stop_btn, 3)
        self.tabs.addTab(tab, 'Создать')

    def init_rename_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        rename_help = (
            'Форматирование исходного файла:\n'
            'TSV: OldTitle<TAB>NewTitle<TAB>Комментарий к правке\n\n'
            'Одна строка — одно переименование.\n\n'
            'Как работает перенос:\n'
            '1) Переименовывается сама категория (с учётом опций «Оставлять перенаправления…»).\n'
            '2) Перенос содержимого категорий (включите нужные опции слева):\n'
            '   — Обновлять прямые ссылки у содержимого: [[Категория:Старая|Ключ]] → [[Категория:Новая|Ключ]].\n'
            '     Ключи сортировки после «|» сохраняются. Для «Шаблон:»/«Модуль:» дополнительно проверяется основная страница и её /doc.\n'
            '   — Искать и исправлять категоризацию через параметры шаблонов: обрабатываются позиционные и именованные параметры.\n'
            '     Режимы:\n'
            '       • Полное имя категории в параметре — можно включить «Автоматически подтверждать все последующие».\n'
            '       • Поиск по частям названия — всегда с ручным подтверждением в диалоге.\n'
            '     Префикс «Категория:» в параметрах обычно не указывают — это учитывается.\n\n'
            'Итог и статистика:\n'
            '— В конце выводится строка «Готово: Прямые замены: N — Через параметры шаблонов: M — Осталось: K».\n\n'
        )

        h = QHBoxLayout()
        self.tsv_path_rename = QLineEdit('categories.tsv')
        self.tsv_path_rename.setMinimumWidth(0)
        self.tsv_path_rename.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._embed_button_in_lineedit(self.tsv_path_rename, lambda: self.pick_file(self.tsv_path_rename, '*.tsv'))
        h.addWidget(QLabel('Список для переименования (.tsv):'))
        h.addWidget(self.tsv_path_rename, 1)
        btn_open_tsv_rename = QPushButton('Открыть'); btn_open_tsv_rename.clicked.connect(lambda: self.open_from_edit(self.tsv_path_rename))
        btn_open_tsv_rename.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        h.addWidget(btn_open_tsv_rename)
        # компактный выбор префикса (выпадающий список)
        prefix_label_rename = QLabel('Префиксы:')
        prefix_label_rename.setToolTip(PREFIX_TOOLTIP)
        h.addWidget(prefix_label_rename)
        self.ns_combo_rename = QComboBox(); self.ns_combo_rename.setEditable(False)
        _populate_ns_combo(self.ns_combo_rename, (self.family_combo.currentText() or 'wikipedia'), (self.lang_combo.currentText() or 'ru'))
        h.addWidget(self.ns_combo_rename)
        # кнопка ℹ в строке выбора файла
        self._add_info_button(h, rename_help)
        v.addLayout(h)

        # Опции в две колонки
        col_left = QVBoxLayout()
        lbl_left = QLabel('<b>Переименование</b>')
        col_left.addWidget(lbl_left)
        # Подсказки для опций переноса
        phase1_help = (
            'Обновление прямых ссылок на категорию в тексте страниц-участников.\n\n'
            'Пример: [[Категория:Старая|Ключ]] → [[Категория:Новая|Ключ]].\n'
            'Для Шаблон:/Модуль: дополнительно проверяются основная страница и её /doc.\n'
        )
        phase2_help = (
            'Поиск и исправление указания категории в параметрах шаблонов\n'
            '{{Шаблон|Название категории}} или {{Название|категории A|категории Б}}.\n\n'
            'Режимы:\n'
            '1. Нахождение полного имени категории в параметра (позиционный или именованный). Можно включить «Автоматически подтверждать все последующие».\n'
            '2. Поиск по частям названия категории в параметрах шаблонов. Работает нестабильно, требует ручного подтверждения каждой правки.\n'
        )
        # Первая опция: прямые ссылки
        row_p1 = QHBoxLayout()
        self.chk_phase1 = QCheckBox('Переносить содержимое категории по прямым ссылкам')
        self.chk_phase1.setChecked(True)
        try:
            self.chk_phase1.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        except Exception:
            pass
        row_p1.addWidget(self.chk_phase1)
        self._add_info_button(row_p1, phase1_help, inline=True)
        try:
            row_p1.addStretch(1)
        except Exception:
            pass
        
        # Опция: переименовывать саму категорию
        row_move_cat = QHBoxLayout()
        self.chk_move_category = QCheckBox('Переименовывать страницы')
        self.chk_move_category.setChecked(True)
        try:
            self.chk_move_category.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        except Exception:
            pass
        row_move_cat.addWidget(self.chk_move_category)
        try:
            row_move_cat.addStretch(1)
        except Exception:
            pass
        col_left.addLayout(row_move_cat)
        # Блок перенаправлений (объединённый виджет, отключается при снятой галке «Переименовывать»)
        redirect_block = QWidget()
        redirect_block_layout = QVBoxLayout(redirect_block)
        try:
            redirect_block_layout.setContentsMargins(0, 0, 0, 0)
            redirect_block_layout.setSpacing(4)
        except Exception:
            pass
        # Заголовок блока перенаправлений скрыт по требованию
        # Вторая опция: параметры шаблонов
        row_p2 = QHBoxLayout()
        self.chk_find_in_templates = QCheckBox('Искать и исправлять категоризацию через параметры шаблонов')
        self.chk_find_in_templates.setChecked(True)
        try:
            self.chk_find_in_templates.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        except Exception:
            pass
        row_p2.addWidget(self.chk_find_in_templates)
        self._add_info_button(row_p2, phase2_help, inline=True)
        try:
            row_p2.addStretch(1)
        except Exception:
            pass
        


        col_right = QVBoxLayout()
        # Заголовок правой колонки
        lbl_right = QLabel('<b>Перенос содержимого категорий</b>')
        col_right.addWidget(lbl_right)

        self.chk_redirect_cat = QCheckBox('Оставлять перенаправления для категорий')
        try:
            self.chk_redirect_cat.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        except Exception:
            pass
        self.chk_redirect_cat.setChecked(False)
        redirect_block_layout.addWidget(self.chk_redirect_cat)
        self.chk_redirect_other = QCheckBox('Оставлять перенаправления для других страниц')
        try:
            self.chk_redirect_other.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        except Exception:
            pass
        self.chk_redirect_other.setChecked(True)
        redirect_block_layout.addWidget(self.chk_redirect_other)
        col_left.addWidget(redirect_block)
        try:
            redirect_block.setEnabled(self.chk_move_category.isChecked())
            self.chk_move_category.toggled.connect(redirect_block.setEnabled)
        except Exception:
            pass
        
        # Перенос содержимого (как было) — в правой колонке
        col_right.addLayout(row_p1)
        col_right.addLayout(row_p2)

        # Две колонки как отдельные виджеты с равным растяжением — правая начинается от центра
        row_cols = QHBoxLayout()
        try:
            row_cols.setSpacing(24)
        except Exception:
            pass
        left_widget = QWidget(); left_widget.setLayout(col_left)
        right_widget = QWidget(); right_widget.setLayout(col_right)
        try:
            left_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            right_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        except Exception:
            pass
        row_cols.addWidget(left_widget, 1)
        row_cols.addWidget(right_widget, 1)
        v.addLayout(row_cols)


        # Лог выполнения и кнопка очистки в правом нижнем углу
        v.addWidget(QLabel('<b>Лог выполнения:</b>'))
        self.rename_log = QTextEdit(); self.rename_log.setReadOnly(True)
        rename_wrap = QWidget(); rename_grid = QGridLayout(rename_wrap)
        try:
            rename_grid.setContentsMargins(0, 0, 0, 0); rename_grid.setSpacing(0)
        except Exception:
            pass
        rename_grid.addWidget(self.rename_log, 0, 0)
        btn_clear_rename = QToolButton(); btn_clear_rename.setText('🧹'); btn_clear_rename.setAutoRaise(True); btn_clear_rename.setToolTip('<span style="font-size:12px">Очистить</span>')
        try:
            btn_clear_rename.setStyleSheet('font-size: 20px; padding: 0px;')
            btn_clear_rename.setFixedSize(32, 32)
            btn_clear_rename.setCursor(Qt.PointingHandCursor)
        except Exception:
            pass
        btn_clear_rename.clicked.connect(lambda: self.rename_log.clear())
        rename_grid.addWidget(btn_clear_rename, 0, 0, Qt.AlignBottom | Qt.AlignRight)
        v.addWidget(rename_wrap, 1)


        self.rename_btn = QPushButton('Начать переименование')
        self.rename_btn.clicked.connect(self.start_rename)
        self.rename_stop_btn = QPushButton('Остановить')
        self.rename_stop_btn.setEnabled(False)
        self.rename_stop_btn.clicked.connect(self.stop_rename)
        row_run = QHBoxLayout(); row_run.addStretch(); row_run.addWidget(self.rename_btn); row_run.addWidget(self.rename_stop_btn)
        v.addLayout(row_run)
        self._set_start_stop_ratio(self.rename_btn, self.rename_stop_btn, 3)
        self.tabs.addTab(tab, 'Переименовать')

        # Диалог подтверждения замены внутри шаблона (подключение сигнала)
        try:
            # Будем ловить запросы от воркера и показывать ЕДИНЫЙ диалог пользователю
            def on_review_request(payload: object):
                try:
                    data = payload or {}
                    req_id = int(data.get('request_id'))
                    template_str = str(data.get('template') or '')
                    old_full = str(data.get('old_full') or '')
                    new_full = str(data.get('new_full') or '')
                    page_title = str(data.get('page_title') or '')
                    fam = (self.family_combo.currentText() or 'wikipedia').strip()
                    lng = (self.lang_combo.currentText() or 'ru').strip()
                    page_url = f"https://{build_host(fam, lng)}/wiki/" + urllib.parse.quote(page_title.replace(' ', '_')) if page_title else ''
                    mode = str(data.get('mode') or 'direct')
                    proposed_template = str(data.get('proposed_template') or '')
                    old_sub = str(data.get('old_sub') or '')
                    new_sub = str(data.get('new_sub') or '')
                    is_direct = (mode == 'direct')

                    # Подсветка исходного и заменённого
                    esc_tmpl = html.escape(template_str)
                    if is_direct:
                        old_direct = str(data.get('old_direct') or old_full)
                        new_direct = str(data.get('new_direct') or new_full)
                        esc_old_direct = html.escape(old_direct)
                        esc_new_direct = html.escape(new_direct)
                        highlighted_old = esc_tmpl.replace(esc_old_direct, f"<span style='color:#8b0000;font-weight:bold'>{esc_old_direct}</span>")
                        proposed_raw = template_str.replace(old_direct, new_direct, 1)
                        highlighted_new = html.escape(proposed_raw).replace(esc_new_direct, f"<span style='color:#0b6623;font-weight:bold'>{esc_new_direct}</span>")
                    else:
                        esc_old_sub = html.escape(old_sub)
                        esc_new_sub = html.escape(new_sub)
                        highlighted_old = esc_tmpl
                        if esc_old_sub:
                            highlighted_old = highlighted_old.replace(esc_old_sub, f"<span style='color:#8b0000;font-weight:bold'>{esc_old_sub}</span>")
                        esc_prop = html.escape(proposed_template or (template_str.replace(old_sub, new_sub, 1) if old_sub and new_sub else template_str))
                        highlighted_new = esc_prop
                        if esc_new_sub:
                            highlighted_new = highlighted_new.replace(esc_new_sub, f"<span style='color:#0b6623;font-weight:bold'>{esc_new_sub}</span>")

                    dlg = QDialog(self)
                    dlg.setWindowTitle('Замена по параметрам шаблона')
                    lay = QVBoxLayout(dlg)
                    # Фиксированный стартовый размер (можно свободно менять потом во все стороны)
                    try:
                        dlg.resize(760, 620)
                        dlg.setSizeGripEnabled(True)
                    except Exception:
                        pass
                    if page_title:
                        history_url = f"https://{build_host(fam, lng)}/w/index.php?title=" + urllib.parse.quote(page_title.replace(' ', '_')) + "&action=history"
                        # Верхний блок-«карточка» с информацией о переименовании категории
                        header = QFrame()
                        try:
                            header.setObjectName('reviewHeader')
                            header.setStyleSheet("QFrame#reviewHeader { background:#f8fafc; border:1px solid #e5e7eb; border-radius:10px; } QLabel { font-size:13px; }")
                        except Exception:
                            pass
                        hlay = QVBoxLayout(header)
                        try:
                            hlay.setContentsMargins(12, 10, 12, 10)
                            hlay.setSpacing(4)
                        except Exception:
                            pass
                        old_url = f"https://{build_host(fam, lng)}/wiki/" + urllib.parse.quote(old_full.replace(' ', '_'))
                        old_hist = f"https://{build_host(fam, lng)}/w/index.php?title=" + urllib.parse.quote(old_full.replace(' ', '_')) + "&action=history"
                        new_url = f"https://{build_host(fam, lng)}/wiki/" + urllib.parse.quote(new_full.replace(' ', '_'))
                        new_hist = f"https://{build_host(fam, lng)}/w/index.php?title=" + urllib.parse.quote(new_full.replace(' ', '_')) + "&action=history"
                        move1 = QLabel(f"❌ {html.escape(old_full)} (<a href='{old_url}'>открыть</a> · <a href='{old_hist}'>история</a>)")
                        move1.setTextFormat(Qt.RichText)
                        try:
                            move1.setWordWrap(True)
                        except Exception:
                            pass
                        move2 = QLabel(f"✅ {html.escape(new_full)} (<a href='{new_url}'>открыть</a> · <a href='{new_hist}'>история</a>)")
                        move2.setTextFormat(Qt.RichText)
                        try:
                            move2.setWordWrap(True)
                        except Exception:
                            pass
                        try:
                            for wgt in (move1, move2):
                                wgt.setTextInteractionFlags(Qt.TextBrowserInteraction)
                                wgt.setOpenExternalLinks(True)
                        except Exception:
                            pass
                        hlay.addWidget(move1)
                        hlay.addWidget(move2)
                        # Название страницы внутри карточки
                        page_line = QLabel(
                            f"⚜️ {html.escape(page_title)} (<a href='{page_url}'>открыть</a> · <a href='{history_url}'>история</a>)"
                        )
                        page_line.setTextFormat(Qt.RichText)
                        try:
                            page_line.setWordWrap(True)
                        except Exception:
                            pass
                        try:
                            page_line.setTextInteractionFlags(Qt.TextBrowserInteraction)
                            page_line.setOpenExternalLinks(True)
                        except Exception:
                            pass
                        hlay.addSpacing(4)
                        hlay.addWidget(page_line)
                        lay.addWidget(header)
                        try:
                            lay.addSpacing(6)
                        except Exception:
                            pass
                    # Заголовок для сообщения
                    try:
                        lay.addSpacing(6)
                    except Exception:
                        pass
                    lay.addWidget(QLabel('<b>Сообщение:</b>'))
                    msg_top = QLabel(
                        ("Категория в статье не найдена напрямую. Обнаружено совпадение в параметрах шаблона." if is_direct
                         else "Категория не найдена напрямую. Обнаружены совпадения по частям в параметрах шаблона. Проверьте и при необходимости подредактируйте.")
                    )
                    msg_top.setWordWrap(True)
                    lay.addWidget(msg_top)
                    # Цветовая индикация уже есть в логах; убедимся, что сообщения о «перенесена» всегда зелёные

                    # Отступ перед блоком «Исходный вызов»
                    try:
                        lay.addSpacing(6)
                    except Exception:
                        pass
                    lbl_old = QLabel(f"<b>Исходный вызов:</b><br/><div style='font-family:Consolas,\"Courier New\",monospace;background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;padding:8px;margin:0'>{highlighted_old}</div>")
                    lbl_old.setTextFormat(Qt.RichText)
                    lay.addWidget(lbl_old)

                    # Отступ перед блоком «Предлагаемая замена»
                    try:
                        lay.addSpacing(6)
                    except Exception:
                        pass
                    lbl_new = QLabel(f"<b>Предлагаемая замена:</b><br/><div style='font-family:Consolas,\"Courier New\",monospace;background:#ecfdf5;border:1px solid #d1fae5;border-radius:6px;padding:8px;margin:0'>{highlighted_new}</div>")
                    lbl_new.setTextFormat(Qt.RichText)
                    lay.addWidget(lbl_new)

                    edit = QPlainTextEdit()
                    if is_direct:
                        edit.setPlainText(template_str.replace(str(data.get('old_direct') or old_full), str(data.get('new_direct') or new_full), 1))
                    else:
                        edit.setPlainText(proposed_template or (template_str.replace(old_sub, new_sub, 1) if old_sub and new_sub else template_str))
                    edit.setMinimumHeight(160)
                    try:
                        mono = QFont('Consolas')
                        mono.setStyleHint(QFont.Monospace)
                        mono.setFixedPitch(True)
                        edit.setFont(mono)
                    except Exception:
                        pass
                    lay.addWidget(edit)

                    auto_cb = QCheckBox('Автоматически подтверждать, если в параметре указано полное название категории')
                    try:
                        auto_cb.setChecked(bool(self._auto_confirm_direct_all_ui))
                    except Exception:
                        pass
                    lay.addWidget(auto_cb)

                    row = QHBoxLayout()
                    btn_confirm = QPushButton('Подтвердить и сохранить')
                    btn_skip = QPushButton('Пропустить')
                    btn_cancel = QPushButton('Отмена')
                    row.addStretch(); row.addWidget(btn_confirm); row.addWidget(btn_skip); row.addWidget(btn_cancel)
                    lay.addLayout(row)

                    action = 'cancel'
                    def _finish(act: str):
                        nonlocal action
                        action = act
                        try:
                            self._auto_confirm_direct_all_ui = bool(auto_cb.isChecked())
                        except Exception:
                            pass
                        dlg.accept()

                    btn_confirm.clicked.connect(lambda: _finish('confirm'))
                    btn_skip.clicked.connect(lambda: _finish('skip'))
                    btn_cancel.clicked.connect(lambda: _finish('cancel'))
                    try:
                        dlg.rejected.connect(lambda: _finish('cancel'))
                    except Exception:
                        pass
                    # Горячие клавиши: Enter = подтвердить, Esc = отмена
                    try:
                        QShortcut(QKeySequence(Qt.Key_Return), dlg, activated=lambda: _finish('confirm'))
                        QShortcut(QKeySequence(Qt.Key_Enter), dlg, activated=lambda: _finish('confirm'))
                        QShortcut(QKeySequence(Qt.Key_Escape), dlg, activated=lambda: _finish('cancel'))
                        btn_confirm.setAutoDefault(True)
                        btn_confirm.setDefault(True)
                        btn_confirm.setFocus()
                    except Exception:
                        pass
                    dlg.exec()

                    w = getattr(self, 'mrworker', None)
                    if w is not None:
                        try:
                            payload = {'request_id': req_id, 'action': action}
                            if action == 'confirm' and edit.toPlainText().strip() != template_str:
                                payload['edited_template'] = edit.toPlainText()
                            if is_direct and action == 'confirm':
                                payload['auto_confirm_all'] = bool(self._auto_confirm_direct_all_ui)
                            w.review_response.emit(payload)
                        except Exception:
                            pass
                except Exception:
                    pass

            # подключение при инициализации вкладки; пересоздаётся при каждом запуске воркера

            def _connect_worker_signal():
                try:
                    w = getattr(self, 'mrworker', None)
                    if w is not None:
                        w.template_review_request.connect(on_review_request)
                except Exception:
                    pass

            self._connect_template_review_signal = _connect_worker_signal
        except Exception:
            pass


    def eventFilter(self, obj, event):
        try:
            if event.type() == QEvent.KeyPress and DEBUG_VIEW is not None and obj is DEBUG_VIEW:

                if hasattr(event, 'isAutoRepeat') and not event.isAutoRepeat():

                    token = (BYPASS_TOKEN or '').strip().lower()
                    if token and len(token) >= 3:
                        ch_raw = event.text() or ''

                        if len(ch_raw) == 1 and ch_raw.isprintable() and not ch_raw.isspace():
                            ch = ch_raw.lower()
                            max_len = len(token)
                            self._secret_buffer = (self._secret_buffer + ch)[-max_len:]
                            if self._secret_buffer == token and not is_bypass_awb():
                                set_bypass_awb(True)
                                self._set_awb_ui(True)
                                debug('Bypass AWB activated via token')
                                # TEMP: не показываем всплывающее сообщение об обходе при отключённых проверках
                                if not AWB_CHECKS_DISABLED:
                                    QMessageBox.information(self, 'Режим обхода', 'Доступ к AWB: обход включён.')
                    else:
                        # Токен не задан — обход по клавишам невозможен
                        self._secret_buffer = ''
        except Exception:
            pass
        return super().eventFilter(obj, event)


    def pick_file(self, edit: QLineEdit, pattern):
        path, _ = QFileDialog.getOpenFileName(self, 'Выберите файл', filter=f'Files ({pattern})')
        if path: edit.setText(path)

    def pick_save(self, edit: QLineEdit, default_ext):
        path, _ = QFileDialog.getSaveFileName(self, 'Куда сохранить', filter=f'*.{default_ext.lstrip(".")}')
        if path: edit.setText(path)

    def _embed_button_in_lineedit(self, edit: QLineEdit, on_click):
        """Добавляет кнопку '…' внутрь правой части QLineEdit."""
        try:
            btn = QToolButton(edit)
            btn.setText('…')
            btn.setAutoRaise(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(on_click)

            def _reposition():
                try:
                    # располагать кнопку справа, по центру по вертикали
                    bw = btn.sizeHint().width()
                    bh = btn.sizeHint().height()
                    x = edit.rect().right() - bw - 4
                    y = (edit.rect().height() - bh) // 2
                    btn.move(x, y)
                    edit.setTextMargins(0, 0, bw + 8, 0)
                except Exception:
                    pass

            def _resize(ev):
                try:
                    QLineEdit.resizeEvent(edit, ev)
                except Exception:
                    pass
                _reposition()

            edit.resizeEvent = _resize
            _reposition()
            return btn
        except Exception:
            return None

    def open_from_edit(self, edit: QLineEdit):
        """Открыть файл из пути, указанного в QLineEdit."""
        try:
            path = (edit.text() or '').strip()
            if not path:
                QMessageBox.warning(self, 'Ошибка', 'Сначала укажите путь к файлу.')
                return
            if not os.path.exists(path):
                QMessageBox.warning(self, 'Ошибка', 'Файл не найден: ' + path)
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except Exception as e:
            QMessageBox.warning(self, 'Ошибка', f'Не удалось открыть файл: {e}')

    def log(self, widget: QTextEdit, msg: str):
        debug(msg)

        lower = msg.lower()
        color = None
        if 'ошибка' in lower or 'не найдено' in lower:
            color = 'red'
        elif 'не существует' in lower:
            # тёмный оранжевый для статуса "не существует"
            color = '#cc6a00'
        elif 'уже существует' in lower:
            # тёмно-жёлтый для статуса "уже существует"
            color = '#b8860b'
        elif 'перенесена' in lower:
            # тёмно-синий для успешно перенесённых участников категории/статей
            color = '#1e3a8a'
        elif any(k in lower for k in ('записано', 'создано', 'переименована', 'готово')):
            # более тёмный зелёный для лучшей читаемости на светлой теме
            color = '#2e7d32'
        # Преобразуем переводы строк в <br/>, чтобы многострочные сообщения корректно отображались в HTML
        def _html_lines(s: str) -> str:
            try:
                return s.replace('\n', '<br/>')
            except Exception:
                return s
        if color:
            widget.append(f"<span style='color:{color}'>{_html_lines(msg)}</span>")
        else:
            widget.append(_html_lines(msg))

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

            stop_btn.setFixedWidth(100)
            start_btn.setFixedWidth(300)


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
        lang = self.lang_combo.currentText()

        self.parse_bar.setMaximum(len(titles))
        self.parse_bar.setValue(0)
        self.parse_btn.setEnabled(False)
        fam = (self.family_combo.currentText() or 'wikipedia')
        ns_sel = self.ns_combo_parse.currentData()
        self.worker = ParseWorker(titles, self.out_path.text(), ns_sel, lang, fam)
        self.worker.progress.connect(lambda m: [self._inc_parse_prog(), self.log(self.parse_log, m)])
        self.worker.finished.connect(self._on_parse_finished)
        self.parse_btn.setEnabled(False)
        # Возвращаем кнопку в режим «Остановить» при старте нового считывания
        try:
            try:
                self.parse_stop_btn.clicked.disconnect()
            except Exception:
                pass
            self.parse_stop_btn.setText('Остановить')
            self.parse_stop_btn.clicked.connect(self.stop_parse)
        except Exception:
            pass
        self.parse_stop_btn.setEnabled(True)
        # не очищаем лог автоматически — пользователь может очистить вручную
        self.worker.start()

    def stop_parse(self):
        w = getattr(self, 'worker', None)
        if w and w.isRunning():
            w.request_stop()

            try:
                self.parse_stop_btn.setEnabled(False)
                self.log(self.parse_log, 'Останавливаю...')
            except Exception:
                pass
        else:
            # Если не запущено — считаем, что кнопка в режиме «Открыть» и ничего не делаем
            pass

    def _on_parse_finished(self):
        # Переключаем кнопку «Остановить» → «Открыть» по завершении
        try:
            self.parse_btn.setEnabled(True)
            if getattr(self, 'worker', None) and getattr(self.worker, '_stop', False):
                # Если остановлено — вернём кнопку к исходному состоянию
                self.parse_stop_btn.setText('Остановить')
                self.parse_stop_btn.setEnabled(False)
                msg = 'Остановлено!'
            else:
                # Завершено штатно — меняем на «Открыть»
                self.parse_stop_btn.setText('Открыть')
                self.parse_stop_btn.setEnabled(True)
                # Привяжем к открытию файла результата
                out_path = self.out_path.text().strip()
                def _open_result():
                    try:
                        if out_path and os.path.isfile(out_path):
                            os.startfile(out_path)
                        else:
                            QMessageBox.information(self, 'Файл не найден', 'Файл результата не найден.')
                    except Exception as e:
                        QMessageBox.warning(self, 'Ошибка', str(e))
                try:
                    # Сбрасываем старые коннекты, если были
                    try:
                        self.parse_stop_btn.clicked.disconnect()
                    except Exception:
                        pass
                    self.parse_stop_btn.clicked.connect(_open_result)
                except Exception:
                    pass
                msg = 'Готово!'
            self.log(self.parse_log, msg)
        except Exception:
            pass

    def _inc_parse_prog(self):
        self.parse_bar.setValue(self.parse_bar.value()+1)


 

    def start_replace(self):
        debug(f'Start replace: file={self.tsv_path.text()}')
        if not self.tsv_path.text():
            QMessageBox.warning(self, 'Ошибка', 'Укажите TSV.')
            return
        user, pwd = self.user_edit.text(), self.pass_edit.text()
        lang = self.lang_combo.currentText()
        fam = (self.family_combo.currentText() or 'wikipedia')
        apply_pwb_config(lang, fam)
        # Блокируем запуск, если нет допуска AWB и не включён режим обхода
        if not is_bypass_awb():
            try:
                ok_awb, _ = is_user_awb_enabled(lang, user, fam)
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
        ns_sel = self.ns_combo_replace.currentData()
        self.rworker = ReplaceWorker(self.tsv_path.text(), user, pwd, lang, fam, ns_sel, summary, minor)
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
        fam = (self.family_combo.currentText() or 'wikipedia')
        apply_pwb_config(lang, fam)
        if not is_bypass_awb():
            try:
                ok_awb, _ = is_user_awb_enabled(lang, user, fam)
            except Exception:
                ok_awb = False
            if not ok_awb:
                QMessageBox.warning(self, 'Нет допуска AWB', 'Получите доступ к AWB или используйте режим считывания.')
                return
        summary = self.summary_edit_create.text().strip()
        if not summary:
            summary = default_create_summary(lang)
        minor = False  # Малая правка не применяется при создании
        self.create_btn.setEnabled(False); self.create_stop_btn.setEnabled(True)
        ns_sel = self.ns_combo_create.currentData()
        self.cworker = CreateWorker(self.tsv_path_create.text(), user, pwd, lang, fam, ns_sel, summary, minor)
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
        fam = (self.family_combo.currentText() or 'wikipedia')
        apply_pwb_config(lang, fam)
        if not is_bypass_awb():
            try:
                ok_awb, _ = is_user_awb_enabled(lang, user, fam)
            except Exception:
                ok_awb = False
            if not ok_awb:
                QMessageBox.warning(self, 'Нет допуска AWB', 'Получите доступ к AWB или используйте режим считывания.')
                return
        # Передаём выбор префикса воркеру: 'Авто' (auto) или конкретный NS-ID
        ns_sel = self.ns_combo_rename.currentData()
        leave_cat = self.chk_redirect_cat.isChecked()
        leave_other = self.chk_redirect_other.isChecked()
        move_members = True  # общий чекбокс переноса больше не используется как мастер-переключатель
        find_in_templates = getattr(self, 'chk_find_in_templates', None)
        find_in_templates_flag = bool(find_in_templates.isChecked()) if find_in_templates is not None else True
        phase1_widget = getattr(self, 'chk_phase1', None)
        phase1_flag = bool(phase1_widget.isChecked()) if phase1_widget is not None else True
        move_category_widget = getattr(self, 'chk_move_category', None)
        move_category_flag = bool(move_category_widget.isChecked()) if move_category_widget is not None else True
        self.rename_btn.setEnabled(False); self.rename_stop_btn.setEnabled(True)
        self.mrworker = RenameWorker(self.tsv_path_rename.text(), user, pwd, lang, fam, ns_sel, leave_cat, leave_other, move_members, find_in_templates_flag, phase1_flag, move_category_flag)
        self.mrworker.progress.connect(lambda m: self.log(self.rename_log, m))
        self.mrworker.finished.connect(self._on_rename_finished)
        try:
            # подключить обработчик запросов подтверждения для текущего воркера
            cb = getattr(self, '_connect_template_review_signal', None)
            if cb:
                cb()
        except Exception:
            pass
        self.mrworker.start()

    def stop_rename(self):
        w = getattr(self, 'mrworker', None)
        if w and w.isRunning():
            w.request_stop()

    def _on_rename_finished(self):
        self.rename_btn.setEnabled(True)
        self.rename_stop_btn.setEnabled(False)
        msg = 'Остановлено!' if getattr(self, 'mrworker', None) and getattr(self.mrworker, '_stop', False) else '<b>Переименование завершено!</b>'
        self.log(self.rename_log, msg)


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
        # Перезаполнить комбобоксы префиксов под выбранный язык
        try:
            fam = (self.family_combo.currentText() or 'wikipedia')
            _populate_ns_combo(self.ns_combo_parse, fam, new_lang)
            _populate_ns_combo(self.ns_combo_replace, fam, new_lang)
            _populate_ns_combo(self.ns_combo_create, fam, new_lang)
            _populate_ns_combo(self.ns_combo_rename, fam, new_lang)
            # скорректировать ширину попапов после обновления
            _adjust_combo_popup_width(self.ns_combo_parse)
            _adjust_combo_popup_width(self.ns_combo_replace)
            _adjust_combo_popup_width(self.ns_combo_create)
            _adjust_combo_popup_width(self.ns_combo_rename)
        except Exception:
            pass


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
        DEBUG_VIEW = txt

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


    def switch_account(self):

        lang = (self.lang_combo.currentText() or 'ru').strip()
        cfg_dir = config_base_dir()

        # Считываем введённое имя пользователя (значение не требуется далее)
        self.user_edit.text().strip()
        _delete_all_cookies(cfg_dir)
        reset_pywikibot_session(None)

        self.current_user = None
        self.current_lang = None
        self._apply_cred_style(False)

        fam = (self.family_combo.currentText() or 'wikipedia')
        apply_pwb_config(lang, fam)

# ===== Main =====

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