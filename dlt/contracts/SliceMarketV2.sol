// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title SliceMarketV2: metered (pay-as-you-go) slice charging on Ethereum
/// @notice The UE buys a session credit (same purchase call and event as v1). While the 5G session is active the CHF
///         (charging agent) debits one tenth of the credit per minute to the operator; on release it closes the credit
///         and the unused part returns to the UE. If the CHF disappears, the UE can recover the rest after expiry.
contract SliceMarketV2 {
    address public immutable owner;
    address payable public immutable operator;
    address public immutable agent;                      // CHF signer
    uint32 public constant SESSION_SECONDS = 600;
    uint8 public constant SESSION_MINUTES = 10;

    struct Credit { address payable buyer; uint8 sliceId; bool adaptive; bool closed; uint8 minutes_;
                    uint64 start; uint128 amount; uint128 remaining; }

    mapping(uint8 => uint256) public price;              // per 10-min session, wei (1 weather, 2 crop, 3 drone)
    mapping(uint8 => uint256) public adaptationFee;
    mapping(uint256 => Credit) public credits;
    uint256 public purchases;

    event SlicePurchased(uint256 indexed purchaseId, address indexed buyer, bytes32 indexed subscriber, uint8 sliceId,
                         bool adaptive, uint256 amount, uint32 sessionSeconds);
    event Charged(uint256 indexed purchaseId, uint8 minute, uint256 amount, uint256 remaining);
    event Closed(uint256 indexed purchaseId, uint256 used, uint256 refunded);
    event PriceSet(uint8 indexed sliceId, uint256 price, uint256 adaptationFee);

    constructor(address payable operator_, address agent_, uint256[3] memory prices, uint256[3] memory fees) {
        require(operator_ != address(0) && agent_ != address(0), "address");
        owner = msg.sender; operator = operator_; agent = agent_;
        for (uint8 i = 0; i < 3; i++) {
            price[i + 1] = prices[i]; adaptationFee[i + 1] = fees[i];
            emit PriceSet(i + 1, prices[i], fees[i]);
        }
    }

    function quote(uint8 sliceId, bool adaptive) public view returns (uint256) {
        require(price[sliceId] != 0, "slice");
        return price[sliceId] + (adaptive ? adaptationFee[sliceId] : 0);
    }

    /// @param subscriber keccak256 of the SUPI (IMSI)
    function purchase(uint8 sliceId, bytes32 subscriber, bool adaptive) external payable returns (uint256 id) {
        require(msg.value == quote(sliceId, adaptive), "amount");
        id = ++purchases;
        credits[id] = Credit(payable(msg.sender), sliceId, adaptive, false, 0, uint64(block.timestamp),
                             uint128(msg.value), uint128(msg.value));
        emit SlicePurchased(id, msg.sender, subscriber, sliceId, adaptive, msg.value, SESSION_SECONDS);
    }

    /// one minute of an active session: 1/10 of the credit (the last minute takes the remainder)
    function charge(uint256 id) external {
        require(msg.sender == agent, "agent");
        Credit storage c = credits[id];
        require(c.amount != 0 && !c.closed && c.minutes_ < SESSION_MINUTES, "credit");
        uint128 amt = c.minutes_ + 1 == SESSION_MINUTES ? c.remaining : c.amount / SESSION_MINUTES;
        c.remaining -= amt; c.minutes_ += 1;
        (bool ok, ) = operator.call{value: amt}("");
        require(ok, "transfer");
        emit Charged(id, c.minutes_, amt, c.remaining);
    }

    /// session released (CHF) or, after expiry plus 10 min, recovered by the UE itself
    function close(uint256 id) external {
        Credit storage c = credits[id];
        require(c.amount != 0 && !c.closed, "credit");
        require(msg.sender == agent || (msg.sender == c.buyer && block.timestamp > c.start + SESSION_SECONDS + 600), "auth");
        c.closed = true;
        uint128 refund = c.remaining; c.remaining = 0;
        if (refund > 0) { (bool ok, ) = c.buyer.call{value: refund}(""); require(ok, "refund"); }
        emit Closed(id, c.amount - refund, refund);
    }
}
