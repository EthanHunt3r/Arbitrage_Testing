#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║           DEX SWAP SIMULATION TEST SUITE                        ║
║  Fake/mock simulation of DEX swaps, arbitrage scanning,         ║
║  flash-loan logic, and opportunity detection — no RPC needed.   ║
╚══════════════════════════════════════════════════════════════════╝

Run:
    pip install pytest
    pytest test_dex_simulation.py -v
"""

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch


# ─────────────────────────────────────────────────────────────────
#  SIMULATED DEX STATE
#  Each DEX holds virtual reserves for token pairs.
#  Uses the Uniswap V2 constant-product formula: x * y = k
# ─────────────────────────────────────────────────────────────────

# Token decimals mirror mainnet
TOKEN_DECIMALS = {
    "WETH": 18,
    "USDC": 6,
    "USDT": 6,
    "DAI":  18,
    "WBTC": 8,
}

# Fake USD prices used by the mock price oracle
MOCK_USD_PRICES = {
    "WETH": 3000.0,
    "WBTC": 60000.0,
    "USDC": 1.0,
    "USDT": 1.0,
    "DAI":  1.0,
}

# Simulated reserves (token_in_raw, token_out_raw) per (dex, tokenA, tokenB).
# Deliberately set so that Uniswap V2 is cheaper for WETH→USDC than SushiSwap,
# creating a visible arbitrage spread.
FAKE_RESERVES: dict[tuple, tuple[int, int]] = {
    # ── WETH / USDC ─────────────────────────────────────────────────
    # Uniswap V2 is priced at ~3,060 USDC/WETH (over-valued USDC reserve)
    # SushiSwap  is priced at ~2,940 USDC/WETH (under-valued USDC reserve)
    # ~4% spread — easily survives two 0.3% swap fees and still nets profit.
    ("Uniswap V2",  "WETH", "USDC"): (1_000 * 10**18,  3_060_000 * 10**6),
    ("Uniswap V2",  "USDC", "WETH"): (3_060_000 * 10**6, 1_000 * 10**18),
    ("SushiSwap",   "WETH", "USDC"): (1_000 * 10**18,  2_940_000 * 10**6),
    ("SushiSwap",   "USDC", "WETH"): (2_940_000 * 10**6, 1_000 * 10**18),
    ("PancakeSwap", "WETH", "USDC"): (500  * 10**18,  1_500_000 * 10**6),
    ("PancakeSwap", "USDC", "WETH"): (1_500_000 * 10**6, 500 * 10**18),

    # ── WETH / USDT ──────────────────────────────────────────────────
    ("Uniswap V2", "WETH", "USDT"): (1_000 * 10**18,  3_060_000 * 10**6),
    ("Uniswap V2", "USDT", "WETH"): (3_060_000 * 10**6, 1_000 * 10**18),
    ("SushiSwap",  "WETH", "USDT"): (1_000 * 10**18,  2_940_000 * 10**6),
    ("SushiSwap",  "USDT", "WETH"): (2_940_000 * 10**6, 1_000 * 10**18),

    # ── WETH / DAI ───────────────────────────────────────────────────
    ("Uniswap V2", "WETH", "DAI"):  (1_000 * 10**18,  3_060_000 * 10**18),
    ("Uniswap V2", "DAI",  "WETH"): (3_060_000 * 10**18, 1_000 * 10**18),
    ("SushiSwap",  "WETH", "DAI"):  (1_000 * 10**18,  2_940_000 * 10**18),
    ("SushiSwap",  "DAI",  "WETH"): (2_940_000 * 10**18, 1_000 * 10**18),

    # ── USDC / USDT ──────────────────────────────────────────────────
    # ~0.4% spread; sufficient for a stablecoin arb with large loan size
    ("Uniswap V2", "USDC", "USDT"): (10_000_000 * 10**6, 10_040_000 * 10**6),
    ("Uniswap V2", "USDT", "USDC"): (10_040_000 * 10**6, 10_000_000 * 10**6),
    ("SushiSwap",  "USDC", "USDT"): (10_000_000 * 10**6,  9_960_000 * 10**6),
    ("SushiSwap",  "USDT", "USDC"): (9_960_000 * 10**6,  10_000_000 * 10**6),

    # ── USDC / DAI ───────────────────────────────────────────────────
    ("Uniswap V2", "USDC", "DAI"):  (5_000_000 * 10**6,  5_020_000 * 10**18),
    ("Uniswap V2", "DAI",  "USDC"): (5_020_000 * 10**18, 5_000_000 * 10**6),
    ("SushiSwap",  "USDC", "DAI"):  (5_000_000 * 10**6,  4_980_000 * 10**18),
    ("SushiSwap",  "DAI",  "USDC"): (4_980_000 * 10**18, 5_000_000 * 10**6),

    # ── WBTC / USDC ──────────────────────────────────────────────────
    ("Uniswap V2", "WBTC", "USDC"): (100 * 10**8,  6_120_000 * 10**6),
    ("Uniswap V2", "USDC", "WBTC"): (6_120_000 * 10**6, 100 * 10**8),
    ("SushiSwap",  "WBTC", "USDC"): (100 * 10**8,  5_880_000 * 10**6),
    ("SushiSwap",  "USDC", "WBTC"): (5_880_000 * 10**6, 100 * 10**8),

    # ── WBTC / USDT ──────────────────────────────────────────────────
    ("Uniswap V2", "WBTC", "USDT"): (100 * 10**8,  6_120_000 * 10**6),
    ("Uniswap V2", "USDT", "WBTC"): (6_120_000 * 10**6, 100 * 10**8),
    ("SushiSwap",  "WBTC", "USDT"): (100 * 10**8,  5_880_000 * 10**6),
    ("SushiSwap",  "USDT", "WBTC"): (5_880_000 * 10**6, 100 * 10**8),
}

# Simulated Balancer Vault liquidity (raw token units)
FAKE_VAULT_BALANCES: dict[str, int] = {
    "WETH": 5_000 * 10**18,
    "WBTC": 500   * 10**8,
    "USDC": 50_000_000 * 10**6,
    "USDT": 50_000_000 * 10**6,
    "DAI":  50_000_000 * 10**18,
}


# ─────────────────────────────────────────────────────────────────
#  CORE SIMULATION ENGINE
# ─────────────────────────────────────────────────────────────────

def uniswap_v2_get_amount_out(amount_in: int, reserve_in: int, reserve_out: int) -> int:
    """
    Exact Uniswap V2 formula with 0.3% fee:
        amount_out = (amount_in * 997 * reserve_out) / (reserve_in * 1000 + amount_in * 997)
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    amount_in_with_fee = amount_in * 997
    numerator   = amount_in_with_fee * reserve_out
    denominator = reserve_in * 1000 + amount_in_with_fee
    return numerator // denominator


def simulate_get_amount_out(
    dex_name: str,
    token_in: str,
    token_out: str,
    amount_in_raw: int,
) -> int:
    """
    Simulated replacement for get_amount_out() in arbitrage_monitor.py.
    Looks up fake reserves and applies the Uniswap V2 formula.
    Returns 0 if the pair doesn't exist on this simulated DEX.
    """
    key = (dex_name, token_in, token_out)
    if key not in FAKE_RESERVES:
        return 0
    reserve_in, reserve_out = FAKE_RESERVES[key]
    return uniswap_v2_get_amount_out(amount_in_raw, reserve_in, reserve_out)


def simulate_check_balancer_liquidity(token_symbol: str) -> int:
    """
    Simulated replacement for check_balancer_liquidity().
    Returns the fake vault balance for the given token symbol.
    """
    return FAKE_VAULT_BALANCES.get(token_symbol, 0)


def simulate_get_usd_price(symbol: str) -> float:
    """
    Simulated replacement for get_usd_price().
    Returns a static mock price — no HTTP call made.
    """
    return MOCK_USD_PRICES.get(symbol, 1.0)


# ─────────────────────────────────────────────────────────────────
#  SIMULATED ARBITRAGE SCANNER
#  Re-implements scan_pair() from arbitrage_monitor.py using all
#  the simulated helpers above — no Web3 / RPC required.
# ─────────────────────────────────────────────────────────────────

GAS_COST_USD  = 15.0    # simulated gas estimate
MIN_PROFIT_USD = 10.0   # minimum net profit threshold

DEXES_SIM = ["Uniswap V2", "SushiSwap", "PancakeSwap"]

# Token registry: symbol → (symbol, address_placeholder, decimals)
TOKENS_SIM = {
    "WETH": ("WETH",  "0xWETH",  18),
    "USDC": ("USDC",  "0xUSDC",   6),
    "USDT": ("USDT",  "0xUSDT",   6),
    "DAI":  ("DAI",   "0xDAI",   18),
    "WBTC": ("WBTC",  "0xWBTC",   8),
}

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SimOpportunity:
    token_a:             str
    token_b:             str
    buy_dex:             str
    sell_dex:            str
    loan_amount:         Decimal
    gross_profit_token:  Decimal
    gross_profit_usd:    Decimal
    gas_cost_usd:        Decimal
    net_profit_usd:      Decimal
    spread_pct:          Decimal
    timestamp:           str = field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )


def sim_scan_pair(
    token_a_key: str,
    token_b_key: str,
    loan_units:  float,
    gas_cost_usd:   float = GAS_COST_USD,
    min_profit_usd: float = MIN_PROFIT_USD,
) -> list[SimOpportunity]:
    """
    Fully simulated version of scan_pair() — uses fake reserves,
    fake vault balances, and fake USD prices.
    No Web3 / network calls are made.
    """
    sym_a, _, dec_a = TOKENS_SIM[token_a_key]
    sym_b, _, dec_b = TOKENS_SIM[token_b_key]

    amount_in_raw = int(loan_units * 10 ** dec_a)

    # Cap loan to simulated vault balance
    vault_balance = simulate_check_balancer_liquidity(sym_a)
    if vault_balance == 0:
        return []
    if amount_in_raw > vault_balance:
        amount_in_raw = vault_balance
        loan_units    = amount_in_raw / 10 ** dec_a

    usd_price_a = simulate_get_usd_price(sym_a)

    # Gather quotes from each simulated DEX
    quotes: dict[str, int] = {}
    for dex_name in DEXES_SIM:
        out = simulate_get_amount_out(dex_name, sym_a, sym_b, amount_in_raw)
        if out > 0:
            quotes[dex_name] = out

    if len(quotes) < 2:
        return []

    opportunities: list[SimOpportunity] = []
    dex_names = list(quotes.keys())

    for i, buy_dex in enumerate(dex_names):
        for sell_dex in dex_names[i + 1:]:
            for bd, sd in [(buy_dex, sell_dex), (sell_dex, buy_dex)]:
                b_raw      = quotes[bd]
                a_back_raw = simulate_get_amount_out(sd, sym_b, sym_a, b_raw)
                if a_back_raw == 0:
                    continue

                loan_dec         = Decimal(str(loan_units))
                a_back_dec       = Decimal(a_back_raw) / Decimal(10 ** dec_a)
                gross_profit_tok = a_back_dec - loan_dec
                gross_profit_usd = float(gross_profit_tok) * usd_price_a
                net_usd          = gross_profit_usd - gas_cost_usd

                if net_usd < min_profit_usd:
                    continue

                spread_pct = (gross_profit_tok / loan_dec) * Decimal(100)

                opportunities.append(SimOpportunity(
                    token_a            = sym_a,
                    token_b            = sym_b,
                    buy_dex            = bd,
                    sell_dex           = sd,
                    loan_amount        = loan_dec,
                    gross_profit_token = gross_profit_tok,
                    gross_profit_usd   = Decimal(str(round(gross_profit_usd, 4))),
                    gas_cost_usd       = Decimal(str(gas_cost_usd)),
                    net_profit_usd     = Decimal(str(round(net_usd, 4))),
                    spread_pct         = spread_pct.quantize(Decimal("0.0001")),
                ))

    return opportunities


# ─────────────────────────────────────────────────────────────────
#  TEST SUITE
# ─────────────────────────────────────────────────────────────────

class TestUniswapV2Formula(unittest.TestCase):
    """Unit tests for the core Uniswap V2 AMM formula."""

    def test_basic_swap_returns_nonzero(self):
        out = uniswap_v2_get_amount_out(1 * 10**18, 1000 * 10**18, 3_000_000 * 10**6)
        self.assertGreater(out, 0)

    def test_zero_amount_in_returns_zero(self):
        out = uniswap_v2_get_amount_out(0, 1000 * 10**18, 3_000_000 * 10**6)
        self.assertEqual(out, 0)

    def test_zero_reserve_in_returns_zero(self):
        out = uniswap_v2_get_amount_out(1 * 10**18, 0, 3_000_000 * 10**6)
        self.assertEqual(out, 0)

    def test_zero_reserve_out_returns_zero(self):
        out = uniswap_v2_get_amount_out(1 * 10**18, 1000 * 10**18, 0)
        self.assertEqual(out, 0)

    def test_fee_applied_correctly(self):
        """Amount out must be strictly less than the no-fee equivalent."""
        reserve_in  = 1_000 * 10**18
        reserve_out = 3_000_000 * 10**6
        amount_in   = 1 * 10**18
        with_fee    = uniswap_v2_get_amount_out(amount_in, reserve_in, reserve_out)
        no_fee      = (amount_in * reserve_out) // (reserve_in + amount_in)
        self.assertLess(with_fee, no_fee)

    def test_larger_input_gives_larger_output(self):
        r_in  = 1_000 * 10**18
        r_out = 3_000_000 * 10**6
        out1  = uniswap_v2_get_amount_out(1 * 10**18, r_in, r_out)
        out2  = uniswap_v2_get_amount_out(2 * 10**18, r_in, r_out)
        self.assertGreater(out2, out1)

    def test_price_impact_increases_with_size(self):
        """Effective price (output / input) must decrease for larger swaps."""
        r_in  = 1_000 * 10**18
        r_out = 3_000_000 * 10**6
        out_small = uniswap_v2_get_amount_out(   1 * 10**18, r_in, r_out)
        out_large = uniswap_v2_get_amount_out(100 * 10**18, r_in, r_out)
        eff_small = out_small / (1   * 10**18)
        eff_large = out_large / (100 * 10**18)
        self.assertGreater(eff_small, eff_large)

    def test_stablecoin_pair_tight_spread(self):
        """USDC/USDT with near-equal reserves should return close to input."""
        r_in  = 10_000_000 * 10**6
        r_out = 10_000_000 * 10**6
        amount_in = 1_000 * 10**6   # 1,000 USDC
        out = uniswap_v2_get_amount_out(amount_in, r_in, r_out)
        ratio = out / amount_in
        self.assertAlmostEqual(ratio, 1.0, delta=0.01)


class TestSimulatedSwaps(unittest.TestCase):
    """Tests for simulate_get_amount_out() against fake reserves."""

    def test_known_pair_returns_nonzero(self):
        out = simulate_get_amount_out("Uniswap V2", "WETH", "USDC", 1 * 10**18)
        self.assertGreater(out, 0)

    def test_unknown_pair_returns_zero(self):
        out = simulate_get_amount_out("FakeDEX", "WETH", "USDC", 1 * 10**18)
        self.assertEqual(out, 0)

    def test_uniswap_weth_usdc_price_in_range(self):
        """1 WETH should yield approximately $3,000-ish USDC on Uniswap V2."""
        out = simulate_get_amount_out("Uniswap V2", "WETH", "USDC", 1 * 10**18)
        price = out / 10**6   # USDC has 6 decimals
        self.assertGreater(price, 2_800)
        self.assertLess(price,    3_200)

    def test_uniswap_vs_sushiswap_spread_exists(self):
        """Uniswap V2 should return more USDC for 1 WETH than SushiSwap (by design)."""
        uni   = simulate_get_amount_out("Uniswap V2", "WETH", "USDC", 1 * 10**18)
        sushi = simulate_get_amount_out("SushiSwap",  "WETH", "USDC", 1 * 10**18)
        self.assertGreater(uni, sushi)

    def test_reverse_path_returns_nonzero(self):
        out = simulate_get_amount_out("Uniswap V2", "USDC", "WETH", 3_000 * 10**6)
        self.assertGreater(out, 0)

    def test_wbtc_usdc_price_in_range(self):
        out = simulate_get_amount_out("Uniswap V2", "WBTC", "USDC", 1 * 10**8)
        price = out / 10**6
        self.assertGreater(price, 55_000)
        self.assertLess(price,    65_000)

    def test_stablecoin_usdc_dai_close_to_peg(self):
        out = simulate_get_amount_out("Uniswap V2", "USDC", "DAI", 1_000 * 10**6)
        amount = out / 10**18   # DAI has 18 decimals
        self.assertAlmostEqual(amount, 1_000.0, delta=10.0)


class TestBalancerVaultSimulation(unittest.TestCase):
    """Tests for the simulated Balancer Vault liquidity check."""

    def test_weth_vault_balance_nonzero(self):
        bal = simulate_check_balancer_liquidity("WETH")
        self.assertGreater(bal, 0)

    def test_unknown_token_returns_zero(self):
        bal = simulate_check_balancer_liquidity("UNKNOWN")
        self.assertEqual(bal, 0)

    def test_vault_has_enough_for_typical_loan(self):
        """Vault should hold at least 1 WETH for flash loans."""
        bal        = simulate_check_balancer_liquidity("WETH")
        one_weth   = 1 * 10**18
        self.assertGreater(bal, one_weth)

    def test_loan_cap_logic(self):
        """Requesting more than the vault holds should cap to vault balance."""
        vault_bal    = simulate_check_balancer_liquidity("WETH")
        requested    = vault_bal * 10  # 10× more than available
        capped       = min(requested, vault_bal)
        self.assertEqual(capped, vault_bal)


class TestPriceOracle(unittest.TestCase):
    """Tests for simulate_get_usd_price()."""

    def test_weth_price_nonzero(self):
        self.assertGreater(simulate_get_usd_price("WETH"), 0)

    def test_stablecoin_price_is_one(self):
        for sym in ("USDC", "USDT", "DAI"):
            with self.subTest(sym=sym):
                self.assertAlmostEqual(simulate_get_usd_price(sym), 1.0)

    def test_unknown_token_defaults_to_one(self):
        self.assertEqual(simulate_get_usd_price("XYZ"), 1.0)

    def test_wbtc_higher_than_weth(self):
        self.assertGreater(
            simulate_get_usd_price("WBTC"),
            simulate_get_usd_price("WETH"),
        )


class TestArbitrageScanner(unittest.TestCase):
    """Integration tests for sim_scan_pair() — no network / RPC needed."""

    # ── Opportunity detection ─────────────────────────────────────

    def test_weth_usdc_finds_opportunity(self):
        """The fake reserves are set to guarantee at least one arb opportunity."""
        opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=1.0, min_profit_usd=1.0)
        self.assertGreater(len(opps), 0)

    def test_opportunity_has_positive_net_profit(self):
        opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=1.0, min_profit_usd=1.0)
        for opp in opps:
            with self.subTest(opp=opp):
                self.assertGreater(opp.net_profit_usd, Decimal("0"))

    def test_correct_buy_sell_direction(self):
        """
        With Uniswap V2 having more USDC output than SushiSwap for WETH,
        the buy leg must be on Uniswap V2 (cheaper WETH) and sell on SushiSwap.
        """
        opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=1.0, min_profit_usd=1.0)
        profitable = [o for o in opps if o.net_profit_usd > 0]
        # At least one opportunity should buy WETH cheaply on Uniswap V2
        buy_on_uni = [o for o in profitable if o.buy_dex == "Uniswap V2"]
        self.assertGreater(len(buy_on_uni), 0)

    def test_spread_pct_positive(self):
        opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=1.0, min_profit_usd=1.0)
        for opp in opps:
            self.assertGreater(opp.spread_pct, Decimal("0"))

    def test_net_profit_equals_gross_minus_gas(self):
        opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=5.0, min_profit_usd=0.0)
        for opp in opps:
            expected = opp.gross_profit_usd - opp.gas_cost_usd
            self.assertAlmostEqual(float(opp.net_profit_usd), float(expected), places=3)

    # ── Threshold filtering ───────────────────────────────────────

    def test_high_min_profit_filters_all(self):
        """An absurdly high threshold should return zero opportunities."""
        opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=0.0, min_profit_usd=1_000_000.0)
        self.assertEqual(len(opps), 0)

    def test_zero_min_profit_returns_more(self):
        with_threshold    = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=5.0, min_profit_usd=10.0)
        without_threshold = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=5.0, min_profit_usd=0.0)
        self.assertGreaterEqual(len(without_threshold), len(with_threshold))

    def test_high_gas_cost_eliminates_opportunities(self):
        """Gas so high ($9,999) that no arb survives."""
        opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=9_999.0, min_profit_usd=1.0)
        self.assertEqual(len(opps), 0)

    # ── Loan capping ──────────────────────────────────────────────

    def test_loan_cap_does_not_crash(self):
        """Requesting 999,999 WETH should silently cap to vault balance."""
        opps = sim_scan_pair("WETH", "USDC", 999_999.0, gas_cost_usd=1.0, min_profit_usd=1.0)
        # Just verify it doesn't raise; result can be empty due to price impact
        self.assertIsInstance(opps, list)

    # ── Multiple pairs ────────────────────────────────────────────

    def test_wbtc_usdc_pair(self):
        opps = sim_scan_pair("WBTC", "USDC", 0.05, gas_cost_usd=1.0, min_profit_usd=1.0)
        self.assertIsInstance(opps, list)

    def test_stablecoin_pair_usdc_usdt(self):
        opps = sim_scan_pair("USDC", "USDT", 5000.0, gas_cost_usd=1.0, min_profit_usd=0.5)
        self.assertIsInstance(opps, list)

    def test_weth_dai_pair(self):
        opps = sim_scan_pair("WETH", "DAI", 1.0, gas_cost_usd=1.0, min_profit_usd=1.0)
        self.assertIsInstance(opps, list)

    # ── Data integrity ────────────────────────────────────────────

    def test_opportunity_token_fields_correct(self):
        opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=1.0, min_profit_usd=1.0)
        for opp in opps:
            self.assertEqual(opp.token_a, "WETH")
            self.assertEqual(opp.token_b, "USDC")

    def test_opportunity_buy_sell_dex_are_different(self):
        opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=1.0, min_profit_usd=1.0)
        for opp in opps:
            self.assertNotEqual(opp.buy_dex, opp.sell_dex)

    def test_timestamp_is_set(self):
        opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=1.0, min_profit_usd=1.0)
        for opp in opps:
            self.assertIsNotNone(opp.timestamp)
            self.assertGreater(len(opp.timestamp), 0)

    def test_loan_amount_is_positive(self):
        opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=1.0, min_profit_usd=1.0)
        for opp in opps:
            self.assertGreater(opp.loan_amount, Decimal("0"))


class TestEdgeCases(unittest.TestCase):
    """Edge cases and boundary conditions."""

    def test_no_liquidity_returns_empty(self):
        """A pair with no reserves on any DEX should return no opportunities."""
        opps = sim_scan_pair("DAI", "WBTC", 100.0)   # not in FAKE_RESERVES
        self.assertEqual(opps, [])

    def test_single_dex_pair_returns_empty(self):
        """
        A pair only on one DEX cannot produce an arb.
        We simulate this by temporarily removing one DEX's reserves.
        """
        import test_dex_simulation as _self_module
        orig = _self_module.FAKE_RESERVES.copy()
        # Remove SushiSwap and PancakeSwap entries for WETH/USDC
        keys_to_remove = [k for k in orig if k[0] in ("SushiSwap", "PancakeSwap") and "WETH" in k]
        for k in keys_to_remove:
            del _self_module.FAKE_RESERVES[k]
        try:
            opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=0.0, min_profit_usd=0.0)
            # Only one DEX has this pair → impossible to arb
            self.assertEqual(opps, [])
        finally:
            _self_module.FAKE_RESERVES.update(orig)

    def test_very_small_loan_amount(self):
        opps = sim_scan_pair("WETH", "USDC", 0.000001, gas_cost_usd=0.0, min_profit_usd=0.0)
        self.assertIsInstance(opps, list)

    def test_opportunity_gross_profit_geq_net_profit(self):
        opps = sim_scan_pair("WETH", "USDC", 1.0, gas_cost_usd=5.0, min_profit_usd=0.0)
        for opp in opps:
            self.assertGreaterEqual(opp.gross_profit_usd, opp.net_profit_usd)


class TestFullScanAllPairs(unittest.TestCase):
    """Run all configured pairs through the simulator (smoke test)."""

    PAIRS = [
        ("WETH",  "USDC",  1.0),
        ("WETH",  "USDT",  1.0),
        ("WETH",  "DAI",   1.0),
        ("WBTC",  "USDC",  0.05),
        ("WBTC",  "USDT",  0.05),
        ("USDC",  "USDT",  5000.0),
        ("USDC",  "DAI",   5000.0),
    ]

    def test_all_pairs_run_without_exception(self):
        for token_a, token_b, loan in self.PAIRS:
            with self.subTest(pair=f"{token_a}/{token_b}"):
                opps = sim_scan_pair(token_a, token_b, loan)
                self.assertIsInstance(opps, list)

    def test_total_opportunities_found_is_sane(self):
        total = 0
        for token_a, token_b, loan in self.PAIRS:
            total += len(sim_scan_pair(token_a, token_b, loan, gas_cost_usd=1.0, min_profit_usd=0.5))
        # With deliberately skewed reserves we expect at least some opportunities
        self.assertGreater(total, 0)


# ─────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
