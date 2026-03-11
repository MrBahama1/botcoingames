"""Shared mutable state for the mining agent — thread-safe."""

import re
from dataclasses import dataclass, field
from collections import deque
import threading
import time
import uuid

# Redact API keys from any string
_KEY_PATTERN = re.compile(r'bk_[A-Za-z0-9]+')


@dataclass
class MinerState:
    phase: str = "INIT"
    miner_address: str = ""
    model: str = ""
    epoch_id: int = 0
    total_solves: int = 0
    total_fails: int = 0
    total_credits: int = 0
    consecutive_fails: int = 0
    current_challenge_id: str = ""
    last_solve_time: float = 0.0
    llm_credits: float = -1.0  # -1 = unknown
    staked_amount: float = 0.0
    log_lines: deque = field(default_factory=lambda: deque(maxlen=100))
    llm_output: str = ""
    cooldown_remaining: int = 0

    # Challenge & solve details
    challenge_questions: list = field(default_factory=list)
    challenge_constraints: list = field(default_factory=list)
    challenge_doc_preview: str = ""  # first 500 chars of doc
    challenge_doc_full: str = ""  # full doc text
    solve_artifact: str = ""
    solve_passed: str = ""  # "", "pass", "fail"
    solve_failed_constraints: list = field(default_factory=list)
    solve_time: float = 0.0
    solve_verification_issues: list = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    mining_active: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _version: int = 0

    # Setup wizard state (api_key removed — lives in SessionManager)
    setup_complete: bool = False
    setup_step: str = ""
    eth_balance: float = 0.0
    botcoin_balance: float = 0.0
    auto_topup: bool = False

    # Staking state
    wallet_botcoin: float = 0.0  # unstaked wallet balance
    staking_tier: int = 0  # 0=none, 1/2/3
    unstake_requested_at: float = 0.0
    withdrawable_at: float = 0.0  # unix timestamp from contract

    # Pending transactions
    pending_transactions: list = field(default_factory=list)

    # Claims
    auto_claim: bool = True
    mined_epochs: set = field(default_factory=set)  # epoch IDs we earned credits in
    claimed_epochs: set = field(default_factory=set)  # epoch IDs already claimed
    claimable_epochs: list = field(default_factory=list)  # [{epochId, credits, claimable, bonus}]
    last_claim_check: float = 0.0
    total_claimed: int = 0

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        clean = _KEY_PATTERN.sub('bk_***', msg)
        with self._lock:
            self.log_lines.append(f"{ts} {clean}")
            self._version += 1

    def bump(self):
        with self._lock:
            self._version += 1

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def uptime(self) -> str:
        elapsed = int(time.time() - self.start_time)
        h, m = divmod(elapsed, 3600)
        m, s = divmod(m, 60)
        return f"{h}h {m}m {s}s"

    def add_pending_tx(self, description: str) -> str:
        tx_id = uuid.uuid4().hex[:12]
        with self._lock:
            self.pending_transactions.append({
                "id": tx_id,
                "description": description,
                "status": "pending",
                "timestamp": time.time(),
                "tx_hash": "",
            })
            self._version += 1
        return tx_id

    def update_pending_tx(self, tx_id: str, status: str, tx_hash: str = ""):
        with self._lock:
            for tx in self.pending_transactions:
                if tx["id"] == tx_id:
                    tx["status"] = status
                    if tx_hash:
                        tx["tx_hash"] = tx_hash
                    break
            self._version += 1

    def clear_old_txs(self):
        cutoff = time.time() - 300  # 5 min
        with self._lock:
            self.pending_transactions = [
                tx for tx in self.pending_transactions
                if tx["status"] == "pending" or tx["timestamp"] > cutoff
            ]

    def snapshot(self) -> dict:
        self.clear_old_txs()
        with self._lock:
            cooldown_left = 0
            if self.withdrawable_at > 0:
                cooldown_left = max(0, int(self.withdrawable_at - time.time()))
            return {
                "phase": self.phase,
                "miner_address": self.miner_address,
                "model": self.model,
                "epoch_id": self.epoch_id,
                "total_solves": self.total_solves,
                "total_fails": self.total_fails,
                "total_credits": self.total_credits,
                "consecutive_fails": self.consecutive_fails,
                "current_challenge_id": self.current_challenge_id,
                "llm_credits": self.llm_credits,
                "staked_amount": self.staked_amount,
                "llm_output": self.llm_output,
                "cooldown_remaining": self.cooldown_remaining,
                "challenge_questions": list(self.challenge_questions),
                "challenge_constraints": list(self.challenge_constraints),
                "challenge_doc_preview": self.challenge_doc_preview,
                "solve_artifact": self.solve_artifact,
                "solve_passed": self.solve_passed,
                "solve_failed_constraints": list(self.solve_failed_constraints),
                "solve_time": self.solve_time,
                "solve_verification_issues": list(self.solve_verification_issues),
                "uptime": self.uptime,
                "mining_active": self.mining_active,
                "log_lines": list(self.log_lines),
                "version": self._version,
                "setup_complete": self.setup_complete,
                "setup_step": self.setup_step,
                "eth_balance": self.eth_balance,
                "botcoin_balance": self.botcoin_balance,
                "wallet_botcoin": self.wallet_botcoin,
                "staking_tier": self.staking_tier,
                "unstake_requested_at": self.unstake_requested_at,
                "withdrawable_at": self.withdrawable_at,
                "unstake_cooldown_remaining": cooldown_left,
                "pending_transactions": list(self.pending_transactions),
                "auto_claim": self.auto_claim,
                "mined_epochs": sorted(self.mined_epochs),
                "claimable_epochs": list(self.claimable_epochs),
                "total_claimed": self.total_claimed,
            }
