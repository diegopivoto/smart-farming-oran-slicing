package br.inatel.smartfarm.farm.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.expandVertically
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import br.inatel.smartfarm.farm.core.Phase
import br.inatel.smartfarm.farm.core.ServiceKind
import br.inatel.smartfarm.farm.core.SessionUi
import br.inatel.smartfarm.farm.protocol.Orchestration

private enum class StepState { PENDING, ACTIVE, DONE, FAILED }

@Composable
fun ConnectingScreen(session: SessionUi?, kind: ServiceKind, onCancel: () -> Unit, onRetry: () -> Unit, onContinue: () -> Unit) {
    val f = LocalFarm.current
    val phase = session?.phase ?: Phase.REQUESTING
    val orch = session?.orch
    val orchestrated = orch?.active == true // the lab orchestrator moves the UN3GPP UE and starts the generator
    val failed = phase == Phase.UNAVAILABLE || phase == Phase.ERROR || phase == Phase.LOST ||
        (orch?.failed == true && phase != Phase.RECEIVING)
    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp).padding(bottom = 16.dp)) {
        AppBar(kind.title, onCancel)
        Column(Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState()), horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center) {
            Spacer(Modifier.height(12.dp))
            Halo(kind, active = !failed)
            Spacer(Modifier.height(22.dp))
            Text(
                if (failed) "Could not connect to the\n${kind.sliceName} slice" else "Connecting to the\n${kind.sliceName} slice…",
                style = MaterialTheme.typography.headlineSmall, color = f.text, textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(22.dp))
            FarmCard(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(horizontal = 18.dp, vertical = 8.dp)) {
                    val s1 = when {
                        session?.grantS != null -> StepState.DONE
                        phase == Phase.UNAVAILABLE -> StepState.FAILED
                        else -> StepState.ACTIVE
                    }
                    Step(1, s1, if (s1 == StepState.DONE) "Network granted" else "Requesting network",
                        when (s1) {
                            StepState.DONE -> "Granted in ${Fmt.n(session!!.grantS!!, 2)} s · cellular + ${session.caps}"
                            StepState.FAILED -> session?.error ?: "Not granted"
                            else -> "Asking Android for cellular + ${session?.caps ?: kind.defaultCaps}"
                        })
                    HorizontalDivider(color = f.line)
                    val s2 = when {
                        session?.ip != null -> StepState.DONE
                        s1 == StepState.DONE && failed -> StepState.FAILED
                        s1 == StepState.DONE -> StepState.ACTIVE
                        else -> StepState.PENDING
                    }
                    Step(2, s2, "Slice session up",
                        if (session?.ip != null) "Phone IP ${session.ip} · Slice SD ${kind.sd}" else "Waiting for the address of slice SD ${kind.sd}")
                    HorizontalDivider(color = f.line)
                    val noun = when (kind) {
                        ServiceKind.WEATHER -> "sensor data"
                        ServiceKind.CROP -> "field captures"
                        ServiceKind.DRONE -> "flight video"
                    }
                    var before = s2
                    var n = 3
                    if (orchestrated && orch != null) {
                        // real steps reported by the lab orchestrator (PC1)
                        val s3o = when {
                            orch.reached(Orchestration.IN_SLICE) -> StepState.DONE
                            orch.failed -> StepState.FAILED
                            s2 == StepState.DONE && failed -> StepState.FAILED
                            s2 == StepState.DONE -> StepState.ACTIVE
                            else -> StepState.PENDING
                        }
                        Step(n++, s3o, if (s3o == StepState.DONE) "UN3GPP UE in the slice" else "UN3GPP UE joining the slice",
                            when (s3o) {
                                StepState.DONE -> "UN3GPP UE ${orch.un3gppIp ?: "—"} · Slice SD ${kind.sd}"
                                StepState.FAILED -> orch.cause ?: session?.error ?: "Stopped"
                                StepState.ACTIVE -> when {
                                    (orch.silentS ?: 0.0) >= 5.0 -> "Waiting for the lab orchestrator (no reply for ${orch.silentS!!.toInt()} s)"
                                    orch.state == Orchestration.SWITCHING && orch.reason == Orchestration.IP_CONFLICT ->
                                        "Its address clashed with this phone's subnet: one more slice hop for a new one"
                                    orch.state == Orchestration.SWITCHING -> "Moving the UN3GPP UE to slice SD ${kind.sd}"
                                    else -> "Asking the lab orchestrator (PC1)"
                                }
                                else -> "The lab orchestrator moves it to slice SD ${kind.sd}"
                            })
                        HorizontalDivider(color = f.line)
                        val s4o = when {
                            orch.reached(Orchestration.GENERATOR_STARTED) && !(orch.failed && !orch.reached(Orchestration.STREAMING)) -> StepState.DONE
                            s3o == StepState.DONE && failed -> StepState.FAILED
                            s3o == StepState.DONE -> StepState.ACTIVE
                            else -> StepState.PENDING
                        }
                        Step(n++, s4o, when {
                                s4o != StepState.DONE -> "Starting the generator"
                                orch.reached(Orchestration.STREAMING) -> "Generator streaming"
                                else -> "Generator started"
                            },
                            when (s4o) {
                                StepState.DONE -> if (orch.reached(Orchestration.STREAMING)) "From ${orch.un3gppIp ?: "the UN3GPP UE"} to ${session?.ip ?: "the phone"}"
                                    else "Started on the UN3GPP UE · checking the slice"
                                StepState.FAILED -> orch.cause ?: session?.error ?: "Stopped"
                                else -> "$noun from the UN3GPP UE to this phone".replaceFirstChar { it.uppercase() }
                            })
                        HorizontalDivider(color = f.line)
                        before = s4o
                    }
                    val s3 = when {
                        phase == Phase.RECEIVING -> StepState.DONE
                        before == StepState.DONE && failed -> StepState.FAILED
                        before == StepState.DONE -> StepState.ACTIVE
                        else -> StepState.PENDING
                    }
                    Step(n, s3, if (s3 == StepState.DONE) "Receiving $noun" else "Receiving $noun…",
                        when (s3) {
                            StepState.FAILED -> session?.error ?: "Stopped"
                            StepState.DONE -> "First data after ${Fmt.n(session?.firstDataS ?: 0.0, 1)} s"
                            else -> "Waiting for the first data from ${session?.generator ?: "the generator"}"
                        })
                }
            }
            val waitedS = (session?.waitingS ?: 0.0) - (if (orchestrated) orch?.streamingAtS ?: Double.MAX_VALUE else 0.0)
            val slow = session?.ip != null && phase == Phase.LINK_UP && !failed && waitedS > 12
            AnimatedVisibility(slow, enter = fadeIn() + expandVertically()) {
                Column(Modifier.padding(top = 14.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(if (orchestrated) "Nothing yet. The generator on ${orch?.un3gppIp ?: "the UN3GPP UE"} was started for ${session?.ip}."
                        else "Nothing yet. Is the generator on PC3 sending to ${session?.ip}?",
                        style = MaterialTheme.typography.bodySmall, color = f.muted, textAlign = TextAlign.Center)
                    Spacer(Modifier.height(10.dp))
                    FarmButton("Open the service anyway", onContinue, filled = false, height = 46.dp)
                }
            }
            orch?.notice?.let { notice ->
                Text(notice, style = MaterialTheme.typography.bodySmall, color = f.muted, textAlign = TextAlign.Center,
                    modifier = Modifier.padding(top = 12.dp))
            }
            if (!session?.ipInRange.let { it ?: true }) {
                Text("The IP is outside the expected range 10.45.${kind.octetLo}–${kind.octetHi}.x: the wrong APN may have answered.",
                    style = MaterialTheme.typography.bodySmall, color = f.bad, textAlign = TextAlign.Center, modifier = Modifier.padding(top = 12.dp))
            }
        }
        if (failed) {
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                FarmButton("Back", onCancel, Modifier.weight(1f), filled = false)
                FarmButton("Try again", onRetry, Modifier.weight(1f), icon = FarmIcons.Refresh)
            }
        } else {
            FarmButton("Cancel", onCancel, filled = false)
        }
    }
}

@Composable
private fun Halo(kind: ServiceKind, active: Boolean) {
    val f = LocalFarm.current
    val (bg, fg) = accent(kind)
    val tr = rememberInfiniteTransition(label = "halo")
    val p by tr.animateFloat(0f, 1f, infiniteRepeatable(tween(2200, easing = LinearEasing), RepeatMode.Restart), label = "p")
    Box(Modifier.size(200.dp), contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) {
            val r = size.minDimension / 2
            drawCircle(bg.copy(alpha = .25f), r)
            drawCircle(bg.copy(alpha = .5f), r * 0.82f)
            drawCircle(bg, r * 0.64f)
            if (active) {
                for (k in 0..1) {
                    val q = (p + k * 0.5f) % 1f
                    drawCircle(fg.copy(alpha = (1 - q) * 0.35f), r * (0.64f + 0.36f * q), center = Offset(size.width / 2, size.height / 2),
                        style = androidx.compose.ui.graphics.drawscope.Stroke(2.dp.toPx()))
                }
            }
        }
        ServiceArt(kind, Modifier.width(150.dp).height(52.dp).clip(RoundedCornerShape(16.dp)))
    }
}

@Composable
private fun Step(n: Int, state: StepState, label: String, detail: String) {
    val f = LocalFarm.current
    Row(Modifier.fillMaxWidth().padding(vertical = 12.dp), verticalAlignment = Alignment.Top) {
        Box(Modifier.size(30.dp).clip(CircleShape).background(
            when (state) { StepState.DONE -> f.ok; StepState.FAILED -> f.bad; StepState.ACTIVE -> f.sunC; StepState.PENDING -> f.surface2 }),
            contentAlignment = Alignment.Center) {
            when (state) {
                StepState.DONE -> Icon(FarmIcons.Check, null, Modifier.size(16.dp), tint = Color.White)
                StepState.FAILED -> Icon(FarmIcons.Close, null, Modifier.size(15.dp), tint = Color.White)
                StepState.ACTIVE -> CircularProgressIndicator(Modifier.size(18.dp), color = f.sunText, strokeWidth = 2.dp)
                StepState.PENDING -> Text("$n", style = MaterialTheme.typography.labelMedium, color = f.muted)
            }
        }
        Spacer(Modifier.width(14.dp))
        Column(Modifier.weight(1f)) {
            Text(label, style = MaterialTheme.typography.titleSmall.copy(fontWeight = androidx.compose.ui.text.font.FontWeight.SemiBold),
                color = if (state == StepState.PENDING) f.muted else f.text)
            Text(detail, style = MaterialTheme.typography.bodySmall.copy(fontSize = androidx.compose.ui.unit.TextUnit(13f, androidx.compose.ui.unit.TextUnitType.Sp)),
                color = if (state == StepState.FAILED) f.bad else f.muted, modifier = Modifier.padding(top = 2.dp))
        }
    }
}
