// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Single-authorized-signer price oracle. Prices use 8 decimals.
/// @dev Signature verification authenticates the reporter, not the truth of market data.
contract AegisOracle {
    struct Report {
        bytes32 feedId;
        uint128 price;
        uint64 observedAt;
        uint64 validUntil;
        uint64 sequence;
        uint32 confidenceBps;
    }

    struct FeedConfig {
        uint64 maxAge;
        uint32 maxConfidenceBps;
        bool enabled;
    }
    bytes32 public constant REPORT_TYPEHASH = keccak256(
        "Report(bytes32 feedId,uint128 price,uint64 observedAt,uint64 validUntil,uint64 sequence,uint32 confidenceBps)"
    );
    bytes32 private constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    uint256 private constant HALF_ORDER = 0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0;
    uint8 public constant decimals = 8;
    address public owner;
    address public pendingOwner;
    address public signer;
    bool public paused;
    mapping(bytes32 => FeedConfig) public feeds;
    mapping(bytes32 => Report) private reports;

    error Unauthorized();
    error InvalidConfig();
    error InvalidReport();
    error InvalidSignature();
    error StalePrice();
    error Paused();
    event PriceUpdated(bytes32 indexed feedId, uint128 price, uint64 observedAt, uint64 sequence, uint64 validUntil);
    event FeedConfigured(bytes32 indexed feedId, uint64 maxAge, uint32 maxConfidenceBps, bool enabled);
    event SignerChanged(address indexed signer);
    event PauseChanged(bool paused);
    event OwnershipProposed(address indexed candidate);
    event OwnershipTransferred(address indexed owner);

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    constructor(address initialOwner, address initialSigner) {
        if (initialOwner == address(0) || initialSigner == address(0)) revert InvalidConfig();
        owner = initialOwner;
        signer = initialSigner;
    }

    function domainSeparator() public view returns (bytes32) {
        return
            keccak256(
                abi.encode(DOMAIN_TYPEHASH, keccak256("AegisOracle"), keccak256("1"), block.chainid, address(this))
            );
    }

    function hashReport(Report calldata r) public view returns (bytes32) {
        bytes32 h = keccak256(
            abi.encode(REPORT_TYPEHASH, r.feedId, r.price, r.observedAt, r.validUntil, r.sequence, r.confidenceBps)
        );
        return keccak256(abi.encodePacked("\x19\x01", domainSeparator(), h));
    }

    function submit(Report calldata r, bytes calldata signature) external {
        if (paused) revert Paused();
        FeedConfig memory f = feeds[r.feedId];
        if (
            !f.enabled || r.price == 0 || r.observedAt == 0 || r.observedAt > block.timestamp
                || r.validUntil < block.timestamp || r.validUntil <= r.observedAt
                || r.validUntil - r.observedAt > f.maxAge || r.confidenceBps > f.maxConfidenceBps
                || r.sequence <= reports[r.feedId].sequence || r.observedAt < reports[r.feedId].observedAt
        ) revert InvalidReport();
        if (signature.length != 65) revert InvalidSignature();
        bytes32 rs;
        bytes32 ss;
        uint8 v;
        assembly {
            rs := calldataload(signature.offset)
            ss := calldataload(add(signature.offset, 32))
            v := byte(0, calldataload(add(signature.offset, 64)))
        }
        if (uint256(ss) > HALF_ORDER || (v != 27 && v != 28)) revert InvalidSignature();
        address recovered = ecrecover(hashReport(r), v, rs, ss);
        if (recovered == address(0) || recovered != signer) revert InvalidSignature();
        reports[r.feedId] = r;
        emit PriceUpdated(r.feedId, r.price, r.observedAt, r.sequence, r.validUntil);
    }

    /// @notice Checked consumer interface. Reverts on disabled, paused, absent or expired data.
    function latestPrice(bytes32 feedId) external view returns (uint128 price, uint64 observedAt, uint64 sequence) {
        if (paused) revert Paused();
        FeedConfig memory f = feeds[feedId];
        Report memory r = reports[feedId];
        if (
            !f.enabled || r.price == 0 || block.timestamp > r.validUntil || block.timestamp - r.observedAt > f.maxAge
                || r.confidenceBps > f.maxConfidenceBps
        ) revert StalePrice();
        return (r.price, r.observedAt, r.sequence);
    }

    /// @notice Unchecked metadata for publishers and monitoring, never use for collateral valuation.
    function latestReport(bytes32 feedId) external view returns (Report memory) {
        return reports[feedId];
    }

    function configureFeed(bytes32 feedId, uint64 maxAge, uint32 maxConfidenceBps, bool enabled) external onlyOwner {
        if (feedId == bytes32(0) || maxAge == 0 || maxAge > 1 days || maxConfidenceBps > 10000) revert InvalidConfig();
        feeds[feedId] = FeedConfig(maxAge, maxConfidenceBps, enabled);
        emit FeedConfigured(feedId, maxAge, maxConfidenceBps, enabled);
    }

    function setSigner(address next) external onlyOwner {
        if (next == address(0)) revert InvalidConfig();
        signer = next;
        emit SignerChanged(next);
    }

    function setPaused(bool value) external onlyOwner {
        paused = value;
        emit PauseChanged(value);
    }

    function transferOwnership(address candidate) external onlyOwner {
        if (candidate == address(0)) revert InvalidConfig();
        pendingOwner = candidate;
        emit OwnershipProposed(candidate);
    }

    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert Unauthorized();
        owner = msg.sender;
        pendingOwner = address(0);
        emit OwnershipTransferred(msg.sender);
    }
}
