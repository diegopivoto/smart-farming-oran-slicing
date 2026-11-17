package br.inatel.smartfarm.farm.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import br.inatel.smartfarm.farm.core.ServiceKind
import br.inatel.smartfarm.farm.core.Wallet
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.math.BigInteger

/**
 * Payment before the slice switch: quote from the SliceMarket contract, purchase signed on the phone, then wait until
 * the operator's CHF sees the purchase with the required confirmations; only then the service session is requested.
 */
@Composable
fun PaymentScreen(wallet: Wallet, kind: ServiceKind, onPaid: () -> Unit, onCancel: () -> Unit) {
    val f = LocalFarm.current
    val scope = rememberCoroutineScope()
    var adaptive by remember { mutableStateOf(false) }
    var quote by remember { mutableStateOf<Wallet.Quote?>(null) }
    var balance by remember { mutableStateOf<BigInteger?>(null) }
    var step by remember { mutableStateOf("quote") }          // quote, signing, pending, mined, confirmed, error
    var tx by remember { mutableStateOf<String?>(null) }
    var conf by remember { mutableStateOf(0) }
    var error by remember { mutableStateOf<String?>(null) }
    var t0 by remember { mutableStateOf(0L) }
    var elapsed by remember { mutableStateOf(0.0) }

    LaunchedEffect(adaptive) {
        error = null
        runCatching { withContext(Dispatchers.IO) { wallet.quote(kind.id, adaptive) to wallet.balanceWei() } }
            .onSuccess { quote = it.first; balance = it.second }.onFailure { error = it.message; step = "error" }
    }

    fun pay() {
        val q = quote ?: return
        step = "signing"; error = null; t0 = System.currentTimeMillis()
        scope.launch {
            runCatching {
                val hash = withContext(Dispatchers.IO) { wallet.pay(q) }
                tx = hash; step = "pending"
                var block: Long? = null
                while (block == null) { delay(1500); block = withContext(Dispatchers.IO) { wallet.receiptBlock(hash) } }
                step = "mined"
                while (true) {
                    val st = withContext(Dispatchers.IO) { wallet.status() }
                    val p = st.purchases.firstOrNull { it.tx.equals(hash, ignoreCase = true) }
                    conf = p?.confirmations ?: (st.head - block + 1).toInt()
                    elapsed = (System.currentTimeMillis() - t0) / 1000.0
                    if (p != null && p.confirmations >= q.confirmations) break
                    delay(2000)
                }
                step = "confirmed"
                balance = withContext(Dispatchers.IO) { wallet.balanceWei() }
                delay(1200)
                onPaid()
            }.onFailure { error = it.message ?: it.toString(); step = "error" }
        }
    }

    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        AppBar("Buy the ${kind.sliceName} slice", onCancel)
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(bottom = 16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            FarmCard {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(kind.title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold, color = f.text)
                    Text("Slice SST 1 · SD ${kind.sd} · ${quote?.sessionSeconds?.div(60) ?: 10} min session, UN3GPP uplink + 5G downlink",
                        style = MaterialTheme.typography.bodyMedium, color = f.muted)
                    Spacer(Modifier.height(4.dp))
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        EthBadge(32.dp)
                        ValueWithUnit(quote?.let { Wallet.eth(it.amountWei) } ?: "…", "SepoliaETH", valueSize = MaterialTheme.typography.headlineMedium.fontSize)
                    }
                    Text("Price set by the operator's smart contract from the radio-resource cost of the DDQN controller.",
                        style = MaterialTheme.typography.bodySmall, color = f.muted)
                }
            }
            FarmCard {
                Row(Modifier.padding(16.dp).fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("New position (online adaptation)", color = f.text, fontWeight = FontWeight.Medium)
                        Text("Adds the cost of the agent learning a radio condition it has not seen", style = MaterialTheme.typography.bodySmall, color = f.muted)
                    }
                    Switch(checked = adaptive, onCheckedChange = { adaptive = it }, enabled = step == "quote" || step == "error")
                }
            }
            ChargeFlow()
            FarmCard {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Info("Your wallet", remember { runCatching { Wallet.short(wallet.address) }.getOrElse { "error: ${it.message}" } })
                    Info("Balance", balance?.let { Wallet.eth(it) + " ETH" } ?: "…")
                    Info("Operator", quote?.operator?.let { Wallet.short(it) } ?: "…")
                    Info("Network", "Ethereum Sepolia (test)")
                    tx?.let { Info("Transaction", Wallet.short(it)) }
                    if (step in listOf("mined", "confirmed")) Info("Confirmations", "$conf / ${quote?.confirmations ?: 2}")
                    if (step == "confirmed") Info("Paid and confirmed in", "%.1f s".format(elapsed))
                }
            }
            val (label, color) = when (step) {
                "signing" -> "Signing on the phone and sending…" to f.muted
                "pending" -> "Waiting for the transaction to be mined…" to f.sunText
                "mined" -> "Mined. Waiting for the operator's CHF to confirm the payment…" to f.sunText
                "confirmed" -> "Payment confirmed by the CHF. Opening the slice…" to f.ok
                "error" -> (error ?: "Error") to f.bad
                else -> "" to f.muted
            }
            if (label.isNotEmpty()) Text(label, color = color, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(horizontal = 4.dp))
            val enough = quote != null && balance != null && balance!! > quote!!.amountWei
            FarmButton(if (step == "error") "Try again" else "Pay ${quote?.let { Wallet.eth(it.amountWei) } ?: ""} ETH",
                { pay() }, enabled = enough && (step == "quote" || step == "error"))
            if (quote != null && balance != null && !enough) Text("Not enough balance in the wallet (Settings → Wallet).", color = f.bad,
                style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
internal fun Info(label: String, value: String) {
    val f = LocalFarm.current
    Row(Modifier.fillMaxWidth()) {
        Text(label, color = f.muted, modifier = Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
        Text(value, color = f.text, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodyMedium)
    }
}
