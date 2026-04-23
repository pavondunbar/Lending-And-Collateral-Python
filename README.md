# Institutional Lending & Collateral Management Platform

<img width="1620" height="971" alt="9bdfb2f4-04e5-472b-9dfe-e5792d84a012" src="https://github.com/user-attachments/assets/11f6b83e-3b38-44ff-b993-b79bb97616e4" />

> :warning: **SANDBOX / EDUCATIONAL USE ONLY — NOT FOR PRODUCTION**
> This codebase is a reference implementation designed for learning, prototyping, and architectural exploration. It is **not audited, not legally reviewed, and must not be used to originate real loans, manage real collateral, or handle real investor funds.** See the [Production Warning](#-production-warning) section for full details.

---

## Table of Contents

- [Overview](#-overview)
- [What is Collateralized Lending?](#-what-is-collateralized-lending)
- [Architecture](#-architecture)
- [Core Services](#-core-services)
- [Key Features & Design Patterns](#-key-features--design-patterns)
- [Database Schema](#-database-schema)
- [State Machines](#-state-machines)
- [Real-World Example: Atlas Capital Borrows $5M Against 150 BTC](#-real-world-example-atlas-capital-borrows-5m-against-150-btc)
- [Running in a Sandbox Environment](#-running-in-a-sandbox-environment)
- [Project Structure](#-project-structure)
- [Production Warning](#-production-warning)
- [License](#-license)

---

## :book: Overview

The **Institutional Lending & Collateral Management Platform** is a Python-based reference implementation that models the full lifecycle of a **crypto-collateralized institutional loan** — from account onboarding and loan origination through collateral custody, real-time LTV monitoring, margin call management, interest accrual, liquidation waterfall distribution, and loan closure.

The system is modeled closely on how institutional digital asset lending infrastructure operates at **Galaxy Digital**, **Genesis (pre-bankruptcy)**, **BlockFi**, **Anchorage Digital**, and **Copper.co**. It demonstrates how traditional lending mechanics (interest accrual, margin calls, waterfall distributions) integrate with digital asset custody infrastructure (MPC signing, real-time price feeds, automated liquidation) to achieve institutional-grade risk management.

| Component | Count | Responsibility |
|-----------|-------|----------------|
| Microservices | 10 | Lending, collateral, margin, liquidation, pricing, compliance, signing, gateway, outbox |
| MPC Nodes | 3 | Threshold cryptography for collateral release signing |
| Kafka Topics | 52 | 26 primary event topics + 26 Dead Letter Queue topics |
| Database Tables | 20+ | Append-only immutable ledger with double-entry accounting |
| API Endpoints | 25+ | Full REST API behind RBAC-authenticated gateway |
| Docker Networks | 3 | Trust boundary isolation (DMZ, internal, signing) |

---

## :earth_americas: What is Collateralized Lending?

**Collateralized lending** is a financial arrangement where a borrower pledges assets (collateral) to a lender to secure a loan. If the borrower defaults, the lender can seize and liquidate the collateral to recover the outstanding debt. In the digital asset space, this means pledging crypto assets (BTC, ETH, SOL, AVAX) against fiat or stablecoin loans.

Collateralized lending is the backbone of:
- **Institutional crypto lending desks** (Galaxy, Genesis, Maple Finance)
- **DeFi lending protocols** (Aave, Compound, MakerDAO) — this system models the off-chain institutional equivalent
- **Prime brokerage services** for hedge funds and asset managers
- **Repo and reverse-repo markets** using tokenized assets as collateral
- **Treasury management** where corporations borrow against crypto holdings instead of selling

The core risk mechanism is the **Loan-to-Value (LTV) ratio**:

```
LTV = Outstanding Debt / Collateral Value

Example:
  Loan: $5,000,000
  Collateral: 150 BTC × $60,000 × (1 - 10% haircut) = $8,100,000
  LTV = $5,000,000 / $8,100,000 = 61.7%
```

As crypto prices drop, LTV rises. The system enforces three thresholds:
- **Initial LTV (65%)** — maximum ratio at loan origination
- **Maintenance LTV (75%)** — triggers a margin call requiring additional collateral
- **Liquidation LTV (85%)** — triggers automatic collateral seizure and sale

This system implements the full institutional workflow: origination, custody, monitoring, margin calls, liquidation, waterfall distribution, interest accrual, and compliance screening — with MPC-signed collateral releases and an immutable double-entry ledger.

---

## :classical_building: Architecture

```
                  ┌─────────────────────────────────────────────────────────────────┐
                  │           LENDING & COLLATERAL MANAGEMENT LIFECYCLE             │
                  └─────────────────────────────────────────────────────────────────┘

  DMZ Network (Internet-Facing)          Internal Network (Sandboxed)
  ─────────────────────────────────────────────────────────────────────────────────

  Clients ──► API Gateway (8000)
              │  RBAC (5 roles via X-API-Key)
              │  Rate limiter (1000/60s)
              │  Audit trail → Kafka (trace_id + actor_id)
              │
              ├──► Lending Engine (8001)      PostgreSQL 16
              │    Loan origination            │  Append-only ledger
              │    Interest accrual            │  Double-entry journal
              │    Repayment processing        │  Status history tables
              │                                │  Transactional outbox
              ├──► Collateral Manager (8002)   │
              │    Deposit / withdraw          │
              │    Substitution (atomic)       │
              │    Real-time valuation ◄───────┤
              │    MPC-signed releases ────────┼──────► Signing Network
              │                                │        │
              ├──► Margin Engine (8003)        │        ├── Signing Gateway (8007)
              │    LTV monitoring              │        │     Fan-out + combine
              │    Margin call lifecycle       │        │
              │    Liquidation triggers        │        ├── MPC Node 1
              │                                │        ├── MPC Node 2
              ├──► Liquidation Engine (8004)   │        └── MPC Node 3
              │    Collateral sale execution   │             2-of-3 threshold
              │    Waterfall distribution      │
              │    MPC-signed releases ────────┤
              │                                │
              ├──► Price Oracle (8005)         │
              │    Multi-source aggregation    │
              │    VWAP computation            │
              │                                │
              └──► Compliance Monitor (8006)   │
                   AML / sanctions screening   │
                   Kafka event consumption     │
                                               │
              Outbox Publisher (8010) ──────────┤──► Apache Kafka
              Polls outbox table                     26 primary + 26 DLQ topics
              Advisory locking (SKIP LOCKED)         7-30 day retention
              At-least-once delivery + DLQ
                                               │
              Prometheus (9090) ◄───────────────┤    Metrics scraping
              Grafana (3000)                         Dashboard visualization
```

### Trust Boundary Network Isolation

| Network | Type | Services | Purpose |
|---------|------|----------|---------|
| `dmz` | bridge | API Gateway, Prometheus, Grafana | Internet-facing — only exposed ports |
| `internal` | internal | All microservices, PostgreSQL, Kafka | No outbound internet access |
| `signing` | internal | MPC nodes, Signing Gateway, Collateral Manager, Liquidation Engine | Isolated cryptographic operations |

---

## :gear: Core Services

### `Lending Engine` — Loan Lifecycle Management

The core loan origination and servicing engine. Handles the full lifecycle from application to closure, enforcing LTV constraints, computing compound interest, and recording every financial movement as immutable double-entry journal pairs.

**Responsibilities:**
- Loan origination with inline collateral deposit and LTV validation
- Partial and full repayment processing with interest/principal split
- Daily compound interest accrual (`simple`, `compound_daily`, `compound_monthly`)
- Loan closure with balance verification
- Immutable journal entries for all financial operations
- Transactional outbox events for reliable downstream notification

### `Collateral Manager` — Asset Custody & Valuation

Manages the custody lifecycle of digital assets pledged as loan collateral. Tracks positions, computes real-time valuations with haircut adjustments, and coordinates MPC-signed releases for withdrawals and liquidations.

**Responsibilities:**
- Accept collateral deposits (BTC, ETH, SOL, AVAX)
- Withdraw collateral with LTV safety validation
- Atomic collateral substitution (remove + add in single transaction)
- Real-time valuation using latest price feeds with configurable haircuts
- MPC-signed collateral release transactions via signing gateway

### `Margin Engine` — LTV Monitoring & Margin Calls

Continuously monitors the health of active loans by computing real-time LTV ratios against live price feeds. Triggers margin calls when maintenance thresholds are breached and initiates liquidation when positions become critically undercollateralized.

**Responsibilities:**
- Evaluate LTV for individual and all active loans
- Trigger margin calls when LTV exceeds 75% maintenance threshold
- Process margin call responses (borrower deposits additional collateral)
- Expire unmet margin calls after configurable deadline (default: 24 hours)
- Initiate liquidation when LTV exceeds 85% liquidation threshold

### `Liquidation Engine` — Collateral Sale & Waterfall Distribution

Executes the forced sale of collateral and distributes proceeds according to a strict priority waterfall. Coordinates with the signing gateway for MPC-signed collateral release and records every distribution step as immutable journal entries.

**Responsibilities:**
- Initiate liquidation proceedings on critically undercollateralized loans
- Execute collateral sales at specified market prices
- Apply waterfall distribution: interest owed → principal owed → fees → borrower remainder
- MPC-signed collateral release transactions
- Append-only status history for regulatory audit trail

### `Price Oracle` — Multi-Source Price Aggregation

Ingests price feeds from multiple external sources and computes volume-weighted average prices (VWAP) for supported crypto assets. Serves as the single source of truth for all valuation and LTV calculations across the platform.

**Responsibilities:**
- Accept price feeds from Coinbase, Binance, Kraken, Chainlink, and internal sources
- Compute VWAP aggregates across multiple sources
- Serve latest and historical prices per asset
- Configurable price simulation for development and demo environments

### `Compliance Monitor` — AML & Sanctions Screening

Real-time compliance screening service that consumes all lending, collateral, and liquidation events from Kafka and applies rule-based AML and sanctions checks. Flags suspicious activity and emits compliance alerts for downstream action.

**Rules:**
- Large transaction threshold: > $1,000,000
- Structuring detection: multiple transactions < $9,500 within time window
- Velocity check: > 20 transactions per hour per entity
- Sanctions screening: OFAC SDN pattern matching on entity identifiers

### `Signing Gateway` — MPC Signature Aggregation

Aggregates partial signatures from MPC nodes to produce combined signatures for high-value collateral release transactions. Implements a 2-of-3 threshold scheme where any two nodes can produce a valid signature.

**Responsibilities:**
- Receive signing requests from collateral-manager and liquidation-engine
- Fan out signing request to all 3 MPC nodes concurrently
- Collect partial signatures and combine once threshold is met
- Return combined signature for on-chain transaction submission

### `MPC Nodes` (3 instances) — Threshold Cryptography

Three independent signing nodes that each produce a partial signature using their shard of the signing key. No single node holds the complete key — a minimum of 2 nodes must cooperate to produce a valid signature.

> **Note:** The MPC implementation uses deterministic SHA256 hashing for demonstration purposes. A production system would use Shamir's Secret Sharing, GG20/FROST threshold ECDSA, or equivalent cryptographic protocols.

### `API Gateway` — Authentication & Routing

The sole internet-facing service. Authenticates all requests via `X-API-Key` with role-based access control, enforces sliding-window rate limiting, propagates trace context to internal services, and writes an audit trail to Kafka for every API call.

**Security features:**
- Role-Based Access Control (RBAC) with 5 roles: `admin`, `operator`, `signer`, `viewer`, `system`
- SHA-256 hashed API key lookup with per-role permission matrix enforcement
- Sliding-window rate limiter: 1,000 requests per 60 seconds per key
- Reverse proxy with per-route upstream mapping
- Audit trail event emission to `audit.trail` Kafka topic
- Propagates `X-Request-ID`, `X-Trace-ID`, `X-Actor-Id`, and `X-Actor-Role` headers to upstream services
- Aggregated health check across all upstream services

### `Outbox Publisher` — Reliable Event Delivery

Standalone service that polls the `outbox_events` table and publishes pending events to Kafka. Uses PostgreSQL advisory locking (`FOR UPDATE SKIP LOCKED`) for safe horizontal scaling across multiple replicas.

**Pattern:** Solves the dual-write problem — business operations and their events are committed atomically to PostgreSQL, then delivered to Kafka asynchronously with at-least-once guarantees.

---

## :sparkles: Key Features & Design Patterns

### :white_check_mark: Double-Entry Accounting Ledger

Every financial operation — loan disbursement, interest accrual, collateral deposit, liquidation distribution — records balanced debit/credit journal entry pairs. All balances are derived from journal entries (no mutable balance columns). The ledger is append-only: corrections are made via offsetting entries, never updates or deletes.

### :white_check_mark: Transactional Outbox Pattern (All Services)

Every service writes its outbox event in the **same database transaction** as the business record. This eliminates the dual-write problem across all services. The outbox publisher delivers events to Kafka only after they are safely committed to PostgreSQL — downstream systems (compliance, risk, audit) never miss an event, even across crashes.

### :white_check_mark: Append-Only Audit Trails with Distributed Tracing

Status history tables (`loan_status_history`, `collateral_status_history`, `margin_call_status_history`, `liquidation_status_history`, `settlement_status_history`) are immutable. Every state transition is recorded with a timestamp, optional JSONB detail payload, `actor_id` (who performed the action), and `trace_id` (request correlation). Context is propagated automatically via Python `contextvars` — upstream services receive trace headers from the API gateway and inject them into all status history writes without explicit parameter passing.

### :white_check_mark: MPC Threshold Signing — No Single Point of Compromise

Collateral release transactions require cryptographic authorization from at least 2 of 3 MPC nodes. A single compromised node cannot produce a valid signature. The signing gateway fans out requests and combines partials once the threshold is met — mirroring Fireblocks and Copper.co's institutional custody architecture.

### :white_check_mark: Real-Time LTV Monitoring with Automated Responses

The margin engine continuously evaluates loan health against live price feeds. When collateral values drop, the system automatically:
1. **Margin call** at 75% LTV — notifies borrower, starts 24-hour countdown
2. **Liquidation** at 85% LTV — seizes collateral, executes sale, distributes proceeds via waterfall

### :white_check_mark: Waterfall Distribution — Creditor Priority Enforcement

Liquidation proceeds are distributed in strict priority order: (1) accrued interest, (2) outstanding principal, (3) origination fees, (4) remainder to borrower. Each step is recorded as a separate journal entry, providing a complete audit trail of every dollar distributed.

### :white_check_mark: Decimal Precision for Financial Arithmetic

All monetary values and quantities use Python's `Decimal` type with `DECIMAL(38,18)` database precision rather than floats, preventing IEEE 754 rounding errors from corrupting financial calculations — mandatory in any system handling institutional-scale lending.

### :white_check_mark: Multi-Source Price Aggregation (VWAP)

The price oracle ingests feeds from multiple sources (Coinbase, Binance, Kraken, Chainlink) and computes volume-weighted average prices. This prevents single-source manipulation and provides more reliable valuations for LTV calculations.

### :white_check_mark: Network Trust Boundaries

Three isolated Docker networks enforce the principle of least privilege: the DMZ exposes only the API gateway, the internal network sandboxes all microservices and databases with no outbound internet access, and the signing network isolates MPC nodes from everything except the signing gateway.

### :white_check_mark: Idempotency Layer (APIs + Message Consumers)

All POST endpoints are protected by an `Idempotency-Key` header mechanism. The gateway passes the header to upstream services, which check the `idempotency_keys` table (using `SELECT FOR UPDATE` on Postgres) before processing. Duplicate requests return the cached response. Kafka consumers deduplicate via the `processed_events` table, checking `event_id` before invoking handlers. Both tables are append-only with immutability triggers.

### :white_check_mark: Role-Based Access Control (RBAC)

Five roles enforce separation of duties at the API gateway:

| Role | Permissions |
|------|-------------|
| `admin` | Full access to all endpoints |
| `operator` | POST on loans, collateral, margin, liquidations, prices; all GET |
| `signer` | POST on signing endpoints; all GET |
| `viewer` | Read-only GET access |
| `system` | POST on prices and margin (automated services); all GET |

API keys are SHA-256 hashed and stored in the `api_keys` table with role assignments. The gateway resolves the role from the key hash and checks the permission matrix before proxying requests.

### :white_check_mark: Settlement State Machine

On-chain settlement transactions follow a deterministic state machine:

```
  PENDING ──► APPROVED ──► SIGNED ──► BROADCASTED ──► CONFIRMED
     ▲           │            │            │
     └───────────┴────────────┴────────────┘
                    FAILED (recoverable → PENDING)
```

Each transition is validated against `VALID_TRANSITIONS`, and invalid transitions raise an error. The `settlements` table tracks `tx_hash`, `block_number`, `confirmations`, and timestamps for each phase. All transitions are recorded in `settlement_status_history` with `actor_id` and `trace_id`.

### :white_check_mark: Dead Letter Queue (DLQ)

Failed Kafka messages are retried up to 3 times. After exhausting retries, the message is published to a `{topic}.dlq` topic and recorded in the `dlq_events` table with the original payload, error message, and retry count. Every primary topic has a corresponding DLQ topic (26 DLQ topics total) with 30-day retention.

### :white_check_mark: Event-Driven Architecture (52 Kafka Topics)

All lifecycle events — loan origination, collateral deposits, margin calls, liquidations, price updates, compliance alerts, settlement transitions — are published to dedicated Kafka topics with 7-30 day retention. Each primary topic has a corresponding DLQ topic for failed message handling. Downstream services consume events asynchronously, enabling loose coupling and independent scaling.

---

## :file_cabinet: Database Schema

### `accounts`

LEI-identified institutional participants.

| Column | Type | Description |
|--------|------|-------------|
| `entity_name` | VARCHAR | Institution name |
| `account_type` | ENUM | `institutional`, `custodian`, `lending_pool`, `system` |
| `lei` | VARCHAR(20) UNIQUE | ISO 17442 Legal Entity Identifier |
| `kyc_verified` | BOOLEAN | KYC verification status |
| `aml_cleared` | BOOLEAN | AML screening clearance |
| `risk_tier` | INTEGER | Risk classification tier |

### `loans`

Root record for each loan agreement.

| Column | Type | Description |
|--------|------|-------------|
| `loan_ref` | UUID UNIQUE | Loan reference identifier |
| `borrower_id` | FK → accounts | Borrowing institution |
| `lender_pool_id` | FK → accounts | Lending pool funding the loan |
| `currency` | ENUM | `USD`, `EUR`, `GBP` |
| `principal` | DECIMAL(38,18) | Original loan amount |
| `interest_rate_bps` | INTEGER | Annual interest rate in basis points |
| `interest_method` | ENUM | `simple`, `compound_daily`, `compound_monthly` |
| `status` | ENUM | `pending` → `approved` → `active` → `repaid` / `defaulted` |
| `maturity_date` | TIMESTAMP | Loan maturity |

### `collateral_positions`

Asset positions pledged against loans.

| Column | Type | Description |
|--------|------|-------------|
| `collateral_ref` | UUID UNIQUE | Position reference |
| `loan_id` | FK → loans | Associated loan |
| `asset_type` | ENUM | `BTC`, `ETH`, `SOL`, `AVAX` |
| `quantity` | DECIMAL(38,18) | Units of asset pledged |
| `haircut_pct` | DECIMAL(5,4) | Volatility haircut (e.g., 0.10 = 10%) |
| `status` | ENUM | `pending_deposit` → `active` → `released` / `seized` |

### `journal_entries`

Immutable double-entry ledger — **no UPDATE or DELETE permitted**.

| Column | Type | Description |
|--------|------|-------------|
| `journal_id` | UUID | Entry pair identifier |
| `account_id` | FK → accounts | Institutional participant |
| `coa_code` | FK → chart_of_accounts | General ledger account code |
| `debit` | DECIMAL(38,18) | Debit amount (zero if credit) |
| `credit` | DECIMAL(38,18) | Credit amount (zero if debit) |
| `currency` | ENUM | Transaction currency |
| `entry_type` | VARCHAR | `loan_disbursement`, `repayment`, `interest_accrual`, etc. |
| `narrative` | TEXT | Human-readable description |

### `margin_calls`

Margin call lifecycle tracking.

| Column | Type | Description |
|--------|------|-------------|
| `margin_call_ref` | UUID UNIQUE | Margin call reference |
| `loan_id` | FK → loans | Affected loan |
| `triggered_ltv` | DECIMAL | LTV at time of trigger |
| `required_additional` | DECIMAL | Additional collateral value needed |
| `deadline` | TIMESTAMP | Response deadline (default: 24 hours) |
| `status` | ENUM | `triggered` → `met` / `expired` → `liquidation_initiated` |

### `liquidation_events`

Liquidation execution and distribution records.

| Column | Type | Description |
|--------|------|-------------|
| `liquidation_ref` | UUID UNIQUE | Liquidation reference |
| `loan_id` | FK → loans | Liquidated loan |
| `collateral_sold_qty` | DECIMAL | Quantity of collateral sold |
| `sale_price_usd` | DECIMAL | Price per unit at sale |
| `proceeds` | DECIMAL | Total sale proceeds |
| `interest_recovered` | DECIMAL | Waterfall: interest portion |
| `principal_recovered` | DECIMAL | Waterfall: principal portion |
| `fees_recovered` | DECIMAL | Waterfall: fees portion |
| `borrower_remainder` | DECIMAL | Waterfall: returned to borrower |
| `status` | ENUM | `initiated` → `executing` → `completed` / `failed` |

### `price_feeds`

Time-series asset price data.

| Column | Type | Description |
|--------|------|-------------|
| `asset_type` | ENUM | `BTC`, `ETH`, `SOL`, `AVAX` |
| `price_usd` | DECIMAL(38,18) | Price in USD |
| `source` | ENUM | `coinbase`, `binance`, `kraken`, `chainlink`, `internal` |
| `is_valid` | BOOLEAN | Feed validity flag |
| `recorded_at` | TIMESTAMP | Price timestamp |

### `outbox_events`

Reliable Kafka delivery buffer.

| Column | Type | Description |
|--------|------|-------------|
| `event_type` | VARCHAR | Kafka topic (e.g., `loan.originated`, `margin.call.triggered`) |
| `aggregate_id` | UUID | Source entity ID |
| `payload` | JSONB | Serialized event data |
| `published_at` | TIMESTAMP | NULL = pending Kafka delivery |

### `api_keys`

RBAC key registry with SHA-256 hashed keys.

| Column | Type | Description |
|--------|------|-------------|
| `key_hash` | VARCHAR(255) UNIQUE | SHA-256 hash of the raw API key |
| `role` | ENUM | `admin`, `operator`, `signer`, `viewer`, `system` |
| `label` | VARCHAR | Human-readable key description |
| `is_active` | BOOLEAN | Whether the key is currently active |

### `idempotency_keys`

Request deduplication for POST endpoints — **immutable (no UPDATE or DELETE)**.

| Column | Type | Description |
|--------|------|-------------|
| `key` | VARCHAR(255) PK | Idempotency key from request header |
| `response_status` | INTEGER | Cached HTTP status code |
| `response_body` | JSONB | Cached response payload |
| `expires_at` | TIMESTAMP | Auto-set to 24 hours after creation |

### `processed_events`

Kafka consumer deduplication — **immutable (no UPDATE or DELETE)**.

| Column | Type | Description |
|--------|------|-------------|
| `event_id` | VARCHAR(255) PK | Unique event identifier |
| `topic` | VARCHAR | Source Kafka topic |
| `processed_at` | TIMESTAMP | When the event was processed |

### `dlq_events`

Dead Letter Queue tracking — **immutable (no UPDATE or DELETE)**.

| Column | Type | Description |
|--------|------|-------------|
| `original_topic` | VARCHAR | Source Kafka topic that failed |
| `event_id` | VARCHAR | Failed event identifier |
| `payload` | JSONB | Original event payload |
| `error_message` | TEXT | Last error encountered |
| `retry_count` | INTEGER | Number of attempts before DLQ |

### `settlements`

On-chain settlement lifecycle tracking.

| Column | Type | Description |
|--------|------|-------------|
| `settlement_ref` | VARCHAR(64) UNIQUE | Settlement reference (e.g., `STL-A1B2C3D4E5F6`) |
| `related_entity_type` | VARCHAR | Entity type (`collateral`, `liquidation`) |
| `related_entity_id` | UUID | Associated entity ID |
| `operation` | VARCHAR | Operation type (`withdrawal`, `liquidation`) |
| `asset_type` | VARCHAR | Crypto asset (BTC, ETH, SOL, AVAX) |
| `quantity` | NUMERIC(28,8) | Amount of asset |
| `tx_hash` | VARCHAR | Simulated transaction hash |
| `block_number` | BIGINT | Simulated block number |
| `confirmations` | INTEGER | Block confirmations received |
| `status` | ENUM | `pending` → `approved` → `signed` → `broadcasted` → `confirmed` / `failed` |

---

## :arrows_counterclockwise: State Machines

### Loan Status

```
                        ┌──────────────────┐
  originate_loan()  ──► │     PENDING      │
                        └────────┬─────────┘
                                 │  LTV validated, collateral deposited
                                 ▼
                        ┌──────────────────┐
                        │     ACTIVE       │◄────────────────────┐
                        └────────┬─────────┘                    │
                                 │                               │ margin call met
                        ┌────────┼────────┐                     │ (additional collateral)
                        │        │        │                     │
                  repay │  LTV>75%│  LTV>85%│                   │
                        ▼        ▼        ▼                     │
               ┌──────────┐ ┌──────────┐ ┌───────────────┐     │
               │  REPAID  │ │ MARGIN   │ │  LIQUIDATING  │     │
               │          │ │  _CALL   ├─┘               │     │
               └──────────┘ └────┬─────┘  └──────┬────────┘     │
                                 │               │              │
                          met ───┘        waterfall applied     │
                                 │               │              │
                                 └───────────────┤              │
                                                 ▼              │
                                        ┌──────────────────┐   │
                                        │   DEFAULTED      │   │
                                        └──────────────────┘   │
                                                               │
                                 margin call met ──────────────┘
```

### Collateral Position Status

```
  deposit() ──► PENDING_DEPOSIT ──► ACTIVE ──► RELEASED  (withdrawal / loan closure)
                                       │
                                       ├──► MARGIN_HOLD  (margin call active)
                                       │
                                       └──► LIQUIDATING ──► SEIZED  (liquidation)
```

### Margin Call Status

```
  evaluate() ──► TRIGGERED ──► MET            (borrower deposits additional collateral)
                    │
                    ├──► EXPIRED               (deadline passed without response)
                    │       │
                    │       └──► LIQUIDATION_INITIATED  (escalated to liquidation)
                    │
                    └──► LIQUIDATION_INITIATED  (LTV > 85% during evaluation)
```

### Liquidation Status

```
  initiate() ──► INITIATED ──► EXECUTING ──► COMPLETED  (waterfall applied, loan closed)
                                    │
                                    └──► FAILED     (execution error)
                                    │
                                    └──► PARTIAL    (insufficient proceeds)
```

### Settlement Status (On-Chain Transaction Lifecycle)

```
  create_settlement() ──► PENDING
                             │
                             ├──► APPROVED    (admin authorization)
                             │       │
                             │       └──► SIGNED        (MPC 2-of-3 threshold signature)
                             │               │
                             │               └──► BROADCASTED  (submitted to blockchain)
                             │                       │
                             │                       └──► CONFIRMED  (block confirmations met)
                             │
                             └──► FAILED      (any stage — recoverable back to PENDING)
```

Transitions are validated against a deterministic `VALID_TRANSITIONS` map. Invalid transitions raise `ValueError`. Each transition records an immutable `settlement_status_history` row with `actor_id`, `trace_id`, and timestamps.

---

## :office: Real-World Example: Atlas Capital Borrows $5M Against 150 BTC

The demo script (`scripts/demo.py`) models a full institutional lending scenario:

### Participants

| Entity | Role | Type |
|--------|------|------|
| Atlas Capital Partners | Borrower | Institutional fund |
| Meridian Lending Pool | Lender | Lending pool ($50M capacity) |
| SecureVault Custody | Custodian | Digital asset custodian |

### Loan Terms

| Attribute | Value |
|-----------|-------|
| Principal | $5,000,000 USD |
| Collateral | 150 BTC at $60,000/BTC |
| Annual Interest Rate | 850 basis points (8.50%) |
| Interest Method | Daily compound |
| Haircut | 10% |
| Initial Collateral Value | $8,100,000 (after haircut) |
| Initial LTV | 61.7% (below 65% initial threshold) |

### Lifecycle Walkthrough

1. **Onboard Participants** — Register Atlas Capital, Meridian Pool, SecureVault Custody with KYC/AML clearance
2. **Fund Lending Pool** — Seed Meridian with $50M lending capacity
3. **Originate Loan** — Create $5M loan, deposit 150 BTC as collateral, validate LTV < 65%
4. **Accrue Interest** — Simulate 30 days of daily compound interest accrual
5. **Market Stress: Margin Call** — BTC price drops to $46,000 → LTV exceeds 75% → margin call triggered → Atlas deposits 40 additional BTC → margin call resolved
6. **Market Crash: Liquidation** — BTC crashes to $28,000 → LTV exceeds 85% → liquidation initiated → 50 BTC sold at market → waterfall distribution applied (interest → principal → fees → remainder)
7. **Repayment & Closure** — Atlas repays remaining balance → collateral released via MPC-signed transaction → loan closed

### Waterfall Distribution Example

```
Collateral Sold:       50 BTC × $28,000 = $1,400,000
├── Interest Owed:     $  35,822.40  (30 days accrued)
├── Principal Owed:    $1,200,000.00 (partial recovery)
├── Fees:              $   25,000.00 (origination fee)
└── Borrower Remainder:$  139,177.60 (returned to Atlas)
```

---

## :test_tube: Running in a Sandbox Environment

### Option A: Docker Compose (Recommended)

The fastest way to run the entire system. Docker Compose orchestrates all 15 services with trust domain network isolation.

**Prerequisites:** Docker and Docker Compose.

```bash
# Clone and start
git clone <repo-url>
cd Lending-And-Collateral
cp .env.example .env

# Build and start all services
make up
```

This starts:

| Service | Port | Network(s) |
|---------|------|------------|
| `api-gateway` | 8000 (exposed) | dmz, internal |
| `lending-engine` | 8001 | internal |
| `collateral-manager` | 8002 | internal, signing |
| `margin-engine` | 8003 | internal |
| `liquidation-engine` | 8004 | internal, signing |
| `price-oracle` | 8005 | internal |
| `compliance-monitor` | 8006 | internal |
| `signing-gateway` | 8007 | internal, signing |
| `mpc-node-1/2/3` | 8001 | signing |
| `outbox-publisher` | 8010 | internal |
| `postgres` | — | internal (no host port) |
| `kafka` | — | internal (no host port) |
| `prometheus` | 9090 | internal, dmz |
| `grafana` | 3000 | dmz |

**Verify health:**

```bash
make health
```

**Run the full demo:**

```bash
make demo
```

**View logs:**

```bash
make logs                                     # All services
docker compose logs -f lending-engine         # Single service
docker compose logs -f liquidation-engine     # Liquidation events
```

**Kafka debugging:**

```bash
make topics                                   # List all topics
make kafka-tail                               # Tail loan.originated (default)
make kafka-tail TOPIC=margin.call.triggered   # Tail a specific topic
```

**Inspect database:**

```bash
make db-loans            # Recent loans
make db-collateral       # Collateral positions
make db-journal          # Journal entries
make db-balances         # Per-account balances (derived from journal)
make db-ledger           # Trial balance by COA code
make shell-pg            # Interactive psql shell
```

**Run test suite:**

```bash
make test                                     # Full suite
make test-unit                                # Unit tests only (excludes e2e)
make test-e2e                                 # End-to-end scenarios only
```

**Tear down:**

```bash
make down                # Stop containers (keep data)
make down-v              # Stop containers AND delete volumes
```

### Option B: Local Development (Manual Setup)

For running the test suite directly on your machine without Docker.

#### Prerequisites

- Python 3.13+
- PostgreSQL 16+ (running locally)
- Apache Kafka (optional — stubbed in tests)

#### 1. Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn sqlalchemy psycopg2-binary confluent-kafka \
            pydantic httpx aiohttp prometheus-client
```

#### 2. Run Tests (In-Memory SQLite)

The test suite uses in-memory SQLite — no PostgreSQL or Kafka required:

```bash
PYTHONPATH=. pytest tests/ -v --tb=short
```

#### 3. Run Individual Services (Requires PostgreSQL)

```bash
# Set environment
export DATABASE_URL=postgresql+psycopg2://lending:s3cr3t@localhost:5432/lending_db
export KAFKA_BOOTSTRAP=localhost:9092

# Initialize schema
psql -U lending -d lending_db -f init/postgres/01_schema.sql

# Start a service
uvicorn services.lending-engine.main:app --host 0.0.0.0 --port 8001
```

### Makefile Reference

| Command | Description |
|---------|-------------|
| `make up` | Build and start all services |
| `make down` | Stop containers (keep volumes) |
| `make down-v` | Stop containers and delete volumes |
| `make build` | Rebuild all images without starting |
| `make restart` | Restart all services |
| `make logs` | Follow all service logs |
| `make ps` | Container status |
| `make health` | Check gateway health endpoint |
| `make demo` | Run live-stack demonstration |
| `make test` | Full pytest suite |
| `make test-unit` | Unit tests (excludes e2e) |
| `make test-e2e` | End-to-end scenario tests |
| `make integrity` | Ledger double-entry integrity check |
| `make kafka-tail` | Tail a Kafka topic (`TOPIC=loan.originated`) |
| `make topics` | List Kafka topics |
| `make shell-pg` | Interactive psql shell |
| `make shell-kafka` | Interactive Kafka bash shell |
| `make db-loans` | Show recent loans |
| `make db-collateral` | Show collateral positions |
| `make db-journal` | Show recent journal entries |
| `make db-balances` | Account balances derived from journal |
| `make db-ledger` | Trial balance by COA code |
| `make open-docs` | Open Swagger UI in browser |

---

## :file_folder: Project Structure

```
Lending-And-Collateral/
│
├── docker-compose.yml                # 15-service orchestration with 3 trust domain networks
├── Makefile                          # Development & operations commands
├── alembic.ini                       # Database migration configuration
├── pytest.ini                        # Test framework configuration
├── .env.example                      # Environment variable template
│
├── services/                         # Microservices (one directory per service)
│   ├── api-gateway/                  # Internet-facing reverse proxy
│   │   ├── Dockerfile
│   │   ├── main.py                   # RBAC, rate limiting, trace context propagation
│   │   └── requirements.txt
│   │
│   ├── lending-engine/               # Loan origination, repayment, interest
│   │   ├── Dockerfile
│   │   ├── main.py                   # ~850 lines — full loan lifecycle
│   │   └── requirements.txt
│   │
│   ├── collateral-manager/           # Asset custody, valuation, substitution
│   │   ├── Dockerfile
│   │   ├── main.py                   # ~750 lines — deposit/withdraw/substitute
│   │   └── requirements.txt
│   │
│   ├── margin-engine/                # LTV monitoring, margin calls
│   │   ├── Dockerfile
│   │   ├── main.py                   # ~650 lines — evaluate/trigger/resolve
│   │   └── requirements.txt
│   │
│   ├── liquidation-engine/           # Collateral sale, waterfall distribution
│   │   ├── Dockerfile
│   │   ├── main.py                   # ~700 lines — initiate/execute/waterfall
│   │   └── requirements.txt
│   │
│   ├── price-oracle/                 # Multi-source price aggregation
│   │   ├── Dockerfile
│   │   ├── main.py                   # VWAP computation, feed ingestion
│   │   └── requirements.txt
│   │
│   ├── compliance-monitor/           # AML/sanctions screening
│   │   ├── Dockerfile
│   │   ├── main.py                   # Rule-based event consumption
│   │   └── requirements.txt
│   │
│   ├── signing-gateway/              # MPC signature aggregation
│   │   ├── Dockerfile
│   │   ├── main.py                   # Fan-out + combine (2-of-3)
│   │   └── requirements.txt
│   │
│   ├── mpc-node/                     # Threshold signing node (×3 instances)
│   │   ├── Dockerfile
│   │   ├── main.py                   # Partial signature generation
│   │   └── requirements.txt
│   │
│   └── outbox-publisher/             # Transactional outbox → Kafka relay
│       ├── Dockerfile
│       ├── main.py                   # Advisory locking, at-least-once delivery
│       └── requirements.txt
│
├── shared/                           # Common packages used by all services
│   ├── models.py                     # SQLAlchemy ORM — complete domain model
│   ├── database.py                   # Session factory, connection pooling
│   ├── journal.py                    # Double-entry ledger operations
│   ├── events.py                     # Pydantic event schemas for Kafka
│   ├── kafka_client.py               # Confluent Kafka producer/consumer with DLQ
│   ├── outbox.py                     # Transactional outbox helpers
│   ├── metrics.py                    # Prometheus instrumentation
│   ├── status.py                     # Append-only status history with trace context
│   ├── idempotency.py                # Idempotency-Key dedup for POST endpoints
│   ├── rbac.py                       # Role-based access control permission matrix
│   ├── request_context.py            # Per-request trace_id/actor_id via contextvars
│   └── settlement.py                 # Settlement state machine (PENDING → CONFIRMED)
│
├── init/                             # Bootstrap scripts (auto-run on first start)
│   ├── postgres/
│   │   └── 01_schema.sql             # Full PostgreSQL schema (~710 lines)
│   └── kafka/
│       └── create_topics.sh          # 52 topics (26 primary + 26 DLQ) with partition & retention config
│
├── scripts/                          # Operational utilities
│   ├── demo.py                       # Live-stack demonstration (~700 lines)
│   ├── ledger_integrity.py           # Double-entry balance verification
│   └── migrate.py                    # Database migration runner
│
├── tests/                            # Full test suite (in-memory SQLite)
│   ├── conftest.py                   # Fixtures, monkeypatched Kafka, seeded COA
│   ├── test_lending_engine.py        # Loan origination, repayment, interest
│   ├── test_collateral_manager.py    # Deposit, withdraw, substitution, valuation
│   ├── test_margin_engine.py         # LTV evaluation, margin call triggering
│   ├── test_liquidation_engine.py    # Liquidation execution, waterfall
│   ├── test_price_oracle.py          # Price feed ingestion, aggregation
│   ├── test_journal.py              # Double-entry balance validation
│   ├── test_outbox.py                # Transactional outbox reliability
│   ├── test_e2e_scenarios.py         # Full loan lifecycle scenarios
│   ├── test_rbac.py                  # Role permission matrix validation
│   ├── test_idempotency.py           # Request dedup and cached response replay
│   ├── test_dlq.py                   # Dead Letter Queue routing and dedup
│   ├── test_settlement.py            # Settlement state machine transitions
│   └── test_audit_context.py         # Audit context propagation via contextvars
│
├── migrations/                       # Alembic database migrations
│   └── versions/
│
└── monitoring/                       # Observability stack configuration
    ├── prometheus.yml                # Metrics scrape targets
    ├── grafana-datasource.yml        # Prometheus data source
    └── grafana-dashboard-provider.yml
```

---

## :rotating_light: Production Warning

**This project is explicitly NOT suitable for production use.** Institutional lending and collateral management is among the most regulated, operationally complex, and financially sensitive activities in digital asset financial services. The following critical components are absent or stubbed:

| Missing Component | Risk if Absent |
|-------------------|----------------|
| Real MPC cryptography (GG20 / FROST) | Private keys exposed — single compromise drains all collateral |
| Licensed custodian integration (Fireblocks, Copper.co) | Cannot verify actual asset custody positions |
| Real price oracle integration (Chainlink, Pyth) | Price manipulation enables collateral theft via bad valuations |
| HSM key management (Thales, AWS CloudHSM) | Signing keys stored in software, not tamper-proof hardware |
| Production-grade auth (OAuth2 / mTLS / JWT) | RBAC with API keys is implemented but not sufficient for production — requires OAuth2, mTLS, or JWT with proper token lifecycle |
| Real AML/KYC provider integration (Chainalysis, Elliptic) | No actual sanctions or transaction screening |
| Banking license / money transmitter license | Lending without appropriate licenses is illegal |
| On-chain settlement integration | Cannot actually move crypto assets on any blockchain |
| Smart contract audit | Exploitable vulnerabilities in escrow/custody contracts |
| Rate limiting per account & position limits | No controls on loan size or collateral concentration |
| TLS/mTLS encryption between services | Internal traffic unencrypted |
| Comprehensive test suite with mutation testing | Untested edge cases in fund handling |
| Disaster recovery & business continuity | No tested failover for service outages |
| Regulatory reporting (FinCEN, SEC, CFTC) | Post-trade reporting violations |
| Insurance coverage (crime, E&O, cyber) | No protection against operational losses |

> Institutional crypto lending at scale requires: appropriate lending licenses (state money transmitter, national bank charter, or equivalent), qualified custodian relationships, real-time price oracle infrastructure, HSM-based key management, regulatory reporting pipelines, and legal agreements with all counterparties. **Do not use this code to originate, manage, or service any real loans or collateral.**

---

## :page_facing_up: License

This project is provided as-is for educational and reference purposes under the MIT License.

---

*Built with :heart: by Pavon Dunbar — Modeled on Galaxy Digital, Anchorage Digital, Copper.co, and Fireblocks institutional lending infrastructure*
