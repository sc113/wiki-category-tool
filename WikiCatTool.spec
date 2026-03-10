# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('icon.ico', '.')]
# Включаем локали и ассеты в сборку (onefile/onedir),
# чтобы в frozen-режиме не пропадали подписи и иконки интерфейса.
for _src_dir, _dst_root in (('locales', 'locales'), ('assets', 'assets')):
    if not os.path.isdir(_src_dir):
        continue
    for _root, _dirs, _files in os.walk(_src_dir):
        _dirs[:] = [d for d in _dirs if d != '__pycache__']
        _rel = os.path.relpath(_root, _src_dir)
        _dst_dir = _dst_root if _rel in ('.', '') else os.path.join(_dst_root, _rel)
        for _name in _files:
            if _src_dir == 'locales' and not _name.lower().endswith('.json'):
                continue
            _src = os.path.join(_root, _name)
            datas.append((_src, _dst_dir))
binaries = []
hiddenimports = []
hiddenimports += collect_submodules('wiki_cat_tool')
# pywikibot сам импортируется статически, но семейства грузятся динамически.
# Подключаем только pywikibot.families, чтобы не тянуть scripts/* и тяжёлые лишние зависимости.
hiddenimports += collect_submodules('pywikibot.families')
# Интерфейс pywikibot выбирается динамически (set_interface -> terminal_interface).
# Добавляем только terminal-* модули, без gui-интерфейса.
hiddenimports += [
    'pywikibot.userinterfaces',
    'pywikibot.userinterfaces._interface_base',
    'pywikibot.userinterfaces.transliteration',
    'pywikibot.userinterfaces.buffer_interface',
    'pywikibot.userinterfaces.terminal_interface',
    'pywikibot.userinterfaces.terminal_interface_base',
    'pywikibot.userinterfaces.terminal_interface_win32',
    'pywikibot.userinterfaces.terminal_interface_unix',
]
# pywikibot.config ожидает физическую папку pywikibot/families на диске.
# Добавляем только family-файлы как datas (без полного collect_all).
_pywikibot_datas = collect_all('pywikibot')[0]
datas += [
    (src, dst) for (src, dst) in _pywikibot_datas
    if str(dst).replace('/', '\\').startswith('pywikibot\\families')
]


a = Analysis(
    ['__main__.py'],
    pathex=['..'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Не используем pywikibot GUI-редактор; эти зависимости только раздувают onefile.
        'pywikibot.userinterfaces.gui',
        'idlelib',
        'tkinter',
        '_tkinter',
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='WikiCatTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',
)
