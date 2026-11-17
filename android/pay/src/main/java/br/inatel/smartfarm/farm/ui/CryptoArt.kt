package br.inatel.smartfarm.farm.ui

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import br.inatel.smartfarm.farm.R

private val EthDark = Color(0xFF3C3C3D)
private val EthLight = Color(0xFF8C8C8C)
private val EthBlue = Color(0xFF627EEA)
private val Coin = Color(0xFFF2B93B)
private val CoinEdge = Color(0xFFC98E12)

/** Ethereum diamond in a round badge. */
@Composable
fun EthBadge(size: Dp = 28.dp, bg: Color = EthBlue) {
    Box(Modifier.size(size).clip(CircleShape).background(bg), contentAlignment = Alignment.Center) {
        Canvas(Modifier.size(size * 0.62f)) {
            val w = this.size.width; val h = this.size.height; val cx = w / 2
            val top = Path().apply { moveTo(cx, 0f); lineTo(w * 0.92f, h * 0.52f); lineTo(cx, h * 0.68f); lineTo(w * 0.08f, h * 0.52f); close() }
            val bottom = Path().apply { moveTo(cx, h * 0.76f); lineTo(w * 0.92f, h * 0.6f); lineTo(cx, h); lineTo(w * 0.08f, h * 0.6f); close() }
            drawPath(top, Color.White); drawPath(bottom, Color.White.copy(alpha = 0.8f))
            drawLine(EthBlue.copy(alpha = 0.6f), Offset(cx, 0f), Offset(cx, h * 0.68f), strokeWidth = 1.2f)
        }
    }
}

/** Smartphone glyph (the UE). */
@Composable
fun PhoneGlyph(size: Dp = 28.dp, color: Color = LocalFarm.current.text) {
    Canvas(Modifier.size(size)) {
        val w = this.size.width; val h = this.size.height
        drawRoundRect(color, Offset(w * 0.24f, h * 0.06f), Size(w * 0.52f, h * 0.88f), CornerRadius(w * 0.1f), style = Stroke(w * 0.08f))
        drawLine(color, Offset(w * 0.42f, h * 0.84f), Offset(w * 0.58f, h * 0.84f), strokeWidth = w * 0.07f)
    }
}

/** Small stack of gold coins. */
@Composable
fun CoinStack(size: Dp = 30.dp) {
    Canvas(Modifier.size(size)) {
        val w = this.size.width; val h = this.size.height
        for (i in 0 until 3) {
            val y = h * (0.62f - i * 0.2f)
            drawOval(CoinEdge, Offset(w * 0.12f, y), Size(w * 0.76f, h * 0.3f))
            drawOval(Coin, Offset(w * 0.12f, y - h * 0.05f), Size(w * 0.76f, h * 0.3f))
        }
    }
}

@Composable
fun InatelLogo(width: Dp = 84.dp) {
    Image(painterResource(R.drawable.inatel_logo), contentDescription = "Inatel", modifier = Modifier.width(width))
}

/** UE -> coins -> operator, with a coin travelling along the arrow (per-minute charge). */
@Composable
fun ChargeFlow(modifier: Modifier = Modifier) {
    val f = LocalFarm.current
    val t by rememberInfiniteTransition(label = "coin").animateFloat(0f, 1f,
        infiniteRepeatable(tween(1600, easing = LinearEasing), RepeatMode.Restart), label = "x")
    Row(modifier.fillMaxWidth().padding(vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            PhoneGlyph(30.dp); Text("UE", style = MaterialTheme.typography.labelSmall, color = f.muted)
        }
        Box(Modifier.weight(1f).height(30.dp).padding(horizontal = 6.dp)) {
            Canvas(Modifier.fillMaxWidth().height(30.dp)) {
                val y = this.size.height / 2; val w = this.size.width
                drawLine(f.muted, Offset(0f, y), Offset(w - 8f, y), strokeWidth = 3f)
                drawPath(Path().apply { moveTo(w, y); lineTo(w - 12f, y - 7f); lineTo(w - 12f, y + 7f); close() }, f.muted)
                val x = (w - 24f) * t + 4f
                drawCircle(CoinEdge, 9f, Offset(x, y)); drawCircle(Coin, 7f, Offset(x, y))
            }
        }
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            InatelLogo(64.dp); Text("operator", style = MaterialTheme.typography.labelSmall, color = f.muted)
        }
    }
}

/** Card header: icon + title + subtitle. */
@Composable
fun WalletHeader(icon: @Composable () -> Unit, title: String, subtitle: String) {
    val f = LocalFarm.current
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        Box(Modifier.size(40.dp).clip(RoundedCornerShape(12.dp)).background(f.surface2), contentAlignment = Alignment.Center) { icon() }
        Column {
            Text(title, color = f.text, fontWeight = FontWeight.SemiBold)
            Text(subtitle, color = f.muted, style = MaterialTheme.typography.bodySmall)
        }
    }
}
