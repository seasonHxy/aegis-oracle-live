// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import "../src/AegisOracle.sol";
import "../src/CollateralLens.sol";

interface ScriptVm {
    function envUint(string calldata) external returns (uint256);
    function envAddress(string calldata) external returns (address);
    function addr(uint256) external returns (address);
    function startBroadcast(uint256) external;
    function stopBroadcast() external;
}

contract Deploy {
    ScriptVm constant vm = ScriptVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function run() external returns (AegisOracle oracle, CollateralLens lens) {
        uint256 key = vm.envUint("DEPLOYER_PRIVATE_KEY");
        address reporter = vm.envAddress("ORACLE_SIGNER_ADDRESS");
        vm.startBroadcast(key);
        oracle = new AegisOracle(vm.addr(key), reporter);
        oracle.configureFeed(keccak256("ETH/USD"), 60, 100, true);
        lens = new CollateralLens(oracle, keccak256("ETH/USD"));
        vm.stopBroadcast();
    }
}
