package br.inatel.smartfarm.farm

import android.Manifest
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import br.inatel.smartfarm.farm.core.FarmController
import br.inatel.smartfarm.farm.core.Phase
import br.inatel.smartfarm.farm.core.ServiceKind
import br.inatel.smartfarm.farm.ui.ConnectingScreen
import br.inatel.smartfarm.farm.ui.CropScreen
import br.inatel.smartfarm.farm.ui.DroneScreen
import br.inatel.smartfarm.farm.ui.FarmTheme
import br.inatel.smartfarm.farm.ui.HomeScreen
import br.inatel.smartfarm.farm.ui.LocalFarm
import br.inatel.smartfarm.farm.ui.SettingsScreen
import br.inatel.smartfarm.farm.ui.WeatherScreen
import kotlinx.coroutines.delay

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.auto(android.graphics.Color.TRANSPARENT, android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.auto(android.graphics.Color.TRANSPARENT, android.graphics.Color.TRANSPARENT),
        )
        super.onCreate(savedInstanceState)
        CrashReporter.install(applicationContext)
        if (Build.VERSION.SDK_INT >= 33) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        val controller = (application as FarmApp).controller
        setContent { FarmTheme { App(controller) } }
    }

    fun keepScreenOn(on: Boolean) {
        if (on) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }
}

/** Screens: "home", "settings", "connect:<service>", "service:<service>". */
@Composable
private fun App(c: FarmController) {
    var screen by rememberSaveable { mutableStateOf("home") }
    val session by c.session.collectAsState()
    val activity = androidx.compose.ui.platform.LocalContext.current as? MainActivity

    val inService = screen.startsWith("connect:") || screen.startsWith("service:")
    DisposableEffect(inService) {
        activity?.keepScreenOn(inService)
        onDispose { }
    }
    // after process death or an external close, there is no session behind a service screen: go home
    LaunchedEffect(session == null, screen) {
        if (inService && session == null) { delay(300); if (c.session.value == null) screen = "home" }
    }
    // first data in: show the service screen after a short beat so the last step is visible
    LaunchedEffect(session?.phase, screen) {
        val s = session ?: return@LaunchedEffect
        if (screen.startsWith("connect:") && s.phase == Phase.RECEIVING) { delay(700); screen = "service:${s.kind.id}" }
        if (screen.startsWith("service:") && (s.phase == Phase.LOST || s.phase == Phase.ERROR)) screen = "connect:${s.kind.id}"
    }
    val goHome = { c.close("back"); screen = "home" }
    BackHandler(enabled = screen != "home") { if (screen == "settings") screen = "home" else goHome() }

    Box(Modifier.fillMaxSize().background(LocalFarm.current.bg).systemBarsPadding()) {
        AnimatedContent(screen, transitionSpec = { fadeIn(tween(220)) togetherWith fadeOut(tween(160)) }, label = "nav") { s ->
            val k = ServiceKind.byId(s.substringAfter(':', ""))
            when {
                s == "settings" -> SettingsScreen(c.settings) { screen = "home" }
                s.startsWith("connect:") && k != null -> ConnectingScreen(session, k, onCancel = goHome, onRetry = { c.retry() },
                    onContinue = { screen = "service:${k.id}" })
                s.startsWith("service:") && k != null -> ServiceScreen(c, k, goHome)
                else -> HomeScreen({ c.defaultAddress() }, onOpen = { svc -> c.open(svc); screen = "connect:${svc.id}" },
                    onSettings = { screen = "settings" })
            }
        }
    }
}

@Composable
private fun ServiceScreen(c: FarmController, kind: ServiceKind, onBack: () -> Unit) {
    val session by c.session.collectAsState()
    val sla by c.sla.collectAsState()
    when (kind) {
        ServiceKind.WEATHER -> {
            val weather by c.weather.collectAsState()
            val irrigation by c.irrigation.collectAsState()
            val link by c.link.collectAsState()
            WeatherScreen(weather, irrigation, sla, session, link, onBack, onIrrigate = { c.startIrrigation() },
                onScenario = { c.setWeatherState(it) }, onDismissMessage = { c.dismissIrrigationMessage() })
        }
        ServiceKind.CROP -> {
            val crop by c.crop.collectAsState()
            CropScreen(crop, sla, session, onBack)
        }
        ServiceKind.DRONE -> {
            val drone by c.drone.collectAsState()
            val frame = c.frame.collectAsState()
            DroneScreen(frame, drone, sla, session, onBack)
        }
    }
}
