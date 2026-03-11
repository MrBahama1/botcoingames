"""Per-user mining loop lifecycle manager — multi-tenant."""

import threading
import time
from bankr_client import BankrClient
from coordinator_client import CoordinatorClient
from llm_client import LLMClient, PROVIDER_CLAUDE_CODE
from credits_monitor import CreditsMonitor
from mining_loop import MiningLoop
from state import MinerState


class MiningManager:
    """Manages per-user mining threads and state."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get_state(self, session_id: str) -> MinerState | None:
        with self._lock:
            entry = self._sessions.get(session_id)
            return entry["state"] if entry else None

    def is_running(self, session_id: str) -> bool:
        with self._lock:
            entry = self._sessions.get(session_id)
            if not entry:
                return False
            return entry["thread"].is_alive()

    def start_mining(self, session_id: str, api_key: str, model: str,
                     state: MinerState, auto_topup: bool = False,
                     topup_amount: float = 25, topup_threshold: float = 5.0,
                     ui_log=None, ui_set_phase=None, ui_update=None):
        """Start a mining loop for a user session."""
        with self._lock:
            if session_id in self._sessions:
                entry = self._sessions[session_id]
                if entry["thread"].is_alive():
                    return  # Already running

        def run():
            try:
                _run_mining(
                    api_key, model, state, auto_topup,
                    topup_amount, topup_threshold,
                    ui_log, ui_set_phase, ui_update, session_id, self
                )
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(f"[miner] Fatal error: {tb}")
                if ui_log:
                    ui_log(f"Fatal error: {e}")
                    ui_log(f"Traceback: {tb[-500:]}")
                if ui_set_phase:
                    ui_set_phase("FAILED")
                state.mining_active = False
                state.bump()

        thread = threading.Thread(target=run, daemon=True, name=f"miner-{session_id[:8]}")
        with self._lock:
            self._sessions[session_id] = {
                "state": state,
                "thread": thread,
            }
        thread.start()

    def stop_mining(self, session_id: str):
        """Stop mining for a session."""
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry:
                entry["state"].mining_active = False

    def remove_session(self, session_id: str):
        """Stop and remove a session entirely."""
        self.stop_mining(session_id)
        with self._lock:
            self._sessions.pop(session_id, None)

    def set_model_callback(self, session_id: str, callback):
        """Store model change callback for a session."""
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry:
                entry["model_callback"] = callback

    def get_model_callback(self, session_id: str):
        with self._lock:
            entry = self._sessions.get(session_id)
            return entry.get("model_callback") if entry else None


def _run_mining(api_key, model, state, auto_topup, topup_amount, topup_threshold,
                ui_log, ui_set_phase, ui_update, session_id, manager):
    """Initialize clients and run mining loop (called in background thread)."""

    class UIAdapter:
        """Adapts individual callbacks to the UI interface expected by MiningLoop."""
        def __init__(self, st, log_fn, phase_fn, update_fn):
            self.state = st
            self._log = log_fn
            self._phase = phase_fn
            self._update = update_fn

        def log(self, msg):
            if self._log:
                self._log(msg)

        def set_phase(self, phase):
            if self._phase:
                self._phase(phase)

        def update(self):
            if self._update:
                self._update()

    ui = UIAdapter(state, ui_log, ui_set_phase, ui_update)

    ui.log("Initializing...")
    ui.set_phase("SETUP")

    bankr = BankrClient(api_key)
    coordinator = CoordinatorClient(miner="")
    is_claude_code = model.startswith("claude-code-")
    credits_monitor = CreditsMonitor(
        bankr, threshold=topup_threshold, topup_amount=topup_amount, ui=ui
    )
    if is_claude_code:
        llm = LLMClient(api_key=api_key, model=model, provider=PROVIDER_CLAUDE_CODE)
    else:
        llm = LLMClient(api_key=api_key, model=model)

    # Resolve wallet
    ui.log("Resolving wallet...")
    me = bankr.get_me()
    wallets = me.get("wallets", [])
    miner = ""
    for w in wallets:
        if w.get("chain", "").lower() in ("base", "evm", "ethereum"):
            miner = w.get("address")
            break
    if not miner and wallets:
        miner = wallets[0].get("address", "")
    if not miner:
        ui.log("ERROR: Could not resolve wallet")
        ui.set_phase("FAILED")
        return

    coordinator.miner = miner
    state.miner_address = miner
    state.model = model
    state.setup_complete = True
    state.bump()
    ui.log(f"Wallet: {miner}")

    # Check on-chain stake
    staked = coordinator.get_staked_amount(miner)
    if staked > 0:
        state.staked_amount = staked
        tier = "3cr" if staked >= 100_000_000 else "2cr" if staked >= 50_000_000 else "1cr"
        ui.log(f"Staked: {staked:,.0f} BOTCOIN ({tier}/solve)")
    elif coordinator.is_eligible(miner):
        ui.log("Eligible to mine (stake detected)")
    else:
        ui.log("WARNING: Not staked. Stake BOTCOIN from the dashboard to mine.")
    state.bump()

    if is_claude_code:
        ui.log(f"Using Claude Code CLI ({model}) — no LLM credits needed.")
        state.llm_credits = -1  # not applicable
        state.bump()
    else:
        # Configure auto top-up only if user opted in
        if auto_topup:
            credits_monitor.ensure_auto_topup()
        else:
            ui.log("Auto top-up disabled. Top up manually at bankr.bot/llm if credits run low.")

        # LLM credits check
        ui.log("Checking LLM credits...")
        credit_bal = credits_monitor.force_check()
        if credit_bal >= 0:
            state.llm_credits = credit_bal
            state.bump()
            ui.log(f"LLM Credits: ${credit_bal:.2f}")
        if credit_bal == 0:
            ui.log("WARNING: $0 LLM credits. Top up at bankr.bot/llm — mining will fail without credits.")

    # Auth with coordinator
    ui.log("Authenticating with coordinator...")
    try:
        coordinator.authenticate(bankr)
        ui.log("Auth complete!")
    except Exception as e:
        import traceback
        ui.log(f"Auth with coordinator failed: {e}")
        print(f"[miner] Auth traceback: {traceback.format_exc()}")
        raise

    # Epoch info
    try:
        epoch = coordinator.get_epoch()
        state.epoch_id = epoch.get("epochId", 0)
        state.bump()
        ui.log(f"Current epoch: {epoch.get('epochId')}")
    except Exception:
        pass

    # Start claim checker with its own client instances to avoid token conflicts
    from claims import ClaimChecker
    claim_bankr = BankrClient(api_key)
    claim_coord = CoordinatorClient(miner)
    claim_coord.authenticate(claim_bankr)
    claim_checker = ClaimChecker(claim_coord, claim_bankr, state, ui)
    claim_checker.start()
    ui.log(f"Auto-claim: {'ON' if state.auto_claim else 'OFF'}")

    ui.log("Starting mining loop...")

    loop = MiningLoop(coordinator, bankr, llm, credits_monitor, ui)
    loop.run()
