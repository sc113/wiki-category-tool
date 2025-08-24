# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

pywikibot_datas, pywikibot_binaries, pywikibot_hidden = collect_all('pywikibot')



a = Analysis(
    ['wiki_cat_tool_qt.py'],
    pathex=[],
    binaries=pywikibot_binaries,
    datas=pywikibot_datas + [
        ('configs', 'configs')
    ],
    hiddenimports=pywikibot_hidden + [
        '_embedded_secrets'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='wiki_cat_tool_qt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
