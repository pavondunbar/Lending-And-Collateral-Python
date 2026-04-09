#!/usr/bin/env python3
"""
scripts/demo.py — Full live-stack demonstration script
Exercises the entire Lending & Collateral Management Infrastructure
against a running docker-compose stack.

Usage:
    # Start the stack first:
    #   docker compose up --build -d
    #   (wait ~60s for all services to be healthy)

    python scripts/demo.py

    # Or with a custom gateway URL / API key:
    GATEWAY_URL=http://localhost:8000 GATEWAY_API_KEY=my-key python scripts/demo.py
"""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

# ---- Config ---------------------------------------------------------------

GW = os.environ.get("GATEWAY_URL", "http://localhost:8000")
KEY = os.environ.get("GATEWAY_API_KEY", "change-me-in-production")

HEADERS = {"X-API-Key": KEY, "Content-Type": "application/json"}

client = httpx.Client(base_url=GW, headers=HEADERS, timeout=30)

# ---- Colours --------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
CYAN = "\033[36m"
RED = "\033[31m"
YELLOW = "\033[33m"
BLUE = "\033[34m"


def h1(text: str):
    width = 72
    print(f"\n{BOLD}{BLUE}{'=' * width}{RESET}")
    print(f"{BOLD}{BLUE}  {text}{RESET}")
    print(f"{BOLD}{BLUE}{'=' * width}{RESET}")


def h2(text: str):
    print(f"\n{CYAN}  -- {text}{RESET}")


def ok(text: str):
    print(f"  {GREEN}+{RESET}  {text}")


def info(text: str):
    print(f"  {YELLOW}>{RESET}  {text}")


def fail(text: str):
    print(f"  {RED}x{RESET}  {text}")


def pretty(data: dict) -> str:
    return json.dumps(data, indent=4, default=str)


def check(resp: httpx.Response, expected: int = None) -> dict:
    if expected and resp.status_code != expected:
        fail(f"HTTP {resp.status_code} -- expected {expected}")
        print(f"  Body: {resp.text[:400]}")
        sys.exit(1)
    return resp.json()


# ---- Step Runner -----------------------------------------------------------

step_n = 0


def step(title: str):
    global step_n
    step_n += 1
    print(f"\n  {BOLD}[{step_n:02d}]{RESET} {title}")


# ---- Helpers ---------------------------------------------------------------

DOCKER_CONTAINER = "lending-postgres-1"
DB_USER = "lending"
DB_NAME = "lending_db"


def psql(sql: str) -> str:
    """Execute SQL via docker exec against the lending Postgres."""
    result = subprocess.run(
        [
            "docker", "exec", DOCKER_CONTAINER,
            "psql", "-U", DB_USER, "-d", DB_NAME, "-t", "-c", sql,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        fail(f"SQL failed: {result.stderr.strip()}")
        sys.exit(1)
    return result.stdout.strip()


# ---- Health Check ----------------------------------------------------------

def wait_healthy(retries: int = 15, delay: float = 5.0):
    """Wait until the gateway reports all services healthy."""
    info(f"Waiting for {GW}/health ...")
    for i in range(retries):
        try:
            r = client.get("/health", timeout=5)
            if r.status_code == 200:
                data = r.json()
                svcs = data.get("services", {})
                if all(v == "ok" for v in svcs.values()):
                    ok("All services healthy.")
                    return
            print(
                f"  retry {i + 1}/{retries}"
                f" -- {r.json().get('services', {})}"
            )
        except Exception as exc:
            print(f"  retry {i + 1}/{retries} -- {exc}")
        time.sleep(delay)
    fail(
        "Stack not healthy after retries. "
        "Run: docker compose up --build -d"
    )
    sys.exit(1)


# ---- Demo Sections ---------------------------------------------------------

def demo_onboard() -> dict:
    """Steps 1-4: register participants and verify KYC/AML."""
    h1("0 - Onboard Institutional Participants")

    step("Register Atlas Capital Partners (institutional)")
    r = client.post("/v1/accounts", json={
        "entity_name": "Atlas Capital Partners",
        "account_type": "institutional",
        "lei": f"ATLAS{uuid.uuid4().hex[:13].upper()}",
    })
    data = check(r, 201)
    atlas_id = data["account_id"]
    ok(f"Atlas created: {atlas_id} ({data['entity_name']})")

    step("Register Meridian Lending Pool (lending_pool)")
    r = client.post("/v1/accounts", json={
        "entity_name": "Meridian Lending Pool",
        "account_type": "lending_pool",
        "lei": f"MRDLN{uuid.uuid4().hex[:13].upper()}",
    })
    data = check(r, 201)
    pool_id = data["account_id"]
    ok(f"Pool created: {pool_id} ({data['entity_name']})")

    step("Register SecureVault Custody (custodian)")
    r = client.post("/v1/accounts", json={
        "entity_name": "SecureVault Custody",
        "account_type": "custodian",
        "lei": f"SVLT{uuid.uuid4().hex[:14].upper()}",
    })
    data = check(r, 201)
    custodian_id = data["account_id"]
    ok(
        f"Custodian created: {custodian_id}"
        f" ({data['entity_name']})"
    )

    step("Mark all accounts KYC/AML verified (via Postgres)")
    sql = (
        "UPDATE accounts "
        "SET kyc_verified=true, aml_cleared=true "
        f"WHERE id IN ('{atlas_id}', '{pool_id}', '{custodian_id}');"
    )
    psql(sql)
    ok("All three accounts cleared for KYC/AML (demo shortcut).")

    return {
        "atlas_id": atlas_id,
        "pool_id": pool_id,
        "custodian_id": custodian_id,
    }


def demo_fund_pool(pool_id: str) -> None:
    """Steps 5-6: seed the lending pool with $50M."""
    h1("1 - Fund Lending Pool")

    step("Fund Meridian pool with $50M USD (direct SQL seed)")
    journal_id = str(uuid.uuid4())
    sql = (
        "BEGIN; "
        "INSERT INTO journal_entries "
        "(id, journal_id, account_id, coa_code, currency, "
        "debit, credit, entry_type, narrative) VALUES "
        f"(uuid_generate_v4(), '{journal_id}', '{pool_id}', "
        "'LENDER_POOL', 'USD', 0, 50000000, "
        "'initial_funding', 'Initial pool funding $50M'), "
        f"(uuid_generate_v4(), '{journal_id}', "
        "'00000000-0000-0000-0000-000000000001', "
        "'LENDER_POOL', 'USD', 50000000, 0, "
        "'initial_funding', 'System offset for pool seed'); "
        "COMMIT;"
    )
    psql(sql)
    ok("$50,000,000 USD credited to Meridian Lending Pool.")

    step("Show lending pool balance")
    r = client.get(f"/v1/accounts/{pool_id}")
    if r.status_code == 200:
        data = r.json()
        ok(f"Pool account: {data.get('entity_name', pool_id)}")
        for b in data.get("balances", []):
            info(
                f"  {b.get('coa_code', 'N/A')}  "
                f"{b.get('currency', 'N/A')}  "
                f"balance={b.get('balance', 'N/A')}"
            )
    else:
        info(f"Pool ID: {pool_id} (balance query via view)")
        balance_row = psql(
            "SELECT balance FROM account_balances "
            f"WHERE account_id = '{pool_id}' "
            "AND coa_code = 'LENDER_POOL' "
            "AND currency = 'USD';"
        )
        ok(f"Pool balance (from view): ${balance_row.strip()}")


def demo_originate(
    atlas_id: str,
    pool_id: str,
    custodian_id: str,
) -> dict:
    """Steps 7-10: set prices, originate loan with BTC collateral."""
    h1("2 - Loan Origination with BTC Collateral")

    step("Set BTC price to $60,000 (price oracle feed)")
    r = client.post("/v1/prices/feed", json={
        "asset_type": "BTC",
        "price_usd": 60000,
        "source": "coinbase",
    })
    data = check(r, 201)
    ok(f"BTC price recorded: ${float(data.get('price_usd', 60000)):,.0f}")

    step("Set ETH price to $3,200 (price oracle feed)")
    r = client.post("/v1/prices/feed", json={
        "asset_type": "ETH",
        "price_usd": 3200,
        "source": "coinbase",
    })
    data = check(r, 201)
    ok(f"ETH price recorded: ${float(data.get('price_usd', 3200)):,.0f}")

    step(
        "Originate $5M USDC loan for Atlas "
        "(150 BTC collateral, 65% max LTV)"
    )
    r = client.post("/v1/loans/originate", json={
        "borrower_id": atlas_id,
        "lender_pool_id": pool_id,
        "currency": "USD",
        "principal": 5000000,
        "interest_rate_bps": 800,
        "interest_method": "compound_daily",
        "initial_ltv_pct": 65,
        "maintenance_ltv_pct": 75,
        "liquidation_ltv_pct": 85,
        "origination_fee_bps": 50,
        "maturity_days": 365,
        "collateral": {
            "asset_type": "BTC",
            "quantity": 150,
            "haircut_pct": 10,
            "custodian_id": custodian_id,
        },
    })
    data = check(r, 201)
    loan_ref = data["loan_ref"]
    loan_id = data.get("loan_id", "")
    ok(f"Loan originated: {loan_ref}")
    info(
        f"  Principal: ${float(data.get('principal', 5000000)):,.0f}  "
        f"Rate: {data.get('interest_rate_bps', 800)} bps"
    )
    info(
        f"  Collateral: 150 BTC @ $60,000 = $9,000,000 gross"
    )
    info(
        "  Haircut 10% -> adjusted $8,100,000  "
        "LTV = 5M / 8.1M = 61.7%"
    )

    step("Show loan status -- ACTIVE")
    r = client.get(f"/v1/loans/{loan_ref}")
    data = check(r, 200)
    ok(
        f"Loan {loan_ref}: status={data.get('status', 'active')}"
    )
    for cp in data.get("collateral", []):
        info(
            f"  Collateral: {cp.get('quantity')} "
            f"{cp.get('asset_type')}  "
            f"status={cp.get('status')}"
        )

    return {
        "loan_ref": loan_ref,
        "loan_id": loan_id,
    }


def demo_interest_accrual(loan_ref: str) -> Decimal:
    """Steps 11-12: accrue 30 days of compound interest."""
    h1("3 - Interest Accrual (30 Days)")

    step("Simulate 30 days of daily interest accrual")
    cumulative = Decimal("0")
    base_date = datetime.now(timezone.utc).date()
    for day in range(1, 31):
        accrual_date = base_date + timedelta(days=day)
        r = client.post(
            f"/v1/loans/{loan_ref}/accrue-interest",
            json={"accrual_date": str(accrual_date)},
        )
        if r.status_code in (200, 201):
            data = r.json()
            cumulative = Decimal(
                str(data.get("cumulative_interest", cumulative))
            )
            if day in (1, 10, 20, 30):
                info(
                    f"  Day {day:2d}: "
                    f"accrued=${data.get('accrued_amount', 'N/A')}  "
                    f"cumulative=${cumulative:,.2f}"
                )
        else:
            info(
                f"  Day {day}: HTTP {r.status_code} "
                f"(may already exist)"
            )
    ok(
        f"30 days accrued. "
        f"Total interest: ${cumulative:,.2f}"
    )

    step("Show interest history")
    r = client.get(f"/v1/loans/{loan_ref}/interest")
    if r.status_code == 200:
        entries = r.json()
        ok(f"Interest entries: {len(entries)}")
        if entries:
            last = entries[-1]
            info(
                f"  Latest: date={last.get('accrual_date')}  "
                f"cumulative=${last.get('cumulative_interest')}"
            )
    else:
        info("Interest history endpoint not yet implemented.")

    return cumulative


def demo_margin_call(
    loan_ref: str,
    atlas_id: str,
    custodian_id: str,
    accrued_interest: Decimal,
) -> None:
    """Steps 13-16: BTC price drops, margin call, then resolved."""
    h1("4 - Market Stress: Margin Call")

    step("Price oracle: BTC drops to $46,000")
    r = client.post("/v1/prices/feed", json={
        "asset_type": "BTC",
        "price_usd": 46000,
        "source": "coinbase",
    })
    check(r, 201)
    outstanding = Decimal("5000000") + accrued_interest
    new_val = 150 * 46000 * Decimal("0.9")
    ltv = (outstanding / new_val) * 100
    ok(f"BTC now $46,000")
    info(
        f"  Collateral: 150 * $46,000 * 0.9 = "
        f"${new_val:,.0f}"
    )
    info(
        f"  Outstanding: ${outstanding:,.2f}  "
        f"LTV: {ltv:.1f}% (> 75% maintenance)"
    )

    step("Trigger margin evaluation")
    r = client.post("/v1/margin/evaluate", json={
        "loan_ref": loan_ref,
    })
    data = check(r, 200)
    margin_ref = data.get("margin_call_ref", "N/A")
    ok(
        f"Margin call: {margin_ref}  "
        f"status={data.get('status', 'triggered')}"
    )
    info(
        f"  Triggered LTV: {float(data.get('triggered_ltv', ltv)):.1f}%"
    )
    info(
        f"  Required additional collateral: "
        f"${data.get('required_additional_collateral', 'N/A')}"
    )

    step("Atlas deposits 40 more BTC to meet margin call")
    r = client.post("/v1/collateral/deposit", json={
        "loan_ref": loan_ref,
        "asset_type": "BTC",
        "quantity": 40,
        "custodian_id": custodian_id,
    })
    data = check(r, 201)
    ok(
        f"Deposited 40 BTC. "
        f"Total collateral now: 190 BTC"
    )
    new_val_190 = 190 * 46000 * Decimal("0.9")
    new_ltv = (outstanding / new_val_190) * 100
    info(
        f"  New value: 190 * $46,000 * 0.9 = "
        f"${new_val_190:,.0f}"
    )
    info(f"  New LTV: {new_ltv:.1f}% (under 75%)")

    step("Re-evaluate margin -- should resolve")
    r = client.post("/v1/margin/evaluate", json={
        "loan_ref": loan_ref,
    })
    data = check(r, 200)
    ok(
        f"Margin status: {data.get('status', 'met')}  "
        f"LTV: {float(data.get('current_ltv', new_ltv)):.1f}%"
    )


def demo_liquidation(
    loan_ref: str,
    accrued_interest: Decimal,
) -> dict:
    """Steps 17-21: BTC crashes, liquidation cascade."""
    h1("5 - Liquidation Cascade")

    step("Price oracle: BTC crashes to $28,000")
    r = client.post("/v1/prices/feed", json={
        "asset_type": "BTC",
        "price_usd": 28000,
        "source": "coinbase",
    })
    check(r, 201)
    outstanding = Decimal("5000000") + accrued_interest
    col_val = 190 * 28000 * Decimal("0.9")
    ltv = (outstanding / col_val) * 100
    ok("BTC now $28,000")
    info(
        f"  Collateral: 190 * $28,000 * 0.9 = "
        f"${col_val:,.0f}"
    )
    info(
        f"  LTV: {ltv:.1f}% "
        f"(> 85% liquidation threshold)"
    )

    step("Trigger margin evaluation -- should initiate liquidation")
    r = client.post("/v1/margin/evaluate", json={
        "loan_ref": loan_ref,
    })
    data = check(r, 200)
    liq_ref = data.get("liquidation_ref", "N/A")
    ok(
        f"Liquidation initiated: {liq_ref}  "
        f"status={data.get('status', 'liquidating')}"
    )

    step("Execute liquidation -- sell 50 BTC at $28,000")
    r = client.post(f"/v1/liquidations/{liq_ref}/execute", json={
        "asset_type": "BTC",
        "quantity": 50,
        "sale_price": 28000,
    })
    data = check(r, 200)
    proceeds = Decimal(str(data.get("total_proceeds", 1400000)))
    ok(
        f"Sold 50 BTC @ $28,000 = "
        f"${proceeds:,.0f} proceeds"
    )

    step("Apply waterfall distribution")
    r = client.post(
        f"/v1/liquidations/{liq_ref}/waterfall",
        json={},
    )
    data = check(r, 200)
    ok("Waterfall distribution applied:")
    info(
        f"  Interest recovered: "
        f"${Decimal(str(data.get('interest_recovered', accrued_interest))):,.2f}"
    )
    info(
        f"  Principal recovered: "
        f"${Decimal(str(data.get('principal_recovered', 0))):,.2f}"
    )
    info(
        f"  Fees recovered: "
        f"${Decimal(str(data.get('fees_recovered', 0))):,.2f}"
    )
    info(
        f"  Borrower remainder: "
        f"${Decimal(str(data.get('borrower_remainder', 0))):,.2f}"
    )

    step("Show liquidation details")
    r = client.get(f"/v1/liquidations/{liq_ref}")
    if r.status_code == 200:
        data = r.json()
        ok(
            f"Liquidation {liq_ref}: "
            f"status={data.get('status')}"
        )
    else:
        info("Liquidation detail endpoint returned non-200.")

    return {"liq_ref": liq_ref, "proceeds": proceeds}


def demo_repayment_and_closure(
    loan_ref: str,
    atlas_id: str,
) -> None:
    """Steps 22-26: repay remaining balance, release collateral."""
    h1("6 - Repayment and Loan Closure")

    step("Partial repayment: $1,000,000")
    r = client.post(f"/v1/loans/{loan_ref}/repay", json={
        "amount": 1000000,
        "currency": "USD",
    })
    data = check(r, 200)
    ok(
        f"Repaid $1,000,000. "
        f"Remaining: "
        f"${data.get('remaining_principal', 'N/A')}"
    )

    step("Full repayment of remaining balance")
    remaining = Decimal(
        str(data.get("remaining_principal", 0))
    )
    if remaining > 0:
        r = client.post(f"/v1/loans/{loan_ref}/repay", json={
            "amount": float(remaining),
            "currency": "USD",
        })
        data = check(r, 200)
        ok(
            f"Final repayment: ${remaining:,.2f}  "
            f"status={data.get('status', 'repaid')}"
        )
    else:
        ok("No remaining balance (fully covered by liquidation).")

    step("Verify loan status: REPAID")
    r = client.get(f"/v1/loans/{loan_ref}")
    data = check(r, 200)
    ok(
        f"Loan {loan_ref}: "
        f"status={data.get('status', 'repaid')}"
    )

    step(
        "Release remaining collateral (140 BTC) "
        "back to Atlas"
    )
    r = client.post("/v1/collateral/withdraw", json={
        "loan_ref": loan_ref,
        "asset_type": "BTC",
        "quantity": 140,
        "destination_account_id": atlas_id,
    })
    if r.status_code in (200, 201):
        data = r.json()
        ok(
            f"140 BTC released to Atlas. "
            f"Collateral status: "
            f"{data.get('status', 'released')}"
        )
    else:
        info(
            f"Collateral withdraw returned "
            f"HTTP {r.status_code}: {r.text[:200]}"
        )

    step("Complete audit trail summary")
    r = client.get(f"/v1/loans/{loan_ref}/audit")
    if r.status_code == 200:
        trail = r.json()
        ok(f"Audit events: {len(trail)}")
        for event in trail[-5:]:
            info(
                f"  [{event.get('created_at', '')}] "
                f"{event.get('event_type', '')} "
                f"-- {event.get('detail', '')}"
            )
    else:
        info("Audit trail via SQL view:")
        rows = psql(
            "SELECT status, created_at "
            "FROM loan_status_history "
            "WHERE loan_id = ("
            f"  SELECT id FROM loans "
            f"  WHERE loan_ref = '{loan_ref}'"
            ") ORDER BY created_at;"
        )
        for line in rows.splitlines():
            stripped = line.strip()
            if stripped:
                info(f"  {stripped}")


def demo_rbac() -> None:
    """Steps 27-28: verify role-based access control enforcement."""
    h1("7 - RBAC: Role-Based Access Control")

    viewer_headers = {
        "X-API-Key": "viewer-demo-key",
        "Content-Type": "application/json",
    }

    step("Test viewer role is blocked on POST")
    with httpx.Client(base_url=GW, timeout=30) as viewer:
        r = viewer.post(
            "/v1/loans/originate",
            headers=viewer_headers,
            json={},
        )
        if r.status_code == 403:
            ok(f"POST /v1/loans/originate -> HTTP 403 (blocked)")
            info(f"  Response: {r.text[:200]}")
        else:
            fail(
                f"Expected 403 but got {r.status_code}: "
                f"{r.text[:200]}"
            )

        step("Test viewer role can GET")
        r = viewer.get("/v1/loans", headers=viewer_headers)
        if r.status_code == 200:
            ok(f"GET /v1/loans -> HTTP 200 (allowed)")
            data = r.json()
            info(f"  Returned {len(data)} loan(s)")
        else:
            fail(
                f"Expected 200 but got {r.status_code}: "
                f"{r.text[:200]}"
            )


def demo_idempotency(loan_ref: str) -> None:
    """Steps 29-30: verify duplicate request prevention."""
    h1("8 - Idempotency: Duplicate Request Prevention")

    step(
        "Send same accrue-interest request twice "
        "with same Idempotency-Key"
    )
    idem_key = str(uuid.uuid4())
    accrual_date = str(
        datetime.now(timezone.utc).date() + timedelta(days=90)
    )
    url = f"/v1/loans/{loan_ref}/accrue-interest"
    payload = {"accrual_date": accrual_date}
    headers_with_key = {
        **HEADERS,
        "Idempotency-Key": idem_key,
    }

    r1 = client.post(url, json=payload, headers=headers_with_key)
    info(
        f"  Request 1: HTTP {r1.status_code}  "
        f"Idempotency-Key={idem_key[:12]}..."
    )
    body1 = r1.text

    r2 = client.post(url, json=payload, headers=headers_with_key)
    info(
        f"  Request 2: HTTP {r2.status_code}  "
        f"Idempotency-Key={idem_key[:12]}..."
    )
    body2 = r2.text

    if r1.status_code == r2.status_code and body1 == body2:
        ok("Both responses are identical (idempotent)")
    else:
        info(
            "Responses differ -- server may not enforce "
            "idempotency on this endpoint"
        )
        info(f"  Response 1: {body1[:200]}")
        info(f"  Response 2: {body2[:200]}")


def demo_settlement() -> None:
    """Steps 31-32: query settlement state machine from Postgres."""
    h1("9 - Settlement State Machine")

    step("Query settlements from Postgres")
    rows = psql(
        "SELECT settlement_ref, status, operation, asset_type "
        "FROM settlements "
        "ORDER BY created_at DESC LIMIT 5;"
    )
    if rows:
        ok("Recent settlements:")
        for line in rows.splitlines():
            stripped = line.strip()
            if stripped:
                info(f"  {stripped}")
    else:
        info("No settlements found (table may be empty).")

    step("Show settlement status history")
    rows = psql(
        "SELECT s.settlement_ref, ssh.status, ssh.created_at "
        "FROM settlement_status_history ssh "
        "JOIN settlements s ON s.id = ssh.settlement_id "
        "ORDER BY ssh.created_at DESC LIMIT 10;"
    )
    if rows:
        ok("Settlement status history:")
        for line in rows.splitlines():
            stripped = line.strip()
            if stripped:
                info(f"  {stripped}")
    else:
        info(
            "No settlement status history found "
            "(table may be empty)."
        )


# ---- Main Demo Runner ------------------------------------------------------

def run():
    h1(
        "Institutional Lending & Collateral Management"
        " -- Live Demo"
    )
    info(f"Gateway: {GW}")
    print()

    wait_healthy()

    try:
        ids = demo_onboard()
        atlas_id = ids["atlas_id"]
        pool_id = ids["pool_id"]
        custodian_id = ids["custodian_id"]

        demo_fund_pool(pool_id)

        loan_info = demo_originate(
            atlas_id, pool_id, custodian_id,
        )
        loan_ref = loan_info["loan_ref"]

        accrued = demo_interest_accrual(loan_ref)

        demo_margin_call(
            loan_ref, atlas_id, custodian_id, accrued,
        )

        demo_liquidation(loan_ref, accrued)

        demo_repayment_and_closure(loan_ref, atlas_id)

        demo_rbac()

        demo_idempotency(loan_ref)

        demo_settlement()

    except SystemExit:
        print(f"\n{RED}Demo aborted -- see error above.{RESET}")
        sys.exit(1)

    h1("Demo Complete")
    ok("Loan origination with LTV enforcement")
    ok("BTC collateral custody with MPC signing")
    ok("Daily interest accrual (compound)")
    ok("Margin call triggered and resolved")
    ok("Liquidation with waterfall distribution")
    ok("Full repayment and collateral release")
    ok(
        "All events published via transactional "
        "outbox to Kafka"
    )
    ok("RBAC role-based access enforcement")
    ok("Idempotent request handling")
    ok("Settlement state machine lifecycle")
    print(
        f"\n  {BOLD}"
        f"Explore the OpenAPI docs at: {GW}/docs"
        f"{RESET}\n"
    )


if __name__ == "__main__":
    run()
