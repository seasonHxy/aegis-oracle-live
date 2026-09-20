// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import "./AegisOracle.sol";

/// @notice Read-only borrowing-capacity demonstration. Does not accept deposits or issue loans.
contract CollateralLens {
    AegisOracle public immutable oracle;
    bytes32 public immutable feedId;
    uint256 public constant LTV_BPS = 7000;

    constructor(AegisOracle oracle_, bytes32 feedId_) {
        oracle = oracle_;
        feedId = feedId_;
    }

    /// @param collateralWei Amount of 18-decimal collateral.
    /// @return valueUsd8 USD valuation in 8 decimals.
    /// @return maxBorrowUsd8 Illustrative 70% LTV borrowing capacity in 8 decimals.
    function quote(uint128 collateralWei) external view returns (uint256 valueUsd8, uint256 maxBorrowUsd8) {
        (uint128 price,,) = oracle.latestPrice(feedId);
        valueUsd8 = uint256(collateralWei) * uint256(price) / 1e18;
        maxBorrowUsd8 = valueUsd8 * LTV_BPS / 10000;
    }
}
