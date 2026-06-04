# Crypto Arbitrage Testing
 
A research project exploring flash-loan-powered DEX arbitrage on Ethereum. It consists of a Solidity smart contract (`ArbitrageBot.sol`), a live on-chain opportunity monitor (`arbitrage_monitor.py`), and a fully offline test suite (`test_dex_simulation.py`) that validates the scanning logic without needing an RPC connection.
 
> **Status: shelved.** Finding real profitable opportunities requires significant latency and gas optimisation that was out of scope. This repo documents what was built and what was learned.
 
---
 
## How It Works
 
The arbitrage strategy is a classic two-leg flash-loan cycle:
 
```
1. Borrow tokenA from the Balancer V2 Vault (0% fee)
2. Swap tokenA → tokenB on DEX A  (buy leg — cheaper price)
3. Swap tokenB → tokenA on DEX B  (sell leg — higher price)
4. Repay exact loan amount to Vault
5. Keep the difference as profit
```
 
Because Balancer V2 charges no flash-loan protocol fee, the only real costs are swap fees (0.3% per leg on Uniswap V2-compatible DEXes) and gas. The Python monitor estimates gas in USD and only flags opportunities where net profit exceeds a configurable threshold.
 
---
 
## Repository Structure
 
```
Arbitrage_Testing/
├── ArbitrageBot.sol          # Solidity contract — flash loan + two-leg swap logic
├── arbitrage_monitor.py      # Python monitor — scans DEXes for live opportunities
├── test_dex_simulation.py    # Offline test suite — no RPC needed
├── arb_monitor.log           # Sample log output from a monitor run
├── artifacts/                # Compiled contract ABIs and build info
└── package.json              # Node dependencies (web3, dotenv)
```
 
---
 
## Smart Contract (`ArbitrageBot.sol`)
 
**Contract:** `CryptoArbitrageBot`  
**Solidity:** `^0.8.19`  
**Flash-loan provider:** [Balancer V2 Vault](https://docs.balancer.fi/reference/contracts/vault-and-pool-abi.html) — `0xBA12222222228d8Ba445958a75a0704d566BF2C8`  
**Supported DEXes:** Any Uniswap V2-compatible router (Uniswap V2, SushiSwap, PancakeSwap, etc.)
 
### Key Functions
 
| Function | Description |
|---|---|
| `executeArbitrage(tokenA, tokenB, routerA, routerB, loanAmount)` | Triggers the flash loan and executes the two-leg swap atomically |
| `checkProfitability(...)` | View function — simulate profitability via `eth_call` before submitting a tx |
| `setRouter(router, approved)` | Owner: whitelist or revoke a DEX router |
| `setSlippage(newBps)` | Owner: update slippage tolerance (default 50 bps = 0.5%, max 500 bps) |
| `withdrawToken(token, amount)` | Owner: pull ERC-20 profit out of the contract |
| `withdrawETH()` | Owner: pull any native ETH out of the contract |
 
### Constructor Arguments
 
```
_balancerVault  0xBA12222222228d8Ba445958a75a0704d566BF2C8  (same on Ethereum, Polygon, Arbitrum, Optimism)
_routerA        0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D  (Uniswap V2)
_routerB        0xEfF92A263d31888d860bD50809A8D171709b7b1c  (PancakeSwap ETH mainnet)
```
 
### Events
 
- `ArbitrageExecuted(tokenA, tokenB, loanAmount, profit)`
- `RouterApproved(router, approved)`
- `SlippageUpdated(oldBps, newBps)`
- `Withdrawn(token, amount)`
### Security Notes
 
- Only the deployer (`owner`) can call `executeArbitrage`, `withdrawToken`, `withdrawETH`, `setRouter`, and `setSlippage`.
- `receiveFlashLoan` is restricted to the Balancer Vault address via `onlyBalancerVault`.
- The contract reverts with `UnprofitableTrade` if the round-trip doesn't return at least `loanAmount` tokens — the flash loan never completes unless it's profitable.
- **This is a research template. Audit thoroughly and test on a forked network before deploying real funds.**
---
 
## Opportunity Monitor (`arbitrage_monitor.py`)
 
A Python script that polls on-chain prices across Uniswap V2, SushiSwap, and PancakeSwap for a set of configured token pairs, calculates net profit after gas, and optionally fires Telegram alerts.
 
### Monitored Pairs (default)
 
| Pair | Loan Size |
|---|---|
| WETH / USDC | 1 WETH |
| WETH / USDT | 1 WETH |
| WETH / DAI | 1 WETH |
| WBTC / USDC | 0.05 WBTC |
| WBTC / USDT | 0.05 WBTC |
| USDC / USDT | 5,000 USDC |
| USDC / DAI | 5,000 USDC |
 
Loan sizes are automatically capped to the Balancer Vault's actual balance for each token.
 
### Setup
 
**1. Install dependencies**
 
```bash
pip install web3 requests python-dotenv colorama tabulate
```
 
**2. Create `API.env`**
 
```env
RPC_URL=https://mainnet.infura.io/v3/YOUR_KEY
 
# Tuning
SCAN_INTERVAL=30          # seconds between scans
MIN_PROFIT_USD=5.0        # minimum net profit to flag
GAS_COST_USD=15.0         # estimated gas cost per arbitrage tx in USD
 
# Optional Telegram alerts
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
 
# Optional: address of your deployed ArbitrageBot (for reference in logs)
CONTRACT_ADDRESS=0x...
```
 
**3. Run**
 
```bash
python arbitrage_monitor.py
```
 
The monitor prints a colour-coded table each scan cycle showing per-DEX WETH/USDC prices and any opportunities above the profit threshold. Opportunities are also written to `arb_monitor.log`.
 
### Telegram Alerts
 
When an opportunity is found and Telegram is configured, the bot sends a message like:
 
```
🚨 ARB OPPORTUNITY
Pair:       WETH/USDC
Buy on:     Uniswap V2
Sell on:    SushiSwap
Loan:       1.0 WETH
Spread:     0.42%
Net profit: $8.73 USD
```
 
---
 
## Test Suite (`test_dex_simulation.py`)
 
A fully self-contained test suite that validates the arbitrage scanning logic without any network connection. It replaces all Web3 and HTTP calls with simulated DEX reserves (using the exact Uniswap V2 constant-product formula) and mock USD prices.
 
The fake reserves are deliberately skewed — Uniswap V2 is set ~4% higher than SushiSwap for WETH/USDC — so opportunity detection tests have a guaranteed signal to find.
 
### Running the Tests
 
```bash
pip install pytest
pytest test_dex_simulation.py -v
```
 
### Test Coverage
 
| Class | What it tests |
|---|---|
| `TestUniswapV2Formula` | Constant-product AMM math, fee application, edge cases |
| `TestBalancerLiquidity` | Vault balance lookups and loan capping |
| `TestPriceOracle` | Mock USD price lookups |
| `TestArbitrageScanner` | Opportunity detection, direction, profit calculation, threshold filtering |
| `TestEdgeCases` | No-liquidity pairs, single-DEX pairs, tiny loan amounts |
| `TestFullScanAllPairs` | Smoke test — all 7 configured pairs run without exception |
 
---
 
## Learnings / Why It's Shelved
 
Real DEX arbitrage in 2024+ is extremely competitive:
 
- **Latency wins.** Most on-chain spread lasts milliseconds. By the time a Python monitor sees it, private relayers and MEV bots have already closed it.
- **Gas eats margin.** At typical mainnet gas prices, a transaction costs $15–30+. That wipes out most small spreads.
- **Mempool competition.** Profitable transactions get front-run or sandwiched unless submitted via a private mempool (e.g. Flashbots).
- **Optimisation required.** A competitive bot needs multi-hop paths, triangle arb, Rust/Go execution, and direct builder submission — well beyond a weekend project.
The contract and monitor are solid starting points if you want to take it further.
 
---
