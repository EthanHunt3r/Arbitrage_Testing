#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║           CRYPTO ARBITRAGE OPPORTUNITY MONITOR                  ║
║  Monitors Uniswap V2-compatible DEXes for price discrepancies   ║
║  and flags profitable flash-loan arbitrage opportunities.        ║
╚══════════════════════════════════════════════════════════════════╝

Usage:
    pip install web3 requests python-dotenv colorama tabulate
    python arbitrage_monitor.py

.env file (required):
    RPC_URL=https://mainnet.infura.io/v3/YOUR_KEY
    TELEGRAM_BOT_TOKEN=...   (optional)
    TELEGRAM_CHAT_ID=...     (optional)
    CONTRACT_ADDRESS=0x...   (optional – your deployed ArbitrageBot)
"""

import os
import sys
import time
import json
import logging
import asyncio
from decimal import Decimal
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

try:
    from web3 import Web3
    from colorama import Fore, Style, init as colorama_init
    from tabulate import tabulate
    import requests
except ImportError:
    print("Missing dependencies. Run:")
    print("  pip install web3 requests python-dotenv colorama tabulate")
    sys.exit(1)

load_dotenv(dotenv_path="API.env")
colorama_init(autoreset=True)

# ─────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────

RPC_URL            = os.getenv("RPC_URL", "")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
CONTRACT_ADDRESS   = os.getenv("CONTRACT_ADDRESS", "")

# Scan interval in seconds
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL"))

# Minimum net profit in USD to flag an opportunity
MIN_PROFIT_USD     = float(os.getenv("MIN_PROFIT_USD"))

# Aave V3 flash-loan fee in basis points (5 = 0.05 %)

# Estimated gas cost per arb tx in USD
GAS_COST_USD       = float(os.getenv("GAS_COST_USD"))

# Balancer V2 Vault — same address on Ethereum, Polygon, Arbitrum, Optimism
BALANCER_VAULT   = os.getenv(
    "BALANCER_VAULT", "0xBA12222222228d8Ba445958a75a0704d566BF2C8"
)

# ── Mainnet DEX Routers ──────────────────────────────────────────
DEXES = {
    "Uniswap V2":  "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    "SushiSwap":   "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F",
    "PancakeSwap": "0xEfF92A263d31888d860bD50809A8D171709b7b1c",  # ETH mainnet
}

# ── Token pairs to monitor ───────────────────────────────────────
# (symbol, address, decimals)
TOKENS = {
    "WETH":  ("WETH",  "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18),
    "USDC":  ("USDC",  "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  6),
    "USDT":  ("USDT",  "0xdAC17F958D2ee523a2206206994597C13D831ec7",  6),
    "DAI":   ("DAI",   "0x6B175474E89094C44Da98b954EedeAC495271d0F", 18),
    "WBTC":  ("WBTC",  "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",  8),
}

# Pairs to check: (tokenA_key, tokenB_key, loan_amount_in_tokenA_units)
PAIRS = [
    ("WETH",  "USDC",  1.0),
    ("WETH",  "USDT",  1.0),
    ("WETH",  "DAI",   1.0),
    ("WBTC",  "USDC",  0.05),
    ("WBTC", "USDT", 0.05),
    ("USDC",  "USDT",  5000.0),
    ("USDC",  "DAI",   5000.0),
]

# ── Uniswap V2 Router ABI (minimal) ─────────────────────────────
ROUTER_ABI = json.loads("""[
  {
    "inputs": [
      {"internalType":"uint256","name":"amountIn","type":"uint256"},
      {"internalType":"address[]","name":"path","type":"address[]"}
    ],
    "name": "getAmountsOut",
    "outputs": [{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
    "stateMutability": "view",
    "type": "function"
  }
]""")

# ── ERC-20 balanceOf ABI (used to check Balancer Vault liquidity) ─
BALANCE_ABI = json.loads("""[
  {
    "inputs": [{"internalType":"address","name":"account","type":"address"}],
    "name": "balanceOf",
    "outputs": [{"internalType":"uint256","name":"","type":"uint256"}],
    "stateMutability": "view",
    "type": "function"
  }
]""")

# ── Balancer V2 Vault ABI (flash-loan method only) ───────────────
BALANCER_VAULT_ABI = json.loads("""[
  {
    "inputs": [
      {"internalType":"contract IFlashLoanRecipient","name":"recipient","type":"address"},
      {"internalType":"contract IERC20[]","name":"tokens","type":"address[]"},
      {"internalType":"uint256[]","name":"amounts","type":"uint256[]"},
      {"internalType":"bytes","name":"userData","type":"bytes"}
    ],
    "name": "flashLoan",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function"
  }
]""")

# ─────────────────────────────────────────────────────────────────
#  LOGGING SETUP
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("arb_monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("arb_monitor")

# ─────────────────────────────────────────────────────────────────
#  DATA CLASSES
# ─────────────────────────────────────────────────────────────────

@dataclass
class PriceQuote:
    dex:        str
    token_in:   str
    token_out:  str
    amount_in:  Decimal
    amount_out: Decimal
    price:      Decimal   # token_out per unit token_in

@dataclass
class Opportunity:
    token_a:        str
    token_b:        str
    buy_dex:        str      # swap A→B here (cheaper)
    sell_dex:       str      # swap B→A here (pricier)
    loan_amount:    Decimal
    gross_profit_token: Decimal
    gross_profit_usd: Decimal
    flash_fee_usd:  Decimal
    gas_cost_usd:   Decimal
    net_profit_usd: Decimal
    spread_pct:     Decimal
    timestamp:      str = field(default_factory=lambda: datetime.utcnow().isoformat())

# ─────────────────────────────────────────────────────────────────
#  WEB3 HELPERS
# ─────────────────────────────────────────────────────────────────

def connect_web3(rpc_url: str) -> Web3:
    if not rpc_url:
        raise ValueError("RPC_URL not set. Add it to your .env file.")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to RPC: {rpc_url}")
    log.info(f"Connected to chain {w3.eth.chain_id}  block #{w3.eth.block_number:,}")
    return w3

def check_balancer_liquidity(w3: Web3, token_address: str) -> int:
    """
    Return the Balancer Vault's token balance.
    Balancer flash loans are capped to whatever the Vault actually holds,
    so this prevents requesting more than is available.
    """
    token = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=BALANCE_ABI,
    )
    try:
        return token.functions.balanceOf(
            Web3.to_checksum_address(BALANCER_VAULT)
        ).call()
    except Exception:
        return 0

def get_amount_out(
    w3: Web3,
    router_address: str,
    token_in: str,
    token_out: str,
    amount_in_raw: int,
) -> int:
    """Call getAmountsOut on a Uniswap V2 router. Returns raw output amount."""
    router = w3.eth.contract(
        address=Web3.to_checksum_address(router_address),
        abi=ROUTER_ABI,
    )
    path = [
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
    ]
    try:
        amounts = router.functions.getAmountsOut(amount_in_raw, path).call()
        return amounts[-1]
    except Exception:
        return 0  # pool may not exist for this pair on this DEX

# ─────────────────────────────────────────────────────────────────
#  PRICE ORACLE (CoinGecko free tier – no key needed)
# ─────────────────────────────────────────────────────────────────

_price_cache: dict[str, tuple[float, float]] = {}  # symbol → (usd_price, timestamp)
COINGECKO_IDS = {
    "WETH":  "ethereum",
    "USDC":  "usd-coin",
    "USDT":  "tether",
    "DAI":   "dai",
    "WBTC":  "wrapped-bitcoin",
}

def get_usd_price(symbol: str) -> float:
    cached = _price_cache.get(symbol)
    if cached and (time.time() - cached[1]) < 60:
        return cached[0]
    cg_id = COINGECKO_IDS.get(symbol)
    if not cg_id:
        return 1.0  # assume stablecoin
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd"
        r = requests.get(url, timeout=5)
        price = r.json()[cg_id]["usd"]
        _price_cache[symbol] = (price, time.time())
        return price
    except Exception:
        return _price_cache.get(symbol, (1.0,))[0]

# ─────────────────────────────────────────────────────────────────
#  OPPORTUNITY SCANNER
# ─────────────────────────────────────────────────────────────────

def scan_pair(
    w3: Web3,
    token_a_key: str,
    token_b_key: str,
    loan_units: float,
) -> list[Opportunity]:
    """
    For a given pair (A, B), query every DEX combination and return
    any opportunities whose net profit (gross - gas) exceeds MIN_PROFIT_USD.

    Balancer V2 charges zero flash-loan fees so repayAmount == loanAmount.
    The only cost deducted is GAS_COST_USD.
    """
    sym_a, addr_a, dec_a = TOKENS[token_a_key]
    sym_b, addr_b, dec_b = TOKENS[token_b_key]

    amount_in_raw = int(loan_units * 10 ** dec_a)

    # ── Cap loan to Balancer Vault's actual balance ───────────────
    vault_balance = check_balancer_liquidity(w3, addr_a)
    if vault_balance == 0:
        log.debug(f"Balancer Vault holds no {sym_a} — skipping pair")
        return []
    if amount_in_raw > vault_balance:
        amount_in_raw = vault_balance
        loan_units    = amount_in_raw / 10 ** dec_a
        log.debug(f"Loan capped to Vault balance: {loan_units:.6f} {sym_a}")

    usd_price_a = get_usd_price(sym_a)

    # ── Gather DEX quotes: dex_name → raw tokenB received ────────
    quotes: dict[str, int] = {}
    for dex_name, router in DEXES.items():
        out = get_amount_out(w3, router, addr_a, addr_b, amount_in_raw)
        if out > 0:
            quotes[dex_name] = out

    if len(quotes) < 2:
        return []   # need at least two DEXes with liquidity

    opportunities = []
    dex_names = list(quotes.keys())

    for i, buy_dex in enumerate(dex_names):
        for sell_dex in dex_names[i + 1:]:
            # Test both directions: buy on buy_dex/sell on sell_dex, and vice versa
            for bd, sd in [(buy_dex, sell_dex), (sell_dex, buy_dex)]:
                b_raw      = quotes[bd]
                a_back_raw = get_amount_out(w3, DEXES[sd], addr_b, addr_a, b_raw)
                if a_back_raw == 0:
                    continue

                loan_dec         = Decimal(loan_units)
                a_back_dec       = Decimal(a_back_raw) / Decimal(10 ** dec_a)

                # Gross profit — repay == loan (Balancer fee = 0)
                gross_profit_tok = a_back_dec - loan_dec
                gross_profit_usd = float(gross_profit_tok) * usd_price_a

                # Net profit — only deduction is gas
                net_usd          = gross_profit_usd - GAS_COST_USD

                if net_usd < MIN_PROFIT_USD:
                    continue

                spread_pct = (gross_profit_tok / loan_dec) * Decimal(100)

                opp = Opportunity(
                    token_a            = sym_a,
                    token_b            = sym_b,
                    buy_dex            = bd,
                    sell_dex           = sd,
                    loan_amount        = loan_dec,
                    gross_profit_token = gross_profit_tok,
                    gross_profit_usd   = Decimal(str(round(gross_profit_usd, 4))),
                    gas_cost_usd       = Decimal(str(GAS_COST_USD)),
                    net_profit_usd     = Decimal(str(round(net_usd, 4))),
                    spread_pct         = spread_pct.quantize(Decimal("0.0001")),
                )
                opportunities.append(opp)

    return opportunities

# ─────────────────────────────────────────────────────────────────
#  NOTIFICATIONS
# ─────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return print("Telegram token and chat id are required.")
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }, timeout=5)
    except Exception as e:
        log.warning(f"Telegram notification failed: {e}")


def alert_opportunity(opp: Opportunity) -> None:
    msg = (
        f"🚨 *ARB OPPORTUNITY*\n"
        f"Pair:       `{opp.token_a}/{opp.token_b}`\n"
        f"Buy on:     `{opp.buy_dex}`\n"
        f"Sell on:    `{opp.sell_dex}`\n"
        f"Loan:       `{opp.loan_amount} {opp.token_a}`\n"
        f"Spread:     `{opp.spread_pct}%`\n"
        f"Net profit: `${opp.net_profit_usd} USD`\n"
        f"Time:       `{opp.timestamp}`"
    )
    log.info(
        f"{Fore.GREEN}★ OPPORTUNITY  "
        f"{opp.token_a}/{opp.token_b}  "
        f"buy={opp.buy_dex}  sell={opp.sell_dex}  "
        f"spread={opp.spread_pct}%  "
        f"net=${opp.net_profit_usd}{Style.RESET_ALL}"
    )
    send_telegram(msg)

# ─────────────────────────────────────────────────────────────────
#  DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────

BANNER = f"""
{Fore.CYAN}╔══════════════════════════════════════════════════════════╗
║        CRYPTO ARBITRAGE OPPORTUNITY MONITOR              ║
║  Scan interval : {SCAN_INTERVAL}s  |  Min profit : ${MIN_PROFIT_USD}              c║
╚══════════════════════════════════════════════════════════╝{Style.RESET_ALL}
"""

def print_scan_header(block: int) -> None:
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{Fore.YELLOW}── Scan @ block {block:,}  {ts} {'─'*20}{Style.RESET_ALL}")


def print_opportunities(opps: list[Opportunity]) -> None:
    if not opps:
        print(f"  {Fore.WHITE}No opportunities above threshold.{Style.RESET_ALL}")
        return

    rows = [
        [
            f"{o.token_a}/{o.token_b}",
            o.buy_dex,
            o.sell_dex,
            f"{o.loan_amount}",
            f"{o.spread_pct}%",
            f"${o.net_profit_usd}",
        ]
        for o in opps
    ]
    headers = ["Pair", "Buy DEX", "Sell DEX", "Loan", "Spread", "Net Profit"]
    print(tabulate(rows, headers=headers, tablefmt="rounded_outline"))


def print_dex_prices(w3: Web3) -> None:
    """Show a quick price comparison table across DEXes for WETH/USDC."""
    sym_a, addr_a, dec_a = TOKENS["WETH"]
    sym_b, addr_b, dec_b = TOKENS["USDC"]
    amount_in = int(1 * 10 ** dec_a)  # 1 WETH

    rows = []
    for dex, router in DEXES.items():
        out = get_amount_out(w3, router, addr_a, addr_b, amount_in)
        price = out / 10 ** dec_b if out else 0
        color = Fore.GREEN if price else Fore.RED
        rows.append([dex, f"{color}{price:,.2f} USDC{Style.RESET_ALL}"])

    print(f"\n{Fore.CYAN}WETH price across DEXes:{Style.RESET_ALL}")
    print(tabulate(rows, headers=["DEX", "1 WETH ="], tablefmt="simple"))

# ─────────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────────

def run() -> None:
    print(BANNER)

    if not RPC_URL:
        print(f"{Fore.RED}ERROR: RPC_URL is not set in your .env file.{Style.RESET_ALL}")
        print("Create a .env file with:")
        print("  RPC_URL=https://mainnet.infura.io/v3/YOUR_PROJECT_ID")
        sys.exit(1)

    w3 = connect_web3(RPC_URL)

    log.info(f"Monitoring {len(PAIRS)} pairs across {len(DEXES)} DEXes")
    log.info(f"Min profit threshold: ${MIN_PROFIT_USD}  |  Gas estimate: ${GAS_COST_USD}")
    if TELEGRAM_TOKEN:
        log.info("Telegram notifications: ENABLED")

    scan_count = 0
    total_found = 0

    while True:
        try:
            block = w3.eth.block_number
            print_scan_header(block)
            print_dex_prices(w3)

            all_opps: list[Opportunity] = []

            for token_a_key, token_b_key, loan_units in PAIRS:
                opps = scan_pair(w3, token_a_key, token_b_key, loan_units)
                for opp in opps:
                    alert_opportunity(opp)
                    total_found += 1
                all_opps.extend(opps)

            print_opportunities(all_opps)

            scan_count += 1
            print(
                f"\n  {Fore.BLUE}Scans: {scan_count}  |  "
                f"Opportunities found (session): {total_found}  |  "
                f"Next scan in {SCAN_INTERVAL}s…{Style.RESET_ALL}"
            )

        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}Shutting down. Total opportunities found: {total_found}{Style.RESET_ALL}")
            sys.exit(0)
        except Exception as e:
            log.error(f"Scan error: {e}", exc_info=True)

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    run()


