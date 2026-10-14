"""Update a single field on Opportunities using the Salesforce REST API.

Default source: the Google Sheet CD_SALESFORCE_STATUS_URL (as written by
`./run.sh sheet`). Every row whose SHEET_TARGET_FIELD cell is empty gets that
field set to SHEET_TARGET_VALUE, matched by its "Opportunity ID".
Opps that already have a value in Salesforce are never overwritten.

Alternatives: --report (every opp in SALESFORCE_REPORT_URL, field/value from
TARGET_FIELD_LABEL/TARGET_VALUE or --field/--value) or --opp <ids>.

One-off: sf_agent.py OPP_ID "FIELD LABEL" "VALUE" updates just that opp.

Auth: reuses the `sid` cookie from Chrome running with --remote-debugging-port=9222.
Complete your Salesforce login (e.g. SSO) in that Chrome window once; this script attaches to that
authenticated session.

Prereqs: run ./launch_chrome.sh first (opens the debug Chrome on :9222).
While that Chrome is open, any local process can reach :9222 and act as you in
Salesforce — close it when you're done.
"""

import argparse
import asyncio
import os
import re
import sys
import time
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from simple_salesforce import Salesforce
from simple_salesforce.exceptions import SalesforceError, SalesforceExpiredSession

load_dotenv()

# Salesforce Report Ids start with "00O", Opportunity Ids with "006".
REPORT_ID_RE = re.compile(r"(00O[A-Za-z0-9]{12,15})")
OPP_ID_RE = re.compile(r"(006[A-Za-z0-9]{12,15})")


def parse_opp_refs(refs: list[str]) -> list[str]:
    """Extract Opportunity Ids from a mix of bare Ids, URLs, and comma-separated lists.

    Deduplicates while preserving first-seen order.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for raw in refs:
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            match = OPP_ID_RE.search(chunk)
            if not match:
                raise SetupError(
                    f"Could not extract an Opportunity Id (starts with '006') from {chunk!r}."
                )
            opp_id = match.group(1)
            if opp_id not in seen:
                seen.add(opp_id)
                ids.append(opp_id)
    return ids


def resolve_report_id() -> str:
    """Read the report reference from .env, accepting either a full URL or a bare Id."""
    raw = os.getenv("SALESFORCE_REPORT_URL") or os.getenv("SALESFORCE_REPORT_ID")
    if not raw:
        raise SetupError(
            "No report configured. Set SALESFORCE_REPORT_URL (or SALESFORCE_REPORT_ID) "
            "in .env, or pass specific opportunities via --opp."
        )
    match = REPORT_ID_RE.search(raw)
    if not match:
        raise SetupError(
            f"Could not extract a Salesforce Report Id (starts with '00O') "
            f"from SALESFORCE_REPORT_URL/SALESFORCE_REPORT_ID = {raw!r}."
        )
    return match.group(1)
CDP_URL = "http://localhost:9222"
SF_HOST_HINT = "salesforce.com"
SF_LOGIN_URL = os.getenv("SALESFORCE_LOGIN_URL") or "https://login.salesforce.com/"


# Sheet-driven mode: which column to check, and what to write when it's empty.
SHEET_FIELD_LABEL = os.getenv("SHEET_TARGET_FIELD") or ""
SHEET_VALUE = os.getenv("SHEET_TARGET_VALUE") or ""
SHEET_ID_COLUMN = "Opportunity ID"


def fetch_sheet_opportunity_ids(tab: Optional[str]) -> list[str]:
    """Opportunity Ids from sheet rows whose SHEET_FIELD_LABEL cell is empty."""
    from sf_to_sheet import read_sheet, resolve_sheet_id

    rows, tab = read_sheet(resolve_sheet_id(), tab)
    if not rows:
        raise SetupError(f"Sheet tab '{tab}' is empty. Run `./run.sh sheet` first.")
    for col in (SHEET_ID_COLUMN, SHEET_FIELD_LABEL):
        if col not in rows[0]:
            raise SetupError(
                f"Sheet tab '{tab}' has no '{col}' column. Run `./run.sh sheet` to rebuild it."
            )
    print(f"  tab '{tab}': {len(rows)} row(s)")

    refs = []
    for i, row in enumerate(rows, 2):
        if row[SHEET_FIELD_LABEL]:
            continue
        if not row[SHEET_ID_COLUMN]:
            print(f"  row {i}: empty '{SHEET_ID_COLUMN}' — skipped", file=sys.stderr)
            continue
        refs.append(row[SHEET_ID_COLUMN])
    return parse_opp_refs(refs)


def resolve_target_field_and_value() -> tuple[str, str]:
    """Read the field label + raw value from env (may be empty; CLI flags can override)."""
    return os.getenv("TARGET_FIELD_LABEL") or "", os.getenv("TARGET_VALUE") or ""


class SetupError(RuntimeError):
    """User-actionable setup problem — printed without a traceback."""


async def read_sf_session_from_chrome() -> tuple[str, str]:
    """Return (session_id, instance_host) from the Chrome debug session."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
            contexts = browser.contexts
            if not contexts:
                raise SetupError(
                    "Chrome is running on :9222 but has no open contexts. "
                    "Open a tab and navigate to your Salesforce instance."
                )

            sid: Optional[dict] = None
            for ctx in contexts:
                for cookie in await ctx.cookies():
                    if cookie["name"] == "sid" and SF_HOST_HINT in cookie["domain"]:
                        sid = cookie
                        break
                if sid:
                    break
            await browser.close()

            if not sid:
                raise SetupError(
                    "No Salesforce `sid` cookie found in Chrome. "
                    "In Chrome (already running on :9222), open "
                    f"{SF_LOGIN_URL} and complete login, "
                    "then re-run this script."
                )

            instance = sid["domain"].lstrip(".")
            return sid["value"], instance
    except SetupError:
        raise
    except Exception as e:
        raise SetupError(
            f"Could not connect to Chrome at {CDP_URL}. Launch Chrome with:\n"
            f"  open -a 'Google Chrome' --args --remote-debugging-port=9222\n"
            f"then complete login on {SF_LOGIN_URL}.\n"
            f"Underlying error: {e}"
        )


def find_field(sf: Salesforce, label: str) -> dict:
    """Return the full describe entry for the field with the given label."""
    describe = sf.Opportunity.describe()
    for f in describe["fields"]:
        if f["label"] == label:
            return f
    raise SetupError(
        f"Field labeled '{label}' not found on Opportunity. "
        f"Check field-level security on your Salesforce profile, "
        f"or confirm the label spelling in Setup → Object Manager → Opportunity."
    )


def resolve_target_value(sf: Salesforce, field: dict, raw: str) -> tuple[object, str]:
    """Turn a user-facing value into the API payload value + a display label.

    - User-lookup field  → raw is a user name; return the User Id.
    - Boolean field      → coerce 'true'/'false'/'yes'/'no'/'1'/'0'.
    - Anything else      → pass raw through; simple_salesforce handles coercion.
    """
    if field["type"] == "reference" and "User" in (field.get("referenceTo") or []):
        return find_user_id(sf, raw), f"user '{raw}'"
    if field["type"] == "boolean":
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "y"):
            return True, "true"
        if low in ("false", "0", "no", "n"):
            return False, "false"
        raise SetupError(
            f"Field '{field['label']}' is a checkbox; TARGET_VALUE must be "
            f"true/false, got {raw!r}."
        )
    return raw, repr(raw)


def find_user_id(sf: Salesforce, name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    q = sf.query(
        f"SELECT Id, Name, Username FROM User WHERE Name = '{escaped}' AND IsActive = true"
    )
    records = q["records"]
    if not records:
        raise SetupError(f"No active Salesforce user named '{name}'.")
    if len(records) > 1:
        detail = "\n  ".join(f"{r['Id']} ({r['Username']})" for r in records)
        raise SetupError(
            f"Multiple active users named '{name}':\n  {detail}\n"
            f"Set TARGET_VALUE in .env to a more specific name, "
            f"or pass a User Id via --value."
        )
    return records[0]["Id"]


def _run_report_async(sf: Salesforce, report_id: str, timeout_s: int = 120) -> dict:
    """Kick off an async report run and poll until it finishes.

    Uses the async endpoint because the sync one is capped at 500/hour/user
    and we hit it fast during testing. Async quota is separate (1200/hour).
    """
    instance = sf.restful(
        f"analytics/reports/{report_id}/instances?includeDetails=true",
        method="POST",
        json={},
    )
    instance_id = instance["id"]

    deadline = time.monotonic() + timeout_s
    delay = 1.0
    while time.monotonic() < deadline:
        result = sf.restful(
            f"analytics/reports/{report_id}/instances/{instance_id}", method="GET"
        )
        status = result.get("attributes", {}).get("status") or result.get("status")
        if status == "Success":
            return result
        if status == "Error":
            raise SetupError(
                f"Async report {report_id} ended with status Error. "
                f"Response: {result.get('attributes', {}).get('errorMessage', result)}"
            )
        time.sleep(delay)
        delay = min(delay * 1.5, 5.0)
    raise SetupError(
        f"Async report {report_id} did not finish within {timeout_s}s."
    )


def fetch_report_opportunity_ids(sf: Salesforce, report_id: str) -> list[str]:
    """Extract Opportunity Ids from every row of the report's fact map."""
    result = _run_report_async(sf, report_id)
    if not result.get("hasDetailRows"):
        raise SetupError(
            "Report was fetched but has no detail rows. "
            "Confirm the report ID and that your user has access to it."
        )

    opp_ids: list[str] = []
    seen: set[str] = set()
    for group in result.get("factMap", {}).values():
        for row in group.get("rows", []):
            for cell in row.get("dataCells", []):
                value = cell.get("value")
                if isinstance(value, dict):
                    value = value.get("id") or value.get("Id")
                if (
                    isinstance(value, str)
                    and len(value) in (15, 18)
                    and value.startswith("006")
                    and value not in seen
                ):
                    seen.add(value)
                    opp_ids.append(value)

    if not opp_ids:
        raise SetupError(
            f"Report {report_id} returned rows but no Opportunity Ids "
            f"were extractable from the fact map. "
            f"Add an 'Opportunity ID' column to the report and try again."
        )
    return opp_ids


def process(sf: Salesforce, opp_ids: list[str], field_api: str, target_value: object,
            display: str, dry_run: bool,
            only_if_empty: bool = False) -> tuple[int, int, int]:
    updated = skipped = errored = 0
    for i, opp_id in enumerate(opp_ids, 1):
        prefix = f"[{i}/{len(opp_ids)}] {opp_id}"
        try:
            opp = sf.Opportunity.get(opp_id)
            current = opp.get(field_api)
            if current == target_value:
                print(f"  {prefix}  skip — already {display}")
                skipped += 1
                continue
            if only_if_empty and current not in (None, ""):
                print(f"  {prefix}  skip — already set to {current!r} in Salesforce")
                skipped += 1
                continue

            if dry_run:
                print(f"  {prefix}  WOULD UPDATE: {current!r} → {display}")
            else:
                sf.Opportunity.update(opp_id, {field_api: target_value})
                print(f"  {prefix}  updated: {current!r} → {display}")
            updated += 1
        except SalesforceError as e:
            print(f"  {prefix}  ERROR: {e}", file=sys.stderr)
            errored += 1
    return updated, skipped, errored


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Read-only: print planned updates without writing.",
    )
    parser.add_argument(
        "--field",
        help="Opportunity field label to update. Overrides TARGET_FIELD_LABEL in .env.",
    )
    parser.add_argument(
        "--value",
        help="Value to write. For User-lookup fields, pass the user's full name. "
             "Overrides TARGET_VALUE in .env.",
    )
    parser.add_argument(
        "--opp", action="append", default=[],
        help="Opportunity Id or URL. Repeat or comma-separate for multiple. "
             "When set, the sheet/report is skipped entirely.",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Legacy mode: target every opp in SALESFORCE_REPORT_URL instead of the sheet.",
    )
    parser.add_argument(
        "--tab", default=os.getenv("CD_SALESFORCE_STATUS_TAB"),
        help="Sheet tab to read. Defaults to CD_SALESFORCE_STATUS_TAB or the first tab.",
    )
    parser.add_argument(
        "target", nargs="*", metavar="OPP_ID FIELD VALUE",
        help='One-off update, e.g. 006Vr00000ABC "Solutions Engineer" '
             '"Jane Doe". Same as --opp ID --field FIELD --value VALUE.',
    )
    args = parser.parse_args()
    if args.target:
        if len(args.target) != 3:
            parser.error('one-off update takes exactly 3 arguments: OPP_ID "FIELD" "VALUE" '
                         "(quote the field and value if they contain spaces)")
        if args.opp or args.field or args.value or args.report:
            parser.error("don't combine OPP_ID FIELD VALUE with --opp/--field/--value/--report")
        opp_ref, args.field, args.value = args.target
        args.opp = [opp_ref]
    sheet_mode = not args.opp and not args.report

    try:
        if sheet_mode:
            field_label = args.field or SHEET_FIELD_LABEL
            raw_value = args.value or SHEET_VALUE
        else:
            env_field, env_value = resolve_target_field_and_value()
            field_label = args.field or env_field
            raw_value = args.value or env_value
        if not field_label or not raw_value:
            env_names = ("SHEET_TARGET_FIELD and SHEET_TARGET_VALUE" if sheet_mode
                         else "TARGET_FIELD_LABEL and TARGET_VALUE")
            raise SetupError(
                f"Target field and value are required. Set {env_names} in .env, "
                "or pass --field and --value."
            )

        if sheet_mode:
            # Read the sheet first — no point attaching to Chrome if there's nothing to do.
            print(f"→ Reading Google Sheet for empty '{SHEET_FIELD_LABEL}' cells...")
            opp_ids = fetch_sheet_opportunity_ids(args.tab)
            print(f"  {len(opp_ids)} opportunity(ies) with an empty '{SHEET_FIELD_LABEL}' in the sheet")
            if not opp_ids:
                print("\nNothing to do.")
                return 0

        print("→ Extracting Salesforce session from Chrome...")
        session_id, instance = await read_sf_session_from_chrome()
        print(f"  instance: {instance}")

        sf = Salesforce(instance=instance, session_id=session_id)

        print(f"→ Resolving field '{field_label}'...")
        field = find_field(sf, field_label)
        print(f"  API name: {field['name']}  (type: {field['type']})")

        print(f"→ Resolving value {raw_value!r}...")
        target_value, display = resolve_target_value(sf, field, raw_value)
        print(f"  → {display}")

        if sheet_mode:
            pass
        elif args.opp:
            opp_ids = parse_opp_refs(args.opp)
            print(f"→ Targeting {len(opp_ids)} opportunity(ies) from --opp")
        else:
            report_id = resolve_report_id()
            print(f"→ Fetching report {report_id}...")
            opp_ids = fetch_report_opportunity_ids(sf, report_id)
            print(f"  {len(opp_ids)} opportunities")

        mode = "DRY RUN" if args.dry_run else "LIVE"
        print(f"→ Processing ({mode})...")
        updated, skipped, errored = process(
            sf, opp_ids, field["name"], target_value, display, args.dry_run,
            only_if_empty=sheet_mode,
        )

        print()
        verb = "would update" if args.dry_run else "updated"
        print(f"Summary: {verb} {updated}, skipped {skipped}, errored {errored}")
        return 0 if errored == 0 else 1

    except SalesforceExpiredSession:
        print(
            "\nSalesforce session expired. Re-authenticate in Chrome "
            f"(refresh {SF_LOGIN_URL}) and re-run.",
            file=sys.stderr,
        )
        return 2
    except SetupError as e:
        print(f"\n{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
