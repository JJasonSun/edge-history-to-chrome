[中文](README.md) | English

# Edge History to Chrome

Move a Microsoft Edge history export into Google Chrome on macOS without editing Chrome's `History` database.

The tool converts the export into a temporary Firefox profile. Chrome reads that profile with its built-in Firefox importer and writes the visits through its own history service.

## Scope

The first release imports browsing history from a CSV with these columns:

```text
DateTime,NavigatedToUrl,PageTitle
```

`DateTime` must contain an ISO 8601 timezone, such as `2026-08-30T10:00:00Z`.

It accepts `http://` and `https://` visits. It does not import passwords, cookies, bookmarks, autofill data, addresses, or payment data.

## Requirements

- macOS
- Python 3.9 or newer
- Google Chrome
- A Microsoft Edge history CSV using the columns above

We validated the bridge workflow with Microsoft Edge 152 and Google Chrome 152 on macOS.

## Use

### 1. Export Edge history as CSV

1. Open `edge://history/all` in Edge.
2. Select **Export browsing data** at the top of the history page.
3. Save the CSV that Edge creates. The commands below use `~/Downloads/BrowserHistory.csv` as an example.

### 2. Prepare the temporary profile

Clone this repository and pass the CSV path to `prepare`:

```bash
git clone https://github.com/JJasonSun/edge-history-to-chrome.git
cd edge-history-to-chrome
python3 edge_history_to_chrome.py prepare ~/Downloads/BrowserHistory.csv
```

Run `status` if you want to check the visit count and database before importing. It does not print URLs:

```bash
python3 edge_history_to_chrome.py status
```

### 3. Import in Chrome

Open `chrome://settings/importData` in Chrome. Choose Mozilla Firefox, select **Browsing history** only, then run the import once. If Chrome lists several Firefox profiles, choose the one named **Edge History To Chrome** followed by the run ID. Chrome may take a short time to refresh its history page.

### 4. Clean up

Remove the temporary profile after you confirm the imported visits:

```bash
python3 edge_history_to_chrome.py cleanup
```

Run `cleanup` before preparing another export. Importing the same profile twice can create duplicate visits.

## Chrome to Edge

Edge has a built-in Chrome importer, so the reverse migration does not need this repository:

1. Open `edge://settings/profiles/importBrowsingData` in Edge.
2. Find **Import data from Google Chrome** and select **Import**.
3. Choose the Chrome profile and data types, including browsing history, then start the import.

[Microsoft Support](https://support.microsoft.com/en-us/microsoft-edge/what-s-imported-to-microsoft-edge-ab7d9fa1-4586-23ce-8116-e46f44987ac2) documents the same entry point.

## Safety

The program runs on your Mac and makes no network requests. It leaves the source CSV untouched and does not open Chrome's profile database.

`prepare` creates a temporary profile under `~/Library/Application Support/Firefox/`. If `profiles.ini` already exists, the program copies it before adding one profile section. `cleanup` removes that section and restores the prior file when it can do so without overwriting later changes.

The program records its state before it creates the history database. You can run `cleanup` again after an interrupted prepare or cleanup. The command refuses to remove a profile that has an unexpected name, state file, symlink, or extra file.

The generated `places.sqlite` contains URLs, titles, and visit times. Treat it as private data. Do not attach the database or your CSV to a GitHub issue.

Quit Firefox before running `prepare` or `cleanup` so Firefox cannot edit `profiles.ini` during the operation.

## Limits

- Chrome's Firefox importer truncates visit timestamps to whole seconds.
- Chrome 152's local history backend expires visits older than about 90 days, so older imported visits may not remain visible.
- Chrome controls the import format and may change it in a later release.
- Chrome requires one manual import step on an internal settings page.
- The tool supports the Edge CSV column names shown above.

## Technical notes

Chrome 152 reads Firefox history by joining `moz_places` with `moz_historyvisits`, then passes those rows to its history service. The importer source is available in [Chromium's `firefox_importer.cc`](https://github.com/chromium/chromium/blob/152.0.7977.65/chrome/utility/importer/firefox_importer.cc#L173-L220). Chrome finds Firefox profiles through `profiles.ini`; the macOS path is defined in [`firefox_importer_utils_mac.mm`](https://github.com/chromium/chromium/blob/152.0.7977.65/chrome/common/importer/firefox_importer_utils_mac.mm#L10-L20).

This repository contains an independent compatibility implementation. It does not include Chromium or Firefox source code. Google, Microsoft, and Mozilla do not sponsor or endorse this project.

## Development

Run the standard-library test suite:

```bash
python3 -m unittest discover -s tests -v
```

Tests use temporary directories and synthetic URLs. They do not read installed browser profiles.

## License

[MIT](LICENSE)
