"""Constants and configuration."""

COORDINATOR_URL = "https://coordinator.agentmoney.net"
BANKR_API_URL = "https://api.bankr.bot"
LLM_GATEWAY_URL = "https://llm.bankr.bot/v1"

BOTCOIN_TOKEN = "0xA601877977340862Ca67f816eb079958E5bd0BA3"
MINING_CONTRACT = "0xcF5F2D541EEb0fb4cA35F1973DE5f2B02dfC3716"

MIN_STAKE_TOKENS = 25_000_000
MIN_STAKE_WEI = "25000000000000000000000000"
TIER_1_TOKENS = 25_000_000
TIER_2_TOKENS = 50_000_000
TIER_3_TOKENS = 100_000_000
STAKE_AMOUNTS = {
    "25M": "25000000000000000000000000",
    "50M": "50000000000000000000000000",
    "100M": "100000000000000000000000000",
}

RATE_LIMIT_SECONDS = 60
BACKOFF_SCHEDULE = [2, 4, 8, 16, 30, 60]
MAX_CONSECUTIVE_FAILS = 5

AVAILABLE_MODELS = [
    # Free — uses your Claude subscription via Claude Code CLI (local only)
    ("claude-code-sonnet", "Claude Sonnet 4.6 — Free (Claude Code)"),
    ("claude-code-opus", "Claude Opus 4.6 — Free (Claude Code)"),
    # Paid — uses Bankr LLM Gateway credits
    ("claude-sonnet-4-6", "Claude Sonnet 4.6 (credits)"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5 (credits, cheaper)"),
    ("claude-opus-4-6", "Claude Opus 4.6 (credits, expensive)"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash (credits)"),
    ("gpt-4.1", "GPT-4.1 (credits)"),
]
DEFAULT_MODEL = "claude-code-sonnet"

# Map Claude Code model IDs to CLI model aliases
CLAUDE_CODE_MODEL_MAP = {
    "claude-code-sonnet": "sonnet",
    "claude-code-opus": "opus",
}

LLM_CREDIT_CHECK_INTERVAL = 600  # 10 minutes
LLM_CREDIT_THRESHOLD = 5.0  # dollars
LLM_TOPUP_AMOUNT = 25  # dollars
