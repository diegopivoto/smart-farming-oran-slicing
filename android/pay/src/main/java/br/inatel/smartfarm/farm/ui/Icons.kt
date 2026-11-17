package br.inatel.smartfarm.farm.ui

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.addPathNodes
import androidx.compose.ui.unit.dp

/**
 * Arc flags are written with separators ("0 0 1 -12 0"): Compose's PathParser misreads the compact SVG form "0 01-12 0".
 *
 * Line/fill icons of the approved mockup (24 x 24 viewport), single color so Icon(tint) colors them. */
object FarmIcons {
    private fun circ(cx: Float, cy: Float, r: Float) = "M${cx - r},${cy}a$r,$r 0 1,0 ${2 * r},0a$r,$r 0 1,0 ${-2 * r},0"

    private fun icon(name: String, block: ImageVector.Builder.() -> Unit): ImageVector =
        ImageVector.Builder(name, 24.dp, 24.dp, 24f, 24f).apply(block).build()

    private fun ImageVector.Builder.stroke(d: String, w: Float = 2f) = addPath(
        addPathNodes(d), fill = null, stroke = SolidColor(Color.Black), strokeLineWidth = w,
        strokeLineCap = StrokeCap.Round, strokeLineJoin = StrokeJoin.Round,
    )

    private fun ImageVector.Builder.fill(d: String) = addPath(addPathNodes(d), fill = SolidColor(Color.Black))

    val Back = icon("back") { stroke("M15 5l-7 7 7 7", 2.4f) }
    val Gear = icon("gear") {
        stroke(circ(12f, 12f, 3.2f))
        stroke("M12 2.8v2.6M12 18.6v2.6M2.8 12h2.6M18.6 12h2.6M5.5 5.5l1.8 1.8M16.7 16.7l1.8 1.8M5.5 18.5l1.8-1.8M16.7 7.3l1.8-1.8")
    }
    val Arrow = icon("arrow") { stroke("M5 12h13M13 6l6 6-6 6", 2.4f) }
    val Check = icon("check") { stroke("M6 12.5l4 4 8-9", 3f) }
    val Close = icon("x") { stroke("M7 7l10 10M17 7L7 17", 3f) }
    val Dash = icon("dash") { stroke("M7 12h10", 3f) }
    val Thermo = icon("thermo") {
        stroke("M10 4a2 2 0 0 1 4 0v9.3a4 4 0 1 1 -4 0z")
        fill(circ(12f, 16.5f, 2f))
        stroke("M12 8v7")
    }
    val Drop = icon("drop") { fill("M12 3s6 6.6 6 11a6 6 0 0 1 -12 0c0-4.4 6-11 6-11z") }
    val Sprout = icon("sprout") {
        stroke("M12 20v-8")
        fill("M12 12c0-4 3-6 7-6 0 4-3 6-7 6zM12 14c0-3-2.5-5-6-5 0 3 2.5 5 6 5z")
        stroke("M5 20h14")
    }
    val Rain = icon("rain") {
        fill("M7 15a4 4 0 0 1 -.5 -8A5 5 0 0 1 16 6.5a3.8 3.8 0 0 1 1 7.5z")
        stroke("M8 18l-1 2.5M12 18l-1 2.5M16 18l-1 2.5")
    }
    val Wind = icon("wind") { stroke("M3 9h11a3 3 0 1 0 -3 -3M3 14h15a3 3 0 1 1 -3 3M3 19h6") }
    val Sun = icon("sun") {
        fill(circ(12f, 12f, 4.5f))
        stroke("M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8")
    }
    val Uv = icon("uv") {
        stroke("M5 15a7 7 0 0 1 14 0")
        stroke("M12 4v2M4.2 7.2l1.4 1.4M19.8 7.2l-1.4 1.4M2 15h2M20 15h2M3 19h18")
    }
    val Gauge = icon("gauge") {
        stroke(circ(12f, 13f, 8f))
        stroke("M12 13l4-4", 2.2f)
        fill(circ(12f, 13f, 1.6f))
    }
    val Leaf = icon("leaf") { fill("M5 19C5 10 10 5 20 4c-1 10-6 15-15 15z") }
    val Shield = icon("shield") {
        stroke("M12 3l7 3v6c0 4.5-3 7.8-7 9-4-1.2-7-4.5-7-9V6z")
        stroke("M8.5 12l2.5 2.5 4.5-5")
    }
    val Signal = icon("signal") { fill("M4 18h2v2H4zM9 14h2v6H9zM14 10h2v10h-2zM19 6h2v14h-2z") }
    val Storm = icon("storm") {
        fill("M7 14a4 4 0 0 1 -.5 -8A5 5 0 0 1 16 5.5a3.8 3.8 0 0 1 1 7.5z")
        fill("M12 13l-3 5h3l-1.5 4 4.5-6h-3l1.5-3z")
    }
    val Warning = icon("warn") {
        stroke("M12 4l9 16H3z")
        stroke("M12 10v4M12 17v.5", 2.2f)
    }
    val Refresh = icon("refresh") { stroke("M20 12a8 8 0 1 1 -2.3 -5.7M20 4v4h-4") }
    val Camera = icon("camera") {
        stroke("M4 8h3l2-3h6l2 3h3v11H4z")
        stroke(circ(12f, 13f, 3.5f))
    }
}
