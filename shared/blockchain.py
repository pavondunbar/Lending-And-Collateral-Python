"""
shared/blockchain.py -- Simulated blockchain for demo transaction receipts.

Generates Ethereum-style 0x-prefixed transaction hashes, incrementing
block numbers, and full transaction receipts for all financial
operations.  This is a DEMO SIMULATION -- not a real chain.
"""

import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone


# Simulated genesis -- starts at an Ethereum-mainnet-like block height
GENESIS_BLOCK = 19_000_000

_lock = threading.Lock()
_block_counter = 0


def _next_block() -> int:
    """Return the next monotonically-increasing block number."""
    global _block_counter
    with _lock:
        _block_counter += 1
        return GENESIS_BLOCK + _block_counter


def generate_tx_hash(operation: str, *parts: str) -> str:
    """Generate an Ethereum-style 0x-prefixed 64-char transaction hash."""
    seed = f"{operation}:{':'.join(parts)}:{time.time_ns()}"
    raw = hashlib.sha256(seed.encode()).hexdigest()
    return f"0x{raw}"


def generate_block_hash(block_number: int) -> str:
    """Generate a 0x-prefixed block hash for a given block number."""
    raw = hashlib.sha256(
        f"block:{block_number}:{time.time_ns()}".encode(),
    ).hexdigest()
    return f"0x{raw}"


# Gas costs by operation type (simulated, in wei-like units)
_GAS_TABLE: dict[str, int] = {
    "loan_origination": 185_000,
    "loan_repayment": 120_000,
    "loan_close": 65_000,
    "collateral_deposit": 145_000,
    "collateral_withdrawal": 165_000,
    "collateral_substitution": 210_000,
    "liquidation_sale": 250_000,
    "liquidation_waterfall": 180_000,
    "interest_accrual": 85_000,
    "signing": 95_000,
}


@dataclass(frozen=True)
class TransactionReceipt:
    """Simulated on-chain transaction receipt."""

    tx_hash: str
    block_number: int
    block_hash: str
    block_timestamp: str
    gas_used: int
    confirmations: int
    status: str

    def to_dict(self) -> dict:
        return {
            "tx_hash": self.tx_hash,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "block_timestamp": self.block_timestamp,
            "gas_used": self.gas_used,
            "confirmations": self.confirmations,
            "status": self.status,
        }


def mine_transaction(
    operation: str,
    *parts: str,
    confirmations: int = 6,
) -> TransactionReceipt:
    """Simulate mining a transaction and return a receipt.

    Args:
        operation: Type of operation (e.g. "loan_origination").
        *parts: Additional entropy seeds (refs, IDs, amounts).
        confirmations: Number of simulated block confirmations.

    Returns:
        A frozen TransactionReceipt with all on-chain fields.
    """
    block_number = _next_block()
    tx_hash = generate_tx_hash(operation, *parts)
    block_hash = generate_block_hash(block_number)
    now = datetime.now(timezone.utc)
    gas = _GAS_TABLE.get(operation, 100_000)

    return TransactionReceipt(
        tx_hash=tx_hash,
        block_number=block_number,
        block_hash=block_hash,
        block_timestamp=now.isoformat(),
        gas_used=gas,
        confirmations=confirmations,
        status="success",
    )


def initialize_from_db(db_session) -> None:
    """Seed the block counter from existing settlement count.

    Call once at service startup so block numbering resumes
    from where the last run left off.
    """
    global _block_counter
    try:
        from sqlalchemy import func, select
        from shared.models import Settlement

        count = db_session.execute(
            select(func.count(Settlement.id)),
        ).scalar() or 0
        with _lock:
            _block_counter = max(_block_counter, count)
    except Exception:
        pass
