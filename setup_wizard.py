"""Interactive first-run setup wizard."""

import time
from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.table import Table
from config import (
    AVAILABLE_MODELS, DEFAULT_MODEL, MIN_STAKE_TOKENS, MIN_STAKE_WEI,
    BOTCOIN_TOKEN,
)


console = Console()


def run_wizard(api_key: str, bankr, coordinator, credits_monitor) -> dict:
    """Run the setup wizard. Returns config dict with model, miner address, etc."""

    console.print("[bold yellow]Running setup...[/]\n")

    # 1. Resolve wallet
    console.print("[cyan]1/7[/] Resolving wallet address...")
    me = bankr.get_me()
    wallets = me.get("wallets", [])
    miner = None
    for w in wallets:
        if w.get("chain", "").lower() in ("base", "evm", "ethereum"):
            miner = w.get("address")
            break
    if not miner and wallets:
        miner = wallets[0].get("address", "")
    if not miner:
        console.print("[red]Could not resolve wallet address from Bankr.[/]")
        raise SystemExit(1)

    console.print(f"  Wallet: [bold]{miner}[/]")
    coordinator.miner = miner

    # 2. Check balances
    console.print("\n[cyan]2/7[/] Checking balances on Base...")
    balances = bankr.get_balances("base")
    eth_bal = 0.0
    botcoin_bal = 0.0

    tokens = balances if isinstance(balances, list) else balances.get("tokens", balances.get("balances", []))
    if isinstance(tokens, list):
        for t in tokens:
            sym = t.get("symbol", "").upper()
            if sym == "ETH":
                eth_bal = float(t.get("balance", 0))
            if sym == "BOTCOIN" or (t.get("address", "").lower() == BOTCOIN_TOKEN.lower()):
                botcoin_bal = float(t.get("balance", 0))
    else:
        # Dict format
        for key, val in (tokens if isinstance(tokens, dict) else {}).items():
            if "eth" in key.lower():
                eth_bal = float(val) if isinstance(val, (int, float, str)) else 0
            if "botcoin" in key.lower():
                botcoin_bal = float(val) if isinstance(val, (int, float, str)) else 0

    console.print(f"  ETH: [bold]{eth_bal:.6f}[/]")
    console.print(f"  BOTCOIN: [bold]{botcoin_bal:,.0f}[/]")

    # 3. Fund ETH if needed
    if eth_bal < 0.001:
        console.print("\n[cyan]3/7[/] [yellow]ETH balance too low for gas.[/]")
        if Confirm.ask("  Bridge ~$2 ETH to Base?", default=True):
            console.print("  Bridging ETH...")
            resp = bankr.prompt_and_poll("bridge $2 of ETH to base")
            console.print(f"  [dim]{resp[:200]}[/]")
            time.sleep(3)
    else:
        console.print("\n[cyan]3/7[/] ETH balance OK")

    # 4. Buy BOTCOIN if needed
    if botcoin_bal < MIN_STAKE_TOKENS:
        console.print(f"\n[cyan]4/7[/] [yellow]Need {MIN_STAKE_TOKENS:,} BOTCOIN to mine (have {botcoin_bal:,.0f})[/]")
        if Confirm.ask("  Buy BOTCOIN with ETH?", default=True):
            console.print("  Swapping ETH for BOTCOIN...")
            resp = bankr.prompt_and_poll(
                f"swap some ETH to {BOTCOIN_TOKEN} on base to get at least {MIN_STAKE_TOKENS} tokens"
            )
            console.print(f"  [dim]{resp[:200]}[/]")
            time.sleep(3)
    else:
        console.print("\n[cyan]4/7[/] BOTCOIN balance OK")

    # 5. Stake
    console.print("\n[cyan]5/7[/] Setting up stake...")
    try:
        # Try to stake — if already staked, the tx may revert but that's OK
        console.print("  Getting approve calldata...")
        approve = coordinator.get_stake_approve_calldata(MIN_STAKE_WEI)
        if "transaction" in approve:
            console.print("  Submitting approve tx...")
            bankr.submit_transaction(approve["transaction"], "Approve BOTCOIN for staking")
            time.sleep(2)

        console.print("  Getting stake calldata...")
        stake = coordinator.get_stake_calldata(MIN_STAKE_WEI)
        if "transaction" in stake:
            console.print("  Submitting stake tx...")
            bankr.submit_transaction(stake["transaction"], "Stake BOTCOIN for mining")
            console.print("  [green]Stake submitted![/]")
    except Exception as e:
        err = str(e)
        if "already" in err.lower() or "nothing" in err.lower():
            console.print("  [green]Already staked[/]")
        else:
            console.print(f"  [yellow]Stake attempt: {err[:100]}[/]")
            console.print("  [dim]Continuing — may already be staked[/]")

    # 6. Check LLM credits
    console.print("\n[cyan]6/7[/] Checking LLM gateway credits...")
    credit_bal = credits_monitor.force_check()
    if credit_bal >= 0:
        console.print(f"  LLM Credits: [bold]${credit_bal:.2f}[/]")
        if credit_bal < 1:
            console.print("  [yellow]Credits very low — will auto top-up when needed[/]")
    else:
        console.print("  [dim]Could not check LLM credits — will attempt on first solve[/]")

    # 7. Model selection
    console.print("\n[cyan]7/7[/] Select LLM model:\n")
    table = Table(show_header=False, box=None)
    table.add_column("#", style="bold cyan", width=4)
    table.add_column("Model")
    for i, (model_id, label) in enumerate(AVAILABLE_MODELS):
        marker = " (default)" if model_id == DEFAULT_MODEL else ""
        table.add_row(str(i + 1), f"{label}{marker}")
    console.print(table)
    console.print()

    choice = Prompt.ask(
        "  Enter number",
        default="1",
        choices=[str(i + 1) for i in range(len(AVAILABLE_MODELS))],
    )
    model = AVAILABLE_MODELS[int(choice) - 1][0]
    model_label = AVAILABLE_MODELS[int(choice) - 1][1]
    console.print(f"  Selected: [bold]{model_label}[/]\n")

    # Auth handshake
    console.print("[cyan]Authenticating with coordinator...[/]")
    coordinator.authenticate(bankr)
    console.print("[green]Auth complete![/]\n")

    # Epoch info
    try:
        epoch = coordinator.get_epoch()
        console.print(f"Current epoch: [bold]{epoch.get('epochId')}[/]")
    except Exception:
        pass

    console.print("\n[bold green]Setup complete! Starting mining loop...[/]\n")
    time.sleep(2)

    return {
        "miner": miner,
        "model": model,
    }
