# Wiki Category Tool

> ## 🇷🇺 [Русская версия](README.md)

[![Latest release](https://img.shields.io/github/v/release/sc113/wiki-category-tool?display_name=tag&sort=semver)](https://github.com/sc113/wiki-category-tool/releases/latest)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)

Wiki Category Tool is a desktop application for batch-processing Wikimedia pages and categories through a Qt interface. It combines `PySide6`, `pywikibot`, and the MediaWiki API to make repetitive maintenance work easier to preview, run, and review.

The application is designed for editors and wiki maintainers who work with TSV-based page lists, category migrations, bulk page creation or replacement, redundant category cleanup, and cross-language category synchronization.

> [Download the latest Windows release](https://github.com/sc113/wiki-category-tool/releases/latest) or follow the instructions below to run the application from source.

## Screenshots

### Overview and operation statistics

![Overview dashboard showing session details, settings, statistics, and operation history](assets/screenshots/overview.png)

<table>
  <tr>
    <td width="50%">
      <strong>Read pages to TSV</strong><br>
      <img src="assets/screenshots/read-pages.png" alt="Read pages and export their contents to TSV">
    </td>
    <td width="50%">
      <strong>Rename pages and move category members</strong><br>
      <img src="assets/screenshots/rename-and-move.png" alt="Rename pages and transfer category contents">
    </td>
  </tr>
  <tr>
    <td width="50%">
      <strong>Redundant category cleanup</strong><br>
      <img src="assets/screenshots/redundant-categories.png" alt="Preview and remove redundant categories">
    </td>
    <td width="50%">
      <strong>Cross-language category synchronization</strong><br>
      <img src="assets/screenshots/category-sync.png" alt="Synchronize category membership between languages">
    </td>
  </tr>
</table>

## Features

- Sign in to Wikimedia projects through `pywikibot`, including interactive two-factor authentication.
- Read page or category contents and export them to UTF-8 TSV files.
- Replace existing pages or create missing pages in bulk from TSV input.
- Rename pages and optionally move direct category links and template-based categorization.
- Preview and remove redundant broad categories when more precise categories are present.
- Remove duplicate target categories using a one-column TSV list.
- Synchronize articles and subcategories between language editions using Wikidata sitelinks and fallback matching.
- Work with localized namespace names and aliases, or force titles into a selected namespace.
- Preview write operations before starting them.
- Review execution logs, progress, edit statistics, and operation history.
- Use the interface in English or Russian with multiple visual themes.

## Requirements

- Python 3.10 or newer
- Windows 10/11 for the primary desktop target
- `PySide6`, `pywikibot`, `requests`, and `packaging`

Running from source on other operating systems may work, but Windows is the main supported and packaged platform.

## Installation

Clone the repository and create a virtual environment:

```powershell
git clone https://github.com/sc113/wiki-category-tool.git
cd wiki-category-tool
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Running from source

From the repository root:

```powershell
python __main__.py
```

When the repository directory is available as the `wiki_cat_tool` package from its parent directory, you can also run:

```powershell
python -m wiki_cat_tool
```

Running `python main.py` directly is not recommended because the project uses package-relative imports.

## TSV formats

TSV files should normally be saved as UTF-8 with BOM.

### Create or replace pages

```text
Title<TAB>Line 1<TAB>Line 2<TAB>...
```

The first column contains the page title. Remaining columns are joined with newline characters to form the page text.

### Rename pages

```text
OldTitle<TAB>NewTitle<TAB>Optional comment
```

The third column is optional. A global edit summary entered in the application can override per-row comments.

### Remove redundant categories

Pair mode removes a broad category when the corresponding precise category is present:

```text
PreciseCategory<TAB>BroadCategory
```

A one-column file enables duplicate-category cleanup mode:

```text
CategoryToDeduplicate
```

## Application sections

- **Overview** — session information, interface settings, updates, statistics, and history.
- **Read** — collect pages or subcategories and export page contents to TSV.
- **Replace** — overwrite existing pages from TSV.
- **Create** — create only pages that do not already exist.
- **Rename** — rename pages and transfer category membership, including categories stored in template parameters.
- **Redundant categories** — preview and remove broad or duplicate categories.
- **Category sync** — transfer category membership between language editions.

## Runtime data

The `configs/` directory is created automatically and may contain:

- `user-config.py` and `user-password.py` for `pywikibot` configuration;
- `pywikibot*.lwp` cookie files;
- `template_rules.json` with saved template replacement rules;
- `update_settings.json` with update preferences;
- `apicache/` with API and namespace metadata.

These files are environment-specific and may contain authentication data. Do not commit or share them.

## Project structure

- `gui/` — the main window, tabs, dialogs, and reusable widgets.
- `workers/` — background workers for batch operations.
- `core/` — the API client, namespace handling, localization, and configuration.
- `locales/` — English and Russian interface dictionaries.
- `assets/` — interface resources and screenshots.

## Building the Windows executable

```powershell
python -m pip install pyinstaller
pyinstaller WikiCatTool.spec
```

The executable is written to `dist/WikiCatTool.exe`.

## Responsible use

- Test batch operations on a small page set before processing a large list.
- Follow the local policies of each Wikimedia project.
- Review previews and edit summaries before starting write operations.
- Make sure the account has the permissions required for the requested action.

## License

Wiki Category Tool is distributed under the `GPL-3.0-or-later` license. See [LICENSE](LICENSE) for the full license text.
