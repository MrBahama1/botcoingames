"""Background claim checker and auto-claimer for mining rewards."""

import time
import threading
from retry import RetryExhausted, HTTPError

CLAIM_CHECK_INTERVAL = 600  # 10 minutes (credits endpoint is rate-limited 1/hour)


class ClaimChecker:
    """Periodically checks for claimable epochs and optionally auto-claims."""

    def __init__(self, coordinator, bankr, state, ui):
        self.coordinator = coordinator
        self.bankr = bankr
        self.state = state
        self.ui = ui
        self._thread = None
        self._stop = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="claim-checker")
        self._thread.start()

    def stop(self):
        self._stop = True

    def _run(self):
        # Initial delay to let mining start
        time.sleep(60)
        while not self._stop:
            try:
                self._check_claims()
            except Exception as e:
                self.ui.log(f"Claim check error: {e}")
            # Sleep in small increments so we can stop quickly
            for _ in range(CLAIM_CHECK_INTERVAL):
                if self._stop:
                    return
                time.sleep(1)

    def _check_claims(self):
        state = self.state

        # Get current epoch info
        try:
            epoch_info = self.coordinator.get_epoch()
        except Exception as e:
            self.ui.log(f"Epoch check failed: {e}")
            return

        current_epoch = int(epoch_info.get("epochId", 0))
        if current_epoch == 0:
            return

        claimable = []

        # Strategy 1: Try the credits endpoint (rate-limited 1/hour)
        credits_ok = False
        try:
            credits_info = self.coordinator.get_credits()
            state.last_claim_check = time.time()
            self.ui.log(f"Credits response: {str(credits_info)[:300]}")
            claimable = self._parse_credits(credits_info, current_epoch)
            credits_ok = True
        except (RetryExhausted, HTTPError) as e:
            err_str = str(e)
            if "429" in err_str or "Rate limit" in err_str:
                self.ui.log("Credits endpoint rate-limited. Using local epoch tracking.")
            else:
                self.ui.log(f"Credits check failed: {e}")
        except Exception as e:
            self.ui.log(f"Credits check failed: {e}")

        # Strategy 2: Use locally tracked mined_epochs
        # If we mined in an epoch that's now ended, try to claim it
        for eid in list(state.mined_epochs):
            eid_int = int(eid)
            if eid_int < current_epoch:
                if not any(e["epochId"] == eid_int for e in claimable):
                    claimable.append({
                        "epochId": eid_int,
                        "credits": "?",
                        "claimable": True,
                        "bonus": False,
                    })

        # Strategy 3: If no data from either source, try claim-calldata directly
        # for recent ended epochs — the coordinator will return data if claimable
        if not claimable and not credits_ok:
            prev_epoch = int(epoch_info.get("prevEpochId", 0))
            if prev_epoch > 0:
                try:
                    test = self.coordinator.get_claim_calldata([prev_epoch])
                    if test.get("transaction"):
                        claimable.append({
                            "epochId": prev_epoch,
                            "credits": "?",
                            "claimable": True,
                            "bonus": False,
                        })
                except Exception:
                    pass  # No claim available or error

        # Check bonus status for claimable epochs
        if claimable:
            bonus_epoch_ids = [e["epochId"] for e in claimable]
            try:
                bonus_info = self.coordinator.get_bonus_status(bonus_epoch_ids)
                if isinstance(bonus_info, dict) and bonus_info.get("isBonusEpoch"):
                    bonus_eid = bonus_info.get("epochId")
                    for e in claimable:
                        if str(e["epochId"]) == str(bonus_eid):
                            e["bonus"] = True
                            e["bonusReward"] = bonus_info.get("reward", "0")
            except Exception:
                pass

        state.claimable_epochs = claimable
        state.bump()

        if claimable:
            epoch_ids = [e["epochId"] for e in claimable]
            self.ui.log(f"Claimable: {len(claimable)} epoch(s) — {epoch_ids}")

            if state.auto_claim:
                self._do_claim(claimable)
        else:
            self.ui.log("No claimable rewards found.")

    def _parse_credits(self, credits_info, current_epoch):
        """Parse the credits response into a list of claimable epochs.

        Handles multiple possible response formats from the coordinator.
        """
        claimable = []

        if not isinstance(credits_info, dict):
            return claimable

        # Format 1: {epochs: [{epochId, credits, claimed}, ...]}
        epoch_list = credits_info.get("epochs", [])
        if isinstance(epoch_list, list) and epoch_list:
            for ec in epoch_list:
                if not isinstance(ec, dict):
                    continue
                eid = int(ec.get("epochId", 0))
                credits = ec.get("credits", 0)
                claimed = ec.get("claimed", False)
                if eid > 0 and eid < current_epoch and credits and not claimed:
                    claimable.append({
                        "epochId": eid,
                        "credits": credits,
                        "claimable": True,
                        "bonus": False,
                    })
            return claimable

        # Format 2: {credits: [{epochId, amount, ...}]}
        credit_list = credits_info.get("credits", [])
        if isinstance(credit_list, list) and credit_list:
            for ec in credit_list:
                if not isinstance(ec, dict):
                    continue
                eid = int(ec.get("epochId", ec.get("epoch", 0)))
                credits = ec.get("credits", ec.get("amount", ec.get("count", 0)))
                claimed = ec.get("claimed", False)
                if eid > 0 and eid < current_epoch and credits and not claimed:
                    claimable.append({
                        "epochId": eid,
                        "credits": credits,
                        "claimable": True,
                        "bonus": False,
                    })
            return claimable

        # Format 3: flat {epochId: credits, ...} or {"18": 5, "17": 3}
        for k, v in credits_info.items():
            try:
                eid = int(k)
                if eid > 0 and eid < current_epoch:
                    credits = v if isinstance(v, (int, float)) else v.get("credits", 0) if isinstance(v, dict) else 0
                    if credits:
                        claimed = v.get("claimed", False) if isinstance(v, dict) else False
                        if not claimed:
                            claimable.append({
                                "epochId": eid,
                                "credits": credits,
                                "claimable": True,
                                "bonus": False,
                            })
            except (ValueError, TypeError, AttributeError):
                continue

        return claimable

    def _do_claim(self, claimable):
        """Claim rewards for the given epochs."""
        # Separate bonus and regular epochs
        bonus_epochs = [e for e in claimable if e.get("bonus")]
        regular_epochs = [e for e in claimable if not e.get("bonus")]

        # Claim regular epochs
        if regular_epochs:
            epoch_ids = [e["epochId"] for e in regular_epochs]
            self._claim_epochs(epoch_ids, "regular")

        # Claim bonus epochs
        if bonus_epochs:
            epoch_ids = [e["epochId"] for e in bonus_epochs]
            self._claim_bonus_epochs(epoch_ids)

    def _claim_epochs(self, epoch_ids, label="regular"):
        state = self.state
        tx_id = state.add_pending_tx(f"Claim rewards (epochs {epoch_ids})")
        try:
            self.coordinator.ensure_auth(self.bankr)
            calldata = self.coordinator.get_claim_calldata(epoch_ids)
            tx = calldata.get("transaction")
            if not tx:
                self.ui.log(f"No claim transaction returned for epochs {epoch_ids}")
                state.update_pending_tx(tx_id, "failed")
                return

            result = self.bankr.submit_transaction(tx, "Claim mining rewards")
            tx_hash = result.get("transactionHash", "")
            if result.get("success") or result.get("status") == "success":
                state.update_pending_tx(tx_id, "confirmed", tx_hash)
                state.total_claimed += len(epoch_ids)
                self.ui.log(f"Claimed epochs {epoch_ids}! TX: {tx_hash[:16]}...")
                # Remove claimed epochs from claimable list
                claimed_set = set(epoch_ids)
                state.claimable_epochs = [e for e in state.claimable_epochs if e["epochId"] not in claimed_set]
                state.bump()
            else:
                state.update_pending_tx(tx_id, "failed")
                self.ui.log(f"Claim TX failed for epochs {epoch_ids}: {result}")
        except HTTPError as e:
            state.update_pending_tx(tx_id, "failed")
            err = str(e)
            if "EpochNotFunded" in err or "NotFunded" in err:
                self.ui.log(f"Epochs {epoch_ids} not yet funded by operator. Will retry later.")
            elif "AlreadyClaimed" in err:
                self.ui.log(f"Epochs {epoch_ids} already claimed.")
                claimed_set = set(epoch_ids)
                state.claimable_epochs = [e for e in state.claimable_epochs if e["epochId"] not in claimed_set]
                state.bump()
            elif "NoCredits" in err:
                self.ui.log(f"No credits in epochs {epoch_ids}.")
                claimed_set = set(epoch_ids)
                state.claimable_epochs = [e for e in state.claimable_epochs if e["epochId"] not in claimed_set]
                state.bump()
            else:
                self.ui.log(f"Claim failed: {e}")
        except Exception as e:
            state.update_pending_tx(tx_id, "failed")
            self.ui.log(f"Claim error: {e}")

    def _claim_bonus_epochs(self, epoch_ids):
        state = self.state
        tx_id = state.add_pending_tx(f"Claim bonus rewards (epochs {epoch_ids})")
        try:
            self.coordinator.ensure_auth(self.bankr)
            calldata = self.coordinator.get_bonus_claim_calldata(epoch_ids)
            tx = calldata.get("transaction")
            if not tx:
                self.ui.log(f"No bonus claim transaction for epochs {epoch_ids}")
                state.update_pending_tx(tx_id, "failed")
                return

            result = self.bankr.submit_transaction(tx, "Claim bonus mining rewards")
            tx_hash = result.get("transactionHash", "")
            if result.get("success") or result.get("status") == "success":
                state.update_pending_tx(tx_id, "confirmed", tx_hash)
                self.ui.log(f"Bonus claimed for epochs {epoch_ids}! TX: {tx_hash[:16]}...")
                claimed_set = set(epoch_ids)
                state.claimable_epochs = [e for e in state.claimable_epochs if e["epochId"] not in claimed_set]
                state.bump()
            else:
                state.update_pending_tx(tx_id, "failed")
                self.ui.log(f"Bonus claim TX failed: {result}")
        except Exception as e:
            state.update_pending_tx(tx_id, "failed")
            self.ui.log(f"Bonus claim error: {e}")

    def force_check(self):
        """Trigger an immediate claim check (called from UI)."""
        threading.Thread(target=self._check_claims, daemon=True).start()
