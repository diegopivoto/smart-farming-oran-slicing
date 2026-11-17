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
import androidx.compose.foundation.layout.padding
import androidx.compose.ui.unit.dp
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
import br.inatel.smartfarm.farm.ui.PaymentScreen
import br.inatel.smartfarm.farm.ui.WalletScreen
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

    val inService = screen.startsWith("connect:") || screen.startsWith("service:") || screen.startsWith("swallet:")
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
    BackHandler(enabled = screen != "home") {
        when {
            screen == "settings" || screen.startsWith("pay:") -> screen = "home"
            screen == "wallet" -> screen = "settings"
            screen.startsWith("swallet:") -> screen = "service:${screen.substringAfter(':')}"   // the session keeps running
            else -> goHome()
        }
    }

    Box(Modifier.fillMaxSize().background(LocalFarm.current.bg).systemBarsPadding()) {
        AnimatedContent(screen, transitionSpec = { fadeIn(tween(220)) togetherWith fadeOut(tween(160)) }, label = "nav") { s ->
            val k = ServiceKind.byId(s.substringAfter(':', ""))
            when {
                s == "settings" -> SettingsScreen(c.settings, onWallet = { screen = "wallet" }) { screen = "home" }
                s == "wallet" -> WalletScreen(c.wallet) { screen = "settings" }
                s.startsWith("pay:") && k != null -> PaymentScreen(c.wallet, k,
                    onPaid = { c.open(k); screen = "connect:${k.id}" }, onCancel = { screen = "home" })
                s.startsWith("connect:") && k != null -> ConnectingScreen(session, k, onCancel = goHome, onRetry = { c.retry() },
                    onContinue = { screen = "service:${k.id}" })
                s.startsWith("swallet:") && k != null -> WalletScreen(c.wallet) { screen = "service:${k.id}" }
                s.startsWith("service:") && k != null -> Box(Modifier.fillMaxSize()) {
                    ServiceScreen(c, k, goHome)
                    // live wallet without leaving the slice session (screen recording of the per-minute charges)
                    br.inatel.smartfarm.farm.ui.FarmButton("Wallet", { screen = "swallet:${k.id}" },
                        modifier = Modifier.align(androidx.compose.ui.Alignment.BottomEnd).padding(16.dp), height = 40.dp)
                }
                else -> HomeScreen({ c.defaultAddress() }, onOpen = { svc -> screen = "pay:${svc.id}" },
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
