# Wiki Category Tool

> ## [In Russian](README.md)

[![Latest release](https://img.shields.io/github/v/release/sc113/wiki-category-tool?display_name=tag&sort=semver)](https://github.com/sc113/wiki-category-tool/releases/latest)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)

Wiki Category Tool is a desktop application for batch-processing Wikimedia pages and categories through a Qt interface. It combines `PySide6`, `pywikibot`, and the MediaWiki API to make repetitive maintenance work easier to preview, run, and review.

The application is designed for editors and wiki maintainers who work with TSV-based page lists, category migrations, bulk page creation or replacement, redundant category cleanup, and cross-language category synchronization.

> [Download the latest Windows release](https://github.com/sc113/wiki-category-tool/releases/latest) or follow the instructions below to run the application from source.

## Screenshots

### Overview and operation statistics

![Overview dashboard showing session details, settings, statistics, and operation history](assets/screenshots/overview.png)

The overview includes scanning, preparation, and interwiki linking. Scanning counts processed categories; preparation and linking count successful page and Wikidata edits.

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
- **Category transfer** — category scanning, preparation, content synchronization. Use each stage independently or carry lists through the sequence. A separate Interwiki linking tab handles existing category pages.

### Category transfer

The sidebar groups the three stages inside a thin border. Next: preparation carries the curated scanned list directly to the second-stage table. Edit titles and wikitext there without an intermediate file, or open this stage independently and import TSV.

Each stage keeps its own list. Loading, scanning or deleting rows on stage one preserves stage two's input; Next explicitly replaces the following stage's list. Languages sit at the top. File controls use the same caption, path, browse and Open row as existing tabs. Browse selects a file; Open loads it into the table. The path field menu opens it in an editor. Export sits in Table tools. The shared working file is categories.tsv. Scanning automatically detects a one-column category list or a saved scan report. Depth 0 reads direct subcategories; each further level adds nesting, as in the Reading tab. Progress and traversal packets appear in the log. Settings are to the right of the languages and explained by the ? button. Clear all resets the current stage only and supports Ctrl+Z. Add list appends manually entered titles to the current table. Blank lines and exact duplicates are omitted; Ctrl+Z undoes the addition. Namespace validation uses the source language during scanning. Opening a file replaces only the first-stage list. Export offers a report with results or a separate creation list.

Set the source language, root category and traversal depth, then click Read on the left above the table. Category to read, with depth on its right, is followed by Ready category list and its file controls. The category belongs to the source language. Depth defaults to 0. Read replaces the table. The root is level 0 and can optionally be included. All branches are traversed by default; the optional skip level excludes categories with an existing counterpart and their branches (0 disables skipping).

Remove unwanted rows and scan the remaining list. Counts include direct mainspace articles with existing, non-redirect counterparts linked through Wikidata in the current language. Duplicates are excluded. Clear empty results removes successful zero-result rows. Retry errors scans only failed rows and preserves successful results. Requests are spaced by 0.5 seconds by default; on API throttling the program respects Retry-After and reduces the request rate.

After stopping, Continue scan checks unfinished rows and errors only. Reset scan retains the list and clears results for a complete rescan; Ctrl+Z undoes the reset. Batches of five categories share link and destination-page requests.

Click the Available count to inspect matched articles. For a linked category, the dialog also shows how many are already included and how many can be added. Current membership is checked only when opening this dialog; older scan-report TSV files remain supported.

Export TSV saves the entire scan table to one file. Reopen it in scanning or import it in preparation: titles are preserved and metadata stays out of category wikitext.

Preparation TSV has no header:

```text
SourceCategory<TAB>CurrentLanguageCategory<TAB>Wikitext line 1<TAB>Wikitext line 2<TAB>...
```

Fill in title translations and category wikitext in the table or TSV. Checking links fills existing counterpart titles automatically. Columns 3 onward form the **category page text**; articles are added separately through synchronization. New categories need nonempty wikitext. You can also export the second-stage table to TSV.

Editing tools sit below the table in a Table tools frame, with Autofill expanding underneath. Checking and creation sit below it; the next-stage button sits separately on the right. Source language is editable; the current language appears in a separate badge and comes from the active profile.

In the second-column panel, enter a translated title template with `{number}` or `{roman}` (Russian aliases `{число}` and `{римская}` also work). For example, `Category:20th-century art` can supply `20` or `XX`. Numbers, English ordinal suffixes and Roman numerals are detected automatically; an optional source pattern restricts a mixed list to one series. Preview each substitution and apply it to the whole list or selected rows. Ambiguous numbers and conflicting titles are skipped. Fill empty cells only, beside the column tabs, applies to all autofill actions and is off by default. An existing translation with the same number supplies a suggested template. Check the list again after filling.

Fill wikitext adds the same text to the whole third column or selected rows, respecting the shared empty-cell setting. Select a third-column cell and drag its bottom-right handle across the desired rows to copy its text. Double-click the handle to fill the cells below, respecting the same setting. Delete removes selected rows; inside a cell editor it deletes characters. Check the list again after changes. Underlined Wikidata links open with a single click. Operation logs and request diagnostics also appear in the shared debug view.

In the third-column panel, First row to column copies the first row's text into the whole column, respecting the shared empty-cell setting. Source category text loads the foreign categories' wikitext from column one for manual translation; it also respects the shared empty-cell setting. Translate and review that text before creating categories.

Check links identifies categories needing creation, existing pages needing linking and already completed categories. Missing translations and new category text have separate Needs translation and Needs wikitext statuses with highlighted cells. Next incomplete row navigates between them. Counters above the table separate ready rows, linked categories, incomplete rows and check errors. Details for the selected row appear below the table. The creation summary sits to the right of Start creation.

Check the list again after filling in missing fields. After confirmation, missing pages are created and Wikidata sitelinks and missing labels are added. New items also get P31 = Wikimedia category (Q4167836). Existing wikitext and occupied sitelinks are preserved; conflicts are shown as errors.

All transfer stages and interwiki linking place languages at the top; settings sit to the right of languages in one row with separators. Preparation and interwiki linking have no additional settings. Request delays are managed automatically. Table tools sit below the table in a labeled frame, followed by processing actions and progress. Counters and status appear inside the progress bar, with Stop on the right. Export TSV sits directly to the left of the next-stage button in the bottom-right corner. The table and log share one frame. Its top row contains Log and counts or a short next-step hint. Available next actions have an accent outline; empty-table operations are disabled. All four pages share the same progress and Stop footer component. The log opens on the right, can be resized, and hides completely to free table space. Processing opens the log automatically. In scanning and synchronization, Prefixes sits in the upper-right header. The sync summary sits to the left of the start button at the right edge, and the Category list and Preview tabs switch the left area without losing the input list or mapping table.

The confirmation shows an action plan separating creation and linking, links for existing categories, label-only updates, and skipped rows. Ctrl+Z undoes local edits, row deletion, and bulk filling in all working tables; Ctrl+Y redoes them. Cell editors keep their own text undo. A new check or write operation clears the affected table's history; Ctrl+Z does not undo edits on wiki sites.

After stopping or a partial failure, check the list again and use Start creation. One run creates missing categories and links existing ones, skipping completed rows. Link only handles existing pages without creating missing pages. Checking opens the log immediately and displays request stages and progress. All preparation and linking columns support sorting. Both item IDs are visible. Generated Wikidata labels include the localized category namespace; Wikidata edit summaries are always English. When sitelinks already exist and only labels change, the row shows Labels updated and is counted separately from new links. Next: synchronization carries all successfully linked categories, including previously completed ones, the source language and source/target pairs with QIDs. The usual sync preview is required before edits. Transferred links are checked again; changed links require checking stage two again. Scanning and synchronization share the same lookup of existing articles through Wikidata.

For a separate operation on existing pages, open Interwiki linking outside the Category transfer group. Its TSV has no header: source category in column one, current-language category in column two. Paste from clipboard inserts two columns copied from Excel or TSV; you can also add rows manually and fill translated titles with the same numeric templates. Stage two's TSV is accepted too; extra columns are ignored.

Check the list and confirm linking. If only one category has a Wikidata item, the other sitelink and missing labels are added to it. Identical items are skipped. If neither has an item, a new one is created with P31 = Q4167836. Wikidata writes use the active profile, including when existing category pages only need linking. This tab does not create category pages.

Pairs with distinct items appear in a conflict report. Save the report as TSV and copy or save `MERGE<TAB>newer QID<TAB>older QID` commands for QuickStatements. The lower QID is kept and duplicate commands are removed. The user performs merges through QuickStatements. Next: synchronization carries successfully linked pairs into content synchronization.

When opening categories.tsv, synchronization uses the target column from transfer files and keeps source/target pairs; a plain target list uses column one.

Synchronization columns are source category, target category, then Wikidata. Item lookup checks Wikidata directly when Wikipedia pageprops lag after a new sitelink. Changed transferred links appear as errors while retaining their source titles.

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
