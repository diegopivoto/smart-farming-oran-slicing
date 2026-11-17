package br.inatel.smartfarm.farm.core

import android.content.Context
import android.net.ConnectivityManager
import org.json.JSONArray
import org.json.JSONObject
import org.web3j.abi.FunctionEncoder
import org.web3j.abi.datatypes.Bool
import org.web3j.abi.datatypes.Function
import org.web3j.abi.datatypes.generated.Bytes32
import org.web3j.abi.datatypes.generated.Uint8
import org.web3j.crypto.Credentials
import org.web3j.crypto.Keys
import org.web3j.crypto.RawTransaction
import org.web3j.crypto.TransactionEncoder
import org.web3j.utils.Numeric
import java.math.BigDecimal
import java.math.BigInteger
import java.net.HttpURLConnection
import java.net.URL

/**
 * UE wallet of the DLT slice market (Ethereum Sepolia, TEST ETH only).
 *
 * The secp256k1 key is created on the phone and never leaves it; every transaction is signed here (EIP-1559) and
 * sent through the operator's JSON-RPC relay (/api/v1/wallet/rpc), because the phone only reaches the 5G network.
 * The purchase calls SliceMarket.purchase(sliceId, keccak256(SUPI), adaptive); the core (SMF -> CHF) establishes
 * the service PDU session only after the CHF sees this purchase with enough confirmations.
 */
class Wallet(private val app: Context, private val settings: FarmSettings) {
    data class Quote(val service: String, val sliceId: Int, val adaptive: Boolean, val amountWei: BigInteger, val subscriber: String,
                     val contract: String, val chainId: Long, val operator: String, val explorer: String?,
                     val confirmations: Int, val sessionSeconds: Int)
    data class Charge(val minute: Int, val amountWei: BigInteger, val remainingWei: BigInteger, val tx: String, val block: Long)
    data class Purchase(val id: String, val tx: String, val block: Long, val time: Long, val sliceId: Int,
                        val amountWei: BigInteger, val adaptive: Boolean, val confirmations: Int, val consumed: Boolean,
                        val charges: List<Charge> = emptyList(), val metering: Boolean = false, val refundedWei: BigInteger? = null) {
        val chargedWei: BigInteger get() = charges.fold(BigInteger.ZERO) { a, c -> a + c.amountWei }
        val remainingWei: BigInteger get() = if (refundedWei != null) BigInteger.ZERO else amountWei - chargedWei
    }
    data class Status(val head: Long, val explorer: String?, val contract: String, val operator: String,
                      val prices: Map<String, String>, val purchases: List<Purchase>, val activeSessions: Int)

    private val prefs = app.getSharedPreferences("dlt_wallet", Context.MODE_PRIVATE)

    // The key is 32 bytes from SecureRandom; the public key/address are derived in pure Java (web3j Sign), so the
    // reduced BouncyCastle JCA provider of Android (no secp256k1 KeyPairGenerator) is never needed.
    val credentials: Credentials by lazy {
        val key = prefs.getString("key", null) ?: run {
            val n = BigInteger("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16)
            var k: BigInteger
            do { val b = ByteArray(32); java.security.SecureRandom().nextBytes(b); k = BigInteger(1, b) } while (k.signum() == 0 || k >= n)
            Numeric.toHexStringWithPrefixZeroPadded(k, 64).also { prefs.edit().putString("key", it).apply() }
        }
        Credentials.create(key)
    }
    val address: String get() = Keys.toChecksumAddress(credentials.address)

    private fun base() = settings.orchestratorUrl.trimEnd('/')

    private fun http(method: String, path: String, body: String? = null): String {
        val url = URL(base() + path)
        val net = app.getSystemService(ConnectivityManager::class.java).activeNetwork
        val c = (net?.openConnection(url) ?: url.openConnection()) as HttpURLConnection
        c.connectTimeout = 8000; c.readTimeout = 25000; c.requestMethod = method
        if (body != null) {
            c.doOutput = true; c.setRequestProperty("content-type", "application/json")
            c.outputStream.use { it.write(body.toByteArray()) }
        }
        val code = c.responseCode
        val text = (if (code < 400) c.inputStream else c.errorStream)?.bufferedReader()?.readText() ?: ""
        if (code >= 400) throw IllegalStateException(runCatching { JSONObject(text).getString("error") }.getOrDefault("HTTP $code"))
        return text
    }

    private fun rpc(method: String, vararg params: Any): Any {
        val req = JSONObject().put("jsonrpc", "2.0").put("id", 1).put("method", method).put("params", JSONArray(params.toList()))
        val res = JSONObject(http("POST", "/api/v1/wallet/rpc", req.toString()))
        res.optJSONObject("error")?.let { throw IllegalStateException(it.optString("message", "JSON-RPC error")) }
        return res.get("result")
    }

    private fun qty(method: String, vararg params: Any) = Numeric.decodeQuantity(rpc(method, *params) as String)

    fun balanceWei(of: String = address): BigInteger = qty("eth_getBalance", of, "latest")

    fun quote(service: String, adaptive: Boolean = false): Quote {
        val j = JSONObject(http("GET", "/api/v1/wallet/quote?service=$service&adaptive=${if (adaptive) 1 else 0}"))
        return Quote(j.getString("service"), j.getInt("slice_id"), j.getBoolean("adaptive"), BigInteger(j.getString("amount_wei")), j.getString("subscriber"),
            j.getString("contract"), j.getLong("chain_id"), j.getString("operator"), j.optString("explorer").ifEmpty { null },
            j.getInt("confirmations"), j.getInt("session_seconds"))
    }

    /** Signs and sends SliceMarket.purchase; returns the transaction hash. */
    fun pay(q: Quote): String {
        val data = FunctionEncoder.encode(Function("purchase",
            listOf(Uint8(q.sliceId.toLong()), Bytes32(Numeric.hexStringToByteArray(q.subscriber)), Bool(q.adaptive)), emptyList()))
        val nonce = qty("eth_getTransactionCount", address, "pending")
        val tip = runCatching { qty("eth_maxPriorityFeePerGas") }.getOrDefault(BigInteger.valueOf(1_500_000_000L))
        val baseFee = Numeric.decodeQuantity((rpc("eth_getBlockByNumber", "latest", false) as JSONObject).getString("baseFeePerGas"))
        val call = JSONObject().put("from", address).put("to", q.contract).put("value", Numeric.toHexStringWithPrefix(q.amountWei)).put("data", data)
        val gas = qty("eth_estimateGas", call).multiply(BigInteger.valueOf(12)).divide(BigInteger.TEN)
        val raw = RawTransaction.createTransaction(q.chainId, nonce, gas, q.contract, q.amountWei, data, tip,
            baseFee.multiply(BigInteger.TWO).add(tip))
        val signed = Numeric.toHexString(TransactionEncoder.signMessage(raw, q.chainId, credentials))
        return rpc("eth_sendRawTransaction", signed) as String
    }

    /** Block of the mined transaction, or null while pending; throws if it reverted. */
    fun receiptBlock(tx: String): Long? {
        val r = rpc("eth_getTransactionReceipt", tx)
        if (r == JSONObject.NULL) return null
        val o = r as JSONObject
        if (Numeric.decodeQuantity(o.getString("status")) != BigInteger.ONE) throw IllegalStateException("transaction reverted")
        return Numeric.decodeQuantity(o.getString("blockNumber")).toLong()
    }

    fun head(): Long = qty("eth_blockNumber").toLong()

    fun status(): Status {
        val j = JSONObject(http("GET", "/api/v1/wallet/status?address=$address"))
        val chain = j.getJSONObject("chain")
        val ps = j.getJSONArray("purchases")
        val list = (0 until ps.length()).map { i ->
            val p = ps.getJSONObject(i)
            val cs = p.optJSONArray("charges") ?: JSONArray()
            val charges = (0 until cs.length()).map { k -> cs.getJSONObject(k).let { c ->
                Charge(c.getInt("minute"), BigInteger(c.getString("amountWei")), BigInteger(c.getString("remainingWei")), c.getString("tx"), c.getLong("block")) } }
                .sortedBy { it.minute }
            Purchase(p.getString("id"), p.getString("tx"), p.getLong("block"), p.getLong("time"), p.getInt("sliceId"),
                BigInteger(p.getString("amountWei")), p.optBoolean("adaptive"), p.optInt("confirmations"), p.optBoolean("consumed"),
                charges, p.optBoolean("metering"), p.optJSONObject("closedOnChain")?.let { BigInteger(it.getString("refundedWei")) })
        }.sortedByDescending { it.block }
        val prices = j.getJSONObject("prices").let { o -> o.keys().asSequence().associateWith { o.optString(it) } }
        return Status(chain.getLong("head"), chain.optString("explorer").ifEmpty { null }, chain.getString("contract"),
            j.getString("operator"), prices, list, j.getJSONArray("sessions").length())
    }

    companion object {
        fun eth(wei: BigInteger, digits: Int = 5): String =
            BigDecimal(wei).movePointLeft(18).setScale(digits, java.math.RoundingMode.HALF_UP).stripTrailingZeros().toPlainString()
        fun short(hex: String) = if (hex.length > 14) hex.take(8) + "…" + hex.takeLast(6) else hex
        val SLICE_NAMES = mapOf(1 to "Weather station", 2 to "Field imagery", 3 to "Drone over the field")
    }
}
