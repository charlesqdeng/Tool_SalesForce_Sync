# webAI — Salesforce Bulk Field Updater

Bulk-updates a single field on every Opportunity in a Salesforce report, using
the Salesforce REST API — e.g. set a User-lookup field like **Solutions Engineer**
to a given person on every Opportunity returned by a saved report.

Deterministic (no LLM in the loop), idempotent, ~10× faster than the previous
browser-automation approach that used `browser-use` + gpt-4o.

## How it works

1. `launch_chrome.sh` opens a dedicated Chrome window with remote debugging on
   port 9222, using an isolated profile at `~/chrome-sf-debug`. You complete
   your Salesforce login (SSO or password) in that window once; the profile persists across runs.
2. `sf_agent.py` extracts the Salesforce `sid` session cookie from that Chrome
   via Playwright's CDP client, then authenticates `simple_salesforce` with it
   — no Connected App, no admin ask, works with SSO.
3. It runs the target report **asynchronously** (POST to
   `analytics/reports/{id}/instances`, then polls) so it doesn't hit the
   500/hour sync-report cap.
4. For each Opportunity Id in the report's fact map, it reads the current
   value of the target field. If already set to the target value → skip. If
   empty or someone else → update.

## Files

| File               | Purpose                                                   |
|--------------------|-----------------------------------------------------------|
| `sf_agent.py`      | Main script                                               |
| `sf_to_sheet.py`   | Export report opps to the Google Sheet (`./run.sh sheet`) |
| `sheet_sync.py`    | Push edited sheet cells to Salesforce (`./run.sh sync`)   |
| `launch_chrome.sh` | Launches the debug Chrome instance                        |
| `run.sh`           | Bootstraps venv + deps and runs `sf_agent.py`             |
| `.env.example`     | Template for `.env` — copy it and fill in your values     |

## First-time setup

```bash
# 1. Create your config from the template and fill it in
cp .env.example .env

# 2. Open the debug Chrome and log into Salesforce
#    (set SALESFORCE_LOGIN_URL to your org's My Domain URL to open it directly)
SALESFORCE_LOGIN_URL=https://yourcompany.my.salesforce.com/ ./launch_chrome.sh

# 3. Dry run — reads only, prints what would change
./run.sh --dry-run
```

## Default mode: fill an empty field from the Google Sheet

`./run.sh` (no `--report` / `--opp`) reads the Google Sheet
`CD_SALESFORCE_STATUS_URL`. For every row whose `SHEET_TARGET_FIELD` cell
(e.g. **Solutions Engineer**) is empty, it sets that field to
`SHEET_TARGET_VALUE` (e.g. your name) on the
Opportunity in the row's **Opportunity ID** column. If Salesforce already has
someone in that field (the sheet is stale), the opp is skipped, never
overwritten. `TARGET_FIELD_LABEL` / `TARGET_VALUE` are ignored in this mode.

```bash
./run.sh sheet              # refresh the sheet from the report
./run.sh --dry-run          # show which opps would be assigned
./run.sh                    # apply
./run.sh sheet              # refresh again so the sheet shows the new SE
```

The previous behaviour (every opp in `SALESFORCE_REPORT_URL`, field/value
from `.env`) is still available with `--report`:

```bash
./run.sh --report --dry-run
```

## Daily usage

```bash
./launch_chrome.sh          # skips if :9222 is already up
# (log into Salesforce in the debug Chrome window if the session has expired)
./run.sh --dry-run          # verify what will change
./run.sh                    # apply changes
```

## Verifying a dry run

For each `WOULD UPDATE` row, open the Opportunity in the debug Chrome and
check the target field:

```
https://yourcompany.lightning.force.com/lightning/r/Opportunity/{OPP_ID}/view
```

- `WOULD UPDATE` rows: field should be blank (or show a different value)
- `skip — already …` rows: field should already show the target value

Note: a field's UI label often differs from its API name (admins rename
labels). The script discovers the mapping via `Opportunity.describe()` — no
hardcoded API names.

## Configuration

### Environment (`.env`)

| Variable                | Required    | Notes                                          |
|-------------------------|-------------|------------------------------------------------|
| `SALESFORCE_REPORT_URL` | Recommended | Full report URL from your browser. Script extracts the `00O…` Id. |
| `SALESFORCE_REPORT_ID`  | Alternative | Bare report Id if you don't want the URL.      |
| `TARGET_FIELD_LABEL`    | Yes*        | Field label as shown in Salesforce (e.g. `"Stage"`). |
| `TARGET_VALUE`          | Yes*        | Value to write. For User-lookup fields, use the person's full name. |
| `SHEET_TARGET_FIELD`    | Sheet mode  | Field label checked/filled by the default sheet mode (e.g. `"Solutions Engineer"`). |
| `SHEET_TARGET_VALUE`    | Sheet mode  | Value written into empty `SHEET_TARGET_FIELD` cells (e.g. your full name). |
| `SALESFORCE_LOGIN_URL`  | Optional    | URL shown in login hints and opened by `launch_chrome.sh`. Defaults to `https://login.salesforce.com/`. |

\* Can be supplied via `--field` / `--value` instead. A report is required
unless you pass `--opp`.

### CLI overrides

Both the field and the value can be overridden per-run without editing `.env`:

```bash
./run.sh --report --dry-run --field "Stage" --value "Closed Won"
```

### One-off: update a single opportunity

```bash
./run.sh --dry-run 006Vr00000ABCDEFGHIJ "Solutions Engineer" "Jane Doe"
./run.sh 006Vr00000ABCDEFGHIJ "Solutions Engineer" "Jane Doe"
```

Arguments are `OPP_ID FIELD_LABEL VALUE` (an Opportunity URL works in place of
the Id). Quote the field and value if they contain spaces. Unlike the sheet
mode, this **overwrites** any existing value — it's an explicit request.

### Targeting specific opportunities (skip the report)

Pass `--opp` with one or more Opportunity Ids or URLs to skip the report
fetch entirely. Repeat the flag or comma-separate:

```bash
./run.sh --dry-run --opp 006Vr00000ABCDEFGHIJ
./run.sh --dry-run --opp 006Vr00000ABC,006Vr00000XYZ
./run.sh --dry-run --opp "https://yourcompany.lightning.force.com/lightning/r/Opportunity/006Vr00000ABC/view"
```

Combine with `--field` / `--value` for a fully-specified one-off:

```bash
./run.sh --dry-run --field "Stage" --value "Closed Won" --opp 006Vr00000ABC
```

## Changing what gets updated

No code edit required for any of the below.

### Different field or value

Set `TARGET_FIELD_LABEL` and `TARGET_VALUE` in `.env` (or pass `--field` /
`--value`). The script auto-detects the field type via `Opportunity.describe()`:

- **User-lookup**: `TARGET_VALUE` is treated as a full user name; script
  resolves the User Id.
- **Checkbox**: `TARGET_VALUE` accepts `true`/`false`/`yes`/`no`/`1`/`0`.
- **Everything else** (text, picklist, number, date): value is written
  literally. Picklist values must match Salesforce exactly.

### Different report

Edit `SALESFORCE_REPORT_URL` in `.env`.

### Different object (Account, Contact, custom object)

Not supported — the script is hardcoded to `Opportunity`. Requires a small
refactor.

### Multiple fields per record in one run

Not supported — the script updates one field per run. Requires a `process()`
refactor.

## Google Sheet export

`./run.sh sheet` copies the `SHEET_COLUMNS` fields (default **Opportunity Name, Opportunity ID, Stage, Close Date, Next Step, Opportunity Owner,
Opportunity URL**) for every Opportunity in
`SALESFORCE_REPORT_URL` into the Google Sheet `CD_SALESFORCE_STATUS_URL`. The
target tab (and its hidden baseline tab, see below) is cleared and rewritten on each run.

```bash
./run.sh sheet --dry-run        # print rows, don't touch the sheet
./run.sh sheet                  # overwrite the first tab
./run.sh sheet --tab "Status"   # overwrite a specific tab
./run.sh sheet --opp 006Vr00000ABC   # specific opps instead of the report
```

| Variable                      | Notes |
|-------------------------------|-------|
| `CD_SALESFORCE_STATUS_URL`    | Sheet URL or bare sheet Id. |
| `CD_SALESFORCE_STATUS_TAB`    | Optional default tab name (else first tab). |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REDIRECT_URI` | "Web application" OAuth client with a `http://localhost:<port>/…` redirect URI (you can reuse another local app's client). A browser opens once (anything else listening on that port must be stopped so it's free); token cached in `google_token.json`. |
| `GOOGLE_OAUTH_CLIENT_FILE`    | OAuth "Desktop app" client JSON. A browser opens once; token cached in `google_token.json`. |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Alternative: service-account key JSON. Share the sheet with the service account email as Editor. |

If neither is set, Application Default Credentials are used
(`gcloud auth application-default login --scopes=https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/cloud-platform`).

## Sync sheet edits back to Salesforce

Edit cells in the sheet, then `./run.sh sync` pushes **only the cells you
changed**. Values you didn't touch are never written.

How it tells what you changed: `./run.sh sheet` writes each export twice, to
the visible tab and to a hidden `_baseline_<tab>` tab. Sync compares each
cell in the visible tab with the baseline, matching rows by **Opportunity
ID** (so sorting, filtering and inserting rows is safe), then checks the
current Salesforce value:

| Sheet vs baseline | Salesforce now   | Result |
|-------------------|------------------|--------|
| same              | —                | untouched, skipped |
| changed           | = baseline       | **pushed** |
| changed           | = your new value | already applied, skipped |
| changed           | something else   | **CONFLICT**: someone edited it in Salesforce after your export. Skipped and reported (`--force` overwrites) |

After a live sync the baseline is updated for every applied cell, so the same
edit is never pushed twice. Conflicts and errors stay pending until you
resolve them.

**Rejected cells.** If Salesforce refuses a value (e.g. a validation rule like
"Please update your Committed Bookings…"), the cell is shaded red and the
error goes into its note (hover to read it). It's also logged in a hidden
`_sync_errors_<tab>` tab. Later syncs skip that cell while it still holds the
rejected value, so fix the underlying issue in Salesforce, then change the
cell (or run with `--retry-errors`) to try again. Once the cell syncs, the
mark is cleared. When an opp has several edited fields, they're retried
one at a time, so one rejected field doesn't block the others. Re-exporting
with `./run.sh sheet` discards rejected cells and their marks.

```bash
./run.sh sheet               # export + baseline (refuses if unsynced edits exist; --force to discard)
# ...edit the sheet...
./run.sh sync --dry-run      # show old → new for every edited cell, plus conflicts
./run.sh sync                # apply
```

| Variable / flag       | Notes |
|-----------------------|-------|
| `SHEET_COLUMNS`       | Columns `./run.sh sheet` exports: comma-separated field labels. `Header=Field` when the header differs, e.g. `Opportunity Name=Name`. "Opportunity ID" is always added after the first column and "Opportunity URL" as the last. |
| `SHEET_SYNC_COLUMNS`  | **Required for sync.** Columns whose edits are pushed. Anything else (Name, Notes History, …) is never written, and edits there are reported as ignored. |
| `SHEET_BASELINE_TAB`  | Optional baseline tab name (default `_baseline_<tab>`). Don't edit it by hand. |
| `--allow-clear`       | A cell you emptied clears the field in Salesforce (default: ignored). |
| `--force`             | Push conflicting cells anyway. |
| `--retry-errors`      | Retry previously rejected cells even if unchanged. |

Value handling: User-lookup cells take a person's full name (or a User Id).
Checkboxes take true/false/yes/no, dates take `YYYY-MM-DD` or `M/D/YYYY`, and
picklist values must exactly match an active value. Rows added to the sheet by
hand (no baseline row) only fill fields that are empty in Salesforce.

## Troubleshooting

**`Chrome debug port is NOT reachable on :9222`**
Chrome was already running when you tried to launch it. Run
`./launch_chrome.sh` — it opens a separate profile so your normal Chrome
stays untouched.

**`No Salesforce sid cookie found in Chrome`**
Open your Salesforce org in the debug Chrome window and complete login.

**`Salesforce session expired`**
Refresh the Salesforce tab in the debug Chrome (your SSO provider may re-issue
a cookie silently if its session is still alive; otherwise re-login).

**`You can't run more than 500 reports synchronously every 60 minutes`**
Shouldn't happen — the script uses async report execution (separate 1200/hr
quota). If it does, wait an hour.

**`Field labeled 'X' not found on Opportunity`**
Either the label is misspelled, or your Salesforce profile can't see that
field (field-level security). Check in Setup → Object Manager → Opportunity
→ Fields & Relationships.

**`Multiple active users named '…'`**
Someone else in your SF org shares the name. Edit `TARGET_VALUE` to be
more specific, or replace `find_user_id`'s query with a `WHERE Id = '…'`
using the exact User Id.

**`Async report … did not finish within 120s`**
Report is unusually large or SF is slow. Bump `timeout_s` in
`_run_report_async`.

## Salesforce prerequisites

- API access enabled on your user profile (default for standard SF licenses)
- Read access to the report (`00O…`) and to the Opportunity object
- Edit access to the target field
- For User-lookup fields, the target user must be an **active** SF user

## Security notes

- **The debug Chrome is a live Salesforce session.** While the Chrome started by
  `launch_chrome.sh` is running, any process on your machine can connect to
  `localhost:9222` and read its cookies — i.e. act as you in Salesforce. Use
  the dedicated profile only for this tool and quit that Chrome when you're done.
- **Keep secrets out of git.** `.env`, `google_token.json` (a long-lived Google
  refresh token) and OAuth client / service-account JSON files are ignored by
  `.gitignore`. The token file is written with owner-only (`0600`)
  permissions; do the same for `.env`: `chmod 600 .env`.
- If a token or secret is ever exposed, revoke it: remove the app at
  <https://myaccount.google.com/permissions> (or rotate the OAuth client
  secret) and log out of Salesforce to invalidate the `sid` session.
