package br.inatel.smartfarm.farm.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.unit.dp
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onRoot
import br.inatel.smartfarm.farm.core.Capture
import br.inatel.smartfarm.farm.core.CropProgress
import br.inatel.smartfarm.farm.core.CropUi
import br.inatel.smartfarm.farm.core.DroneFrame
import br.inatel.smartfarm.farm.core.DroneUi
import br.inatel.smartfarm.farm.core.FarmSettings
import br.inatel.smartfarm.farm.core.IrrigationUi
import br.inatel.smartfarm.farm.core.LinkUi
import br.inatel.smartfarm.farm.core.OrchUi
import br.inatel.smartfarm.farm.core.Phase
import br.inatel.smartfarm.farm.core.ServiceKind
import br.inatel.smartfarm.farm.core.SessionUi
import br.inatel.smartfarm.farm.core.WeatherUi
import br.inatel.smartfarm.farm.protocol.CropMeta
import br.inatel.smartfarm.farm.protocol.CropZone
import br.inatel.smartfarm.farm.protocol.SlaMonitor
import br.inatel.smartfarm.farm.protocol.WeatherTelemetry
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File

/**
 * Renders every screen with sample data at the OnePlus 8T size (1080 x 2400 px, 420 dpi = 411 x 914 dp) into
 * app/build/screens/, to check layout, wrapping and clipping without the phone. Opt-in (slow):
 *   SF_SCREENS=1 ./gradlew :app:testDebugUnitTest --tests '*ScreenshotRender*'
 * Set SF_SCREENS_NIGHT=1 for the dark theme.
 */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [34], qualifiers = "w411dp-h914dp-port-420dpi", application = android.app.Application::class)
class ScreenshotRender {
    @get:Rule val rule = createAndroidComposeRule<androidx.activity.ComponentActivity>()

    private val out = File("build/screens").apply { mkdirs() }

    private fun shot(name: String, content: @Composable () -> Unit) {
        assumeTrue(System.getenv("SF_SCREENS") == "1")
        if (System.getenv("SF_SCREENS_NIGHT") == "1") RuntimeEnvironment.setQualifiers("+night")
        rule.mainClock.autoAdvance = false // infinite animations (art, live dot) never go idle
        rule.setContent { FarmTheme { Box(Modifier.fillMaxSize().background(LocalFarm.current.bg)) { content() } } }
        rule.mainClock.advanceTimeBy(1500)
        rule.waitForIdle()
        // View.draw onto a software canvas (captureToImage's forceRedraw never completes under Robolectric)
        val view = rule.activity.window.decorView
        val bmp = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888)
        view.draw(Canvas(bmp))
        File(out, "$name${if (System.getenv("SF_SCREENS_NIGHT") == "1") "-dark" else ""}.png").outputStream().use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    private fun session(kind: ServiceKind, phase: Phase = Phase.RECEIVING) = SessionUi(
        kind, phase, kind.defaultCaps, grantS = 0.49, ip = when (kind) { ServiceKind.WEATHER -> "10.45.192.4"; ServiceKind.CROP -> "10.45.128.4"; else -> "10.45.160.6" },
        iface = "rmnet_data3", firstDataS = 0.9, generator = "10.45.0.2", rttMs = 74.0, waitingS = 3.0,
        offeredBps = br.inatel.smartfarm.farm.protocol.SlaCatalog.DEFAULT_OFFERED_BPS[kind.id],
    )

    private val drought = WeatherTelemetry.parseJson("""{"v":1,"type":"weather","station":"field-A","state":"drought",
        "readings":{"air_temperature_c":31.8,"air_humidity_pct":38,"soil_moisture_pct":24.6,"rain_rate_mm_h":0.0,"rain_today_mm":4.2,
        "wind_speed_kmh":17.8,"wind_direction_deg":135,"solar_radiation_w_m2":780,"uv_index":7.2,"pressure_hpa":1012.6,"leaf_wetness_pct":4},
        "thresholds":{"soil_moisture_pct":{"min":30},"air_humidity_pct":{"min":40},"rain_rate_mm_h":{"max":20}},
        "alerts":[{"id":"dry_soil","severity":"warning","text":"Soil moisture below 30 % — irrigation recommended","action":"irrigation"}],
        "irrigation":{"active":false,"remaining_s":0}}""")!!

    private val storm = WeatherTelemetry.parseJson("""{"v":1,"type":"weather","station":"field-A","state":"storm",
        "readings":{"air_temperature_c":19.2,"air_humidity_pct":93,"soil_moisture_pct":47.1,"rain_rate_mm_h":34.5,"rain_today_mm":28.4,
        "wind_speed_kmh":58.3,"wind_direction_deg":250,"solar_radiation_w_m2":60,"uv_index":0.8,"pressure_hpa":998.2,"leaf_wetness_pct":96},
        "thresholds":{"soil_moisture_pct":{"min":30},"air_humidity_pct":{"min":40},"rain_rate_mm_h":{"max":20}},
        "alerts":[{"id":"excess_rain","severity":"warning","text":"Rain above 20 mm/h — check drainage"}],
        "irrigation":{"active":false,"remaining_s":0}}""")!!

    private fun sla(service: String, dip: Boolean = false): br.inatel.smartfarm.farm.protocol.SlaSnapshot {
        val now = 100_000_000_000L
        val m = SlaMonitor(service) { now }
        if (service == "crop") {
            m.addObject(mapOf("at_ns" to now - 3_000_000_000L, "sha256_valid" to true, "object_transfer_time_s" to 10.9))
            return m.evaluate(cropRateBps = 19.6e6, retransRatio = 0.0041) // contract v1.1: live from the generator
        }
        repeat(10) { i ->
            m.addWindow(mapOf("at_ns" to now - i * 1_000_000_000L, "received" to if (service == "drone") 4660L else 10L,
                "expected_in_window" to if (service == "drone") 4688L else 10L,
                // a 36.4 Mbit/s dip (the live case) is within the new drone SLA: p10 >= 34 Mbit/s
                "goodput_bps" to if (service == "drone") (if (dip && i == 2) 36_400_000L else 39_600_000L) else 100_000L,
                "jitter_s" to 0.0031, "max_receive_gap_s" to if (service == "drone") 0.041 else 0.14, "aoi_p95_s" to 0.18))
        }
        return m.evaluate(silenceS = 0.01)
    }

    private fun fieldBitmap(ndvi: Boolean, w: Int = 1600, h: Int = 900): Bitmap {
        val b = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        val c = Canvas(b)
        c.drawColor(if (ndvi) 0xFF2E9A47.toInt() else 0xFF5E9E45.toInt())
        val p = Paint().apply { color = if (ndvi) 0xFFF4D03F.toInt() else 0xFF7A5A3A.toInt() }
        var y = 20f
        while (y < h) { c.drawRect(0f, y, w.toFloat(), y + 22f, p); y += 90f }
        p.color = if (ndvi) 0xFFD7302A.toInt() else 0xFFC3A044.toInt()
        c.drawOval(RectF(w * 0.56f, h * 0.18f, w * 0.75f, h * 0.44f), p)
        return b
    }

    @Test fun home() = shot("01-home") { HomeScreen({ "10.45.0.15" }, {}, {}) }

    @Test fun connecting() = shot("02-connecting") {
        ConnectingScreen(session(ServiceKind.WEATHER, Phase.LINK_UP), ServiceKind.WEATHER, {}, {}, {})
    }

    @Test fun connectingOrchestrated() = shot("02c-connecting-orchestrator") {
        ConnectingScreen(session(ServiceKind.CROP, Phase.LINK_UP).copy(generator = "10.45.0.4", orch = OrchUi(enabled = true,
            session = "0123456789abcdef", state = "generator_started", un3gppIp = "10.45.0.4",
            reached = listOf("accepted", "un3gpp_switching", "un3gpp_in_slice", "generator_started"))), ServiceKind.CROP, {}, {}, {})
    }

    @Test fun connectingOrchestratorFailed() = shot("02d-connecting-orchestrator-failed") {
        ConnectingScreen(session(ServiceKind.DRONE, Phase.LINK_UP).copy(orch = OrchUi(enabled = true, session = "0123456789abcdef",
            state = "failed", reached = listOf("accepted", "un3gpp_switching", "failed"),
            cause = "UN3GPP slice switch failed: N3IWUE slice switch rejected (active SD 000002)")), ServiceKind.DRONE, {}, {}, {})
    }

    @Test fun connectingOrchestratorUnavailable() = shot("02e-connecting-orchestrator-unavailable") {
        ConnectingScreen(session(ServiceKind.WEATHER, Phase.LINK_UP).copy(orch = OrchUi(enabled = true,
            notice = "Lab orchestrator unavailable (not reachable at http://10.45.0.1:8090). Using the manual generator address 10.45.0.2.")),
            ServiceKind.WEATHER, {}, {}, {})
    }

    @Test fun connectingOrchestratorIpHop() = shot("02f-connecting-orchestrator-ip-hop") {
        ConnectingScreen(session(ServiceKind.DRONE, Phase.LINK_UP).copy(orch = OrchUi(enabled = true, session = "0123456789abcdef",
            state = "un3gpp_switching", reason = br.inatel.smartfarm.farm.protocol.Orchestration.IP_CONFLICT, un3gppIp = "10.45.0.23",
            reached = listOf("accepted", "un3gpp_switching"))), ServiceKind.DRONE, {}, {}, {})
    }

    @Test fun connectingFailed() = shot("02b-connecting-failed") {
        ConnectingScreen(session(ServiceKind.DRONE, Phase.UNAVAILABLE).copy(grantS = null, ip = null,
            error = "No network satisfied cellular + MMS+CBS within 30 s (APN type, modem or core)"), ServiceKind.DRONE, {}, {}, {})
    }

    @Test fun weatherAlert() = shot("03-weather-alert") {
        WeatherScreen(WeatherUi(drought, 0), IrrigationUi(), sla("weather"), session(ServiceKind.WEATHER), LinkUi(71.0, 100_000.0), {}, {}, {}, {})
    }

    @Test fun weatherIrrigating() = shot("03b-weather-irrigating") {
        val t = drought.copy(alerts = emptyList(), irrigationActive = true, irrigationRemainingS = 58.0, state = "stable",
            readings = drought.readings + mapOf(WeatherTelemetry.SOIL_MOISTURE to 29.1, WeatherTelemetry.AIR_HUMIDITY to 44.0))
        WeatherScreen(WeatherUi(t, 0), IrrigationUi(ok = true), sla("weather"), session(ServiceKind.WEATHER), LinkUi(71.0, 100_000.0), {}, {}, {}, {})
    }

    @Test fun weatherStorm() = shot("03c-weather-storm") {
        WeatherScreen(WeatherUi(storm, 0), IrrigationUi(), sla("weather"), session(ServiceKind.WEATHER), LinkUi(80.0, 100_000.0), {}, {}, {}, {})
    }

    @Test fun crop() = shot("04-crop") {
        val meta = CropMeta("12", "field-A", "pest", 1600, 900, 0.64, listOf(CropZone(0.56f, 0.18f, 0.19f, 0.26f, "Pest hotspot", "high")), "")
        val rgb = fieldBitmap(false); val ndvi = fieldBitmap(true)
        val caps = (12L downTo 7L).map { id ->
            Capture(id, meta, rgb, ndvi, Bitmap.createScaledBitmap(rgb, 240, 135, true), Bitmap.createScaledBitmap(ndvi, 240, 135, true), true, 10.9, 19.6e6, 1_762_180_562_000L)
        }
        CropScreen(CropUi(caps, CropProgress(13, 16_800_000, 26_214_400, 0)), sla("crop"), session(ServiceKind.CROP), {})
    }

    @Test fun cropWaiting() = shot("04b-crop-waiting") {
        CropScreen(CropUi(emptyList(), CropProgress(0, 4_000_000, 26_214_400, 0)), null, session(ServiceKind.CROP), {})
    }

    @Test fun drone() = shot("05-drone") {
        val frame = Bitmap.createBitmap(960, 540, Bitmap.Config.ARGB_8888).also { b ->
            val c = Canvas(b)
            c.drawColor(0xFF5F9C47.toInt())
            val p = Paint().apply { color = 0xFF3F7A35.toInt(); strokeWidth = 26f }
            for (x in -400 until 1400 step 90) c.drawLine(x.toFloat(), 540f, x + 300f, 0f, p)
        }
        DroneScreen(mutableStateOf(DroneFrame(frame, 1)), DroneUi(29.6, 1780, 1, 0, 9.0, 39.6e6, 68.0), sla("drone", dip = true), session(ServiceKind.DRONE), {})
    }

    private val oranJson = """{"available":true,"controller":"e2-action","decided_by":"E2 action (O-RAN xApp)",
        "uplink":{"label":"MCS 11 · 66 RB","by_e2":true,"stale":false,"readback":"MAC uses MCS 11 · 66 RB · SNR 22.4 dB",
          "last_action":{"status":"REJECTED_STALE","ok":false,"value":"MCS 13 · 99 RB","age_s":1.1}},
        "downlink":{"label":"78 PRB quota","by_e2":true,"stale":false,"readback":"gNB enforces 78 PRB for the phone in this slice",
          "last_action":{"status":"APPLIED","ok":true,"value":"78 PRB","age_s":2.0}},
        "history":[{"link":"uplink","status":"REJECTED_STALE","ok":false,"value":"UL MCS 13 · 99 RB","age_s":1.1},
          {"link":"downlink","status":"APPLIED","ok":true,"value":"DL 78 PRB","age_s":2.0},
          {"link":"uplink","status":"CONSISTENT","ok":true,"value":"UL MCS 11 · 66 RB","age_s":16.1},
          {"link":"uplink","status":"CONFIRMED","ok":true,"value":"UL MCS 9 · 66 RB","age_s":31.1}]}"""

    private val ranDefaultsJson = """{"available":true,"controller":"ran-default","decided_by":"RAN defaults",
        "uplink":{"label":"BS link adaptation (MCS 8)","by_e2":false,"stale":false,
          "last_action":{"status":"RELEASED","ok":true,"value":"BS link adaptation","age_s":21403.8}},
        "downlink":{"label":"No quota · 106 PRB","by_e2":false,"stale":true}}"""

    private fun oran(json: String) = br.inatel.smartfarm.farm.protocol.Orchestration.parseOran(org.json.JSONObject("""{"oran":$json}"""))

    private val ddqnJson = """{"available":true,"controller":"e2-action","decided_by":"DDQN agent (step 4)",
        "mode":{"requested":"ddqn","effective":"ddqn","detail":"frozen DDQN agent in control"},"agent_running":true,
        "agent":{"running":true,"step":4,"last_verdict":"met"},
        "uplink":{"label":"MCS 9 · 99 RB","by_e2":true,"stale":false,"readback":"MAC uses MCS 9 · 99 RB · SNR 22.6 dB",
          "last_action":{"status":"CONFIRMED","ok":true,"value":"MCS 9 · 99 RB","age_s":4.0}},
        "downlink":{"label":"52 PRB quota","by_e2":true,"stale":false,"readback":"gNB enforces 52 PRB for the phone in this slice",
          "last_action":{"status":"APPLIED","ok":true,"value":"52 PRB","age_s":3.0}},
        "history":[{"link":"step","value":"#3 UL MCS 9 / 99 RB + DL 52 PRB","status":"SLA met","ok":true,"age_s":8.0},
          {"link":"step","value":"#2 UL MCS 10 / 99 RB + DL 52 PRB","status":"SLA not met","ok":false,"age_s":44.0},
          {"link":"step","value":"#1 UL MCS 9 / 99 RB + DL 52 PRB","status":"SLA undetermined","ok":null,"age_s":80.0},
          {"link":"step","value":"#0 UL link adaptation + DL 52 PRB","status":"SLA met","ok":true,"age_s":116.0}]}"""

    private val busyJson = """{"available":true,"controller":"ran-default","decided_by":"RAN defaults",
        "mode":{"requested":"ddqn","effective":"ran-default","detail":"xApp busy: another control client is connected"},
        "uplink":{"label":"BS link adaptation (MCS 8)","by_e2":false,"stale":false},
        "downlink":{"label":"No quota · 106 PRB","by_e2":false,"stale":false}}"""

    @Test fun oranCardModes() = shot("07b-oran-card-modes") {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            OranCard(oran(busyJson))
            OranCard(oran(ddqnJson))
            OranCard(oran(ddqnJson), initiallyExpanded = true)
        }
    }

    @Test fun oranCard() = shot("07-oran-card") {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            OranCard(oran(ranDefaultsJson))
            OranCard(oran(oranJson))
            OranCard(oran(oranJson), initiallyExpanded = true)
        }
    }

    @Test fun settings() = shot("06-settings") {
        SettingsScreen(FarmSettings(RuntimeEnvironment.getApplication()), {})
    }
}
