# -*- coding: utf-8 -*-
"""
Константы для Wiki Category Tool.

Этот модуль содержит все глобальные константы, используемые в приложении:
- Версия приложения и URL-адреса
- HTTP заголовки и настройки API
- Пространства имен и префиксы
- Стили CSS для UI
- Tooltip тексты
- Настройки rate limiting
"""

# ===== Версия приложения и URL-адреса =====
APP_VERSION = "1.01"
RELEASES_URL = 'https://github.com/sc113/wiki-category-tool/releases'
GITHUB_API_RELEASES = 'https://api.github.com/repos/sc113/wiki-category-tool/releases'

# ===== HTTP заголовки и настройки API =====
USER_AGENT = f'WikiCatTool/{APP_VERSION} (+https://github.com/sc113/wiki-category-tool; contact:none) requests'
REQUEST_HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept': 'application/json'
}

# ===== Rate limiting настройки =====
MIN_REQUEST_INTERVAL = 0.12
MAX_RATE_INTERVAL = 2.5

# ===== Пространства имен - английские префиксы =====
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

# ===== Английские алиасы префиксов =====
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

# ===== Tooltip тексты =====
PREFIX_TOOLTIP = (
    "Для любого языка локальные префиксы автоматически\n"
    "подгружаются из API и кэшируются.\n\n"
    "Префиксы пространств имён:\n"
    "• «Авто» — не меняет заголовки; распознаёт все префиксы.\n"
    "• Выбор пространства — подставляет локальный префикс к названиям без префикса.\n"
    "• Английские префиксы в исходных данных распознаются.\n\n"
)

# ===== Стили CSS для UI =====
TAB_STYLESHEET = "QWidget { font-size: 13px; } QLineEdit, QComboBox, QPushButton { min-height: 30px; }"

GROUP_BOX_STYLESHEET = (
    "QGroupBox { border: 1px solid lightgray; border-radius: 5px; margin-top: 10px; } "
    "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; }"
)

CLEAR_BUTTON_STYLESHEET = 'font-size: 20px; padding: 0px;'

CRED_OK_STYLESHEET = 'background-color:#d4edda'
CRED_DEFAULT_STYLESHEET = ''

# ===== Пути и конфигурация =====
CONFIG_DIR_NAME = "configs"
CACHE_DIR_NAME = "apicache"
TEMPLATE_RULES_FILE = "template_rules.json"

# ===== Настройки операций =====
DEFAULT_TIMEOUT = 6
DEFAULT_RETRIES = 5
MAX_WORKERS = 8

# ===== Rate limiting для сохранений =====
BASE_SAVE_INTERVAL = 0.25
MAX_SAVE_INTERVAL = 2.5
SAVE_RETRY_ATTEMPTS = 6

# ===== Языковые настройки =====
SUPPORTED_LANGUAGES = ['ru', 'uk', 'be', 'en', 'fr', 'es', 'de']
SUPPORTED_FAMILIES = [
    'wikipedia', 'commons', 'wikibooks', 'wikinews', 'wikiquote', 
    'wikisource', 'wikiversity', 'wikivoyage', 'wiktionary', 
    'wikidata', 'meta', 'species', 'incubator', 'mediawiki', 'wikifunctions'
]

# ===== Размеры UI элементов =====
CLEAR_BUTTON_SIZE = 32
MIN_LOG_HEIGHT = 220
DIALOG_DEBUG_SIZE = (800, 600)
DIALOG_TEMPLATE_REVIEW_SIZE = (900, 700)

# ===== Форматирование логов =====
LOG_TIMESTAMP_FORMAT = '%H:%M:%S'

# ===== Стили для диалогов =====
REVIEW_HEADER_STYLESHEET = (
    "QFrame#reviewHeader { background:#f8fafc; border:1px solid #e5e7eb; border-radius:10px; } "
    "QLabel { font-size:13px; }"
)

TEMPLATE_OLD_STYLE = (
    "font-family:Consolas,\"Courier New\",monospace;"
    "background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;"
    "padding:2px 8px 2px 8px;margin:0"
)

TEMPLATE_NEW_STYLE = (
    "font-family:Consolas,\"Courier New\",monospace;"
    "background:#ecfdf5;border:1px solid #d1fae5;border-radius:6px;"
    "padding:2px 8px 2px 8px;margin:0"
)

# ===== Цвета для подсветки =====
HIGHLIGHT_OLD_COLOR = "#8b0000"
HIGHLIGHT_NEW_COLOR = "#0b6623"

