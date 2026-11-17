// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title SliceMarket: on-chain price table and payment ledger of the 5G slices
/// @notice The UE pays for one service session of a slice; the amount goes straight to the operator account.
///         The 5G CHF reads the SlicePurchased event (after N confirmations) before granting the charging quota.
contract SliceMarket {
    address public immutable owner;
    address payable public immutable operator;
    uint32 public constant SESSION_SECONDS = 600;

    /// price per session and online-adaptation surcharge per slice (1 weather, 2 crop, 3 drone), in wei
    mapping(uint8 => uint256) public price;
    mapping(uint8 => uint256) public adaptationFee;
    uint256 public purchases;

    event SlicePurchased(uint256 indexed purchaseId, address indexed buyer, bytes32 indexed subscriber, uint8 sliceId,
                         bool adaptive, uint256 amount, uint32 sessionSeconds);
    event PriceSet(uint8 indexed sliceId, uint256 price, uint256 adaptationFee);

    constructor(address payable operator_, uint256[3] memory prices, uint256[3] memory fees) {
        require(operator_ != address(0), "operator");
        owner = msg.sender;
        operator = operator_;
        for (uint8 i = 0; i < 3; i++) {
            price[i + 1] = prices[i];
            adaptationFee[i + 1] = fees[i];
            emit PriceSet(i + 1, prices[i], fees[i]);
        }
    }

    function setPrice(uint8 sliceId, uint256 newPrice, uint256 newFee) external {
        require(msg.sender == owner, "owner");
        require(price[sliceId] != 0, "slice");
        price[sliceId] = newPrice;
        adaptationFee[sliceId] = newFee;
        emit PriceSet(sliceId, newPrice, newFee);
    }

    function quote(uint8 sliceId, bool adaptive) public view returns (uint256) {
        require(price[sliceId] != 0, "slice");
        return price[sliceId] + (adaptive ? adaptationFee[sliceId] : 0);
    }

    /// @param subscriber keccak256 of the SUPI (IMSI), so the IMSI itself is not published
    function purchase(uint8 sliceId, bytes32 subscriber, bool adaptive) external payable returns (uint256 id) {
        require(msg.value == quote(sliceId, adaptive), "amount");
        id = ++purchases;
        (bool ok, ) = operator.call{value: msg.value}("");
        require(ok, "transfer");
        emit SlicePurchased(id, msg.sender, subscriber, sliceId, adaptive, msg.value, SESSION_SECONDS);
    }
}
