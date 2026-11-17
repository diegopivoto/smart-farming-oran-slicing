package br.inatel.smartfarm.farm.ui

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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import br.inatel.smartfarm.farm.core.ServiceKind
import kotlinx.coroutines.delay
import java.util.Calendar

@Composable
fun HomeScreen(defaultIp: () -> String?, onOpen: (ServiceKind) -> Unit, onSettings: () -> Unit) {
    val f = LocalFarm.current
    var ip by remember { mutableStateOf(defaultIp()) }
    LaunchedEffect(Unit) { while (true) { ip = defaultIp(); delay(3000) } }
    val hour = Calendar.getInstance().get(Calendar.HOUR_OF_DAY)
    val greeting = when (hour) { in 5..11 -> "Good morning"; in 12..17 -> "Good afternoon"; else -> "Good evening" }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp).padding(bottom = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(Modifier.fillMaxWidth().padding(top = 8.dp, start = 4.dp), verticalAlignment = Alignment.Top) {
            Column(Modifier.weight(1f)) {
                Text("SMART FARM · FIELD A", style = MaterialTheme.typography.labelMedium.copy(letterSpacing = 0.3.sp), color = f.muted)
                Text(greeting, style = MaterialTheme.typography.displaySmall, color = f.text, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.padding(top = 2.dp))
            }
            IconCircleButton(FarmIcons.Gear, "Settings", onSettings)
        }
        Text("Pick a service. Each one opens its own 5G network slice.", style = MaterialTheme.typography.bodyMedium, color = f.muted,
            modifier = Modifier.padding(start = 4.dp, end = 4.dp, bottom = 2.dp))
        ServiceKind.entries.forEach { kind -> ServiceCard(kind) { onOpen(kind) } }
        Spacer(Modifier.height(4.dp))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(8.dp).clip(CircleShape).background(if (ip != null) f.ok else f.na))
            Text(if (ip != null) "  Connected to the 5G core · $ip" else "  No mobile data connection",
                style = MaterialTheme.typography.bodySmall, color = f.muted, textAlign = TextAlign.Center)
        }
    }
}

@Composable
private fun ServiceCard(kind: ServiceKind, onClick: () -> Unit) {
    val f = LocalFarm.current
    val (bg, fg) = accent(kind)
    FarmCard(Modifier.fillMaxWidth(), onClick = onClick) {
        ServiceArt(kind, Modifier.fillMaxWidth().height(132.dp))
        Row(Modifier.padding(start = 16.dp, end = 16.dp, top = 12.dp, bottom = 14.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(kind.title, style = MaterialTheme.typography.titleMedium, color = f.text, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(kind.subtitle, style = MaterialTheme.typography.bodySmall.copy(fontSize = 13.sp), color = f.muted, maxLines = 1,
                    overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(top = 2.dp))
            }
            Box(Modifier.size(44.dp).clip(CircleShape).background(bg), contentAlignment = Alignment.Center) {
                Icon(FarmIcons.Arrow, "Open ${kind.title}", Modifier.size(22.dp), tint = fg)
            }
        }
    }
}

@Composable
fun accent(kind: ServiceKind): Pair<Color, Color> {
    val f = LocalFarm.current
    return when (kind) {
        ServiceKind.DRONE -> f.skyC to f.sky
        ServiceKind.CROP -> f.greenC to f.green
        ServiceKind.WEATHER -> f.sunC to f.sunText
    }
}
