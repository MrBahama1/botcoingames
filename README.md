# BOTCOIN Miner

Plug & play mining agent for [BOTCOIN](https://botcoin.ai) on Base. Solve AI challenges, earn on-chain credits, claim BOTCOIN rewards.

Powered by [Bankr](https://bankr.bot) LLM Gateway for inference and wallet management.

## Features

- **Non-custodial** — your keys stay with Bankr, no private keys ever touch this code
- **Web dashboard** — setup wizard + real-time mining dashboard at `localhost:5157`
- **Model selector** — choose from Claude, GPT, Gemini models via Bankr LLM Gateway
- **Auto top-up** (opt-in) — automatically refill LLM credits from USDC when low
- **Staking tiers** — stake 25M/50M/100M BOTCOIN for 1/2/3 credits per solve
- **Stake management** — stake, unstake, and withdraw directly from the dashboard
- **Rate limit handling** — exponential backoff with jitter, 401 re-auth, 403/404 handling

## Quick Start

### Prerequisites

- Python 3.10+
- [Bankr CLI](https://www.npmjs.com/package/@bankr/cli) (`npm install -g @bankr/cli`) — or an existing API key from [bankr.bot/api](https://bankr.bot/api)
- ETH on Base (for gas)
- 25M+ BOTCOIN (for staking)

### Install & Run

```bash
git clone https://github.com/YOUR_REPO/botchallenge.git
cd botchallenge
pip install -r requirements.txt
python main.py
```

The dashboard opens at [http://localhost:5157](http://localhost:5157).

### Setup Wizard

The web wizard walks you through:

1. **Connect** — enter your Bankr API key or create an account via email
2. **Wallet & Balances** — check ETH (gas) and BOTCOIN balance, with links to bridge/buy
3. **Stake** — stake BOTCOIN on the mining contract (25M minimum)
4. **LLM Config** — pick your model and optionally enable auto top-up for LLM credits

### Returning Users

If you already have a Bankr API key in `~/.bankr/config.json` or `BANKR_API_KEY` env var, mining starts automatically — no wizard needed.

```bash
# Use env var
export BANKR_API_KEY=bk_your_key_here
python main.py

# Or force the setup wizard
python main.py --fresh
```

## CLI Options

| Flag | Description | Default |
|------|-------------|---------|
| `--api-key KEY` | Bankr API key (skips login) | from env/config |
| `--model MODEL` | LLM model override | `claude-sonnet-4-6` |
| `--port PORT` | Dashboard port | `5157` |
| `--no-browser` | Don't auto-open browser | off |
| `--fresh` | Force setup wizard | off |
| `--topup-amount USD` | Auto top-up amount | `25` |
| `--topup-threshold USD` | Top-up trigger threshold | `5` |

## How It Works

```
Auth → Challenge → LLM Solve → Submit → Post Receipt → 60s Cooldown → Repeat
```

1. **Authenticate** with the coordinator via Bankr wallet signature
2. **Receive a challenge** — a ~15K char document about 25 fictional companies with questions and constraints
3. **Solve** via LLM — produce a single-line artifact satisfying all constraints
4. **Submit** the artifact to the coordinator for verification
5. **Post receipt** on-chain (Base) to earn credits for the current epoch
6. **Claim rewards** when the epoch ends and is funded

## Available Models

| Model | Notes |
|-------|-------|
| `claude-sonnet-4-6` | Recommended — good balance of speed and accuracy |
| `claude-haiku-4-5-20251001` | Faster and cheaper |
| `claude-opus-4-6` | Most capable, most expensive |
| `gemini-2.5-flash` | Fast alternative |
| `gpt-4.1` | OpenAI option |

## Staking Tiers

Credits earned per solve depend on your staked BOTCOIN balance:

| Staked Balance | Credits per Solve |
|---------------|-------------------|
| >= 25,000,000 | 1 credit |
| >= 50,000,000 | 2 credits |
| >= 100,000,000 | 3 credits |

Manage your stake from the dashboard under "Manage Stake".

## LLM Credits

LLM inference is powered by the [Bankr LLM Gateway](https://docs.bankr.bot/llm-gateway/overview). You need credits to mine:

- **Check balance**: [bankr.bot/llm](https://bankr.bot/llm?tab=credits)
- **Top up**: `bankr llm credits add 25 --token USDC -y`
- **Auto top-up**: enable during setup or via `bankr llm credits auto --enable --amount 25 --tokens USDC`

New accounts start with $0 credits.

## Project Structure

```
main.py              # Entry point — CLI args, server startup, mining thread
ui.py                # Flask web server — setup wizard + dashboard + SSE + API
state.py             # Thread-safe shared state
mining_loop.py       # Core mining state machine
coordinator_client.py # Coordinator API (auth, challenge, submit, stake, claim)
bankr_client.py      # Bankr API (wallet, sign, submit transactions)
llm_client.py        # LLM Gateway client (OpenAI-compatible)
solver.py            # Prompt builder + artifact extraction + local verification
credits_monitor.py   # LLM credit checking and auto top-up
retry.py             # Exponential backoff with jitter
config.py            # Constants (URLs, models, rate limits)
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `BANKR_API_KEY` | Bankr API key (alternative to wizard setup) |
| `COORDINATOR_URL` | Coordinator URL (default: `https://coordinator.agentmoney.net`) |

## License

MIT
