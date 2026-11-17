package br.inatel.smartfarm.farm.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import br.inatel.smartfarm.farm.R

/** Brand palette of the approved mockup (docs/design/app-mockup/index.html). Fixed theme, no dynamic color. */
@Immutable
data class FarmColors(
    val bg: Color, val surface: Color, val surface2: Color, val line: Color,
    val text: Color, val muted: Color,
    val green: Color, val green2: Color, val greenC: Color,
    val soil: Color, val soilC: Color,
    val sky: Color, val skyC: Color,
    val sun: Color, val sunC: Color, val sunText: Color,
    val ok: Color, val okC: Color, val bad: Color, val badC: Color, val na: Color,
    val alertTop: Color, val alertBottom: Color, val alertBorder: Color, val alertTitle: Color, val alertText: Color,
    val waterTop: Color, val waterBottom: Color, val waterTitle: Color, val waterText: Color,
    val track: Color,
    val dark: Boolean,
)

val LightFarm = FarmColors(
    bg = Color(0xFFF5F3EA), surface = Color(0xFFFFFFFF), surface2 = Color(0xFFEEF2E6), line = Color(0xFFE1E4D6),
    text = Color(0xFF1D2A1F), muted = Color(0xFF5E6B5F),
    green = Color(0xFF2F6B3A), green2 = Color(0xFF4E9A4F), greenC = Color(0xFFDCEBD3),
    soil = Color(0xFF8A5A3B), soilC = Color(0xFFF1E2D3),
    sky = Color(0xFF3A7CA5), skyC = Color(0xFFD9EAF3),
    sun = Color(0xFFE9A23B), sunC = Color(0xFFFCEBC9), sunText = Color(0xFFA86A12),
    ok = Color(0xFF2E8B57), okC = Color(0xFFDDF1E3), bad = Color(0xFFC6453A), badC = Color(0xFFFADCD7), na = Color(0xFFC9CEC4),
    alertTop = Color(0xFFFFF4DD), alertBottom = Color(0xFFFBE3BE), alertBorder = Color(0xFFF0D29E),
    alertTitle = Color(0xFF6B4210), alertText = Color(0xFF7A5A2E),
    waterTop = Color(0xFFDDEFF8), waterBottom = Color(0xFFCFE6F3), waterTitle = Color(0xFF1F4E6E), waterText = Color(0xFF3E6580),
    track = Color(0xFFECEADF),
    dark = false,
)

val DarkFarm = FarmColors(
    bg = Color(0xFF121812), surface = Color(0xFF1B231C), surface2 = Color(0xFF243025), line = Color(0xFF2E3A2F),
    text = Color(0xFFE6ECE2), muted = Color(0xFFA3B09F),
    green = Color(0xFF8FCB8F), green2 = Color(0xFF6DB56E), greenC = Color(0xFF2A3D2B),
    soil = Color(0xFFD9A47F), soilC = Color(0xFF3A2C22),
    sky = Color(0xFF8CC3E6), skyC = Color(0xFF1F3442),
    sun = Color(0xFFF2B95A), sunC = Color(0xFF3D3020), sunText = Color(0xFFF2C77A),
    ok = Color(0xFF5CC489), okC = Color(0xFF1F3A2A), bad = Color(0xFFF07B70), badC = Color(0xFF462522), na = Color(0xFF4F5A4E),
    alertTop = Color(0xFF3A2E1A), alertBottom = Color(0xFF453419), alertBorder = Color(0xFF5E4824),
    alertTitle = Color(0xFFF7D9A6), alertText = Color(0xFFE1C79C),
    waterTop = Color(0xFF1B3445), waterBottom = Color(0xFF1F3B4F), waterTitle = Color(0xFFCBE6F7), waterText = Color(0xFFA6C8DD),
    track = Color(0xFF2B352C),
    dark = true,
)

val LocalFarm = staticCompositionLocalOf { LightFarm }

val Inter = FontFamily(
    Font(R.font.inter_regular, FontWeight.Normal),
    Font(R.font.inter_medium, FontWeight.Medium),
    Font(R.font.inter_semibold, FontWeight.SemiBold),
    Font(R.font.inter_bold, FontWeight.Bold),
    Font(R.font.inter_extrabold, FontWeight.ExtraBold),
)

private fun t(size: Int, weight: FontWeight, line: Int = (size * 1.3).toInt()) =
    TextStyle(fontFamily = Inter, fontSize = size.sp, fontWeight = weight, lineHeight = line.sp)

private val FarmTypography = Typography(
    displaySmall = t(28, FontWeight.ExtraBold, 34),
    headlineSmall = t(22, FontWeight.ExtraBold, 28),
    titleLarge = t(19, FontWeight.Bold, 24),
    titleMedium = t(17, FontWeight.Bold, 22),
    titleSmall = t(15, FontWeight.Bold, 20),
    bodyLarge = t(15, FontWeight.Normal, 21),
    bodyMedium = t(14, FontWeight.Normal, 20),
    bodySmall = t(12, FontWeight.Normal, 16),
    labelLarge = t(16, FontWeight.Bold, 20),
    labelMedium = t(13, FontWeight.SemiBold, 17),
    labelSmall = t(12, FontWeight.SemiBold, 15),
)

@Composable
fun FarmTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val f = if (dark) DarkFarm else LightFarm
    val scheme = if (dark) {
        darkColorScheme(primary = f.green, onPrimary = Color(0xFF0F2412), primaryContainer = f.greenC, background = f.bg,
            surface = f.surface, onSurface = f.text, onBackground = f.text, surfaceVariant = f.surface2, outline = Color(0xFF4A574A),
            secondary = f.soil, tertiary = f.sky, error = f.bad)
    } else {
        lightColorScheme(primary = f.green, onPrimary = Color.White, primaryContainer = f.greenC, background = f.bg,
            surface = f.surface, onSurface = f.text, onBackground = f.text, surfaceVariant = f.surface2, outline = Color(0xFFB9C2B2),
            secondary = f.soil, tertiary = f.sky, error = f.bad)
    }
    CompositionLocalProvider(LocalFarm provides f) {
        MaterialTheme(colorScheme = scheme, typography = FarmTypography, content = content)
    }
}
