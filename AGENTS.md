# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project overview
Wiki Category Tool is a PySide6 GUI for batch operations on Wikimedia projects (Wikipedia, Commons, etc.) via **pywikibot** and direct **MediaWiki API** calls:
- Read page lists / subcategories and export to TSV
- Bulk replace existing pages from TSV
- Bulk create missing pages from TSV
- Rename pages and (optionally) migrate category membership, including template-parameter based categorization with interactive review + cached rules

## Common commands

### Install dependencies (dev)
Python 3.10+ (per `README.md`).

PowerShell (Windows):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Run from source
This repo’s package name is `wiki_cat_tool`. Running with `-m` requires the *parent directory* of this repo on `sys.path`.

From the directory that contains this repo (one level above `wiki_cat_tool/`):
```bash
python -m wiki_cat_tool
```

From *inside* the repo root (this folder), use:
```bash
python __main__.py
```

Avoid running `python main.py` directly; it uses package-relative imports.

### Build Windows executable (PyInstaller)
Build via the included spec (recommended):
```bash
python -m pip install pyinstaller
pyinstaller WikiCatTool.spec
# output: dist/WikiCatTool.exe
```

Alternative command (see `compile.txt`):
```bash
pyinstaller --noconfirm --clean --onefile --name WikiCatTool --windowed --icon icon.ico --collect-all pywikibot --collect-submodules wiki_cat_tool --add-data "icon.ico;." --paths .. __main__.py
```

### Lint / tests
No linting or test runner is currently configured in-repo (no `pyproject.toml`, `setup.cfg`, `tox.ini`, or `tests/` folder were found).

## Architecture (big picture)

### Entry points / startup
- `__main__.py` is the module entrypoint used by `python -m wiki_cat_tool` and by the PyInstaller spec. It adjusts `sys.path` then calls `wiki_cat_tool.main.main()`.
- `main.py` does early environment setup (configs path via `core/pywikibot_config.ensure_base_env()`), redirects stdout/stderr into the GUI (`utils.setup_gui_stdout_redirect()`), hooks pywikibot logging into `utils.debug()`, starts the Qt app, builds `gui/main_window.MainWindow`, and runs an async update check (`UpdateCheckerThread`).

### GUI layer (`gui/`)
The GUI is a `QMainWindow` with a `QTabWidget`:
- `gui/main_window.py` wires the app together and owns shared “core” singletons:
  - `core/api_client.WikimediaAPIClient` (HTTP session + rate limiting)
  - `core/namespace_manager.NamespaceManager` (NS prefixes cache + title normalization)
  - `core/pywikibot_config.PywikibotConfigManager` (pywikibot config/cookies/credentials in `configs/`)
  - `core/template_manager.TemplateManager` (template replacement rules cache in `configs/template_rules.json`)
  It also keeps the current auth state (`current_user/current_password/current_lang/current_family`) and synchronizes the namespace dropdown across tabs.

Tabs map 1:1 to background workers:
- `gui/tabs/auth_tab.py` → `workers/login_worker.LoginWorker` (writes pywikibot configs, clears cookies, logs in)
- `gui/tabs/parse_tab.py` → `workers/parse_worker.ParseWorker` (read-only API fetch → TSV; can fetch subcategories / open PetScan)
- `gui/tabs/replace_tab.py` → `workers/replace_worker.ReplaceWorker` (overwrite existing pages from TSV via pywikibot)
- `gui/tabs/create_tab.py` → `workers/create_worker.CreateWorker` (create missing pages from TSV via pywikibot)
- `gui/tabs/rename_tab.py` → `workers/rename_worker.RenameWorker` (rename + category member migration; integrates template review UI)

Shared UI utilities:
- `gui/widgets/ui_helpers.py` centralizes file pickers, log helpers (including the tree-log used by RenameTab), progress helpers, etc.

Dialogs:
- `gui/dialogs/template_review_dialog.py` is the interactive dialog used during rename when template-parameter based categorization needs confirmation/edits; its decisions can be persisted as rules.
- `gui/dialogs/debug_dialog.py` displays the in-memory debug log (`utils.DEBUG_BUFFER`) and receives new log lines via the Qt signal bridge (`utils.get_debug_bridge()`).

### Worker layer (`workers/`)
Workers are `QThread`s so the GUI stays responsive during network/edit operations.
- `workers/base_worker.BaseWorker` provides shared save throttling + retry logic around `pywikibot.Page.save()` and supports cooperative cancellation (`request_stop()` / `graceful_stop()`).
- `workers/parse_worker.ParseWorker` does not require login; it uses `core/api_client.fetch_contents_batch()` (50 titles per API request) and writes TSV incrementally (`utf-8-sig`).
- `workers/replace_worker.ReplaceWorker` and `workers/create_worker.CreateWorker` both:
  - normalize titles using `core/namespace_manager.normalize_title_by_selection()`
  - read TSV as `utf-8-sig` to handle BOM from Excel
  - use `BaseWorker._save_with_retry()` for edits
- `workers/rename_worker.RenameWorker` is the most complex worker:
  - reads TSV rows (`OldTitle\tNewTitle\tComment`)
  - optionally renames pages and/or migrates category membership
  - “phase 1” edits direct category links `[[Category:Old|...]]`
  - “phase 2” searches template parameters and can open `TemplateReviewDialog` for ambiguous edits
  - uses `core/template_manager.TemplateManager` to persist “auto approve/skip” and dedupe behavior in `configs/template_rules.json`
  - emits both string logs (`progress`) and structured events (`log_event`) plus progress-bar signals for TSV-level and per-category member progress

### Core layer (`core/`)
- `core/api_client.py`: shared `requests.Session` with rate limiting/backoff; also fetches namespace info for caching (`NamespaceManager`) and batch page content for ParseWorker.
- `core/namespace_manager.py`: loads/caches namespace prefixes to `configs/apicache/ns_<family>_<lang>.json`; recognizes local + English prefixes and normalizes titles for all TSV-driven operations.
- `core/pywikibot_config.py`: decides where `configs/` lives (next to exe or source), sets `PYWIKIBOT_DIR`, writes `configs/user-config.py` and `configs/user-password.py`, manages cookie cleanup and pywikibot session resets.
- `core/template_manager.py`: reads/writes `configs/template_rules.json` and applies cached rules when possible; supports per-project scoping (`family::lang::template`) and reload-on-change.
- `core/update_checker.py` + `core/update_settings.py`: GitHub release polling and local skip state (`configs/update_settings.json`).

### Repo state & data files
- `configs/` is runtime state (created/updated when the app runs). It contains pywikibot config, cookies, namespace caches, and template rules. Treat it as environment-specific.
- Default TSV examples often use `categories.tsv` and the GUI defaults to that filename.
