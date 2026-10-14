"""Copy selected Opportunity fields from a Salesforce report into a Google Sheet.

Reads every Opportunity in SALESFORCE_REPORT_URL and overwrites the target tab
of CD_SALESFORCE_STATUS_URL with one row per Opportunity. Columns come from
SHEET_COLUMNS in .env. The same rows are also written to a hidden baseline tab
so `./run.sh sync` (sheet_sync.py) can tell which cells you edited afterwards.
Refuses to overwrite the sheet while it has edits not yet synced (--force to override).

Salesforce auth: same as sf_agent.py (sid cookie from the debug Chrome on :9222).
Google auth (first match wins):
  1. GOOGLE_SERVICE_ACCOUNT_FILE — service-account JSON key; share the sheet
     with the service account's email as Editor.
  2. GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REDIRECT_URI — a "Web
     application" OAuth client with a localhost redirect URI. Opens a
     browser once, then caches the token in GOOGLE_TOKEN_FILE.
  3. GOOGLE_OAUTH_CLIENT_FILE — OAuth "Desktop app" client JSON; opens a
     browser once, then caches the token in GOOGLE_TOKEN_FILE.
  4. Application Default Credentials (gcloud auth application-default login).
"""

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime
from typing import Optional

from simple_salesforce import Salesforce
from simple_salesforce.exceptions import SalesforceExpiredSession

from sf_agent import (
    SF_LOGIN_URL,
    SetupError,
    fetch_report_opportunity_ids,
    parse_opp_refs,
    read_sf_session_from_chrome,
    resolve_report_id,
)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")

# Default for SHEET_COLUMNS: comma-separated column headers, each the
# Opportunity field label (or API name) in Salesforce. "Header=Field" uses a
# different header than the field label — the standard name field's API label
# is "Name", even though layouts show "Opportunity Name".
DEFAULT_COLUMNS = (
    "Opportunity Name=Name, Stage, Close Date, Next Step, Opportunity Owner=OwnerId"
)

# Extra columns: the bare Id (inserted after "Opportunity Name") and a link to
# the record in Lightning (appended as the last column).
URL_HEADER = "Opportunity URL"
ID_HEADER = "Opportunity ID"

# Google Sheets rejects cells longer than 50,000 characters.
MAX_CELL_CHARS = 50_000
SOQL_CHUNK = 200


def resolve_sheet_id() -> str:
    raw = (os.getenv("CD_SALESFORCE_STATUS_URL") or "").strip()
    if not raw:
        raise SetupError("Set CD_SALESFORCE_STATUS_URL in .env (sheet URL or Id).")
    match = SHEET_ID_RE.search(raw)
    return match.group(1) if match else raw


def parse_columns(raw: str) -> list[tuple[str, str]]:
    """Parse "Header, Header=Field, ..." into (header, field label) pairs."""
    cols = []
    for entry in raw.split(","):
        header, _, field = entry.partition("=")
        header, field = header.strip(), field.strip()
        if header:
            cols.append((header, field or header))
    return cols


def export_columns() -> list[tuple[str, str]]:
    cols = parse_columns(os.getenv("SHEET_COLUMNS") or DEFAULT_COLUMNS)
    if not cols:
        raise SetupError("SHEET_COLUMNS in .env is empty.")
    return cols


def resolve_fields(sf: Salesforce, labels: list[str]) -> list[dict]:
    """Describe entry for each field label (or API name), case-insensitive."""
    fields = sf.Opportunity.describe()["fields"]
    by_label = {f["name"].lower(): f for f in fields}
    by_label.update({f["label"].lower(): f for f in fields})
    missing = [label for label in labels if label.lower() not in by_label]
    if missing:
        raise SetupError(
            f"Field(s) not found on Opportunity: {', '.join(missing)}. "
            f"Check the label spelling and your field-level security."
        )
    return [by_label[label.lower()] for label in labels]


def soql_path(field: dict) -> str:
    """SOQL path for a field. Lookup fields (e.g. a User) resolve to
    "<Relationship>.Name" so the sheet shows the record name instead of a raw Id.
    """
    if field["type"] == "reference" and field.get("relationshipName"):
        return f"{field['relationshipName']}.Name"
    return field["name"]


def resolve_field_api_names(sf: Salesforce) -> list[str]:
    """SOQL path for each export column, in column order."""
    return [soql_path(f) for f in resolve_fields(sf, [label for _, label in export_columns()])]


def lightning_base_url(instance: str) -> str:
    """Base URL for record links — the report URL's host if it's a Lightning
    URL, else derived from the API instance (x.my.salesforce.com → x.lightning.force.com).
    """
    report_url = os.getenv("SALESFORCE_REPORT_URL") or ""
    match = re.match(r"https://[^/]+\.lightning\.force\.com", report_url)
    if match:
        return match.group(0)
    return "https://" + instance.replace(".my.salesforce.com", ".lightning.force.com")


def headers() -> list[str]:
    names = [h for h, _ in export_columns()]
    names.insert(1, ID_HEADER)
    names.append(URL_HEADER)
    return names


def query_opportunities(sf: Salesforce, opp_ids: list[str],
                        paths: list[str]) -> dict[str, dict]:
    """Records keyed by 15-char Id (so 15- and 18-char Ids both match)."""
    records: dict[str, dict] = {}
    select = ", ".join(dict.fromkeys(["Id", *paths]))
    for i in range(0, len(opp_ids), SOQL_CHUNK):
        chunk = opp_ids[i:i + SOQL_CHUNK]
        id_list = ", ".join(f"'{x}'" for x in chunk)
        result = sf.query_all(f"SELECT {select} FROM Opportunity WHERE Id IN ({id_list})")
        for r in result["records"]:
            records[r["Id"][:15]] = r
    return records


def fetch_rows(sf: Salesforce, opp_ids: list[str], api_names: list[str],
               base_url: str) -> list[list[str]]:
    """Query the fields for every Opportunity, preserving report order."""
    records = query_opportunities(sf, opp_ids, api_names)
    rows = []
    for opp_id in opp_ids:
        r = records.get(opp_id[:15])
        if r is None:
            print(f"  {opp_id}  not readable — skipped", file=sys.stderr)
            continue
        row = [_cell(_get_path(r, api)) for api in api_names]
        row.insert(1, r["Id"])
        row.append(f"{base_url}/lightning/r/Opportunity/{r['Id']}/view")
        rows.append(row)
    return rows


def _get_path(record: dict, path: str) -> object:
    """Read a dotted SOQL path like "Owner.Name"; None if any hop is empty."""
    value: object = record
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    if len(text) > MAX_CELL_CHARS:
        text = text[:MAX_CELL_CHARS - 20] + "\n…[truncated]"
    return text


def google_credentials():
    sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if sa_file:
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_file(sa_file, scopes=SCOPES)

    client_file = os.getenv("GOOGLE_OAUTH_CLIENT_FILE")
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if client_file or client_id:
        return _oauth_user_credentials(client_file, client_id)

    import google.auth
    from google.auth.exceptions import DefaultCredentialsError
    try:
        creds, _ = google.auth.default(scopes=SCOPES)
        return creds
    except DefaultCredentialsError:
        raise SetupError(
            "No Google credentials. Set GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/"
            "GOOGLE_REDIRECT_URI, GOOGLE_OAUTH_CLIENT_FILE, or "
            "GOOGLE_SERVICE_ACCOUNT_FILE in .env (see README → Google Sheet export)."
        )


def _oauth_user_credentials(client_file: Optional[str], client_id: Optional[str]):
    """User OAuth: reuse the cached token, else run a one-time browser consent."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = os.getenv("GOOGLE_TOKEN_FILE") or "google_token.json"
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        from google.auth.exceptions import RefreshError
        try:
            creds.refresh(Request())
        except RefreshError:
            print("  cached Google token was revoked or expired — re-authorizing")
            creds = None
    if not creds or not creds.valid:
        if client_file:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
            creds = flow.run_local_server(port=0)
        else:
            creds = _web_client_consent(client_id)
    # Holds a long-lived refresh token: owner-only permissions.
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.chmod(token_file, 0o600)
    return creds


def _web_client_consent(client_id: str):
    """Consent via a "Web application" OAuth client (e.g. one shared with
    another local app). Google only redirects to the client's registered URI,
    so we listen on exactly that host/port/path for the one callback.
    """
    import http.server
    import webbrowser
    from urllib.parse import urlsplit

    from google_auth_oauthlib.flow import Flow

    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI")
    if not client_secret or not redirect_uri:
        raise SetupError(
            "GOOGLE_CLIENT_ID is set; also set GOOGLE_CLIENT_SECRET and "
            "GOOGLE_REDIRECT_URI (must match the client's authorized redirect URI)."
        )
    parts = urlsplit(redirect_uri)
    if parts.hostname not in ("localhost", "127.0.0.1"):
        raise SetupError(f"GOOGLE_REDIRECT_URI must point at localhost, got {redirect_uri!r}.")

    flow = Flow.from_client_config(
        {"web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }},
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

    callback: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if urlsplit(self.path).path != parts.path:
                self.send_response(404)
                self.end_headers()
                return
            callback["path"] = self.path
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"Google authorization complete. You can close this tab.")

        def log_message(self, *args):
            pass

    try:
        server = http.server.HTTPServer((parts.hostname, parts.port or 80), Handler)
    except OSError:
        raise SetupError(
            f"Google login needed, but port {parts.port} (from GOOGLE_REDIRECT_URI) is in "
            f"use by another app. This only happens when {os.getenv('GOOGLE_TOKEN_FILE') or 'google_token.json'} "
            f"is missing or revoked: stop that app for ~1 minute, re-run, approve "
            f"in the browser, then restart it. To never need this, set "
            f"GOOGLE_OAUTH_CLIENT_FILE to a \"Desktop app\" OAuth client (any free port)."
        )
    print(f"→ Opening browser for one-time Google authorization...\n  {auth_url}")
    webbrowser.open(auth_url)
    with server:
        while "path" not in callback:
            server.handle_request()

    # oauthlib refuses http:// callbacks; the token exchange itself is over https.
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    flow.fetch_token(authorization_response=f"{redirect_uri.split('?')[0]}?{urlsplit(callback['path']).query}")
    return flow.credentials


def _sheets_service():
    from googleapiclient.discovery import build

    service = build("sheets", "v4", credentials=google_credentials(), cache_discovery=False)
    return service.spreadsheets()


def _resolve_tab(sheets, sheet_id: str, tab: Optional[str]) -> tuple[str, dict[str, int]]:
    """Return (tab, {tab title: numeric sheetId}), defaulting to the first visible tab."""
    meta = sheets.get(spreadsheetId=sheet_id,
                      fields="sheets.properties(sheetId,title,hidden)").execute()
    props = [s["properties"] for s in meta["sheets"]]
    if not tab:
        tab = next((p["title"] for p in props if not p.get("hidden")), props[0]["title"])
    return tab, {p["title"]: p["sheetId"] for p in props}


def baseline_tab_name(tab: str) -> str:
    """Hidden tab holding what the last export wrote to `tab`."""
    return os.getenv("SHEET_BASELINE_TAB") or f"_baseline_{tab}"


def errors_tab_name(tab: str) -> str:
    """Hidden tab listing cells of `tab` that Salesforce rejected during sync."""
    return f"_sync_errors_{tab}"


def _a1(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def _sheets_error(e) -> SetupError:
    return SetupError(
        f"Google Sheets API error ({e.status_code}): {e.reason}. "
        f"If 403/404, make sure your Google account (or service account) "
        f"has Editor access to the sheet."
    )


def _get_values(sheets, sheet_id: str, tab: str) -> tuple[list[str], list[dict[str, str]]]:
    """Return (header, rows as {header: cell}). Missing trailing cells are ''."""
    values = sheets.values().get(
        spreadsheetId=sheet_id, range=_a1(tab)
    ).execute().get("values", [])
    if not values:
        return [], []
    header = [h.strip() for h in values[0]]
    rows = [
        {h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(header)}
        for row in values[1:]
    ]
    return header, rows


def read_sheet(sheet_id: str, tab: Optional[str]) -> tuple[list[dict[str, str]], str]:
    """Return (rows as {header: cell}, tab name). Missing trailing cells are ''."""
    from googleapiclient.errors import HttpError

    try:
        sheets = _sheets_service()
        tab, _ = _resolve_tab(sheets, sheet_id, tab)
        _, rows = _get_values(sheets, sheet_id, tab)
    except HttpError as e:
        raise _sheets_error(e)
    return rows, tab


def read_sheet_with_baseline(sheet_id: str, tab: Optional[str]) -> tuple[
        list[dict[str, str]], Optional[tuple[list[str], list[dict[str, str]]]], str]:
    """Return (rows, (baseline header, baseline rows) or None if never exported, tab)."""
    from googleapiclient.errors import HttpError

    try:
        sheets = _sheets_service()
        tab, titles = _resolve_tab(sheets, sheet_id, tab)
        if tab not in titles:
            raise SetupError(f"Sheet has no tab named '{tab}'.")
        _, rows = _get_values(sheets, sheet_id, tab)
        btab = baseline_tab_name(tab)
        baseline = _get_values(sheets, sheet_id, btab) if btab in titles else None
    except HttpError as e:
        raise _sheets_error(e)
    return rows, baseline, tab


def write_sheet(sheet_id: str, tab: Optional[str], rows: list[list[str]]) -> str:
    """Overwrite `tab` and its hidden baseline tab with the same rows."""
    from googleapiclient.errors import HttpError

    try:
        sheets = _sheets_service()
        tab, titles = _resolve_tab(sheets, sheet_id, tab)
        btab = baseline_tab_name(tab)
        if btab not in titles:
            sheets.batchUpdate(spreadsheetId=sheet_id, body={"requests": [
                {"addSheet": {"properties": {"title": btab, "hidden": True}}},
            ]}).execute()
        values = [headers(), *rows]
        sheets.values().batchClear(
            spreadsheetId=sheet_id, body={"ranges": [_a1(tab), _a1(btab)]}
        ).execute()
        sheets.values().batchUpdate(spreadsheetId=sheet_id, body={
            "valueInputOption": "RAW",
            "data": [{"range": f"{_a1(t)}!A1", "values": values} for t in (tab, btab)],
        }).execute()
    except HttpError as e:
        raise _sheets_error(e)
    return tab


def read_tab(sheet_id: str, title: str) -> list[dict[str, str]]:
    """Rows of a tab as {header: cell}; [] if the tab doesn't exist."""
    from googleapiclient.errors import HttpError

    try:
        sheets = _sheets_service()
        _, titles = _resolve_tab(sheets, sheet_id, title)
        if title not in titles:
            return []
        return _get_values(sheets, sheet_id, title)[1]
    except HttpError as e:
        raise _sheets_error(e)


def write_tab(sheet_id: str, title: str, header: list[str],
              rows: list[dict[str, str]]) -> None:
    """Overwrite a tab with header + rows, creating it hidden if missing."""
    from googleapiclient.errors import HttpError

    values = [header, *[[r.get(h, "") for h in header] for r in rows]]
    try:
        sheets = _sheets_service()
        _, titles = _resolve_tab(sheets, sheet_id, title)
        if title not in titles:
            sheets.batchUpdate(spreadsheetId=sheet_id, body={"requests": [
                {"addSheet": {"properties": {"title": title, "hidden": True}}},
            ]}).execute()
        sheets.values().clear(spreadsheetId=sheet_id, range=_a1(title)).execute()
        sheets.values().update(
            spreadsheetId=sheet_id, range=f"{_a1(title)}!A1",
            valueInputOption="RAW", body={"values": values},
        ).execute()
    except HttpError as e:
        raise _sheets_error(e)


def write_baseline(sheet_id: str, tab: str, header: list[str],
                   rows: list[dict[str, str]]) -> None:
    """Overwrite the baseline tab for `tab` (after sync pushes edits)."""
    write_tab(sheet_id, baseline_tab_name(tab), header, rows)


ERROR_FILL = {"red": 0.96, "green": 0.8, "blue": 0.8}


def mark_cells(sheet_id: str, tab: str,
               cells: list[tuple[int, int, Optional[str]]]) -> None:
    """Set (note → red fill + note) or clear (None → no fill, no note) cells.

    `cells` holds (1-based sheet row, 0-based column, note).
    """
    from googleapiclient.errors import HttpError

    if not cells:
        return
    try:
        sheets = _sheets_service()
        _, titles = _resolve_tab(sheets, sheet_id, tab)
        gid = titles[tab]
        requests = []
        for row, col, note in cells:
            cell = {"note": note, "userEnteredFormat": {"backgroundColor": ERROR_FILL}} \
                if note else {}
            requests.append({"updateCells": {
                "range": {"sheetId": gid, "startRowIndex": row - 1, "endRowIndex": row,
                          "startColumnIndex": col, "endColumnIndex": col + 1},
                "rows": [{"values": [cell]}],
                "fields": "note,userEnteredFormat.backgroundColor",
            }})
        sheets.batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()
    except HttpError as e:
        raise _sheets_error(e)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the rows instead of writing to the sheet.")
    parser.add_argument("--tab", default=os.getenv("CD_SALESFORCE_STATUS_TAB"),
                        help="Sheet tab to overwrite. Defaults to CD_SALESFORCE_STATUS_TAB "
                             "or the first tab.")
    parser.add_argument("--opp", action="append", default=[],
                        help="Opportunity Id or URL (repeat or comma-separate). "
                             "When set, the report is skipped.")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite the sheet even if it has edits not yet synced "
                             "to Salesforce (those edits are lost).")
    args = parser.parse_args()

    try:
        sheet_id = resolve_sheet_id()

        if not args.dry_run and not args.force:
            from sheet_sync import check_no_pending_edits
            check_no_pending_edits(sheet_id, args.tab)

        print("→ Extracting Salesforce session from Chrome...")
        session_id, instance = await read_sf_session_from_chrome()
        print(f"  instance: {instance}")
        sf = Salesforce(instance=instance, session_id=session_id)

        print("→ Resolving fields...")
        api_names = resolve_field_api_names(sf)
        for (header, _), api in zip(export_columns(), api_names):
            print(f"  {header} → {api}")

        if args.opp:
            opp_ids = parse_opp_refs(args.opp)
            print(f"→ Targeting {len(opp_ids)} opportunity(ies) from --opp")
        else:
            report_id = resolve_report_id()
            print(f"→ Fetching report {report_id}...")
            opp_ids = fetch_report_opportunity_ids(sf, report_id)
            print(f"  {len(opp_ids)} opportunities")

        print("→ Reading opportunity fields...")
        rows = fetch_rows(sf, opp_ids, api_names, lightning_base_url(instance))

        if args.dry_run:
            print(f"→ DRY RUN — {len(rows)} row(s) would be written:")
            for row in rows:
                print("  " + " | ".join(c.replace("\n", " ")[:40] for c in row))
            return 0

        from sheet_sync import clear_rejected

        print(f"→ Writing {len(rows)} row(s) to Google Sheet...")
        cleared = clear_rejected(sheet_id, args.tab)
        if cleared:
            print(f"  discarded {cleared} rejected cell(s) from the last sync (values reset from Salesforce)")
        tab = write_sheet(sheet_id, args.tab, rows)
        print(f"\nDone: wrote {len(rows)} row(s) to tab '{tab}' "
              f"at {datetime.now():%Y-%m-%d %H:%M}.")
        return 0

    except SalesforceExpiredSession:
        print("\nSalesforce session expired. Re-authenticate in Chrome "
              f"(refresh {SF_LOGIN_URL}) and re-run.", file=sys.stderr)
        return 2
    except SetupError as e:
        print(f"\n{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
