#!/usr/bin/env python3
"""BOTCOIN Miner — Plug & Play Mining Agent powered by Bankr LLM Gateway."""

import os
import sys
import json
import signal
import argparse
import threading
import time

from config import DEFAULT_MODEL, LLM_CREDIT_THRESHOLD, LLM_TOPUP_AMOUNT
from ui import MinerUI


def main():
    parser = argparse.ArgumentParser(description="BOTCOIN Miner — Plug & Play")
    parser.add_argument("--model", help="LLM model ID override")
    parser.add_argument("--topup-amount", type=float, default=LLM_TOPUP_AMOUNT)
    parser.add_argument("--topup-threshold", type=float, default=LLM_CREDIT_THRESHOLD)
    parser.add_argument("--port", type=int, default=5157,
                        help="Web dashboard port (default: 5157)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open browser")
    args = parser.parse_args()

    ui = MinerUI()

    # Wire up the setup finish callback — starts mining when wizard completes
    def on_setup_finish(session_id, api_key, model, state, auto_topup):
        from mining_manager import MiningManager
        ui._mining.start_mining(
            session_id=session_id,
            api_key=api_key,
            model=model,
            state=state,
            auto_topup=auto_topup,
            topup_amount=args.topup_amount,
            topup_threshold=args.topup_threshold,
            ui_log=lambda msg: state.log(msg),
            ui_set_phase=lambda phase: _set_phase(state, phase),
            ui_update=lambda: state.bump(),
        )
    ui._on_setup_finish = on_setup_finish

    # Start web server
    ui.print_banner()
    print(f"  Dashboard: http://localhost:{args.port}")
    print(f"  {'Opening browser...' if not args.no_browser else 'Open the URL above in your browser.'}\n")
    ui.start(port=args.port, open_browser=not args.no_browser)

    # Keep main thread alive
    try:
        signal.signal(signal.SIGINT, lambda *_: _shutdown(ui))
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        _shutdown(ui)


def _set_phase(state, phase):
    state.phase = phase
    state.bump()


def _shutdown(ui):
    s = ui.state
    print()
    print(f"  Mining stopped.")
    print(f"  Solves: {s.total_solves}  |  Fails: {s.total_fails}  |  Credits: {s.total_credits}")
    print(f"  Uptime: {s.uptime}")
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
