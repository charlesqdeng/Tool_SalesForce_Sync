"""Push your Google Sheet edits back to Salesforce, and only your edits.

`./run.sh sheet` writes the export to the visible tab and an identical hidden
baseline tab. A cell counts as edited when the visible tab differs from the
baseline. Rows are matched by "Opportunity ID", not position, so sorting,
filtering or inserting rows is safe. Only columns in SHEET_SYNC_COLUMNS are
ever pushed.

For each edited cell (S = sheet, B = baseline, F = Salesforce now):
  F == S          already applied: skip, refresh baseline
  F == B          push S to Salesforce
  otherwise       conflict, because Salesforce changed since the export: skip
                  and report (--force pushes anyway)
Rows with no baseline (opps added to the sheet by hand) only fill fields that
are empty in Salesforce; anything else is a conflict.

Cleared cells are ignored unless --allow-clear is passed. After a live run the
baseline is updated for every applied cell, so nothing is pushed twice.

Cells Salesforce rejects (e.g. a validation rule) are logged in a hidden
_sync_errors_<tab> tab and shaded red in the sheet, with the error as a cell
note. Later syncs skip a rejected cell until you change its value (or pass
--retry-errors). The mark is cleared once the cell syncs.
"""

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime
from datetime import datetime
from typing import Optional

from simple_salesforce import Salesforce
from simple_salesforce.exceptions import SalesforceError, SalesforceExpiredSession

from sf_agent import (
    OPP_ID_RE,
    SF_LOGIN_URL,
    SHEET_ID_COLUMN,
    SetupError,
    find_user_id,
    read_sf_session_from_chrome,
)
from sf_to_sheet import (
    URL_HEADER,
    _cell,
    errors_tab_name,
    mark_cells,
    read_tab,
    write_tab,
    _get_path,
    export_columns,
    parse_columns,
    query_opportunities,
    read_sheet_with_baseline,
    resolve_fields,
    resolve_sheet_id,
    soql_path,
    write_baseline,
)

NUMERIC_TYPES = ("double", "currency", "percent", "int", "long")
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")
REJECTED_HEADER = [SHEET_ID_COLUMN, "Column", "Rejected value", "Error", "When"]
RECORD_ID_RE = re.compile(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?")


def sync_columns() -> list[tuple[str, str]]:
    """(sheet header, field label) for each column that may be pushed.

    Labels come from SHEET_COLUMNS' "Header=Field" mapping unless
    SHEET_SYNC_COLUMNS gives its own.
    """
    raw = (os.getenv("SHEET_SYNC_COLUMNS") or "").strip()
    if not raw:
        raise SetupError(
            "Set SHEET_SYNC_COLUMNS in .env to the comma-separated sheet columns "
            "whose edits should be pushed to Salesforce, "
            "e.g. SHEET_SYNC_COLUMNS=Stage,Next Step"
        )
    labels = dict(export_columns())
    return [
        (header, field if field != header else labels.get(header, header))
        for header, field in parse_columns(raw)
    ]


def row_opp_key(row: dict[str, str]) -> Optional[str]:
    """15-char Opportunity Id from the ID column (or the URL column as fallback)."""
    for col in (SHEET_ID_COLUMN, URL_HEADER):
        match = OPP_ID_RE.search(row.get(col, ""))
        if match:
            return match.group(1)[:15]
    return None


def _norm_text(value: str) -> str:
    return value.replace("\r\n", "\n").strip()


def normalize(value: str, field: dict) -> str:
    """Canonical form for comparing a sheet cell with a Salesforce value."""
    text = _norm_text(value)
    if not text:
        return ""
    ftype = field["type"]
    if ftype == "boolean":
        low = text.lower()
        if low in ("true", "1", "yes", "y"):
            return "true"
        if low in ("false", "0", "no", "n"):
            return "false"
    elif ftype in NUMERIC_TYPES:
        try:
            return repr(float(text.replace(",", "").rstrip("%").lstrip("$")))
        except ValueError:
            pass
    elif ftype == "date":
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
    elif ftype == "multipicklist":
        return ";".join(sorted(p.strip() for p in text.split(";") if p.strip()))
    return text


class SyncPlanError(ValueError):
    """A sheet value that can't be written to its field."""


def to_payload(sf: Salesforce, field: dict, text: str, user_ids: dict[str, str]) -> object:
    """Turn a sheet cell into the value Salesforce's REST API expects."""
    text = _norm_text(text)
    if not text:
        return None
    ftype = field["type"]
    if ftype == "reference":
        if RECORD_ID_RE.fullmatch(text) and any(c.isdigit() for c in text):
            return text  # a raw record Id
        if "User" not in (field.get("referenceTo") or []):
            raise SyncPlanError(
                f"lookup to {'/'.join(field.get('referenceTo') or ['?'])} — "
                f"put a record Id in the cell, names only work for User lookups"
            )
        if text not in user_ids:
            try:
                user_ids[text] = find_user_id(sf, text)
            except SetupError as e:
                raise SyncPlanError(str(e).splitlines()[0])
        return user_ids[text]
    norm = normalize(text, field)
    if ftype == "boolean":
        if norm not in ("true", "false"):
            raise SyncPlanError(f"checkbox needs true/false, got {text!r}")
        return norm == "true"
    if ftype in NUMERIC_TYPES:
        try:
            number = float(norm)
        except ValueError:
            raise SyncPlanError(f"not a number: {text!r}")
        return int(number) if ftype in ("int", "long") else number
    if ftype == "date":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", norm):
            raise SyncPlanError(f"date must be YYYY-MM-DD or M/D/YYYY, got {text!r}")
        return norm
    if ftype == "picklist":
        allowed = {v["value"] for v in field.get("picklistValues") or [] if v.get("active")}
        if allowed and text not in allowed:
            raise SyncPlanError(f"{text!r} is not an active picklist value")
    return text


def find_edits(rows: list[dict[str, str]], baseline: Optional[tuple[list[str], list[dict]]],
               headers: list[str], allow_clear: bool) -> tuple[list[dict], list[str]]:
    """Cells that differ from the baseline in the given columns.

    Returns (edits, notes). Each edit is {row, key, header, sheet, base} where
    base is None if the opp has no baseline row.
    """
    base_rows = {}
    for b in baseline[1] if baseline else []:
        key = row_opp_key(b)
        if key:
            base_rows.setdefault(key, b)

    edits, notes, seen = [], [], set()
    for i, row in enumerate(rows, 2):
        key = row_opp_key(row)
        if not key:
            if any(row.get(h) for h in headers):
                notes.append(f"row {i}: no Opportunity ID — ignored")
            continue
        if key in seen:
            notes.append(f"row {i}: duplicate of an earlier row for {key} — ignored")
            continue
        seen.add(key)
        base = base_rows.get(key)
        for h in headers:
            sheet_val = row.get(h, "")
            base_val = base.get(h, "") if base is not None else None
            if base_val is not None and _norm_text(sheet_val) == _norm_text(base_val):
                continue
            if not _norm_text(sheet_val):
                if base_val is None:
                    continue
                if not allow_clear:
                    notes.append(f"row {i} {key} '{h}': cleared in sheet — ignored "
                                 f"(pass --allow-clear to clear it in Salesforce)")
                    continue
            edits.append({"row": i, "key": key, "header": h,
                          "sheet": sheet_val, "base": base_val})
    return edits, notes


def check_no_pending_edits(sheet_id: str, tab: Optional[str]) -> None:
    """Raise if the sheet has edits that a re-export would wipe out."""
    rows, baseline, tab = read_sheet_with_baseline(sheet_id, tab)
    if baseline is None:
        return
    try:
        headers = [h for h, _ in sync_columns()]
    except SetupError:
        return
    edits, _ = find_edits(rows, baseline, headers, allow_clear=False)
    # Cells Salesforce rejected can't be synced as-is; re-exporting discards them.
    rejected = load_rejected(sheet_id, tab)
    edits = [e for e in edits
             if _norm_text(rejected.get((e["key"], e["header"]), {}).get("Rejected value", "\0"))
             != _norm_text(e["sheet"])]
    if edits:
        raise SetupError(
            f"Tab '{tab}' has {len(edits)} edited cell(s) not yet synced to Salesforce, "
            f"and re-exporting would overwrite them.\n"
            f"Run `./run.sh sync --dry-run` then `./run.sh sync` first, "
            f"or pass --force to discard them."
        )


def load_rejected(sheet_id: str, tab: str) -> dict[tuple[str, str], dict[str, str]]:
    """{(15-char opp Id, column): log row} for cells Salesforce rejected."""
    rejected = {}
    for r in read_tab(sheet_id, errors_tab_name(tab)):
        key = row_opp_key(r)
        if key and r.get("Column"):
            rejected[(key, r["Column"])] = r
    return rejected


def skip_rejected(edits: list[dict], rejected: dict[tuple[str, str], dict]) -> list[dict]:
    """Drop edits whose value is the one Salesforce already rejected."""
    kept = []
    for e in edits:
        r = rejected.get((e["key"], e["header"]))
        if r and _norm_text(r.get("Rejected value", "")) == _norm_text(e["sheet"]):
            print(f"  row {e['row']} {e['key']} '{e['header']}'  skip — rejected "
                  f"{r.get('When', '')}: {r.get('Error', '')} (change the cell to retry)",
                  file=sys.stderr)
            continue
        kept.append(e)
    return kept


def update_rejected(sheet_id: str, tab: str, rows: list[dict[str, str]],
                    rejected: dict[tuple[str, str], dict], edits: list[dict]) -> None:
    """Log/mark cells rejected this run; unlog/unmark ones that synced or changed."""
    col_index = {h: i for i, h in enumerate(rows[0])} if rows else {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    new, marks = dict(rejected), []
    for e in edits:
        cell = (e["key"], e["header"])
        if e["action"] in ("push", "applied"):
            if new.pop(cell, None) is not None:
                marks.append((e["row"], col_index[e["header"]], None))
        elif e["action"] == "error" and e.get("rejected"):
            new[cell] = {SHEET_ID_COLUMN: e.get("id", e["key"]), "Column": e["header"],
                         "Rejected value": e["sheet"], "Error": e["reason"], "When": now}
            marks.append((e["row"], col_index[e["header"]],
                          f"Salesforce rejected this value ({now}):\n{e['reason']}\n\n"
                          f"Change the cell to retry."))
    if new != rejected:
        write_tab(sheet_id, errors_tab_name(tab), REJECTED_HEADER, list(new.values()))
    mark_cells(sheet_id, tab, marks)


def clear_rejected(sheet_id: str, tab: Optional[str]) -> int:
    """Before a re-export: unmark every logged cell and empty the log.

    The export rewrites values from Salesforce, so old rejections no longer apply.
    """
    from sf_to_sheet import read_sheet

    rows, tab = read_sheet(sheet_id, tab)
    rejected = load_rejected(sheet_id, tab)
    if not rejected:
        return 0
    col_index = {h: i for i, h in enumerate(rows[0])} if rows else {}
    marks = [(i, col_index[col], None)
             for i, r in enumerate(rows, 2)
             for (key, col) in rejected if row_opp_key(r) == key and col in col_index]
    mark_cells(sheet_id, tab, marks)
    write_tab(sheet_id, errors_tab_name(tab), REJECTED_HEADER, [])
    return len(rejected)


def salesforce_message(err: SalesforceError) -> str:
    """Human-readable message(s) from a Salesforce API error."""
    content = getattr(err, "content", None)
    if isinstance(content, list):
        msgs = [c.get("message", "") for c in content if isinstance(c, dict)]
        if any(msgs):
            return "; ".join(m for m in msgs if m)
    return str(err).splitlines()[0]


def plan(sf: Salesforce, edits: list[dict], fields: dict[str, dict],
         force: bool) -> list[dict]:
    """Compare each edit with Salesforce and decide its action, in place."""
    paths = sorted({soql_path(f) for f in fields.values()})
    records = query_opportunities(sf, sorted({e["key"] for e in edits}), paths)
    user_ids: dict[str, str] = {}
    for e in edits:
        field = fields[e["header"]]
        record = records.get(e["key"])
        if record is None:
            e["action"], e["reason"] = "error", "opportunity not found or not readable"
            continue
        e["id"] = record["Id"]
        current = _cell(_get_path(record, soql_path(field)))
        e["current"] = current
        ns, nf = normalize(e["sheet"], field), normalize(current, field)
        if ns == nf:
            e["action"] = "applied"
            continue
        if e["base"] is not None:
            conflict = nf != normalize(e["base"], field)
            reason = "changed in Salesforce since the export"
        else:
            conflict = nf != ""
            reason = "row not in last export and Salesforce already has a value"
        if conflict and not force:
            e["action"], e["reason"] = "conflict", reason
            continue
        try:
            e["payload"] = to_payload(sf, field, e["sheet"], user_ids)
            e["action"] = "push"
        except SyncPlanError as err:
            e["action"], e["reason"] = "error", str(err)
    return edits


def _short(text: Optional[str], n: int = 60) -> str:
    text = (text or "").replace("\n", " ⏎ ")
    return repr(text if len(text) <= n else text[:n - 1] + "…")


def apply(sf: Salesforce, edits: list[dict], fields: dict[str, dict], dry_run: bool) -> None:
    """Print the plan; unless dry_run, write each opp's pushes in one update."""
    by_opp: dict[str, list[dict]] = {}
    for e in edits:
        by_opp.setdefault(e["key"], []).append(e)
    for key, opp_edits in by_opp.items():
        pushes = [e for e in opp_edits if e["action"] == "push"]
        if pushes and not dry_run:
            _push(sf, pushes, fields)
        for e in opp_edits:
            where = f"row {e['row']} {key} '{e['header']}'"
            action = e["action"]
            if action == "push":
                verb = "WOULD UPDATE" if dry_run else "updated"
                print(f"  {where}  {verb}: {_short(e['current'])} → {_short(e['sheet'])}")
            elif action == "applied":
                print(f"  {where}  skip — Salesforce already has {_short(e['sheet'])}")
            elif action == "conflict":
                print(f"  {where}  CONFLICT — {e['reason']}\n"
                      f"      exported: {_short(e['base'])}  Salesforce: "
                      f"{_short(e['current'])}  sheet: {_short(e['sheet'])}",
                      file=sys.stderr)
            else:
                print(f"  {where}  ERROR: {e['reason']}", file=sys.stderr)


def _push(sf: Salesforce, pushes: list[dict], fields: dict[str, dict]) -> None:
    """Write an opp's pushes in one update. If Salesforce rejects it, retry
    each field alone so one bad field doesn't block the rest.
    """
    def update(batch):
        sf.Opportunity.update(batch[0]["id"],
                              {fields[e["header"]]["name"]: e["payload"] for e in batch})

    try:
        update(pushes)
        return
    except SalesforceError as err:
        if len(pushes) == 1:
            pushes[0].update(action="error", reason=salesforce_message(err), rejected=True)
            return
    for e in pushes:
        try:
            update([e])
        except SalesforceError as err:
            e.update(action="error", reason=salesforce_message(err), rejected=True)


def refresh_baseline(sheet_id: str, tab: str, rows: list[dict[str, str]],
                     baseline: Optional[tuple[list[str], list[dict]]],
                     edits: list[dict]) -> int:
    """Record applied cells in the baseline. Returns the number of cells recorded.

    Opps with no baseline row get one only if every edit on them was applied.
    Otherwise their conflicts would turn into pushes on the next run.
    """
    header, base_rows = (list(baseline[0]), baseline[1]) if baseline else ([], [])
    base_by_key = {}
    for b in base_rows:
        key = row_opp_key(b)
        if key:
            base_by_key.setdefault(key, b)
    sheet_by_key = {}
    for r in rows:
        key = row_opp_key(r)
        if key:
            sheet_by_key.setdefault(key, r)

    done = [e for e in edits if e["action"] in ("push", "applied")]
    failed_keys = {e["key"] for e in edits if e["action"] not in ("push", "applied")}
    recorded = 0
    for e in done:
        base = base_by_key.get(e["key"])
        if base is None:
            if e["key"] in failed_keys:
                continue
            base = dict(sheet_by_key[e["key"]])
            base_by_key[e["key"]] = base
            base_rows.append(base)
            header.extend(h for h in base if h not in header)
        base[e["header"]] = e["sheet"]
        if e["header"] not in header:
            header.append(e["header"])
        recorded += 1
    if recorded:
        write_baseline(sheet_id, tab, header, base_rows)
    return recorded


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Read-only: print planned updates without writing anything.")
    parser.add_argument("--tab", default=os.getenv("CD_SALESFORCE_STATUS_TAB"),
                        help="Sheet tab to read. Defaults to CD_SALESFORCE_STATUS_TAB "
                             "or the first tab.")
    parser.add_argument("--allow-clear", action="store_true",
                        help="Cells you emptied clear the field in Salesforce "
                             "(default: ignored).")
    parser.add_argument("--force", action="store_true",
                        help="Push conflicting cells too, overwriting changes made "
                             "in Salesforce since the export.")
    parser.add_argument("--retry-errors", action="store_true",
                        help="Retry cells Salesforce rejected before, even if unchanged.")
    args = parser.parse_args()

    try:
        sheet_id = resolve_sheet_id()
        columns = sync_columns()
        headers = [h for h, _ in columns]

        print("→ Reading Google Sheet and baseline...")
        rows, baseline, tab = read_sheet_with_baseline(sheet_id, args.tab)
        if not rows:
            raise SetupError(f"Sheet tab '{tab}' is empty. Run `./run.sh sheet` first.")
        present = set(rows[0])
        missing = [h for h in [SHEET_ID_COLUMN, *headers] if h not in present]
        if missing:
            raise SetupError(f"Sheet tab '{tab}' has no column(s): {', '.join(missing)}.")
        print(f"  tab '{tab}': {len(rows)} row(s); syncing columns: {', '.join(headers)}")
        if baseline is None:
            print("  no baseline yet (run `./run.sh sheet` to create one); every row "
                  "is treated as new, so only fields empty in Salesforce get filled")

        edits, notes = find_edits(rows, baseline, headers, args.allow_clear)
        for note in notes:
            print(f"  {note}", file=sys.stderr)
        if baseline is not None:
            other = [h for h, _ in export_columns() if h not in headers and h in present]
            ignored, _ = find_edits(rows, baseline, other, allow_clear=True)
            if ignored:
                cols = ", ".join(sorted({e["header"] for e in ignored}))
                print(f"  {len(ignored)} edited cell(s) in columns not in "
                      f"SHEET_SYNC_COLUMNS ignored: {cols}", file=sys.stderr)
        rejected = load_rejected(sheet_id, tab)
        if not args.retry_errors:
            edits = skip_rejected(edits, rejected)
        print(f"  {len(edits)} edited cell(s) in synced columns to process")
        if not edits:
            print("\nNothing to do.")
            return 0

        print("→ Extracting Salesforce session from Chrome...")
        session_id, instance = await read_sf_session_from_chrome()
        print(f"  instance: {instance}")
        sf = Salesforce(instance=instance, session_id=session_id)

        print("→ Resolving fields...")
        resolved = resolve_fields(sf, [label for _, label in columns])
        fields = dict(zip(headers, resolved))
        for h, f in fields.items():
            print(f"  {h} → {f['name']}  ({f['type']})")
        readonly = [h for h, f in fields.items() if not f.get("updateable")]
        if readonly:
            raise SetupError(
                f"Not editable in Salesforce (formula, system, or no edit permission): "
                f"{', '.join(readonly)}. Remove from SHEET_SYNC_COLUMNS."
            )

        mode = "DRY RUN" if args.dry_run else "LIVE"
        print(f"→ Comparing with Salesforce ({mode})...")
        plan(sf, edits, fields, args.force)
        apply(sf, edits, fields, args.dry_run)

        counts = {a: sum(e["action"] == a for e in edits)
                  for a in ("push", "applied", "conflict", "error")}
        if not args.dry_run:
            recorded = refresh_baseline(sheet_id, tab, rows, baseline, edits)
            if recorded:
                print(f"→ Baseline updated for {recorded} cell(s)")
            update_rejected(sheet_id, tab, rows, rejected, edits)
            failed = sum(bool(e.get("rejected")) for e in edits)
            if failed:
                print(f"→ Marked {failed} rejected cell(s) red in the sheet "
                      f"(skipped on later syncs until changed)")

        print()
        verb = "would update" if args.dry_run else "updated"
        print(f"Summary: {verb} {counts['push']}, already applied {counts['applied']}, "
              f"conflicts {counts['conflict']}, errored {counts['error']}")
        return 0 if counts["conflict"] == counts["error"] == 0 else 1

    except SalesforceExpiredSession:
        print("\nSalesforce session expired. Re-authenticate in Chrome "
              f"(refresh {SF_LOGIN_URL}) and re-run.", file=sys.stderr)
        return 2
    except SetupError as e:
        print(f"\n{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
