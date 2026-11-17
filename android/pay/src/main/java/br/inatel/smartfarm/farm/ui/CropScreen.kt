package br.inatel.smartfarm.farm.ui

import androidx.compose.animation.Crossfade
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import br.inatel.smartfarm.farm.core.Capture
import br.inatel.smartfarm.farm.core.CropUi
import br.inatel.smartfarm.farm.core.SessionUi
import br.inatel.smartfarm.farm.protocol.SlaSnapshot
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

private val Overlay = Color(0x8C0A100C)

@Composable
fun CropScreen(crop: CropUi, sla: SlaSnapshot?, session: SessionUi?, onBack: () -> Unit) {
    val f = LocalFarm.current
    var selectedId by rememberSaveable { mutableStateOf<Long?>(null) } // null = follow the newest capture
    var ndvi by rememberSaveable { mutableStateOf(false) }
    val selected = crop.captures.firstOrNull { it.objectId == selectedId } ?: crop.captures.firstOrNull()
    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        AppBar("Field imagery", onBack) {
            selected?.meta?.let { m ->
                val (label, bg, fg) = when (m.state) {
                    "pest" -> Triple("Pest hotspot", f.sunC, f.sunText)
                    "water_stress" -> Triple("Water stress", f.soilC, f.soil)
                    else -> Triple("Healthy", f.greenC, f.green)
                }
                Pill(label, bg, fg)
            }
        }
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(bottom = 16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            FarmCard(Modifier.fillMaxWidth()) {
                CaptureView(selected, ndvi)
                Column(Modifier.padding(start = 14.dp, end = 14.dp, top = 12.dp, bottom = 14.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Segmented(listOf("True color", "Vigor map (NDVI)"), if (ndvi) 1 else 0) { ndvi = it == 1 }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text("Average vigor (NDVI)", style = MaterialTheme.typography.labelSmall, color = f.muted, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            val v = selected?.meta?.ndviMean
                            val anim by animateFloatAsState((v ?: 0.0).toFloat(), tween(700), label = "ndvi")
                            ValueWithUnit(if (v == null) "—" else Fmt.n(anim.toDouble(), 2), "", 22.sp)
                        }
                        Column(Modifier.weight(1.3f)) {
                            val v = selected?.meta?.ndviMean
                            MeterBar(0f, null, Brush.horizontalGradient(listOf(Color(0xFFD7302A), Color(0xFFF4D03F), Color(0xFF1E8E3E))),
                                marker = v?.toFloat()?.coerceIn(0f, 1f), markerColor = f.text, height = 10.dp)
                            Row {
                                Text("Stressed", style = MaterialTheme.typography.bodySmall.copy(fontSize = 10.5.sp), color = f.muted, modifier = Modifier.weight(1f))
                                Text("Vigorous", style = MaterialTheme.typography.bodySmall.copy(fontSize = 10.5.sp), color = f.muted)
                            }
                        }
                    }
                }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Recent captures", style = MaterialTheme.typography.titleSmall.copy(fontSize = 13.sp), color = f.text, modifier = Modifier.weight(1f))
                val p = crop.inFlight
                Text(if (p != null) "Receiving #${p.objectId} · ${(p.received * 100 / maxOf(1, p.size))} %" else if (crop.captures.isEmpty()) "Waiting…" else "Up to date",
                    style = MaterialTheme.typography.labelSmall, color = f.muted, maxLines = 1, softWrap = false)
            }
            val frac by animateFloatAsState(crop.inFlight?.let { it.received.toFloat() / maxOf(1L, it.size) } ?: 0f, tween(250), label = "prog")
            MeterBar(frac, Brush.horizontalGradient(listOf(f.green2, f.green2)), Brush.horizontalGradient(listOf(f.track, f.track)), height = 6.dp)
            if (crop.captures.isNotEmpty()) {
                LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(crop.captures, key = { it.objectId }) { c ->
                        val on = c.objectId == selected?.objectId
                        val bmp = if (ndvi) c.ndviThumb ?: c.thumb else c.thumb
                        Box(Modifier.size(64.dp, 44.dp).clip(RoundedCornerShape(10.dp))
                            .border(2.dp, if (on) f.green else Color.Transparent, RoundedCornerShape(10.dp))
                            .let { m -> m }) {
                            Surface(onClick = { selectedId = if (c.objectId == crop.captures.first().objectId) null else c.objectId },
                                color = f.surface2, modifier = Modifier.fillMaxSize()) {
                                if (bmp != null) {
                                    val img = remember(bmp) { bmp.asImageBitmap() }
                                    Image(img, "Capture ${c.objectId}", contentScale = ContentScale.Crop, modifier = Modifier.fillMaxSize())
                                }
                            }
                        }
                    }
                }
            }
            if (crop.rejected > 0) {
                FarmCard(Modifier.fillMaxWidth(), color = f.badC) {
                    Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                        IconTile(FarmIcons.Warning, Color.White.copy(alpha = .7f), f.bad)
                        Spacer(Modifier.width(10.dp))
                        Text("${crop.rejected} capture(s) failed the SHA-256 check and were discarded (last #${crop.lastRejectedId}).",
                            style = MaterialTheme.typography.bodySmall, color = f.bad)
                    }
                }
            }
            SlaPanel(sla, session)
            OranCard(session?.oran)
        }
    }
}

@Composable
private fun CaptureView(c: Capture?, ndvi: Boolean) {
    val f = LocalFarm.current
    val ratio = c?.meta?.let { if (it.width > 0 && it.height > 0) it.width.toFloat() / it.height else null }
        ?: c?.rgb?.let { it.width.toFloat() / it.height } ?: (16f / 9f)
    BoxWithConstraints(Modifier.fillMaxWidth().aspectRatio(ratio.coerceIn(1f, 2.4f)).background(Color(0xFF5E9E45))) {
        if (c == null) {
            val tr = rememberInfiniteTransition(label = "scan")
            val t by tr.animateFloat(0f, 1f, infiniteRepeatable(tween(2600, easing = LinearEasing), RepeatMode.Restart), label = "t")
            Canvas(Modifier.fillMaxSize()) { drawFieldPlaceholder(t) }
            Box(Modifier.align(Alignment.Center)) { OverlayChip("Waiting for the first capture…", icon = FarmIcons.Camera) }
        } else {
            Crossfade(targetState = (if (ndvi) c.ndvi ?: c.rgb else c.rgb) to c.objectId, animationSpec = tween(350), label = "img") { (bmp, _) ->
                if (bmp != null) {
                    val img = remember(bmp) { bmp.asImageBitmap() }
                    Image(img, "Field capture", contentScale = ContentScale.Crop, modifier = Modifier.fillMaxSize())
                }
            }
            val w = maxWidth
            val h = maxHeight
            c.meta?.zones?.forEach { z ->
                val sevColor = when (z.severity) { "high" -> f.bad; "medium" -> Color(0xFFE08A1E); else -> f.sky }
                Box(Modifier.offset(w * z.x, h * z.y).size(w * z.w, h * z.h)
                    .border(2.5.dp, Color.White, RoundedCornerShape(12.dp))
                    .border(4.5.dp, sevColor.copy(alpha = .45f), RoundedCornerShape(12.dp)))
                val labelY = (h * z.y - 26.dp).coerceAtLeast(4.dp)
                Box(Modifier.offset((w * z.x).coerceAtMost(w - 150.dp).coerceAtLeast(4.dp), labelY)
                    .height(22.dp).clip(RoundedCornerShape(11.dp)).background(sevColor).padding(horizontal = 10.dp), contentAlignment = Alignment.Center) {
                    Text("${z.label} · ${z.severity}", style = MaterialTheme.typography.labelSmall.copy(fontSize = 11.sp), color = Color.White, maxLines = 1, softWrap = false)
                }
            }
            Row(Modifier.align(Alignment.BottomStart).fillMaxWidth().padding(10.dp), verticalAlignment = Alignment.CenterVertically) {
                if (c.verified) OverlayChip("SHA-256 verified", icon = FarmIcons.Shield, iconTint = Color(0xFF7EE2A0))
                Spacer(Modifier.weight(1f))
                OverlayChip("Capture #${c.objectId} · ${SimpleDateFormat("HH:mm:ss", Locale.US).format(Date(c.receivedAtMs))}")
            }
        }
    }
}

@Composable
fun OverlayChip(text: String, icon: androidx.compose.ui.graphics.vector.ImageVector? = null, iconTint: Color = Color.White) {
    Row(Modifier.height(24.dp).clip(RoundedCornerShape(12.dp)).background(Overlay).padding(horizontal = 9.dp),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        if (icon != null) Icon(icon, null, Modifier.size(12.dp), tint = iconTint)
        Text(text, style = MaterialTheme.typography.labelSmall.copy(fontSize = 11.5.sp, fontWeight = FontWeight.SemiBold), color = Color.White,
            maxLines = 1, softWrap = false)
    }
}

@Composable
fun Segmented(options: List<String>, selected: Int, onSelect: (Int) -> Unit) {
    val f = LocalFarm.current
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(20.dp)).background(f.surface2).padding(4.dp), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        options.forEachIndexed { i, label ->
            val on = i == selected
            Surface(onClick = { onSelect(i) }, shape = RoundedCornerShape(17.dp), color = if (on) f.surface else Color.Transparent,
                shadowElevation = if (on) 1.dp else 0.dp, modifier = Modifier.weight(1f).height(36.dp)) {
                Box(contentAlignment = Alignment.Center, modifier = Modifier.padding(horizontal = 6.dp)) {
                    AutoSizeText(label, MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold, color = if (on) f.green else f.muted))
                }
            }
        }
    }
}
