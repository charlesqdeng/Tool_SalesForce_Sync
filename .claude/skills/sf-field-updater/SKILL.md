---
name: sf-field-updater
description: Update a single field on Salesforce Opportunities — either every opp in a saved report, or a specific list of opps by Id/URL. Use whenever the user asks to update, set, assign, or clear a field on one or more Salesforce opportunities — e.g. "set SE-Primary to Alice on the report", "clear Discovery Date on 006Vr00000ABC", "mark opps X and Y as approved". Handles User-lookup fields (looks up User Id by name) and literal fields (text, picklist, number, date, checkbox) via one code path. ALWAYS dry-runs first and requires explicit confirmation before writing.
---

# Salesforce bulk field updater

Drives `sf_agent.py` in the project root. Do NOT reimplement the Salesforce API calls — the Python is the tested path.

## Inputs to gather from the user

Before running anything, make sure you have:

1. **Field label** — the label as it appears in Salesforce (e.g. `"Solutions Engineer"`, `"Stage"`, `"Close Date"`). The API name is auto-resolved via `Opportunity.describe()`. If the user gives you only an API name, ask for the label.
2. **Target value** — what to write.
   - User-lookup fields: the person's **full name** (e.g. `"Alice Smith"`). Script resolves to a User Id.
   - Picklist: the exact picklist value.
   - Text / number / date: the literal value.
   - Checkbox: `true` / `false` (also accepts `yes/no`, `1/0`).
3. **Scope** — which opps to update. Three mutually exclusive paths:
   - **Sheet** (default, no flags): rows of the Google Sheet `CD_SALESFORCE_STATUS_URL` whose `SHEET_TARGET_FIELD` cell is empty get that field set to `SHEET_TARGET_VALUE` (both from `.env`; TARGET_* ignored). Opps already set in Salesforce are skipped.
   - **Report** (`--report`): every Opportunity returned by the current `SALESFORCE_REPORT_URL` in `.env`. If the user wants a different report, get the URL first.
   - **Specific opps**: one or more Opportunity Ids (start with `006`) or full Opportunity URLs. Route these through `--opp` — no report needed. This is the right path when the user names specific opps, gives you an Opportunity URL, or says "just this one."

If any of those is missing, ask the user before proceeding. Do not guess.

## Steps

1. **Check Chrome is up on :9222.** Run `curl -sf http://localhost:9222/json/version >/dev/null && echo ok || echo missing`. If missing, tell the user to run `./launch_chrome.sh` and complete Salesforce login, then stop and wait.
2. **Set config.**
   - **Field + value**: for a persistent change, edit `.env` to set `TARGET_FIELD_LABEL` and `TARGET_VALUE`. For a one-off, pass `--field "…" --value "…"` on the command line instead. Preserve other lines in `.env`.
   - **Scope**: for the report path, make sure `SALESFORCE_REPORT_URL` is the report the user wants (edit `.env` if changing). For specific opps, do not touch `.env` — pass them via `--opp`.
3. **Dry run.** Run one of:
   - Sheet path: `./run.sh --dry-run`
   - Report path: `./run.sh --report --dry-run`
   - Specific opps: `./run.sh --dry-run --opp 006Vr00000ABC,006Vr00000XYZ` (comma-separated or repeat `--opp` per Id; URLs work too — the script extracts the Id).

   Show the user the full output (or, if the list is long, the summary line plus first + last few rows).
4. **Wait for explicit confirmation.** Do NOT run the live update unless the user says "apply", "run it", "go", "for real", or similar. "Looks good" alone is ambiguous — ask if you're not sure.
5. **Apply.** Re-run the same command without `--dry-run`. Show the summary line.

Single opp, positional shorthand: `./run.sh --dry-run 006Vr00000ABC "Field Label" "Value"`.

Full one-liner for a fully-specified one-off (no `.env` edits):
```
./run.sh --dry-run --field "Stage" --value "Closed Won" --opp 006Vr00000ABC
```

## Guardrails

- **Never** run `./run.sh` (live) without a dry-run in the same conversation.
- **Never** proceed if the dry-run output shows unexpected opportunity counts (e.g. 0, or 10× what the user expected) — surface the number and ask.
- If the dry-run errors with `Field labeled '…' not found`, the label is misspelled or field-level security is hiding it. Do not attempt to guess variants; ask the user to confirm the label from Salesforce Setup.
- If it errors with `Multiple active users named '…'`, ask the user which one — do not pick.
- If Chrome's Salesforce session is expired, the script prints an actionable message; relay it and stop.
- Do NOT modify `sf_agent.py` for a field-type or report change. The refactor is done — env vars and `--field`/`--value` cover it. Only touch Python if the user asks for a new object (Account, Contact, custom) or a multi-field-per-opp update.

## Reference

- Script: [sf_agent.py](../../../sf_agent.py)
- README (authoritative for setup / troubleshooting): [README.md](../../../README.md)
- Config: [.env](../../../.env) — `SALESFORCE_REPORT_URL`, `TARGET_FIELD_LABEL`, `TARGET_VALUE`
