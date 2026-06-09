# Vault Capture

Vault Capture is a local Windows workflow for collecting web pages into an Obsidian vault and a Notion calendar database. It runs a small local web page, extracts and summarizes a URL, previews the result, then writes the confirmed capture to Obsidian and Notion.

## Features

- Paste a URL and preview the extracted title, summary, category, tags, and duplicate status before writing anything.
- Save the original clipping and the cleaned note into an Obsidian vault.
- Create or update a Notion database named `网页采集日历`.
- Reuse existing Obsidian and Notion records when the same source URL is captured again.
- Keep secrets and runtime history out of Git by default.

## Requirements

- Windows with PowerShell
- Python 3.10 or newer
- An Obsidian vault
- A Zhipu API key
- A Notion integration token and a parent page/database location the integration can access

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .\config.example.ps1 .\config.ps1
notepad .\config.ps1

.\launch-url-capture.bat
```

After launch, open:

```text
http://127.0.0.1:8765
```

## Configuration

`config.ps1` is intentionally ignored by Git because it contains local paths and tokens. Start from `config.example.ps1` and fill in these values:

| Variable | Purpose |
| --- | --- |
| `ZHIPU_API_KEY` | Zhipu API key used for Chinese summaries |
| `ZHIPU_MODEL` | Model name, default `glm-5.1` |
| `OBSIDIAN_VAULT_PATH` | Local Obsidian vault path |
| `URL_CAPTURE_PORT` | Local server port, default `8765` |
| `NOTION_API_TOKEN` | Notion integration token |
| `NOTION_PARENT_PAGE_ID` | Parent Notion page id used to create or find the capture database |

## Output

Vault Capture writes to:

- `<Obsidian Vault>/Clippings/Archive`
- `<Obsidian Vault>/信息汇总/自动整理/<分类>/`
- A Notion database named `网页采集日历`

The Notion database includes fields such as title, capture date, publish date, category, platform, site, source URL, tags, summary, and Obsidian path.

## Project Files

- `launch-url-capture.bat` starts the local app.
- `start-url-capture.ps1` loads `config.ps1`, opens the browser, and runs the Python server.
- `url_capture_server.py` serves the API and handles capture, summary, Obsidian, and Notion sync.
- `url-capture.html` is the local capture UI.
- `summary-schema.json` documents the summary shape.
- `config.example.ps1` is the safe template for local configuration.
- `requirements.txt` lists Python dependencies.

Older n8n exports and manual test payloads are treated as local artifacts and are ignored by Git until they are sanitized.

## Health Check

With the server running:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/health
```

## GitHub Safety

Before publishing, confirm that these files are not staged or committed:

- `config.ps1`
- `runtime/`
- n8n workflow exports with local paths
- Notion or API test payloads containing real ids or tokens

If a real token is ever committed, rotate it immediately in the provider dashboard.
