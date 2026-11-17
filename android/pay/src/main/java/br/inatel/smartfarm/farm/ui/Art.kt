package br.inatel.smartfarm.farm.ui

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.RoundRect
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Matrix
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.clipPath
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.graphics.drawscope.translate
import androidx.compose.ui.graphics.drawscope.withTransform
import androidx.compose.ui.graphics.vector.PathParser
import br.inatel.smartfarm.farm.core.ServiceKind
import kotlin.math.max
import kotlin.math.sin
import kotlin.math.tan

/**
 * Flat vector scenes of the approved mockup, drawn in a 380 x 132 viewBox and scaled like SVG "xMidYMid slice".
 * [t] in [0, 1) animates small details (drone hover, rotors, drifting cloud, spinning anemometer).
 */
private fun svg(d: String): Path = PathParser().parsePathString(d).toPath()

private val c = object {
    val sun = Color(0xFFF6C453)
    val hillLight = Color(0xFF8CC56A)
    val hill = Color(0xFF4E9A4F)
    val row = Color(0xFF3D7F3F)
    val ink = Color(0xFF2B3A2E)
    val sky = Color(0xFF3A7CA5)
    val skyLight = Color(0xFFD9EAF3)
    val lens = Color(0xFF9FD3F0)
    val grey = Color(0xFF5E6B5F)
}

private val droneHill1 = svg("M0 92 Q90 70 190 88 T380 80 V132 H0Z")
private val droneHill2 = svg("M0 108 Q120 88 240 104 T380 100 V132 H0Z")
private val droneCone = svg("M190 56 L150 112 L232 112 Z")
private val weatherHill1 = svg("M0 104 Q100 90 200 102 T380 96 V132 H0Z")
private val weatherHill2 = svg("M0 118 Q140 106 260 116 T380 114 V132 H0Z")
private val cropMiniHill = svg("M8 46 Q30 34 50 44 T80 40 V62 H8Z")

fun DrawScope.viewBox(w: Float, h: Float, block: DrawScope.() -> Unit) {
    val s = max(size.width / w, size.height / h)
    val dx = (size.width - w * s) / 2f
    val dy = (size.height - h * s) / 2f
    clipPath(Path().apply { addRect(androidx.compose.ui.geometry.Rect(0f, 0f, size.width, size.height)) }) {
        withTransform({ translate(dx, dy); scale(s, s, Offset.Zero) }) { block() }
    }
}

private fun DrawScope.rrect(x: Float, y: Float, w: Float, h: Float, r: Float, color: Color, alpha: Float = 1f) =
    drawRoundRect(color, Offset(x, y), Size(w, h), CornerRadius(r, r), alpha = alpha)

fun DrawScope.drawDroneArt(t: Float) {
    viewBox(380f, 132f) {
        drawRect(Brush.verticalGradient(listOf(Color(0xFFBFE0F2), Color(0xFFE9F4F7)), 0f, 132f), Offset.Zero, Size(380f, 132f))
        drawCircle(c.sun, 18f, Offset(318f, 34f))
        drawPath(droneHill1, c.hillLight)
        drawPath(droneHill2, c.hill)
        val rows = listOf(20f to 122f, 70f to 126f, 120f to 128f, 170f to 130f, 230f to 130f, 290f to 130f, 340f to 130f)
        val rowEnd = listOf(70f to 104f, 120f to 106f, 170f to 108f, 220f to 110f, 270f to 112f, 320f to 112f, 360f to 112f)
        rows.zip(rowEnd).forEach { (a, b) ->
            drawLine(c.row, Offset(a.first, a.second), Offset(b.first, b.second), 3f, StrokeCap.Round, alpha = .55f)
        }
        val hover = sin(t * 2 * Math.PI).toFloat() * 3f
        drawPath(droneCone, Color.White, alpha = .28f)
        translate(190f, 44f + hover) {
            rrect(-46f, -4f, 92f, 5f, 2.5f, c.ink)
            val spin = 0.65f + 0.35f * kotlin.math.abs(sin(t * 40 * Math.PI).toFloat())
            drawOval(c.ink, Offset(-46f - 18f * spin, -11.5f), Size(36f * spin, 7f), alpha = .35f)
            drawOval(c.ink, Offset(46f - 18f * spin, -11.5f), Size(36f * spin, 7f), alpha = .35f)
            rrect(-49f, -10f, 6f, 8f, 2f, c.ink)
            rrect(43f, -10f, 6f, 8f, 2f, c.ink)
            rrect(-18f, -10f, 36f, 16f, 7f, c.sky)
            rrect(-10f, -6f, 20f, 5f, 2.5f, c.skyLight)
            drawCircle(c.ink, 5f, Offset(0f, 10f))
            drawCircle(c.lens, 2f, Offset(0f, 10f))
        }
    }
}

fun DrawScope.drawCropArt(t: Float) {
    viewBox(380f, 132f) {
        drawRect(Color(0xFFE8F1DE), Offset.Zero, Size(380f, 132f))
        val skew = Matrix().apply {
            values[Matrix.SkewX] = tan(Math.toRadians(-14.0)).toFloat()
            values[Matrix.TranslateX] = 26f
            values[Matrix.TranslateY] = 14f
        }
        withTransform({ transform(skew) }) {
            val field = Path().apply { addRoundRect(RoundRect(0f, 0f, 210f, 104f, CornerRadius(12f, 12f))) }
            clipPath(field) {
                drawRect(Color(0xFF5FA24E), Offset.Zero, Size(210f, 104f))
                for (i in 0 until 7) drawRect(Color(0xFF3F8444), Offset(10f + 30f * i, 0f), Size(12f, 104f))
                drawOval(Color(0xFFC9A548), Offset(106f, 26f), Size(44f, 28f), alpha = .9f)
            }
            // scanning frame sweeps gently over the hotspot
            val dx = sin(t * 2 * Math.PI).toFloat() * 6f
            drawRoundRect(Color.White, Offset(100f + dx, 20f), Size(58f, 40f), CornerRadius(8f, 8f),
                style = Stroke(3f, pathEffect = PathEffect.dashPathEffect(floatArrayOf(6f, 5f))))
        }
        translate(262f, 22f) {
            drawRoundRect(Brush.horizontalGradient(listOf(Color(0xFFD7302A), Color(0xFFF4D03F), Color(0xFF1E8E3E)), 16f, 104f),
                Offset(16f, 16f), Size(88f, 70f), CornerRadius(12f, 12f), alpha = .95f)
            rrect(8f, 8f, 88f, 70f, 12f, Color(0xFF8A5A3B), .9f)
            rrect(0f, 0f, 88f, 70f, 12f, Color.White)
            val inner = Path().apply { addRoundRect(RoundRect(8f, 8f, 80f, 62f, CornerRadius(8f, 8f))) }
            clipPath(inner) {
                drawRect(Color(0xFF7DB95B), Offset(8f, 8f), Size(72f, 54f))
                drawPath(cropMiniHill, c.hill)
                drawCircle(c.sun, 7f, Offset(62f, 22f))
            }
        }
    }
}

fun DrawScope.drawWeatherArt(t: Float) {
    viewBox(380f, 132f) {
        drawRect(Brush.verticalGradient(listOf(Color(0xFFFDE7B8), Color(0xFFFFF6E2)), 0f, 132f), Offset.Zero, Size(380f, 132f))
        drawCircle(c.sun, 22f, Offset(96f, 44f))
        val drift = sin(t * 2 * Math.PI).toFloat() * 6f
        translate(drift, 0f) {
            drawCircle(Color.White, 16f, Offset(118f, 58f))
            drawCircle(Color.White, 20f, Offset(140f, 50f))
            drawCircle(Color.White, 14f, Offset(162f, 60f))
            rrect(104f, 58f, 72f, 16f, 8f, Color.White)
        }
        drawPath(weatherHill1, c.hillLight)
        drawPath(weatherHill2, c.hill)
        translate(270f, 0f) {
            rrect(-3f, 34f, 6f, 80f, 3f, c.grey)
            rotate(t * 360f, Offset(0f, 26f)) {
                // anemometer: three cups around the hub
                drawLine(c.grey, Offset(-30f, 26f), Offset(30f, 26f), 4f, StrokeCap.Round)
                drawLine(c.grey, Offset(0f, 26f), Offset(0f, 8f), 4f, StrokeCap.Round)
                drawCircle(c.sky, 7f, Offset(-30f, 26f))
                drawCircle(c.sky, 7f, Offset(30f, 26f))
                drawCircle(c.sky, 7f, Offset(0f, 8f))
            }
            drawCircle(c.grey, 4f, Offset(0f, 26f))
            rotate(-18f, Offset(25f, 74f)) { rrect(8f, 64f, 34f, 20f, 3f, Color(0xFF2B4E6E)) }
            rrect(-20f, 76f, 16f, 22f, 4f, Color.White)
            drawRoundRect(c.grey, Offset(-20f, 76f), Size(16f, 22f), CornerRadius(4f, 4f), style = Stroke(2f))
            for (y in listOf(81f, 86f, 91f)) drawRect(c.grey, Offset(-16f, y), Size(8f, 2f))
        }
        listOf(40f to 8f, 46f to 11f, 52f to 7f, 200f to 9f, 206f to 12f, 212f to 7f).forEach { (x, h) ->
            val base = if (x < 100) 118f else 116f
            drawLine(c.row, Offset(x, base), Offset(x, base - h), 2.4f, StrokeCap.Round)
        }
    }
}

fun DrawScope.drawServiceArt(kind: ServiceKind, t: Float) = when (kind) {
    ServiceKind.DRONE -> drawDroneArt(t)
    ServiceKind.CROP -> drawCropArt(t)
    ServiceKind.WEATHER -> drawWeatherArt(t)
}

@Composable
fun ServiceArt(kind: ServiceKind, modifier: Modifier = Modifier, animate: Boolean = true) {
    val t = if (animate) {
        val tr = rememberInfiniteTransition(label = "art")
        val v by tr.animateFloat(0f, 1f, infiniteRepeatable(tween(if (kind == ServiceKind.WEATHER) 6000 else 3200, easing = LinearEasing), RepeatMode.Restart), label = "t")
        v
    } else 0.15f
    Canvas(modifier) { drawServiceArt(kind, t) }
}

/** Placeholder field seen from above (crop screen before the first capture). */
fun DrawScope.drawFieldPlaceholder(t: Float) {
    viewBox(400f, 225f) {
        drawRect(Color(0xFF5E9E45), Offset.Zero, Size(400f, 225f))
        for (i in 0 until 8) drawRect(Color(0xFF7A5A3A), Offset(0f, 12f + 28f * i), Size(400f, 7f), alpha = .85f)
        val x = (t * 480f) - 40f
        drawRect(Brush.horizontalGradient(listOf(Color.Transparent, Color.White.copy(alpha = .22f), Color.Transparent), x - 40f, x + 40f),
            Offset(x - 40f, 0f), Size(80f, 225f))
    }
}
