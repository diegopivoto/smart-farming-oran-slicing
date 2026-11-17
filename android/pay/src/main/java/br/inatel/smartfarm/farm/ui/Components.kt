package br.inatel.smartfarm.farm.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import br.inatel.smartfarm.farm.core.SessionUi
import br.inatel.smartfarm.farm.protocol.MetricUnit
import br.inatel.smartfarm.farm.protocol.Op
import br.inatel.smartfarm.farm.protocol.SlaCatalog
import br.inatel.smartfarm.farm.protocol.SlaCheck
import br.inatel.smartfarm.farm.protocol.SlaSnapshot
import java.util.Locale

val CardShape = RoundedCornerShape(24.dp)

@Composable
fun FarmCard(
    modifier: Modifier = Modifier,
    shape: RoundedCornerShape = CardShape,
    color: Color = LocalFarm.current.surface,
    onClick: (() -> Unit)? = null,
    content: @Composable ColumnScope.() -> Unit,
) {
    val m = modifier
        .shadow(1.5.dp, shape, ambientColor = Color(0x22283220), spotColor = Color(0x22283220))
        .clip(shape)
        .background(color)
        .let { if (onClick != null) it.clickable(onClick = onClick) else it }
    Column(m, content = content)
}

@Composable
fun IconCircleButton(icon: ImageVector, contentDescription: String, onClick: () -> Unit, tint: Color = LocalFarm.current.text) {
    val f = LocalFarm.current
    Box(
        Modifier.size(40.dp).shadow(1.dp, CircleShape).clip(CircleShape).background(f.surface).clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) { Icon(icon, contentDescription, Modifier.size(22.dp), tint = tint) }
}

@Composable
fun AppBar(title: String, onBack: () -> Unit, trailing: @Composable RowScope.() -> Unit = {}) {
    Row(Modifier.fillMaxWidth().height(56.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        IconCircleButton(FarmIcons.Back, "Back", onBack)
        Text(title, style = MaterialTheme.typography.titleLarge, color = LocalFarm.current.text, maxLines = 1,
            overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        trailing()
    }
}

@Composable
fun Pill(text: String, bg: Color, fg: Color, icon: ImageVector? = null, height: Dp = 28.dp) {
    Row(
        Modifier.height(height).clip(RoundedCornerShape(height / 2)).background(bg).padding(horizontal = 12.dp),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        if (icon != null) Icon(icon, null, Modifier.size(14.dp), tint = fg)
        Text(text, style = MaterialTheme.typography.labelSmall, color = fg, maxLines = 1, softWrap = false)
    }
}

/** Rounded icon tile used by the sensor cards and alerts. */
@Composable
fun IconTile(icon: ImageVector, bg: Color, fg: Color, size: Dp = 28.dp, iconSize: Dp = 18.dp, corner: Dp = 9.dp) {
    Box(Modifier.size(size).clip(RoundedCornerShape(corner)).background(bg), contentAlignment = Alignment.Center) {
        Icon(icon, null, Modifier.size(iconSize), tint = fg)
    }
}

/** Big primary/outlined buttons: fixed height, single line, never wraps. */
@Composable
fun FarmButton(text: String, onClick: () -> Unit, modifier: Modifier = Modifier, filled: Boolean = true, icon: ImageVector? = null,
               enabled: Boolean = true, height: Dp = 52.dp) {
    val f = LocalFarm.current
    val shape = RoundedCornerShape(height / 2)
    Surface(
        onClick = onClick, enabled = enabled, shape = shape,
        color = if (filled) f.green else Color.Transparent,
        border = if (filled) null else BorderStroke(1.5.dp, if (f.dark) Color(0xFF4A574A) else Color(0xFFB9C2B2)),
        modifier = modifier.height(height).fillMaxWidth(),
    ) {
        Row(Modifier.padding(horizontal = 20.dp), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
            val fg = if (filled) (if (f.dark) Color(0xFF0F2412) else Color.White) else f.green
            if (icon != null) { Icon(icon, null, Modifier.size(20.dp), tint = fg); Spacer(Modifier.width(10.dp)) }
            AutoSizeText(text, MaterialTheme.typography.labelLarge.copy(color = if (enabled) fg else fg.copy(alpha = .5f)))
        }
    }
}

/** Single-line text that shrinks its font (down to 70 %) instead of wrapping or clipping. */
@Composable
fun AutoSizeText(text: String, style: TextStyle, modifier: Modifier = Modifier) =
    AutoSizeText(AnnotatedString(text), style, modifier)

@Composable
fun AutoSizeText(text: AnnotatedString, style: TextStyle, modifier: Modifier = Modifier) {
    // keyed by length, not content: live values (19.6 -> 19.7) keep their size without re-measuring flicker
    var scale by remember(text.text.length) { mutableFloatStateOf(1f) }
    var ready by remember { mutableStateOf(false) }
    Text(
        text, maxLines = 1, softWrap = false,
        style = style.copy(fontSize = style.fontSize * scale),
        modifier = modifier.drawWithContent { if (ready) drawContent() },
        onTextLayout = { r ->
            if (r.didOverflowWidth && scale > 0.7f) scale *= 0.92f else ready = true
        },
    )
}

/** "24.6 %" with the unit smaller and muted, always on one line. */
@Composable
fun ValueWithUnit(value: String, unit: String, valueSize: TextUnit = 24.sp, color: Color = LocalFarm.current.text, modifier: Modifier = Modifier) {
    val f = LocalFarm.current
    val text = buildAnnotatedString {
        withStyle(SpanStyle(fontWeight = FontWeight.ExtraBold, color = color)) { append(value) }
        if (unit.isNotEmpty()) withStyle(SpanStyle(fontSize = 13.sp, fontWeight = FontWeight.SemiBold, color = f.muted)) { append(" " + unit) }
    }
    AutoSizeText(text, MaterialTheme.typography.headlineSmall.copy(fontSize = valueSize, letterSpacing = (-0.2).sp), modifier)
}

// --------------------------------------------------------------------------------------------------- formatting

object Fmt {
    fun n(v: Double, d: Int) = String.format(Locale.US, "%.${d}f", v)

    fun bps(v: Double) = if (v >= 1e6) "${n(v / 1e6, 1)} Mbit/s" else "${n(v / 1e3, 0)} kbit/s"

    fun seconds(v: Double) = when {
        v < 1.0 -> "${n(v * 1000, 0)} ms"
        v < 10.0 -> "${n(v, 2)} s"
        else -> "${n(v, 1)} s"
    }

    fun pct(ratio: Double): String {
        val p = ratio * 100
        return when {
            p >= 99.95 && p < 100.0 -> "99.9 %" // never round a miss up to 100 %
            p == 100.0 || p == 0.0 -> "${n(p, 0)} %"
            p < 1.0 -> "${n(p, 2)} %"
            else -> "${n(p, 1)} %"
        }
    }

    fun value(c: SlaCheck): String {
        val v = c.value ?: return if (c.spec.metric == SlaCatalog.RETRANS) "n/a until 1st capture" else "waiting…"
        return when (c.spec.unit) {
            MetricUnit.BPS -> bps(v)
            MetricUnit.SECONDS -> seconds(v)
            MetricUnit.RATIO_PCT -> pct(v)
            MetricUnit.COUNT -> n(v, 0)
        }
    }

    fun target(c: SlaCheck): String {
        val s = c.spec
        val thr = when (s.unit) {
            MetricUnit.BPS -> "${n(s.threshold / 1e6, if (s.threshold % 1e6 == 0.0) 0 else 1)} Mbit/s"
            MetricUnit.SECONDS -> if (s.threshold < 1) "${n(s.threshold * 1000, 0)} ms" else "${n(s.threshold, 0)} s"
            MetricUnit.RATIO_PCT -> "${n(s.threshold * 100, if (s.threshold * 100 % 1.0 == 0.0) 0 else 1)} %"
            MetricUnit.COUNT -> n(s.threshold, 0)
        }
        val op = if (s.op == Op.EQ) "" else s.op.symbol + " "
        // the generator reports retransmissions cumulatively for its run, not over a sliding window
        if (s.metric == SlaCatalog.RETRANS) return "Target $op$thr · stream so far"
        return "Target $op$thr · last ${s.windowS} s"
    }
}

// --------------------------------------------------------------------------------------------------- SLA panel

@Composable
fun CheckBadge(met: Boolean?) {
    val f = LocalFarm.current
    val bg by animateColorAsState(when (met) { true -> f.ok; false -> f.bad; null -> f.na }, label = "badge")
    Box(Modifier.size(22.dp).clip(CircleShape).background(bg), contentAlignment = Alignment.Center) {
        Icon(when (met) { true -> FarmIcons.Check; false -> FarmIcons.Close; null -> FarmIcons.Dash }, null, Modifier.size(13.dp), tint = Color.White)
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SlaPanel(sla: SlaSnapshot?, session: SessionUi?, modifier: Modifier = Modifier) {
    val f = LocalFarm.current
    FarmCard(modifier.fillMaxWidth()) {
        Column(Modifier.padding(start = 16.dp, end = 16.dp, top = 14.dp, bottom = 12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Service quality (SLA)", style = MaterialTheme.typography.titleSmall, color = f.text, maxLines = 1,
                    overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
                when (sla?.metShown) {
                    true -> Pill("SLA met", f.okC, f.ok, FarmIcons.Check)
                    false -> Pill("SLA missed", f.badC, f.bad, FarmIcons.Close)
                    null -> Pill("Measuring…", f.surface2, f.muted)
                }
            }
            Spacer(Modifier.height(6.dp))
            val checks = sla?.checks ?: emptyList()
            checks.forEachIndexed { i, c ->
                if (i > 0) HorizontalDivider(color = f.line, thickness = 1.dp)
                Row(Modifier.fillMaxWidth().heightIn(min = 52.dp).padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(c.spec.friendly, style = MaterialTheme.typography.labelMedium.copy(fontSize = 13.5.sp), color = f.text, maxLines = 2)
                        Text(Fmt.target(c), style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.5.sp), color = f.muted, maxLines = 1,
                            overflow = TextOverflow.Ellipsis)
                    }
                    Spacer(Modifier.width(10.dp))
                    val na = c.value == null
                    AutoSizeText(Fmt.value(c), if (na) MaterialTheme.typography.labelMedium.copy(color = f.muted, fontSize = 12.5.sp)
                    else MaterialTheme.typography.titleSmall.copy(color = f.text, fontSize = 14.sp, fontWeight = FontWeight.Bold))
                    Spacer(Modifier.width(10.dp))
                    CheckBadge(c.met)
                }
            }
            if (session != null) {
                FlowRow(Modifier.fillMaxWidth().padding(top = 10.dp, start = 4.dp), horizontalArrangement = Arrangement.spacedBy(14.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    InfoItem("Slice", "SD ${session.kind.sd}")
                    session.offeredBps?.let { InfoItem("Offered", Fmt.bps(it)) }
                    InfoItem("Phone", session.ip ?: "—")
                    InfoItem("Generator", session.generator)
                    InfoItem("RTT", session.rttMs?.let { "${Fmt.n(it, 0)} ms" } ?: "—")
                }
                if (!session.ipInRange) {
                    Text("This IP is outside the ${session.kind.id} range 10.45.${session.kind.octetLo}–${session.kind.octetHi}.x: check the APN types.",
                        style = MaterialTheme.typography.bodySmall, color = f.bad, modifier = Modifier.padding(top = 6.dp, start = 4.dp))
                }
            }
        }
    }
}

@Composable
private fun InfoItem(label: String, value: String) {
    val f = LocalFarm.current
    Text(buildAnnotatedString {
        withStyle(SpanStyle(color = f.muted)) { append("$label ") }
        withStyle(SpanStyle(color = f.text, fontWeight = FontWeight.SemiBold)) { append(value) }
    }, style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.5.sp), maxLines = 1, softWrap = false)
}

/** Thin horizontal bar with an optional threshold marker (soil moisture, UV, NDVI). */
@Composable
fun MeterBar(fraction: Float, fill: androidx.compose.ui.graphics.Brush?, track: androidx.compose.ui.graphics.Brush, marker: Float? = null,
             markerColor: Color = LocalFarm.current.alertTitle, height: Dp = 8.dp, modifier: Modifier = Modifier) {
    androidx.compose.foundation.Canvas(modifier.fillMaxWidth().height(height + 8.dp)) {
        val h = height.toPx()
        val top = (size.height - h) / 2
        val r = androidx.compose.ui.geometry.CornerRadius(h / 2, h / 2)
        drawRoundRect(track, androidx.compose.ui.geometry.Offset(0f, top), androidx.compose.ui.geometry.Size(size.width, h), r)
        if (fill != null && fraction > 0f) {
            drawRoundRect(fill, androidx.compose.ui.geometry.Offset(0f, top),
                androidx.compose.ui.geometry.Size(size.width * fraction.coerceIn(0f, 1f), h), r)
        }
        marker?.let { m ->
            val x = size.width * m.coerceIn(0f, 1f)
            drawRoundRect(markerColor, androidx.compose.ui.geometry.Offset(x - 1.5f.dp.toPx() / 2, 0f),
                androidx.compose.ui.geometry.Size(2.5.dp.toPx(), size.height), androidx.compose.ui.geometry.CornerRadius(2f, 2f))
        }
    }
}
