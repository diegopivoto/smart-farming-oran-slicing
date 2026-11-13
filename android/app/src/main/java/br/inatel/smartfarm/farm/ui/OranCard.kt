package br.inatel.smartfarm.farm.ui

import androidx.compose.animation.animateContentSize
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import br.inatel.smartfarm.farm.protocol.Orchestration

/**
 * Read-only O-RAN card: the radio action in force for this slice (UN3GPP uplink, OAI downlink), who set it and the
 * E2 verdict read back; tap for the read-backs and the recent decisions. Shown only while the orchestrator reports it.
 */
@Composable
fun OranCard(oran: Orchestration.Oran?, modifier: Modifier = Modifier, initiallyExpanded: Boolean = false) {
    if (oran == null) return
    val f = LocalFarm.current
    var expanded by rememberSaveable { mutableStateOf(initiallyExpanded) }
    FarmCard(modifier.fillMaxWidth(), onClick = { expanded = !expanded }) {
        Column(Modifier.animateContentSize().padding(start = 16.dp, end = 16.dp, top = 12.dp, bottom = 12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("O-RAN control", style = MaterialTheme.typography.titleSmall, color = f.text, maxLines = 1,
                    overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
                when {
                    oran.ddqn -> Pill("DDQN agent", f.greenC, f.green, height = 24.dp)
                    oran.modeEffective != null -> Pill("RAN defaults", f.surface2, f.muted, height = 24.dp)
                    oran.byE2 -> Pill("E2 action", f.greenC, f.green, height = 24.dp)
                    else -> Pill("RAN defaults", f.surface2, f.muted, height = 24.dp)
                }
            }
            modeLine(oran)?.let {
                Text(it, style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.5.sp), color = f.muted,
                    modifier = Modifier.padding(top = 2.dp))
            }
            Spacer(Modifier.height(6.dp))
            LinkRow("UN3GPP uplink", oran.uplink)
            LinkRow("OAI downlink", oran.downlink)
            if (expanded) {
                listOfNotNull(oran.uplink?.readback, oran.downlink?.readback).forEach {
                    Text(it, style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.5.sp), color = f.muted,
                        modifier = Modifier.padding(top = 2.dp))
                }
                Text("Set by: ${oran.decidedBy}", style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.5.sp), color = f.muted,
                    modifier = Modifier.padding(top = 2.dp))
                if (oran.history.isNotEmpty()) {
                    Text("Recent decisions", style = MaterialTheme.typography.labelMedium, color = f.text,
                        modifier = Modifier.padding(top = 10.dp, bottom = 2.dp))
                    oran.history.forEachIndexed { i, d ->
                        if (i > 0) HorizontalDivider(color = f.line, thickness = 1.dp)
                        // Full text on its own line and the outcome below it: a decision is never cut with an ellipsis.
                        Row(Modifier.fillMaxWidth().padding(vertical = 5.dp), verticalAlignment = Alignment.Top) {
                            Box(Modifier.padding(top = 5.dp)) { StatusDot(d.ok) }
                            Spacer(Modifier.width(8.dp))
                            Column(Modifier.weight(1f)) {
                                Text(d.value, style = MaterialTheme.typography.bodySmall.copy(fontSize = 12.sp), color = f.text)
                                Text(listOfNotNull(d.status, d.ageS?.let { ago(it) }).joinToString(" · "),
                                    style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.sp), color = statusColor(d.ok))
                            }
                        }
                    }
                }
            } else {
                Text("Tap for read-backs and recent decisions", style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.sp),
                    color = f.muted, modifier = Modifier.padding(top = 4.dp))
            }
        }
    }
}

@Composable
private fun LinkRow(name: String, link: Orchestration.OranLink?) {
    val f = LocalFarm.current
    // Name, the full action (wrapping if needed) and the outcome on the line below: nothing is cut with an ellipsis.
    Column(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
        Text(name, style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.5.sp), color = f.muted, maxLines = 1)
        Text(link?.label ?: "—", style = MaterialTheme.typography.labelMedium.copy(fontSize = 13.5.sp, fontWeight = FontWeight.SemiBold),
            color = f.text)
        if (link != null) {
            val status = when {
                link.stale -> "no recent read-back"
                link.lastStatus != null -> listOfNotNull(link.lastStatus, link.lastAgeS?.let { ago(it) }).joinToString(" · ")
                else -> null
            }
            if (status != null) {
                Row(Modifier.padding(top = 2.dp), verticalAlignment = Alignment.CenterVertically) {
                    StatusDot(if (link.stale) null else link.lastOk)
                    Spacer(Modifier.width(6.dp))
                    Text(status, style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.sp),
                        color = if (link.stale) f.muted else statusColor(link.lastOk))
                }
            }
        }
    }
}

@Composable
private fun StatusDot(ok: Boolean?) {
    Box(Modifier.size(8.dp).clip(CircleShape).background(statusColor(ok)))
}

@Composable
private fun statusColor(ok: Boolean?): Color {
    val f = LocalFarm.current
    return when (ok) { true -> f.ok; false -> f.bad; null -> f.muted }
}

/** One line on the radio-control mode: what is in force and, when it differs from the request, why. */
internal fun modeLine(o: Orchestration.Oran): String? {
    val effective = o.modeEffective ?: return null
    fun name(m: String?) = when (m) { Orchestration.ORAN_DDQN -> "DDQN agent"; Orchestration.ORAN_RAN_DEFAULT -> "RAN defaults"; else -> m ?: "?" }
    return when {
        effective == Orchestration.ORAN_DDQN -> listOfNotNull("Mode: DDQN agent", o.agentStep?.let { "step $it" },
            o.lastVerdict?.let { if (it in setOf("met", "not met", "undetermined")) "last step: SLA $it" else "last step: $it" })
            .joinToString(" · ")
        effective == "starting" -> "Mode: DDQN agent starting${o.modeDetail?.let { " ($it)" } ?: ""}"
        o.modeRequested != null && o.modeRequested != effective -> "Mode: ${name(effective)} (requested ${name(o.modeRequested)}: ${o.modeDetail ?: "unavailable"})"
        else -> "Mode: ${name(effective)}"
    }
}

private fun ago(s: Double): String = when {
    s < 60 -> "${s.toInt()} s ago"
    s < 3600 -> "${(s / 60).toInt()} min ago"
    else -> "${(s / 3600).toInt()} h ago"
}
