# Veri-Fact Desktop

A native desktop application that checks numeric claims — the kind ChatGPT, a
colleague's email, or a quarterly deck produces — against your real data.

There is **no web server, no localhost, no browser, no extension**. You
double-click `run_app.bat` and a normal resizable Windows window opens.

---

## What it does

Paste this into the app:

> "In Q3 2024 our Region_A active users reached 984.5k, global revenue was
> $4.8M and churn held at 2.3%."

and it answers:

| Claimed | Metric | Timeframe | Actual | Status |
|---------|--------|-----------|--------|--------|
| 984.5k  | active_users | Q3 2024 | 984.5 | **PASS** |
| $4.8M   | revenue      | Q3 2024 | 4,800,000 | **PASS** |
| 2.3%    | churn_rate   | Q3 2024 | 2.3 | **PASS** |

Change any number and it turns red, tells you the correct figure, and shows
the exact database row and SQL query it used.

### Built for the whole world, not just the US

**All global number formats are supported out of the box.** Veri-Fact detects
the convention each document uses and reads it correctly — no configuration,
no extra packages:

| Convention | Example | Reads as |
|---|---|---|
| US / UK | `1,234,567.89` | 1234567.89 |
| European | `1.234.567,89` | 1234567.89 |
| Swiss | `1'234'567.89` | 1234567.89 |
| Indian (lakh/crore) | `12,34,567.89` | 1234567.89 |
| SI / French | `1 234 567,89` | 1234567.89 |

Scale suffixes work with any of them — `4.8M`, `4,8 Mio`, `2 crore`,
`5 lakh`, `984.5k`, `1.2 Mrd`.

Genuinely ambiguous tokens like `1,234` (1234 in London, 1.234 in Berlin) are
resolved from the rest of the document. Where the document gives no clue, both
readings are kept and either one can satisfy a check — so a locale difference
can **never** cause a false failure.

### No hardcoded geography

There is no built-in list of world regions. Veri-Fact runs

```sql
SELECT DISTINCT region FROM financial_metrics
```

at startup and recognises whatever labels *your* data uses — sales
territories, business units, store numbers, legal entities, anything. See
**Help → Region Vocabulary** to view what it learned and where from.

---

## Setup (5 minutes, once)

### 1. Install Python 3.10 or newer

Download from <https://www.python.org/downloads/>.
During installation tick **"Add python.exe to PATH"** and make sure
**"tcl/tk and IDLE"** is selected (that is tkinter — the app needs it).

### 2. Install the dependencies

Open a terminal in this folder and run:

```
pip install -r requirements.txt
```

### 3. Download the spaCy language model

```
python -m spacy download en_core_web_sm
```

### 4. Start the app

Double-click **`run_app.bat`**, or run:

```
python main.py
```

On first launch the app creates `verifact.db` (a SQLite file seeded with demo
data) and `config.json` in this same folder.

> **If spaCy will not install** on your Python version, do not worry — the app
> detects this and switches to a built-in regex extractor automatically. Check
> *Help → Backend Diagnostics* to see which extractor is active.

---

## Using it

### Tab 1 — Paste Text

1. Paste or type text containing numbers.
2. Press **Verify Text** (or `Ctrl+Enter`).
3. Read the colour-coded log below.
4. Press **Send Results to Batch Table** if you want to export them.

### Tab 2 — Batch Files

1. **Import Files** (multi-select) or drag files onto the window.
   Supported: `.txt` `.log` `.csv` `.xlsx` `.xls` `.json`
2. Press **Verify All Files**. The status bar shows
   *"Processing file 3 of 10…"* and the window stays responsive.
3. Click any row to read its full original context in the pane below.
4. Use the **context carousel** — the `< Previous` / `Next >` buttons above the
   detail pane (or `Alt+Left` / `Alt+Right`) — to step through every claim in
   the selected file one at a time. The table highlights and scrolls to each
   row as you go, so you can scan a long document without losing your place.
   The carousel is scoped to a single file; clicking a row from another file
   re-scopes it automatically.
5. **Right-click** a row for:
   - **Correct the Claim in Database** — update (or create) the truth value in
     the local SQLite database.
   - **Copy Source Query** — puts the exact SQL used on your clipboard.
   - Copy Full Context / Copy Row as Text.

### Tab 3 — Compare (draft vs final)

A pure difference tool that **never touches the database**. Use it to see what
changed between two versions of a document.

1. Choose (or drag in) **Source A** and **Source B**.
2. Press **Compare Files**.
3. You get one row per `(Metric, Region, Timeframe)` showing
   `Source A Value`, `Source B Value`, the absolute delta and the percentage
   delta.

| Status | Meaning |
|---|---|
| `CHANGED` | the value moved between the two documents |
| `SAME` | identical in both |
| `ONLY_IN_A` | present in the draft, **missing from the final** |
| `ONLY_IN_B` | new in the final |

`ONLY_IN_A` is often the most valuable result — it catches figures that quietly
disappeared between revisions. Export the comparison to CSV, colour-coded XLSX
or JSON.

### Smart suggest — "Did you mean…?"

When a number's metric name isn't recognised (a typo like `revenu`, or an
unfamiliar term like `custmer count`), Veri-Fact fuzzy-matches it against every
metric it knows and asks:

> `'custmer count'` is not a known metric.
> **Did you mean `customer_count`?**   [Yes] [No] [Ignore] [Stop asking]

- **Yes** — the claim is re-verified immediately with the correct metric, and
  the mapping is saved.
- **No** — that specific pairing is never suggested again.
- **Ignore** — you're never asked about that text again.
- **Stop asking** — silences prompts for the rest of the batch.

Choices persist in a `metric_aliases` table **across sessions**, so you only
ever teach it once. Manage what it has learned via
**Edit → Learned Metric Corrections…**.

For unattended runs set `suggestions.prompt_user` to `false` in `config.json`;
unrecognised metrics then simply stay amber.

### Derived metrics (formula checking)

Some figures aren't stored anywhere — they're calculated. Define them in
`config.json`:

```json
"derived_metrics": {
    "total_revenue": "region_a_revenue + region_b_revenue",
    "gross_margin":  "(revenue - cost_of_sales) / revenue * 100",
    "arpu":          "revenue / active_users"
}
```

When a claim names one of these, Veri-Fact resolves each constituent for that
claim's region and period, evaluates the expression, and compares against the
computed result. The report shows the full working:

> `PASS  total_revenue  1,750,000 + 3,050,000 = 4,800,000`

If a constituent is missing you get **AMBER** naming exactly which one — never
a misleading red, because the correct total genuinely isn't knowable.

**On safety:** these expressions come from a hand-edited file, so they are
*never* passed to `eval()`. They're parsed to an AST and checked against a
strict whitelist (numbers, metric names, `+ - * / // % **`, and
`min max abs round sum avg sqrt`). Attribute access, subscripts, lambdas,
comprehensions, unknown functions and runaway exponents are all rejected at
load time. Malformed or circular formulas are disabled with a clear message —
see **Help → Derived Metric Formulas**.

### Validation rules

Some claims are wrong even when no database row contradicts them. The
`validation_rules` block catches these:

```json
"validation_rules": {
    "enabled": true,
    "ranges": {
        "churn_rate": { "min": 0.0, "max": 100.0 }
    },
    "non_negative": ["active_users", "revenue"],
    "on_violation": "warning"
}
```

A churn rate of 3800% or a negative user count is flagged regardless of what
the database says — usually a units mix-up. Set `on_violation` to `"fail"` to
make breaches red instead of amber.

### Exporting

**File → Export Results**, then pick:

- **`.csv`** — every column, opens anywhere.
- **`.xlsx`** — formatted report, each row filled green / amber / red, with a
  Summary sheet recording the tolerance and backends used.
- **`.json`** — machine-readable audit log with a metadata header.

---

## Verdicts

| Colour | Status | Meaning |
|--------|--------|---------|
| Green  | PASS    | Within tolerance (default 1%) of the database value. |
| Red    | FAIL    | Outside tolerance. The true value and its source row are shown. |
| Amber  | WARNING | Nothing to compare — unknown metric, region or period, or no number extracted. |

**Scale awareness:** "984.5k" is compared against the stored `984.5` *and*
against `984,500`. If either reading is within tolerance it passes, and the
report records which interpretation matched. This stops false failures caused
by thousands/millions shorthand.

---

## How files are parsed

| Type | Rule |
|------|------|
| `.txt`, `.log` | Whole file read as raw text; every number extracted with its surrounding context. |
| `.csv`, `.xlsx` | Read with pandas. Every numeric cell becomes a claim; the **column header** is the metric name and the other cells in that row are the context. All sheets in a workbook are scanned. |
| `.json` | Traversed recursively. Every key whose value is a number becomes a claim; the key is the metric name and the dotted path plus text siblings are the context. |

A corrupt or unreadable file produces a friendly popup and an amber row — the
rest of the batch continues.

---

## Connecting your own database

The app ships with a local SQLite file, but it can query your real warehouse
first and only fall back to SQLite if the metric is not there.

Close the app, open **`config.json`** in Notepad, and edit:

```json
"external_database": {
    "enabled": true,
    "type": "postgresql",
    "host": "10.0.0.14",
    "port": 5432,
    "database": "analytics",
    "user": "readonly_bob",
    "password": "hunter2",
    "table": "financial_metrics",
    "columns": {
        "metric_name": "metric_name",
        "region":      "region",
        "quarter":     "quarter",
        "year":        "year",
        "value":       "value"
    }
}
```

- `type` can be `postgresql` (psycopg2), `mysql` (pymysql) or `mssql` (pyodbc).
- The `columns` block is a **rename map**: the left side is what Veri-Fact
  calls the column internally, the right side is what it is called in *your*
  table. If your amounts live in `amt_usd`, write `"value": "amt_usd"`.
- You may instead supply a full DSN in `"connection_string"`, which wins over
  the discrete fields.
- Reopen the app (or **Edit → Reload config.json**). The status bar and
  *Help → Backend Diagnostics* confirm which backend is live.

If the driver package is missing or the server is unreachable, the app says so
in the status bar and quietly uses SQLite. It never crashes.

---

## Tuning it without being a developer

Everything below is a one-line edit.

**Teach it a new metric word** — `verifact/nlp_engine.py`, in
`METRIC_KEYWORD_MAP`:

```python
"arr": "annual_recurring_revenue",
"nps": "net_promoter_score",
```

Matching is substring-based, so `"arr"` also catches "our ARR" and
`arr_total`. You usually won't need to: every metric name in your database is
registered automatically at startup, and smart-suggest handles typos.

**Add a region** — you do *not* edit any Python file. Insert a row with that
region into the database and it is recognised on the next launch. To teach it
extra prose words for an existing label, use `config.json`:

```json
"regions": {
    "fallback": ["Global", "Region_A", "Region_B"],
    "synonyms": { "Region_A": ["emea", "europe", "european"] }
}
```

`fallback` is used only when the database query returns nothing.

**Change the pass/fail tolerance** — `config.json`:

```json
"verification": { "tolerance_percent": 2.5 }
```

**Use the real clock instead of the 2024 demo date** — `config.json`, set
`"demo_reference_year": 0`.

**Support a new file format** — `verifact/file_parsers.py`: write a
`parse_myformat(path, engine) -> ParseResult` function and add one line to
`PARSER_REGISTRY`. The file dialog and batch loop pick it up automatically.

---

## Project layout

```
verifact-desktop/
├── main.py                     launcher (version checks, then opens the window)
├── run_app.bat                 double-click this on Windows
├── requirements.txt            dependencies (optional ones clearly marked)
├── config.json.template        copy to config.json, or let the app create it
├── .gitignore                  keeps config.json + verifact.db out of git
├── README.md                   this file
│
├── verifact/                   the application package
│   ├── __init__.py             package map
│   ├── config_manager.py       reads/validates config.json
│   ├── database.py             dual data source: external SQL -> SQLite
│   │                           fallback, region discovery, schema migration
│   ├── numbers.py              global number normalizer
│   │                           (US / EU / Swiss / Indian / spaced)
│   ├── nlp_engine.py           number/metric/timeframe extraction
│   │                           (spaCy + regex), dynamic RegionRegistry —
│   │                           no hardcoded geography
│   ├── formulas.py             safe AST-whitelisted derived-metric evaluator
│   ├── suggestions.py          fuzzy metric matching + persistent alias memory
│   ├── compare.py              two-file diff engine (never touches the DB)
│   ├── file_parsers.py         txt / log / csv / xlsx / json readers
│   ├── verifier.py             verdict logic, formula checks, validation rules
│   ├── exporters.py            csv / colour-coded xlsx / json writers
│   ├── dialogs.py              DB browser, suggest prompt, alias manager
│   └── gui.py                  the tkinter window (3 tabs)
│
├── migrations/                 database upgrade scripts
│   ├── migrate.py              standalone runner (see below)
│   ├── 001_add_schema_meta.sql additive — schema version marker
│   ├── 002_add_metric_aliases.sql additive — smart-suggest memory
│   └── 003_neutral_region_labels.sql optional/cosmetic — renames rows
│
└── sample_data/                example files for the batch and compare tabs
    ├── chatgpt_summary.txt
    ├── board_deck_claims.log
    ├── metrics_export.csv
    ├── api_report.json
    ├── international_formats.txt   all five number conventions
    ├── compare_draft.txt           ─┬─ try these two in the Compare tab
    └── compare_final.txt           ─┘
```

Created at runtime and **deliberately not in git** (see `.gitignore`):

```
config.json     your settings — may contain database passwords
verifact.db     your local truth data + learned metric corrections
```

---

## Seeded demo data

`verifact.db` starts with these rows. Region names are **neutral placeholders**
on purpose — Veri-Fact learns regions from your data, so shipping real
geography would have implied it cares about specific places. It doesn't.

| metric_name | region | quarter | year | value |
|---|---|---|---|---|
| active_users | Region_A | Q3 | 2024 | 984.5 |
| active_users | Region_B | Q3 | 2024 | 2100.0 |
| revenue | Global | Q3 | 2024 | 4800000.0 |
| churn_rate | Global | Q3 | 2024 | 2.3 |

plus extra rows (total_sales, profit, gross_margin, Q2 figures, and
`region_a_revenue` / `region_b_revenue` for the demo formula) so the timeframe
parser and formula checker have something to find. View them any time with
**Help → Browse Truth Database**.

---

## Upgrading an existing `verifact.db`

**Your data is never modified without asking.** The database now carries a
`schema_version` marker in a small `schema_meta` table.

On launch, Veri-Fact:

1. Creates `schema_meta` and `metric_aliases` if they're missing —
   additive only, nothing is dropped or altered.
2. Leaves every existing `financial_metrics` row exactly as it is.
3. Notices if you still have rows labelled `Europe` / `North America`.

To move those to the neutral placeholders, run
**Help → Migrate Legacy Region Labels…**. It shows you precisely which rows
would change and asks for confirmation first:

```
Europe        -> Region_A
North America -> Region_B
```

**Choose No if those are your real figures** — your own labels work perfectly
well, because the region vocabulary is read from the data. The migration is
purely cosmetic, entirely optional, and safe to re-run (it becomes a no-op once
there's nothing left to rename).

To start completely fresh instead, just delete `verifact.db` and relaunch.

| Table | Added in | Purpose |
|---|---|---|
| `financial_metrics` | v1 | the truth data (unchanged) |
| `schema_meta` | v2 | schema version marker |
| `metric_aliases` | v2 | remembered smart-suggest corrections |

### Migrating without launching the app

The `migrations/` folder holds the same changes as reviewable SQL, plus a
runner. Useful for DBAs, headless servers, or scripted upgrades.

```bash
# Report what state the database is in — changes nothing
python migrations/migrate.py --status

# Apply the additive migrations (001 + 002). Takes a timestamped backup first.
python migrations/migrate.py

# Also apply the optional cosmetic region rename (003), with confirmation
python migrations/migrate.py --all

# Target a database somewhere else
python migrations/migrate.py --db /path/to/verifact.db
```

Or apply the SQL directly if you prefer:

```bash
sqlite3 verifact.db < migrations/001_add_schema_meta.sql
sqlite3 verifact.db < migrations/002_add_metric_aliases.sql
```

| Script | Type | Safe to re-run | Modifies existing rows |
|---|---|---|---|
| `001_add_schema_meta.sql` | additive | yes | no |
| `002_add_metric_aliases.sql` | additive | yes | no |
| `003_neutral_region_labels.sql` | cosmetic | yes | **yes** — opt-in only |

Only 003 touches existing data, which is why it is not applied automatically
and why the runner asks you to type `yes` before including it.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `.bat` window flashes and closes | Run `python main.py` in a terminal to see the error. |
| "No module named tkinter" | Reinstall Python with the *tcl/tk and IDLE* option ticked. |
| Everything comes back amber | Check *Help → Browse Truth Database* for the metric names you actually have. Smart-suggest should offer a fix for typos; if it's off, re-enable `suggestions.enabled`. |
| Regions never match | *Help → Region Vocabulary* shows what was loaded. If it says "no regions loaded", your `region` column is empty — add rows, or set `regions.fallback` in config.json. |
| A number reads wrong (e.g. `1.234` as 1234) | That token is ambiguous across locales. Add more numbers to the document, or set `number_formats.force_profile` to `us`, `eu`, `swiss`, `indian` or `space`. |
| A formula is ignored | *Help → Derived Metric Formulas* lists rejected expressions and the exact reason. |
| Suggest prompts keep appearing | Press **Stop asking**, or set `suggestions.prompt_user` to `false` for unattended runs. |
| Drag-and-drop does nothing | `pip install tkinterdnd2` (optional; the Import Files button always works). |
| Excel export fails | `pip install openpyxl`. |
| External DB shows unavailable | *Help → Backend Diagnostics* names the missing driver or the connection error. |

---

## Privacy

Everything runs on your machine. No network calls are made except to the
database you explicitly configure. Nothing is uploaded, logged remotely, or
phoned home.
