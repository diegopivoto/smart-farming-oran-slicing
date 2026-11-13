package br.inatel.smartfarm.farm.ui

import android.os.SystemClock
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.State
import androidx.compose.runtime.getValue
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.FilterQuality
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import br.inatel.smartfarm.farm.core.DroneFrame
import br.inatel.smartfarm.farm.core.DroneUi
import br.inatel.smartfarm.farm.core.ServiceKind
import br.inatel.smartfarm.farm.core.SessionUi
import br.inatel.smartfarm.farm.protocol.SlaSnapshot
import kotlinx.coroutines.delay

@Composable
fun DroneScreen(frame: State<DroneFrame?>, drone: DroneUi, sla: SlaSnapshot?, session: SessionUi?, onBack: () -> Unit) {
    val f = LocalFarm.current
    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        AppBar("Drone over the field", onBack)
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(bottom = 16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            VideoView(frame, drone)
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Kpi("Video rate", drone.goodputBps?.let { Fmt.n(it / 1e6, 1) } ?: "—", "Mbit/s")
                Kpi("Delay", drone.delayMs?.let { Fmt.n(it, 0) } ?: "—", "ms")
                val total = drone.framesShown + drone.framesDropped
                Kpi("Frames shown", if (total > 0) Fmt.n(drone.framesShown * 100.0 / total, 1) else "—", "%")
            }
            SlaPanel(sla, session)
            OranCard(session?.oran)
        }
    }
}

@Composable
private fun VideoView(frame: State<DroneFrame?>, drone: DroneUi) {
    // Polled, not read in composition: 30 frames/s must not recompose the whole screen.
    var frozen by remember { mutableStateOf(false) }
    val hasFrame by remember { derivedStateOf { frame.value != null } }
    LaunchedEffect(Unit) {
        var lastVersion = -1L
        var lastFrameMs = SystemClock.elapsedRealtime()
        while (true) {
            delay(250)
            val v = frame.value?.version ?: -1L
            val now = SystemClock.elapsedRealtime()
            if (v != lastVersion) { lastVersion = v; lastFrameMs = now }
            frozen = v >= 0 && now - lastFrameMs > 1000
        }
    }

    Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f).clip(RoundedCornerShape(20.dp)).background(Color(0xFF0E140F))) {
        if (!hasFrame) {
            ServiceArt(ServiceKind.DRONE, Modifier.fillMaxSize())
            Box(Modifier.fillMaxSize().background(Color(0x33000000)))
        }
        // The frame is read inside the draw phase: a new frame redraws without recomposing the screen.
        Box(Modifier.fillMaxSize().drawBehind {
            val fr = frame.value ?: return@drawBehind
            val bmp = fr.bitmap
            if (bmp.isRecycled) return@drawBehind
            val img = bmp.asImageBitmap()
            val scale = maxOf(size.width / img.width, size.height / img.height)
            val w = img.width * scale
            val h = img.height * scale
            drawImage(img, IntOffset.Zero, IntSize(img.width, img.height),
                IntOffset(((size.width - w) / 2).toInt(), ((size.height - h) / 2).toInt()), IntSize(w.toInt(), h.toInt()),
                filterQuality = FilterQuality.Low)
        })
        Row(Modifier.fillMaxWidth().padding(10.dp), verticalAlignment = Alignment.CenterVertically) {
            LiveChip(hasFrame && !frozen)
            Spacer(Modifier.weight(1f))
            if (hasFrame) OverlayChip("${Fmt.n(drone.fps, 1)} fps · ${drone.framesDropped} dropped")
        }
        if (!hasFrame) Box(Modifier.align(Alignment.Center)) { OverlayChip("Waiting for the flight video…") }
        else if (frozen) Box(Modifier.align(Alignment.Center)) { OverlayChip("Weak signal — waiting for the next frame") }
        if (hasFrame) Box(Modifier.align(Alignment.BottomEnd).padding(10.dp)) {
            val res by remember { derivedStateOf { frame.value?.bitmap?.let { "${it.width}×${it.height}" } } }
            OverlayChip(res?.let { "MJPEG $it" } ?: "MJPEG")
        }
    }
}

@Composable
private fun LiveChip(live: Boolean) {
    val tr = rememberInfiniteTransition(label = "live")
    val a by tr.animateFloat(1f, .35f, infiniteRepeatable(tween(900, easing = LinearEasing), RepeatMode.Reverse), label = "a")
    Row(Modifier.height(24.dp).clip(RoundedCornerShape(12.dp)).background(Color(0x8C0A100C)).padding(horizontal = 9.dp),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        Canvas(Modifier.size(7.dp)) { drawCircle(if (live) Color(0xFFFF4D4D).copy(alpha = a) else Color(0xFFB0B0B0)) }
        Text(if (live) "LIVE" else "PAUSED", style = MaterialTheme.typography.labelSmall.copy(fontSize = 11.5.sp), color = Color.White, maxLines = 1)
    }
}

@Composable
private fun RowScope.Kpi(label: String, value: String, unit: String) {
    val f = LocalFarm.current
    FarmCard(Modifier.weight(1f)) {
        Column(Modifier.padding(12.dp)) {
            Text(label, style = MaterialTheme.typography.labelSmall.copy(fontSize = 11.5.sp), color = f.muted, maxLines = 1, overflow = TextOverflow.Ellipsis)
            ValueWithUnit(value, unit, 20.sp, modifier = Modifier.padding(top = 2.dp))
        }
    }
}
