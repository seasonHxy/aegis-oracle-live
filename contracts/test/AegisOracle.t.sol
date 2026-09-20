// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import "../src/AegisOracle.sol";
import "../src/CollateralLens.sol";

interface Vm {
    function addr(uint256) external returns (address);
    function sign(uint256, bytes32) external returns (uint8, bytes32, bytes32);
    function warp(uint256) external;
    function prank(address) external;
    function expectRevert(bytes4) external;
    function chainId(uint256) external;
}

contract AegisOracleTest {
    Vm constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    AegisOracle oracle;
    bytes32 constant FEED = keccak256("ETH/USD");
    uint256 constant KEY = 12345;

    function setUp() public {
        vm.warp(1000);
        oracle = new AegisOracle(address(this), vm.addr(KEY));
        oracle.configureFeed(FEED, 60, 100, true);
    }

    function report() internal pure returns (AegisOracle.Report memory) {
        return AegisOracle.Report(FEED, 3000e8, 1000, 1060, 1, 10);
    }

    function signature(AegisOracle.Report memory r) internal returns (bytes memory) {
        (uint8 v, bytes32 rs, bytes32 ss) = vm.sign(KEY, oracle.hashReport(r));
        return abi.encodePacked(rs, ss, v);
    }

    function testPublishAndValue() public {
        AegisOracle.Report memory r = report();
        oracle.submit(r, signature(r));
        CollateralLens lens = new CollateralLens(oracle, FEED);
        (uint256 value, uint256 borrow) = lens.quote(2e18);
        require(value == 6000e8 && borrow == 4200e8, "valuation");
    }

    function testReplayRejected() public {
        AegisOracle.Report memory r = report();
        bytes memory s = signature(r);
        oracle.submit(r, s);
        vm.expectRevert(AegisOracle.InvalidReport.selector);
        oracle.submit(r, s);
    }

    function testExpiredReadAndWriteRejected() public {
        AegisOracle.Report memory r = report();
        bytes memory s = signature(r);
        oracle.submit(r, s);
        vm.warp(1061);
        vm.expectRevert(AegisOracle.StalePrice.selector);
        oracle.latestPrice(FEED);
        r.sequence = 2;
        s = signature(r);
        vm.expectRevert(AegisOracle.InvalidReport.selector);
        oracle.submit(r, s);
    }

    function testFutureAndLongValidityRejected() public {
        AegisOracle.Report memory r = report();
        r.observedAt = 1001;
        bytes memory s = signature(r);
        vm.expectRevert(AegisOracle.InvalidReport.selector);
        oracle.submit(r, s);
        r = report();
        r.validUntil = 1061;
        s = signature(r);
        vm.expectRevert(AegisOracle.InvalidReport.selector);
        oracle.submit(r, s);
    }

    function testWrongSigner() public {
        AegisOracle.Report memory r = report();
        bytes memory s = signature(r);
        oracle.setSigner(vm.addr(987));
        vm.expectRevert(AegisOracle.InvalidSignature.selector);
        oracle.submit(r, s);
    }

    function testCrossChainReplay() public {
        AegisOracle.Report memory r = report();
        bytes memory s = signature(r);
        vm.chainId(999);
        vm.expectRevert(AegisOracle.InvalidSignature.selector);
        oracle.submit(r, s);
    }

    function testCrossContractReplay() public {
        AegisOracle.Report memory r = report();
        bytes memory s = signature(r);
        AegisOracle other = new AegisOracle(address(this), vm.addr(KEY));
        other.configureFeed(FEED, 60, 100, true);
        vm.expectRevert(AegisOracle.InvalidSignature.selector);
        other.submit(r, s);
    }

    function testPausedReadAndWrite() public {
        AegisOracle.Report memory r = report();
        bytes memory s = signature(r);
        oracle.submit(r, s);
        oracle.setPaused(true);
        vm.expectRevert(AegisOracle.Paused.selector);
        oracle.latestPrice(FEED);
        vm.expectRevert(AegisOracle.Paused.selector);
        oracle.submit(r, s);
    }

    function testUnknownFeedAndConfidence() public {
        AegisOracle.Report memory r = report();
        r.feedId = keccak256("BTC/USD");
        bytes memory s = signature(r);
        vm.expectRevert(AegisOracle.InvalidReport.selector);
        oracle.submit(r, s);
        r = report();
        r.confidenceBps = 101;
        s = signature(r);
        vm.expectRevert(AegisOracle.InvalidReport.selector);
        oracle.submit(r, s);
    }

    function testOwnershipAndPermissions() public {
        vm.prank(address(99));
        vm.expectRevert(AegisOracle.Unauthorized.selector);
        oracle.setPaused(true);
        oracle.transferOwnership(address(99));
        require(oracle.owner() == address(this));
        vm.prank(address(99));
        oracle.acceptOwnership();
        require(oracle.owner() == address(99));
    }

    function testObservationCannotGoBackwards() public {
        AegisOracle.Report memory r = report();
        oracle.submit(r, signature(r));
        r.sequence = 2;
        r.observedAt = 999;
        r.validUntil = 1059;
        bytes memory s = signature(r);
        vm.expectRevert(AegisOracle.InvalidReport.selector);
        oracle.submit(r, s);
    }

    function testConfigTighteningAffectsReads() public {
        AegisOracle.Report memory r = report();
        oracle.submit(r, signature(r));
        oracle.configureFeed(FEED, 10, 100, true);
        vm.warp(1011);
        vm.expectRevert(AegisOracle.StalePrice.selector);
        oracle.latestPrice(FEED);
    }

    function testTamperedPriceAndMalformedSignature() public {
        AegisOracle.Report memory r = report();
        bytes memory sig = signature(r);
        r.price += 1;
        vm.expectRevert(AegisOracle.InvalidSignature.selector);
        oracle.submit(r, sig);
        vm.expectRevert(AegisOracle.InvalidSignature.selector);
        oracle.submit(r, hex"1234");
    }

    function testDisabledAndAbsentFeedCannotBeRead() public {
        vm.expectRevert(AegisOracle.StalePrice.selector);
        oracle.latestPrice(FEED);
        AegisOracle.Report memory r = report();
        oracle.submit(r, signature(r));
        oracle.configureFeed(FEED, 60, 100, false);
        vm.expectRevert(AegisOracle.StalePrice.selector);
        oracle.latestPrice(FEED);
    }

    function testZeroPriceAndSequenceRejected() public {
        AegisOracle.Report memory r = report();
        r.price = 0;
        bytes memory sig = signature(r);
        vm.expectRevert(AegisOracle.InvalidReport.selector);
        oracle.submit(r, sig);
        r = report();
        r.sequence = 0;
        sig = signature(r);
        vm.expectRevert(AegisOracle.InvalidReport.selector);
        oracle.submit(r, sig);
    }

    function testMalleableSignatureRejected() public {
        AegisOracle.Report memory r = report();
        (uint8 v, bytes32 rs, bytes32 ss) = vm.sign(KEY, oracle.hashReport(r));
        uint256 order = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141;
        bytes memory sig = abi.encodePacked(rs, bytes32(order - uint256(ss)), uint8(v == 27 ? 28 : 27));
        vm.expectRevert(AegisOracle.InvalidSignature.selector);
        oracle.submit(r, sig);
    }

    function testFuzzPriceRoundTrip(uint128 price) public {
        if (price == 0) return;
        AegisOracle.Report memory r = report();
        r.price = price;
        oracle.submit(r, signature(r));
        (uint128 actual,,) = oracle.latestPrice(FEED);
        require(actual == price);
    }
}
