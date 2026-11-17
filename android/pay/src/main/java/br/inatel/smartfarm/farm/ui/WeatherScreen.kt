package br.inatel.smartfarm.farm.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import android.os.SystemClock
import br.inatel.smartfarm.farm.core.IrrigationUi
import br.inatel.smartfarm.farm.core.LinkUi
import br.inatel.smartfarm.farm.core.SessionUi
import br.inatel.smartfarm.farm.core.WeatherUi
import br.inatel.smartfarm.farm.protocol.SlaSnapshot
import br.inatel.smartfarm.farm.protocol.WeatherTelemetry as W
import kotlinx.coroutines.delay

@Composable
fun WeatherScreen(
    weather: WeatherUi?,
    irrigation: IrrigationUi,
    sla: SlaSnapshot?,
    session: SessionUi?,
    link: LinkUi,
    onBack: () -> Unit,
    onIrrigate: () -> Unit,
    onScenario: (String) -> Unit,
    onDismissMessage: () -> Unit,
) {
    val f = LocalFarm.current
    val tel = weather?.telemetry
    var nowMs by remember { mutableLongStateOf(SystemClock.elapsedRealtime()) }
    LaunchedEffect(Unit) { while (true) { delay(250); nowMs = SystemClock.elapsedRealtime() } }
    val localLeft = ((irrigation.localUntilMs - nowMs) / 1000.0).coerceAtLeast(0.0)
    val irrigating = (tel?.irrigationActive == true) || localLeft > 0.5
    val remaining = if (tel?.irrigationActive == true) tel.irrigationRemainingS else localLeft

    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        AppBar("Weather station", onBack) {
            val (label, bg, fg) = when {
                irrigating -> Triple("Recovering", f.greenC, f.green)
                tel?.state == "drought" -> Triple("Drought", f.soilC, f.soil)
                tel?.state == "storm" -> Triple("Storm", f.skyC, f.sky)
                tel != null -> Triple("Stable", f.greenC, f.green)
                else -> Triple("Waiting", f.surface2, f.muted)
            }
            Pill(label, bg, fg)
        }
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(bottom = 16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            val alert = tel?.irrigationAlert
            AnimatedVisibility(alert != null && !irrigating, enter = fadeIn() + expandVertically(), exit = fadeOut() + shrinkVertically()) {
                IrrigationAlert(alert?.text ?: "", irrigation.sending, onIrrigate)
            }
            AnimatedVisibility(irrigating, enter = fadeIn() + expandVertically(), exit = fadeOut() + shrinkVertically()) {
                IrrigationRunning(remaining, rising = true)
            }
            tel?.alerts?.filter { it.action == null }?.forEach { a -> StormAlert(a.id, a.text) }
            irrigation.message?.takeIf { irrigation.ok == false }?.let { msg ->
                FarmCard(Modifier.fillMaxWidth(), color = f.badC, onClick = onDismissMessage) {
                    Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                        IconTile(FarmIcons.Warning, Color.White.copy(alpha = .7f), f.bad)
                        Spacer(Modifier.width(10.dp))
                        Text(msg, style = MaterialTheme.typography.bodySmall, color = f.bad, modifier = Modifier.weight(1f))
                    }
                }
            }
            if (tel == null) {
                FarmCard(Modifier.fillMaxWidth()) {
                    Row(Modifier.padding(18.dp), verticalAlignment = Alignment.CenterVertically) {
                        CircularProgressIndicator(Modifier.size(22.dp), color = f.sunText, strokeWidth = 2.dp)
                        Spacer(Modifier.width(14.dp))
                        Text("Waiting for the first reading from the weather station…", style = MaterialTheme.typography.bodyMedium, color = f.muted)
                    }
                }
            } else {
                SensorGrid(tel)
            }
            SlaPanel(sla, session)
            link.delayMeanMs?.let { d ->
                FarmCard(Modifier.fillMaxWidth()) {
                    Row(Modifier.padding(horizontal = 16.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text("Delay to the phone", style = MaterialTheme.typography.titleSmall.copy(fontSize = 14.sp), color = f.text)
                            Text("One-way, mean of the last second", style = MaterialTheme.typography.bodySmall, color = f.muted)
                        }
                        ValueWithUnit(Fmt.n(d, 0), "ms", 18.sp)
                    }
                }
            }
            ScenarioCard(tel?.state, onScenario)
            OranCard(session?.oran)
        }
    }
}

@Composable
private fun IrrigationAlert(text: String, sending: Boolean, onIrrigate: () -> Unit) {
    val f = LocalFarm.current
    Column(
        Modifier.fillMaxWidth().clip(CardShape).background(Brush.linearGradient(listOf(f.alertTop, f.alertBottom)))
            .border(1.dp, f.alertBorder, CardShape).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(verticalAlignment = Alignment.Top) {
            IconTile(FarmIcons.Sprout, if (f.dark) f.soilC else Color.White, f.soil, size = 40.dp, iconSize = 24.dp, corner = 14.dp)
            Spacer(Modifier.width(12.dp))
            Column {
                Text("Soil is dry — start irrigation?", style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.ExtraBold), color = f.alertTitle)
                Text(text, style = MaterialTheme.typography.bodySmall.copy(fontSize = 13.sp), color = f.alertText, modifier = Modifier.padding(top = 2.dp))
            }
        }
        FarmButton(if (sending) "Starting irrigation…" else "Start irrigation", onIrrigate, icon = FarmIcons.Drop, enabled = !sending, height = 48.dp)
    }
}

@Composable
private fun IrrigationRunning(remainingS: Double, rising: Boolean) {
    val f = LocalFarm.current
    val frac by animateFloatAsState((remainingS / 60.0).toFloat().coerceIn(0f, 1f), tween(900), label = "irr")
    Row(
        Modifier.fillMaxWidth().clip(CardShape).background(Brush.linearGradient(listOf(f.waterTop, f.waterBottom))).padding(horizontal = 16.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(44.dp), contentAlignment = Alignment.Center) {
            Canvas(Modifier.fillMaxSize()) {
                val sw = 5.dp.toPx()
                drawCircle(Color.White.copy(alpha = if (f.dark) .25f else 1f), size.minDimension / 2 - sw / 2, style = Stroke(sw))
                drawArc(f.sky, -90f, 360f * frac, false, Offset(sw / 2, sw / 2), Size(size.width - sw, size.height - sw), style = Stroke(sw, cap = StrokeCap.Round))
            }
            androidx.compose.material3.Icon(FarmIcons.Drop, null, Modifier.size(20.dp), tint = f.sky)
        }
        Spacer(Modifier.width(14.dp))
        Column(Modifier.weight(1f)) {
            AutoSizeText("Irrigation running · ${remainingS.toInt()} s", MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.ExtraBold, color = f.waterTitle))
            Text(if (rising) "Soil moisture is rising" else "Watering the field", style = MaterialTheme.typography.bodySmall.copy(fontSize = 12.5.sp), color = f.waterText)
        }
    }
}

@Composable
private fun StormAlert(id: String, text: String) {
    val f = LocalFarm.current
    val title = when (id) {
        "excess_rain" -> "Heavy rain alert"
        "strong_wind" -> "Strong wind alert"
        else -> "Weather alert"
    }
    FarmCard(Modifier.fillMaxWidth(), color = f.skyC) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.Top) {
            IconTile(FarmIcons.Storm, if (f.dark) f.surface else Color.White, f.sky, size = 40.dp, iconSize = 24.dp, corner = 14.dp)
            Spacer(Modifier.width(12.dp))
            Column {
                Text(title, style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.ExtraBold), color = f.waterTitle)
                Text(text, style = MaterialTheme.typography.bodySmall.copy(fontSize = 13.sp), color = f.waterText, modifier = Modifier.padding(top = 2.dp))
            }
        }
    }
}

@Composable
private fun animated(v: Double?): Float {
    val target = (v ?: 0.0).toFloat()
    val a by animateFloatAsState(target, tween(800), label = "v")
    return a
}

@Composable
private fun SensorGrid(t: W) {
    val f = LocalFarm.current
    val temp = animated(t.reading(W.AIR_TEMPERATURE))
    val hum = animated(t.reading(W.AIR_HUMIDITY))
    val soil = animated(t.reading(W.SOIL_MOISTURE))
    val rain = animated(t.reading(W.RAIN_RATE))
    val rainToday = animated(t.reading(W.RAIN_TODAY))
    val wind = animated(t.reading(W.WIND_SPEED))
    val solar = animated(t.reading(W.SOLAR))
    val uv = animated(t.reading(W.UV))
    val pressure = animated(t.reading(W.PRESSURE))
    val leaf = animated(t.reading(W.LEAF_WETNESS))
    val humMin = t.min(W.AIR_HUMIDITY) ?: 40.0
    val soilMin = t.min(W.SOIL_MOISTURE) ?: 30.0
    val rainMax = t.max(W.RAIN_RATE)

    // pressure trend over ~10 s of readings
    val history = remember { ArrayDeque<Pair<Long, Double>>() }
    val nowMs = SystemClock.elapsedRealtime()
    t.reading(W.PRESSURE)?.let { p ->
        if (history.isEmpty() || nowMs - history.last().first > 900) history.addLast(nowMs to p)
        while (history.size > 12) history.removeFirst()
    }
    val trend = if (history.size >= 4) history.last().second - history.first().second else 0.0

    Row2 {
        Sensor("Temperature", FarmIcons.Thermo, if (f.dark) Color(0xFF452722) else Color(0xFFFBE0D8), Color(0xFFC6453A)) {
            ValueWithUnit(Fmt.n(temp.toDouble(), 1), "°C")
            Sub(when { temp < 10 -> "Cold"; temp < 18 -> "Cool"; temp < 26 -> "Mild"; temp < 32 -> "Warm"; else -> "Hot afternoon" })
        }
        SensorWithGraphic("Air humidity", FarmIcons.Drop, f.skyC, f.sky, graphic = { RingGauge(hum / 100f, f.sky) }) {
            ValueWithUnit(Fmt.n(hum.toDouble(), 0), "%")
            if (hum < humMin) Sub("Below ${Fmt.n(humMin, 0)} %", f.bad)
            else Sub(when { hum < 55 -> "Comfortable"; hum < 80 -> "Humid"; else -> "Very humid" })
        }
    }
    Row2 {
        Sensor("Soil moisture", FarmIcons.Sprout, f.soilC, f.soil) {
            val dry = soil < soilMin
            ValueWithUnit(Fmt.n(soil.toDouble(), 1), "%", color = if (dry) f.bad else f.text)
            val scale = maxOf(50.0, soilMin * 2).toFloat()
            MeterBar(soil / scale,
                if (dry) Brush.horizontalGradient(listOf(f.bad, f.sun)) else Brush.horizontalGradient(listOf(f.sun, f.green2)),
                Brush.horizontalGradient(listOf(f.track, f.track)), marker = (soilMin / scale).toFloat())
            Sub("Needs ≥ ${Fmt.n(soilMin, 0)} %")
        }
        Sensor("Rain", FarmIcons.Rain, f.skyC, f.sky) {
            val heavy = rainMax != null && rain > rainMax
            ValueWithUnit(Fmt.n(rain.toDouble(), 1), "mm/h", color = if (heavy) f.bad else f.text)
            Sub("Today ${Fmt.n(rainToday.toDouble(), 1)} mm")
        }
    }
    Row2 {
        SensorWithGraphic("Wind", FarmIcons.Wind, if (f.dark) f.greenC else Color(0xFFE3EEE6), f.green,
            graphic = { Compass(t.reading(W.WIND_DIRECTION) ?: 0.0) }) {
            ValueWithUnit(Fmt.n(wind.toDouble(), 1), "km/h")
            Sub("From ${cardinal(t.reading(W.WIND_DIRECTION) ?: 0.0)}")
        }
        Sensor("Sunlight", FarmIcons.Sun, f.sunC, Color(0xFFC98414)) {
            ValueWithUnit(Fmt.n(solar.toDouble(), 0), "W/m²")
            Sub(when { solar > 600 -> "Strong sun"; solar > 300 -> "Bright"; solar > 80 -> "Cloudy"; else -> "Dim light" })
        }
    }
    Row2 {
        Sensor("UV index", FarmIcons.Uv, if (f.dark) Color(0xFF35263F) else Color(0xFFEEE2F5), Color(0xFF7B4BA0)) {
            ValueWithUnit(Fmt.n(uv.toDouble(), 1), uvLevel(uv.toDouble()))
            MeterBar(0f, null, Brush.horizontalGradient(listOf(Color(0xFF4CAF50), Color(0xFFF4D03F), Color(0xFFF39C12), Color(0xFFE74C3C), Color(0xFF8E44AD))),
                marker = uv / 11f, markerColor = f.text)
        }
        Sensor("Pressure", FarmIcons.Gauge, if (f.dark) Color(0xFF262C3B) else Color(0xFFE5E9F2), Color(0xFF4A5A86)) {
            ValueWithUnit(Fmt.n(pressure.toDouble(), 0), "hPa")
            Sub(when { trend < -0.4 -> "Falling ▼"; trend > 0.4 -> "Rising ▲"; else -> "Steady" }, if (trend < -0.4) f.bad else f.muted)
        }
    }
    FarmCard(Modifier.fillMaxWidth()) {
        Row(Modifier.padding(horizontal = 14.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
            IconTile(FarmIcons.Leaf, f.greenC, f.green)
            Spacer(Modifier.width(10.dp))
            Text("Leaf wetness", style = MaterialTheme.typography.labelMedium.copy(fontSize = 12.5.sp), color = f.muted, maxLines = 1, modifier = Modifier.weight(1f))
            ValueWithUnit(Fmt.n(leaf.toDouble(), 0), "%", 18.sp)
            Spacer(Modifier.width(10.dp))
            Text(when { leaf < 15 -> "Dry leaves"; leaf < 50 -> "Damp leaves"; else -> "Wet leaves" }, style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.5.sp),
                color = f.muted, maxLines = 1, softWrap = false)
        }
    }
}

@Composable
private fun Row2(content: @Composable RowScope.() -> Unit) =
    Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min), horizontalArrangement = Arrangement.spacedBy(10.dp), content = content)

@Composable
private fun RowScope.Sensor(label: String, icon: androidx.compose.ui.graphics.vector.ImageVector, bg: Color, fg: Color, content: @Composable ColumnScope.() -> Unit) {
    FarmCard(Modifier.weight(1f).fillMaxHeight().heightIn(min = 118.dp)) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            SensorLabel(label, icon, bg, fg)
            content()
        }
    }
}

@Composable
private fun RowScope.SensorWithGraphic(label: String, icon: androidx.compose.ui.graphics.vector.ImageVector, bg: Color, fg: Color,
                                       graphic: @Composable () -> Unit, content: @Composable ColumnScope.() -> Unit) {
    FarmCard(Modifier.weight(1f).fillMaxHeight().heightIn(min = 118.dp)) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            SensorLabel(label, icon, bg, fg)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp), content = content)
                Spacer(Modifier.width(6.dp))
                graphic()
            }
        }
    }
}

@Composable
private fun SensorLabel(label: String, icon: androidx.compose.ui.graphics.vector.ImageVector, bg: Color, fg: Color) {
    val f = LocalFarm.current
    Row(verticalAlignment = Alignment.CenterVertically) {
        IconTile(icon, bg, fg)
        Spacer(Modifier.width(8.dp))
        Text(label, style = MaterialTheme.typography.labelMedium.copy(fontSize = 12.5.sp), color = f.muted, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
private fun Sub(text: String, color: Color = LocalFarm.current.muted) =
    Text(text, style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.5.sp), color = color, maxLines = 1, overflow = TextOverflow.Ellipsis)

@Composable
private fun RingGauge(fraction: Float, color: Color) {
    val f = LocalFarm.current
    Canvas(Modifier.size(52.dp)) {
        val sw = 7.dp.toPx()
        val r = Size(size.width - sw, size.height - sw)
        drawArc(if (f.dark) f.track else Color(0xFFE6ECEF), 0f, 360f, false, Offset(sw / 2, sw / 2), r, style = Stroke(sw))
        drawArc(color, -90f, 360f * fraction.coerceIn(0f, 1f), false, Offset(sw / 2, sw / 2), r, style = Stroke(sw, cap = StrokeCap.Round))
    }
}

/** Compass dial; the needle takes the shortest way round when the direction crosses north. */
@Composable
private fun Compass(directionDeg: Double) {
    val f = LocalFarm.current
    var unwrapped by remember { mutableFloatStateOf(directionDeg.toFloat()) }
    var last by remember { mutableStateOf(directionDeg) }
    if (directionDeg != last) {
        var delta = (directionDeg - last) % 360.0
        if (delta > 180) delta -= 360.0
        if (delta < -180) delta += 360.0
        unwrapped += delta.toFloat()
        last = directionDeg
    }
    val angle by animateFloatAsState(unwrapped, tween(900), label = "needle")
    val measurer = rememberTextMeasurer()
    val labelStyle = TextStyle(fontFamily = Inter, fontSize = 8.sp, fontWeight = FontWeight.Bold, color = Color(0xFF7A8576))
    Canvas(Modifier.size(56.dp)) {
        val cx = size.width / 2
        val r = size.minDimension / 2 - 1.5f
        drawCircle(if (f.dark) f.surface2 else Color(0xFFF3F5EE), r)
        drawCircle(if (f.dark) f.line else Color(0xFFD5DCCD), r, style = Stroke(1.5f))
        val u = size.width / 56f
        listOf("N" to Offset(cx, 7f * u), "S" to Offset(cx, 47f * u), "E" to Offset(48f * u, cx), "W" to Offset(8f * u, cx)).forEach { (s, o) ->
            val m = measurer.measure(s, labelStyle)
            drawText(m, topLeft = Offset(o.x - m.size.width / 2f, o.y - m.size.height / 2f))
        }
        // wind "from" direction: the red tip points where the wind comes from
        rotate(angle, Offset(cx, cx)) {
            drawPath(Path().apply { moveTo(cx, 10 * u); lineTo(cx + 4 * u, cx); lineTo(cx, cx + 3 * u); lineTo(cx - 4 * u, cx); close() }, Color(0xFFC6453A))
            drawPath(Path().apply { moveTo(cx, 46 * u); lineTo(cx + 4 * u, cx); lineTo(cx, cx - 3 * u); lineTo(cx - 4 * u, cx); close() }, Color(0xFF9AA596))
        }
    }
}

private fun cardinal(deg: Double): String {
    val names = listOf("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return names[(((deg % 360 + 360) % 360 + 22.5) / 45).toInt() % 8]
}

private fun uvLevel(uv: Double) = when { uv < 3 -> "Low"; uv < 6 -> "Moderate"; uv < 8 -> "High"; uv < 11 -> "Very high"; else -> "Extreme" }

@Composable
private fun ScenarioCard(state: String?, onScenario: (String) -> Unit) {
    val f = LocalFarm.current
    FarmCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(horizontal = 16.dp, vertical = 14.dp)) {
            Text("Demo scenario", style = MaterialTheme.typography.titleSmall, color = f.text)
            Text("Changes the weather simulated by the station on PC3", style = MaterialTheme.typography.bodySmall, color = f.muted)
            Spacer(Modifier.height(10.dp))
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(20.dp)).background(f.surface2).padding(4.dp), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                listOf("stable" to "Stable", "drought" to "Drought", "storm" to "Storm").forEach { (id, label) ->
                    val on = state == id
                    Surface(onClick = { onScenario(id) }, shape = RoundedCornerShape(17.dp), color = if (on) f.surface else Color.Transparent,
                        shadowElevation = if (on) 1.dp else 0.dp, modifier = Modifier.weight(1f).height(36.dp)) {
                        Box(contentAlignment = Alignment.Center) {
                            Text(label, style = MaterialTheme.typography.labelMedium, color = if (on) f.green else f.muted, maxLines = 1, softWrap = false)
                        }
                    }
                }
            }
        }
    }
}
