package br.inatel.smartfarm.farm.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import br.inatel.smartfarm.farm.BuildConfig
import br.inatel.smartfarm.farm.core.FarmSettings
import br.inatel.smartfarm.farm.core.ServiceKind
import br.inatel.smartfarm.farm.protocol.Orchestration

@Composable
fun SettingsScreen(settings: FarmSettings, onBack: () -> Unit) {
    val f = LocalFarm.current
    var reset by remember { mutableIntStateOf(0) }
    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        AppBar("Settings", onBack)
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(bottom = 16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            key(reset) {
                Section("Lab connection")
                Toggle("Use lab orchestrator", "Moves the UN3GPP UE and starts its generator",
                    settings.useOrchestrator) { settings.useOrchestrator = it }
                Choice("O-RAN radio control", "Applies to the next service session (lab orchestrator)",
                    listOf("RAN defaults", "DDQN agent"), Orchestration.ORAN_MODES.indexOf(settings.oranMode).coerceAtLeast(0)) {
                    settings.oranMode = Orchestration.ORAN_MODES[it]
                }
                Field("Lab orchestrator URL", settings.orchestratorUrl, KeyboardType.Uri) { settings.orchestratorUrl = it }
                Field("Manual generator address (UN3GPP UE, only without the orchestrator)", settings.generatorAddress, KeyboardType.Uri) { settings.generatorAddress = it }
                Field("Lab collector URL", settings.collectorUrl, KeyboardType.Uri) { settings.collectorUrl = it }
                Toggle("Report metrics to the lab collector", "Per-second quality records for the RL agent",
                    settings.reportEnabled) { settings.reportEnabled = it }
                Section("Network request per service")
                ServiceKind.entries.forEach { k ->
                    Field("${k.title} · slice SD ${k.sd}", settings.caps(k), KeyboardType.Ascii) { settings.setCaps(k, it) }
                }
                Text("Each capability set must match the APN type of that service on the phone (crop = mms,fota · drone = mms,cbs · weather = supl). " +
                    "Separate with +; prefix with - to remove a default capability.",
                    style = MaterialTheme.typography.bodySmall, color = f.muted, modifier = Modifier.padding(horizontal = 4.dp))
            }
            Spacer(Modifier.height(4.dp))
            FarmButton("Restore defaults", { settings.restoreDefaults(); reset++ }, filled = false)
            Text("Smart Farm ${BuildConfig.VERSION_NAME} · font Inter (SIL OFL)", style = MaterialTheme.typography.bodySmall, color = f.muted,
                modifier = Modifier.padding(horizontal = 4.dp))
        }
    }
}

@Composable
private fun Toggle(title: String, subtitle: String, initial: Boolean, onChange: (Boolean) -> Unit) {
    val f = LocalFarm.current
    var on by remember { mutableStateOf(initial) }
    FarmCard(Modifier.fillMaxWidth()) {
        Row(Modifier.padding(horizontal = 16.dp, vertical = 14.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.titleSmall, color = f.text)
                Text(subtitle, style = MaterialTheme.typography.bodySmall.copy(fontSize = 12.5.sp), color = f.muted)
            }
            Spacer(Modifier.width(12.dp))
            Switch(on, { on = it; onChange(it) },
                colors = SwitchDefaults.colors(checkedTrackColor = f.green, checkedThumbColor = Color.White))
        }
    }
}

@Composable
private fun Choice(title: String, subtitle: String, options: List<String>, initial: Int, onChange: (Int) -> Unit) {
    val f = LocalFarm.current
    var selected by remember { mutableIntStateOf(initial) }
    FarmCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(horizontal = 16.dp, vertical = 14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Column {
                Text(title, style = MaterialTheme.typography.titleSmall, color = f.text)
                Text(subtitle, style = MaterialTheme.typography.bodySmall.copy(fontSize = 12.5.sp), color = f.muted)
            }
            Segmented(options, selected) { selected = it; onChange(it) }
        }
    }
}

@Composable
private fun Section(text: String) {
    Text(text, style = MaterialTheme.typography.labelMedium, color = LocalFarm.current.green, modifier = Modifier.padding(start = 4.dp, top = 4.dp))
}

@Composable
private fun Field(label: String, initial: String, type: KeyboardType, onChange: (String) -> Unit) {
    val f = LocalFarm.current
    var value by remember { mutableStateOf(initial) }
    OutlinedTextField(
        value = value, onValueChange = { value = it; onChange(it) }, singleLine = true,
        label = { Text(label, maxLines = 1) },
        keyboardOptions = KeyboardOptions(keyboardType = type),
        shape = RoundedCornerShape(14.dp),
        textStyle = MaterialTheme.typography.bodyLarge.copy(color = f.text),
        colors = OutlinedTextFieldDefaults.colors(focusedContainerColor = f.surface, unfocusedContainerColor = f.surface,
            focusedBorderColor = f.green, focusedLabelColor = f.green),
        modifier = Modifier.fillMaxWidth(),
    )
}
