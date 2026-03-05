// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

// ============================================================

// ============================================================

// ============================================================
//  INTERFACES
// ============================================================

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}


/// @dev Uniswap V2-compatible router (also covers SushiSwap, PancakeSwap, etc.)
interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);

    function getAmountsOut(uint256 amountIn, address[] calldata path)
        external
        view
        returns (uint256[] memory amounts);
}

// ── Balancer V2 interfaces ───────────────────────────────────────

/**
 * @dev Implement this interface to receive Balancer V2 flash loans.
 *      The Vault calls receiveFlashLoan() atomically after sending tokens.
 *      You MUST transfer tokens + feeAmounts back to the Vault before returning.
 *
 *      Balancer V2 charges 0% flash-loan protocol fee (removed via governance).
 *      feeAmounts will always be zero arrays.
 */
 
interface IFlashLoanRecipient {
    function receiveFlashLoan(
        IERC20[] calldata tokens,
        uint256[] calldata amounts,
        uint256[] calldata feeAmounts,   // always 0 on Balancer V2
        bytes calldata userData
    ) external;
}

/**
 * @dev Minimal Balancer V2 Vault interface.
 *      Canonical address (same on every supported chain):
 *      0xBA12222222228d8Ba445958a75a0704d566BF2C8
 */
interface IBalancerVault {
    function flashLoan(
        IFlashLoanRecipient recipient,
        IERC20[] calldata tokens,
        uint256[] calldata amounts,
        bytes calldata userData
    ) external;
}

// ============================================================
//  ARBITRAGE BOT CONTRACT
// ============================================================

/**
 * @title  CryptoArbitrageBot
 * @notice Flash-loan-powered arbitrage across two Uniswap-V2-compatible DEXes,
 *         using the Balancer V2 Vault as the zero-fee flash-loan provider.
 *
 * ┌──────────────────────────────────────────────────────────────┐
 * │  FLOW                                                        │
 * │  1. Owner calls executeArbitrage()                           │
 * │  2. Contract requests a flash loan from the Balancer Vault   │
 * │  3. Vault sends tokenA and calls receiveFlashLoan()          │
 * │  4. Swap tokenA → tokenB on dexA (buy leg  — cheaper price) │
 * │  5. Swap tokenB → tokenA on dexB (sell leg — higher price)  │
 * │  6. Repay exact loanAmount to Vault (Balancer fee = 0)       │
 * │  7. Profit stays in this contract                            │
 * │  8. Owner calls withdrawToken() to collect profit            │
 * └──────────────────────────────────────────────────────────────┘
 *
 * @dev  Balancer Vault: 0xBA12222222228d8Ba445958a75a0704d566BF2C8
 *       (Ethereum mainnet, Polygon, Arbitrum, Optimism — same address)
 *
 *       THIS IS A TEMPLATE. Audit thoroughly, test on a forked network,
 *       and understand all risks before deploying with real funds.
 */
contract CryptoArbitrageBot is IFlashLoanRecipient {

    // --------------------------------------------------------
    //  STATE
    // --------------------------------------------------------

    address public owner;

    /// @notice Balancer V2 Vault — zero-fee flash-loan provider.
    IBalancerVault public immutable balancerVault;

    /// @notice Whitelisted DEX routers. Add more via setRouter().
    mapping(address => bool) public approvedRouters;

    /// @dev Slippage tolerance in basis points (default 50 = 0.5 %).
    uint256 public slippageBps = 50;
    uint256 private constant BPS_DENOM = 10_000;

    // --------------------------------------------------------
    //  EVENTS
    // --------------------------------------------------------

    event ArbitrageExecuted(
        address indexed tokenA,
        address indexed tokenB,
        uint256 loanAmount,
        uint256 profit
    );
    event RouterApproved(address indexed router, bool approved);
    event SlippageUpdated(uint256 oldBps, uint256 newBps);
    event Withdrawn(address indexed token, uint256 amount);

    // --------------------------------------------------------
    //  ERRORS
    // --------------------------------------------------------

    error NotOwner();
    error RouterNotApproved(address router);
    error UnprofitableTrade(uint256 repayAmount, uint256 received);
    error InvalidCaller(address caller);
    error ZeroAmount();
    error TokenMismatch();

    // --------------------------------------------------------
    //  MODIFIERS
    // --------------------------------------------------------

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    /// @dev Only the Balancer Vault may invoke receiveFlashLoan().
    modifier onlyBalancerVault() {
        if (msg.sender != address(balancerVault)) revert InvalidCaller(msg.sender);
        _;
    }

    // --------------------------------------------------------
    //  CONSTRUCTOR
    // --------------------------------------------------------

    /**
     * @param _balancerVault  Balancer V2 Vault address.
     *                        Use 0xBA12222222228d8Ba445958a75a0704d566BF2C8
     *                        on Ethereum, Polygon, Arbitrum, and Optimism.
     * @param _routerA        First DEX router  (e.g. Uniswap V2).
     * @param _routerB        Second DEX router (e.g. SushiSwap).
     */


    constructor(
        address _balancerVault, //0xBA12222222228d8Ba445958a75a0704d566BF2C8
        address _routerA, //0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D
        address _routerB //0xEfF92A263d31888d860bD50809A8D171709b7b1c
    ) {
        owner         = msg.sender;
        balancerVault = IBalancerVault(_balancerVault);

        _setRouter(_routerA, true);
        _setRouter(_routerB, true);
    }

    // --------------------------------------------------------
    //  OWNER: CONFIGURATION
    // --------------------------------------------------------

    /// @notice Approve or revoke a DEX router.
    function setRouter(address router, bool approved) external onlyOwner {
        _setRouter(router, approved);
    }

    function _setRouter(address router, bool approved) internal {
        approvedRouters[router] = approved;
        emit RouterApproved(router, approved);
    }

    /// @notice Update slippage tolerance in basis points (max 500 = 5 %).
    function setSlippage(uint256 newBps) external onlyOwner {
        require(newBps <= 500, "Slippage too high");
        emit SlippageUpdated(slippageBps, newBps);
        slippageBps = newBps;
    }

    /// @notice Transfer contract ownership.
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "Zero address");
        owner = newOwner;
    }

    // --------------------------------------------------------
    //  ARBITRAGE ENTRY POINT
    // --------------------------------------------------------

    /**
     * @notice Initiate a Balancer-flash-loan-powered arbitrage.
     *
     * @param tokenA      Token to borrow and end up with (base token).
     * @param tokenB      Intermediate token used across both swap legs.
     * @param loanAmount  Amount of tokenA to borrow from the Balancer Vault.
     * @param routerA     DEX router for leg 1  (tokenA → tokenB, buy leg).
     * @param routerB     DEX router for leg 2  (tokenB → tokenA, sell leg).
     */
    function executeArbitrage(
        address tokenA,
        address tokenB,
        uint256 loanAmount,
        address routerA,
        address routerB
    ) external onlyOwner {
        if (loanAmount == 0) revert ZeroAmount();
        if (!approvedRouters[routerA]) revert RouterNotApproved(routerA);
        if (!approvedRouters[routerB]) revert RouterNotApproved(routerB);

        // Build Balancer flash-loan arrays (borrowing a single token)
        IERC20[]  memory tokens  = new IERC20[](1);
        uint256[] memory amounts = new uint256[](1);
        tokens[0]  = IERC20(tokenA);
        amounts[0] = loanAmount;

        // Encode arb params — forwarded as userData through the Vault
        bytes memory userData = abi.encode(tokenA, tokenB, routerA, routerB);

        // Request flash loan.  Vault calls receiveFlashLoan() synchronously.
        balancerVault.flashLoan(
            IFlashLoanRecipient(address(this)),
            tokens,
            amounts,
            userData
        );
    }

    // --------------------------------------------------------
    //  BALANCER FLASH-LOAN CALLBACK
    // --------------------------------------------------------

    /**
     * @notice Called by the Balancer Vault right after it sends the tokens.
     * @dev    Must push tokens[i] + feeAmounts[i] back to the Vault before
     *         returning.  On Balancer V2 feeAmounts are always 0, so
     *         repayAmount == loanAmount exactly.
     *
     * @param tokens      Borrowed token array (length 1).
     * @param amounts     Borrowed amounts array.
     * @param feeAmounts  Fee array — currently always 0 on Balancer V2.
     * @param userData    ABI-encoded (tokenA, tokenB, routerA, routerB).
     */
    function receiveFlashLoan(
        IERC20[] calldata tokens,
        uint256[] calldata amounts,
        uint256[] calldata feeAmounts,
        bytes calldata userData
    ) external override onlyBalancerVault {

        (
            address tokenA,
            address tokenB,
            address routerA,
            address routerB
        ) = abi.decode(userData, (address, address, address, address));

        // Confirm the borrowed token matches what executeArbitrage() requested
        if (address(tokens[0]) != tokenA) revert TokenMismatch();

        uint256 loanAmount  = amounts[0];
        uint256 fee         = feeAmounts[0];      // 0 on Balancer V2
        uint256 repayAmount = loanAmount + fee;   // == loanAmount

        // ── Leg 1: tokenA → tokenB on routerA (buy leg) ──────────────
        uint256 tokenBReceived = _swap(
            routerA,
            tokenA,
            tokenB,
            loanAmount,
            0            // min out set by slippage tolerance inside _swap
        );

        // ── Leg 2: tokenB → tokenA on routerB (sell leg) ─────────────
        uint256 tokenAReceived = _swap(
            routerB,
            tokenB,
            tokenA,
            tokenBReceived,
            repayAmount  // must receive at least enough to repay the Vault
        );

        // Revert if the round-trip was not profitable
        if (tokenAReceived < repayAmount) {
            revert UnprofitableTrade(repayAmount, tokenAReceived);
        }

        // ── Repay Balancer Vault via direct transfer (not approve) ────
        // Balancer's Vault pulls nothing; the recipient pushes repayment.
        IERC20(tokenA).transfer(address(balancerVault), repayAmount);

        uint256 profit = tokenAReceived - repayAmount;
        emit ArbitrageExecuted(tokenA, tokenB, loanAmount, profit);
    }

    // --------------------------------------------------------
    //  INTERNAL: SWAP HELPER
    // --------------------------------------------------------

    /**
     * @dev Executes a single-hop swap on a Uniswap-V2-compatible router.
     *      Applies slippage tolerance to the on-chain getAmountsOut quote.
     *
     * @param router    DEX router address.
     * @param tokenIn   Token being sold.
     * @param tokenOut  Token being bought.
     * @param amountIn  Exact amount of tokenIn to spend.
     * @param minOut    Hard minimum output; 0 means use slippage-adjusted quote.
     * @return received Actual amount of tokenOut received after the swap.
     */
    function _swap(
        address router,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minOut
    ) internal returns (uint256 received) {
        address[] memory path = new address[](2);
        path[0] = tokenIn;
        path[1] = tokenOut;

        // On-chain quote with slippage applied
        uint256[] memory quoted  = IUniswapV2Router(router).getAmountsOut(amountIn, path);
        uint256 quotedOut        = quoted[quoted.length - 1];
        uint256 slippageMin      = (quotedOut * (BPS_DENOM - slippageBps)) / BPS_DENOM;

        // Use the stricter lower bound
        uint256 amountOutMin = slippageMin > minOut ? slippageMin : minOut;

        // Approve router to pull tokenIn
        IERC20(tokenIn).approve(router, amountIn);

        uint256[] memory results = IUniswapV2Router(router).swapExactTokensForTokens(
            amountIn,
            amountOutMin,
            path,
            address(this),
            block.timestamp + 300   // 5-minute deadline
        );

        received = results[results.length - 1];
    }

    // --------------------------------------------------------
    //  OWNER: PROFIT WITHDRAWAL
    // --------------------------------------------------------

    /**
     * @notice Withdraw any ERC-20 token held by this contract.
     * @param token  Address of the token to withdraw.
     * @param amount Amount to withdraw; pass 0 to withdraw the full balance.
     */
    function withdrawToken(address token, uint256 amount) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        uint256 out = (amount == 0 || amount > bal) ? bal : amount;
        require(out > 0, "Nothing to withdraw");
        IERC20(token).transfer(owner, out);
        emit Withdrawn(token, out);
    }

    /// @notice Withdraw any native ETH that ended up in this contract.
    function withdrawETH() external onlyOwner {
        uint256 bal = address(this).balance;
        require(bal > 0, "No ETH");
        payable(owner).transfer(bal);
    }

    // --------------------------------------------------------
    //  READ: PROFITABILITY CHECKER (off-chain simulation)
    // --------------------------------------------------------

    /**
     * @notice Simulate whether an arb would be profitable before submitting.
     *         Invoke via eth_call — does not send a transaction.
     *
     *         Because Balancer V2 charges 0% flash-loan fees, repayAmount
     *         equals loanAmount exactly; only gas costs remain (handled
     *         off-chain in the Python monitor).
     *
     * @param tokenA      Base token (borrowed and repaid).
     * @param tokenB      Intermediate token.
     * @param loanAmount  Hypothetical borrow size.
     * @param routerA     DEX router for the buy leg  (tokenA → tokenB).
     * @param routerB     DEX router for the sell leg (tokenB → tokenA).
     *
     * @return profitable     True when tokenAOut > loanAmount.
     * @return expectedProfit Gross profit in tokenA units (before gas costs).
     */
    function checkProfitability(
        address tokenA,
        address tokenB,
        uint256 loanAmount,
        address routerA,
        address routerB
    ) external view returns (bool profitable, uint256 expectedProfit) {
        address[] memory pathAB = new address[](2);
        pathAB[0] = tokenA;
        pathAB[1] = tokenB;

        address[] memory pathBA = new address[](2);
        pathBA[0] = tokenB;
        pathBA[1] = tokenA;

        uint256[] memory leg1 = IUniswapV2Router(routerA).getAmountsOut(loanAmount, pathAB);
        uint256 tokenBOut     = leg1[1];

        uint256[] memory leg2 = IUniswapV2Router(routerB).getAmountsOut(tokenBOut, pathBA);
        uint256 tokenAOut     = leg2[1];

        // Balancer fee = 0  →  repayAmount == loanAmount
        if (tokenAOut > loanAmount) {
            profitable     = true;
            expectedProfit = tokenAOut - loanAmount;
        }
    }

    // --------------------------------------------------------
    //  RECEIVE ETH
    // --------------------------------------------------------

    receive() external payable {}
}
