package br.inatel.smartfarm.farm.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.foundation.layout.height
import androidx.compose.material3.LinearProgressIndicator
import kotlinx.coroutines.delay
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import br.inatel.smartfarm.farm.core.Wallet
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.math.BigInteger
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** UE wallet: address, balances of the UE and of the operator, and every slice purchase with its on-chain proof. */
@Composable
fun WalletScreen(wallet: Wallet, onBack: () -> Unit) {
    val f = LocalFarm.current
    val ctx = LocalContext.current
    var refresh by remember { mutableIntStateOf(0) }
    var ue by remember { mutableStateOf<BigInteger?>(null) }
    var op by remember { mutableStateOf<BigInteger?>(null) }
    var st by remember { mutableStateOf<Wallet.Status?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val address = remember { runCatching { wallet.address }.getOrElse { e -> "wallet error: ${e.message}" } }

    LaunchedEffect(refresh) {                 // live: refreshes every 3 s while the screen is open
        while (true) {
            runCatching {
                withContext(Dispatchers.IO) { val s = wallet.status(); Triple(wallet.balanceWei(), wallet.balanceWei(s.operator), s) }
            }.onSuccess { ue = it.first; op = it.second; st = it.third; error = null }.onFailure { error = it.message }
            delay(3000)
        }
    }

    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        AppBar("Wallet", onBack)
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(bottom = 16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            FarmCard(onClick = {
                ctx.getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("wallet", address))
            }) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    WalletHeader({ PhoneGlyph(26.dp) }, "UE wallet · OnePlus 8T", "Ethereum Sepolia (test ETH)")
                    Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        EthBadge(30.dp)
                        ValueWithUnit(ue?.let { Wallet.eth(it, 6) } ?: "…", "ETH", valueSize = MaterialTheme.typography.headlineMedium.fontSize)
                    }
                    Text(address, fontFamily = FontFamily.Monospace, color = f.text, style = MaterialTheme.typography.bodySmall)
                    Text("Tap to copy the address", color = f.muted, style = MaterialTheme.typography.bodySmall)
                }
            }
            st?.purchases?.firstOrNull { it.metering }?.let { live ->
                FarmCard(color = f.greenC) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text("Live slice session · ${Wallet.SLICE_NAMES[live.sliceId] ?: ""}", color = f.green, fontWeight = FontWeight.SemiBold)
                        ChargeFlow()
                        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            CoinStack(32.dp)
                            ValueWithUnit(Wallet.eth(live.remainingWei, 6), "ETH credit left", valueSize = MaterialTheme.typography.headlineMedium.fontSize)
                        }
                        val frac = if (live.amountWei.signum() == 0) 0f else live.remainingWei.toFloat() / live.amountWei.toFloat()
                        LinearProgressIndicator(progress = { frac }, modifier = Modifier.fillMaxWidth().height(8.dp), color = f.green, trackColor = f.track)
                        Info("Charged by the CHF", "${live.charges.size} / 10 min · ${Wallet.eth(live.chargedWei, 6)} ETH")
                        live.charges.lastOrNull()?.let { Info("Last charge", "min ${it.minute} · ${Wallet.short(it.tx)}") }
                        Text("One tenth of the credit per minute goes to the operator; the rest returns when you leave the service.",
                            color = f.muted, style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
            FarmCard {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text("Operator · private 5G core", color = f.text, fontWeight = FontWeight.SemiBold)
                            Text("receives every slice payment", color = f.muted, style = MaterialTheme.typography.bodySmall)
                        }
                        InatelLogo(84.dp)
                    }
                    Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        EthBadge(26.dp)
                        ValueWithUnit(op?.let { Wallet.eth(it, 6) } ?: "…", "ETH", valueSize = MaterialTheme.typography.titleLarge.fontSize)
                    }
                    Info("Account", st?.operator?.let { Wallet.short(it) } ?: "…")
                    Info("Slice market", st?.contract?.let { Wallet.short(it) } ?: "…")
                    Info("Chain head", st?.head?.toString() ?: "…")
                    st?.prices?.forEach { (k, v) -> Info("Price · $k", "$v ETH") }
                }
            }
            Text("Purchases", style = MaterialTheme.typography.titleMedium, color = f.text, modifier = Modifier.padding(horizontal = 4.dp))
            val list = st?.purchases.orEmpty()
            if (st != null && list.isEmpty()) Text("No purchase yet.", color = f.muted, modifier = Modifier.padding(horizontal = 4.dp))
            val fmt = SimpleDateFormat("dd/MM HH:mm:ss", Locale.US)
            list.forEach { p ->
                FarmCard {
                    Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                        Row(Modifier.fillMaxWidth()) {
                            Text("#${p.id} · ${Wallet.SLICE_NAMES[p.sliceId] ?: "slice ${p.sliceId}"}${if (p.adaptive) " · adaptation" else ""}",
                                color = f.text, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f))
                            Text("−${Wallet.eth(p.amountWei)} ETH", color = f.bad, fontWeight = FontWeight.SemiBold)
                        }
                        Info("Time", fmt.format(Date(p.time * 1000)))
                        Info("Tx", Wallet.short(p.tx))
                        Info("Block", "${p.block} · ${p.confirmations} conf.")
                        Info("Charging (CHF)", when {
                            p.metering -> "live: ${p.charges.size}/10 min charged"
                            p.refundedWei != null -> "closed · used ${Wallet.eth(p.chargedWei)} · refunded ${Wallet.eth(p.refundedWei)}"
                            p.consumed -> "used by a 5G session"
                            else -> "not used yet" })
                        p.charges.takeLast(3).forEach { c -> Info("  min ${c.minute}", "−${Wallet.eth(c.amountWei, 6)} · ${Wallet.short(c.tx)}") }
                    }
                }
            }
            st?.explorer?.let {
                Text("Every balance and transaction can be checked on $it/address/$address", color = f.muted,
                    style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(horizontal = 4.dp))
            }
            error?.let { Text(it, color = f.bad, modifier = Modifier.padding(horizontal = 4.dp)) }
            Text("Updates every 3 s", color = f.muted, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(horizontal = 4.dp))
        }
    }
}
