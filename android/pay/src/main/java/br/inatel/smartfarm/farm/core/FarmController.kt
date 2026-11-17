package br.inatel.smartfarm.farm.core

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.ConnectivityManager
import android.net.Network
import android.os.SystemClock
import androidx.core.content.ContextCompat
import br.inatel.smartfarm.farm.protocol.ClockSync
import br.inatel.smartfarm.farm.protocol.CropMeta
import br.inatel.smartfarm.farm.protocol.FrameAssembler
import br.inatel.smartfarm.farm.protocol.Orchestration
import br.inatel.smartfarm.farm.protocol.SlaCatalog
import br.inatel.smartfarm.farm.protocol.SlaMonitor
import br.inatel.smartfarm.farm.protocol.SlaSnapshot
import br.inatel.smartfarm.farm.protocol.SlaSpec
import br.inatel.smartfarm.farm.protocol.UdpStats
import br.inatel.smartfarm.farm.protocol.WallClock
import br.inatel.smartfarm.farm.protocol.WeatherTelemetry
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.net.DatagramSocket
import java.net.Inet4Address
import java.util.UUID
import java.util.concurrent.Executors

enum class Phase { REQUESTING, GRANTED, LINK_UP, RECEIVING, LOST, UNAVAILABLE, ERROR }

data class SessionUi(
    val kind: ServiceKind,
    val phase: Phase,
    val caps: String,
    val grantS: Double? = null,
    val ip: String? = null,
    val iface: String? = null,
    val ipInRange: Boolean = true,
    val error: String? = null,
    val firstDataS: Double? = null,
    val generator: String,
    val rttMs: Double? = null,
    val waitingS: Double = 0.0,
    val orch: OrchUi = OrchUi(),
    /** Offered load of the service: the orchestrator's (SERVICE_CATALOG) when it answered, else the built-in one. */
    val offeredBps: Double? = null,
    /** O-RAN control in force for the slice, from the orchestrator's view (read-only card; null hides it). */
    val oran: Orchestration.Oran? = null,
)

/** Progress of the lab orchestrator (PC1) for this session: UN3GPP UE slice switch and generator start. */
data class OrchUi(
    /** "Use lab orchestrator" was on when the session opened. */
    val enabled: Boolean = false,
    val session: String? = null,
    val state: String? = null,
    val reached: List<String> = emptyList(),
    /** UN3GPP UE address on the slice = the generator address. */
    val un3gppIp: String? = null,
    val cause: String? = null,
    /** Friendly notice when the orchestrator could not be used (manual generator address instead). */
    val notice: String? = null,
    /** Session time (as waitingS) when the generator was confirmed streaming. */
    val streamingAtS: Double? = null,
    /** Machine-readable reason of the orchestrator's latest extra step (e.g. Orchestration.IP_CONFLICT). */
    val reason: String? = null,
    /** Seconds without a valid orchestrator reply while connecting (null while it answers). */
    val silentS: Double? = null,
) {
    /** The orchestrator drives this session (enabled and it accepted the request, or is about to). */
    val active get() = enabled && notice == null
    val failed get() = active && state in Orchestration.TERMINAL
    fun reached(step: String) = step in reached
}

data class WeatherUi(val telemetry: WeatherTelemetry, val receivedAtMs: Long)

data class IrrigationUi(val sending: Boolean = false, val message: String? = null, val ok: Boolean? = null, val localUntilMs: Long = 0)

data class Capture(
    val objectId: Long,
    val meta: CropMeta?,
    val rgb: Bitmap?,
    val ndvi: Bitmap?,
    val thumb: Bitmap?,
    val ndviThumb: Bitmap?,
    val verified: Boolean,
    val transferS: Double?,
    val goodputBps: Double,
    val receivedAtMs: Long,
)

data class CropUi(val captures: List<Capture> = emptyList(), val inFlight: CropProgress? = null, val rejected: Int = 0, val lastRejectedId: Long? = null)

data class DroneUi(
    val fps: Double = 0.0,
    val framesShown: Long = 0,
    val framesDropped: Long = 0,
    val framesSkipped: Long = 0,
    val decodeMs: Double = 0.0,
    val goodputBps: Double? = null,
    val delayMs: Double? = null,
    val fillers: Long = 0,
)

class DroneFrame(val bitmap: Bitmap, val version: Long)

/** Live delay of the last window (one-way, generator -> phone, through the clock offset). */
data class LinkUi(val delayMeanMs: Double? = null, val goodputBps: Double? = null)

/**
 * Process-wide owner of the active service session: the slice network request, the receivers, the per-second
 * collector records (same as sf_traffic.py receive) and the live SLA. The foreground [KeepAliveService] keeps the
 * process (and so the requested network) alive while a session is open.
 */
class FarmController(private val app: Context) {
    val settings = FarmSettings(app)
    val wallet = Wallet(app, settings)
    val reporter = Reporter({ settings.collectorUrl }, { settings.reportEnabled })
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val imageExecutor = Executors.newSingleThreadExecutor()

    private val _session = MutableStateFlow<SessionUi?>(null)
    val session: StateFlow<SessionUi?> = _session
    private val _weather = MutableStateFlow<WeatherUi?>(null)
    val weather: StateFlow<WeatherUi?> = _weather
    private val _irrigation = MutableStateFlow(IrrigationUi())
    val irrigation: StateFlow<IrrigationUi> = _irrigation
    private val _crop = MutableStateFlow(CropUi())
    val crop: StateFlow<CropUi> = _crop
    private val _drone = MutableStateFlow(DroneUi())
    val drone: StateFlow<DroneUi> = _drone
    private val _frame = MutableStateFlow<DroneFrame?>(null)
    val frame: StateFlow<DroneFrame?> = _frame
    private val _sla = MutableStateFlow<SlaSnapshot?>(null)
    val sla: StateFlow<SlaSnapshot?> = _sla
    private val _link = MutableStateFlow(LinkUi())
    val link: StateFlow<LinkUi> = _link

    private var slice: SliceSession? = null
    private var network: Network? = null
    private var receivers = ArrayList<Stoppable>()
    private var ticker: Job? = null
    private var stats: UdpStats? = null
    private var monitor: SlaMonitor? = null
    private var clock: ClockSync? = null
    private var assembler: FrameAssembler? = null
    private var decoder: JpegDecoder? = null
    private var cropReceiver: CropReceiver? = null
    private var appRunId = ""
    @Volatile private var orchSession: Pair<String, String>? = null // (base URL, session id) to DELETE on close
    private var orchJob: Job? = null
    private var openedMs = 0L
    @Volatile private var firstData = false
    /** SLA targets from the orchestrator's session view (null: built-in SlaCatalog). */
    @Volatile private var slaSpecs: List<SlaSpec>? = null
    /** Generator's cumulative TCP retransmitted-bytes ratio, from the latest valid crop object (contract v1.1). */
    @Volatile private var retransRatio: Double? = null
    private val generation = java.util.concurrent.atomic.AtomicInteger()

    /** Phone's default-session address (the `internet` DNN, 10.45.0.x), shown on the home screen. */
    fun defaultAddress(): String? {
        val cm = app.getSystemService(ConnectivityManager::class.java)
        val lp = cm.getLinkProperties(cm.activeNetwork ?: return null) ?: return null
        return lp.linkAddresses.map { it.address }.filterIsInstance<Inet4Address>().firstOrNull()?.hostAddress
    }

    // ------------------------------------------------------------------------------------------ session lifecycle

    fun open(kind: ServiceKind) {
        close("new session")
        val gen = generation.incrementAndGet()
        appRunId = UUID.randomUUID().toString().replace("-", "")
        openedMs = SystemClock.elapsedRealtime()
        firstData = false
        slaSpecs = null
        retransRatio = null
        _weather.value = null; _crop.value = CropUi(); _drone.value = DroneUi(); _frame.value = null
        _sla.value = null; _link.value = LinkUi(); _irrigation.value = IrrigationUi()
        val caps = settings.caps(kind)
        _session.value = SessionUi(kind, Phase.REQUESTING, caps, generator = settings.generatorAddress,
            orch = OrchUi(enabled = settings.useOrchestrator), offeredBps = SlaCatalog.DEFAULT_OFFERED_BPS[kind.id])
        ContextCompat.startForegroundService(app, Intent(app, KeepAliveService::class.java).putExtra("service", kind.title))
        val s = SliceSession(app, kind, caps, object : SliceSession.Listener {
            override fun onGranted(network: Network, elapsedS: Double) {
                if (gen != generation.get()) return
                _session.update { it?.copy(phase = Phase.GRANTED, grantS = elapsedS) }
                event("granted", mapOf("grant_s" to elapsedS))
            }

            override fun onLinkUp(network: Network, ip: Inet4Address, iface: String?) {
                if (gen != generation.get()) return
                this@FarmController.network = network
                _session.update {
                    it?.copy(phase = Phase.LINK_UP, ip = ip.hostAddress, iface = iface, ipInRange = kind.owns(ip))
                }
                event("session_up", mapOf("phone_ip" to ip.hostAddress, "iface" to iface, "ip_in_service_range" to kind.owns(ip)))
                startReceivers(kind, network, ip, gen)
                if (_session.value?.orch?.enabled == true && orchJob == null) {
                    orchJob = scope.launch(Dispatchers.IO) { orchestrate(kind, ip.hostAddress ?: "", gen) }
                }
            }

            override fun onLost(network: Network) {
                if (gen != generation.get()) return
                stopReceivers()
                _session.update { it?.copy(phase = Phase.LOST, error = "The ${kind.sliceName} slice session was released by the network") }
                event("lost", emptyMap())
            }

            override fun onUnavailable(reason: String) {
                if (gen != generation.get()) return
                _session.update { it?.copy(phase = Phase.UNAVAILABLE, error = reason) }
                event("unavailable", mapOf("reason" to reason))
            }
        })
        slice = s
        s.start()
        event("request", mapOf("caps" to caps))
    }

    fun retry() { _session.value?.kind?.let { open(it) } }

    fun close(why: String = "user") {
        val had = slice != null || _session.value != null
        generation.incrementAndGet()
        orchJob?.cancel(); orchJob = null
        orchSession?.let { (base, id) ->
            orchSession = null
            event("orchestrator_stop", mapOf("orchestrator_session" to id, "why" to why))
            // DELETE stops the generator on the UN3GPP UE; a newer request supersedes this session anyway
            Thread({ runCatching { Orchestration.call(::openDefault, "DELETE", Orchestration.sessionUrl(base, id)) } },
                "orchestrator-stop").apply { isDaemon = true }.start()
        }
        stopReceivers()
        slice?.let { event("released", mapOf("why" to why, "held_s" to it.elapsedS())) }
        slice?.release()
        slice = null
        network = null
        _session.value = null
        if (had) app.stopService(Intent(app, KeepAliveService::class.java))
    }

    private fun event(name: String, extra: Map<String, Any?>) {
        val s = _session.value ?: return
        reporter.post(linkedMapOf<String, Any?>(
            "kind" to "app_session", "run_id" to appRunId, "service" to s.kind.id, "event" to name,
            "at_ns" to WallClock.nowNs(), "sd" to s.kind.sd, "caps" to s.caps, "receiver" to RECEIVER_NAME,
        ) + extra)
    }

    private fun markFirstData() {
        if (firstData) return
        firstData = true
        val s = (SystemClock.elapsedRealtime() - openedMs) / 1000.0
        _session.update { it?.copy(phase = Phase.RECEIVING, firstDataS = s) }
        event("first_data", mapOf("after_s" to s))
    }

    private fun fail(message: String) {
        _session.update { it?.copy(phase = Phase.ERROR, error = message) }
    }

    // ------------------------------------------------------------------------------------------------ receivers

    private fun startReceivers(kind: ServiceKind, network: Network, ip: Inet4Address, gen: Int) {
        stopReceivers()
        // same order as the capability probe: unbound socket -> Network.bindSocket -> bind (ephemeral port)
        val clockSync = ClockSync({ DatagramSocket(null).also { network.bindSocket(it); it.bind(java.net.InetSocketAddress(0)) } })
        clock = clockSync
        val mon = SlaMonitor(kind.id, specs = slaSpecs ?: SlaCatalog.forService(kind.id))
        monitor = mon
        when (kind) {
            ServiceKind.WEATHER, ServiceKind.DRONE -> {
                val st = UdpStats(kind.id)
                stats = st
                val handler: (ByteArray, Int, String, Long) -> Unit = if (kind == ServiceKind.WEATHER) {
                    { buf, len, _, _ ->
                        markFirstData()
                        WeatherTelemetry.parseDatagram(buf, 0, len)?.let { _weather.value = WeatherUi(it, SystemClock.elapsedRealtime()) }
                    }
                } else {
                    val asm = FrameAssembler()
                    assembler = asm
                    val dec = JpegDecoder { bmp, ms -> onDroneFrame(bmp, ms) }
                    decoder = dec
                    dec.isDaemon = true
                    dec.start()
                    var lastRun: String? = null
                    { buf, len, runId, arrival ->
                        markFirstData()
                        if (runId != lastRun) { lastRun = runId; asm.reset() }
                        asm.feed(buf, 0, len, arrival)?.let { dec.submit(it) }
                    }
                }
                val r = UdpReceiver(network, ip, kind.port, kind.code, st, clockSync, { fail(it) }, handler)
                r.isDaemon = true
                receivers += r
                r.start()
            }
            ServiceKind.CROP -> {
                val r = CropReceiver(ip, clockSync, { fail(it) }, { markFirstData() },
                    { p -> if (gen == generation.get()) _crop.update { it.copy(inFlight = p) } },
                    { res -> onCropObject(res, gen) })
                r.isDaemon = true
                cropReceiver = r
                receivers += r
                r.start()
            }
        }
        decoder?.let { receivers += it }
        ticker = scope.launch { tick(kind, gen) }
    }

    private fun stopReceivers() {
        ticker?.cancel(); ticker = null
        receivers.forEach { runCatching { it.halt() } }
        receivers = ArrayList()
        stats = null; assembler = null; decoder = null; cropReceiver = null; monitor = null; clock = null
    }

    /** sf_traffic.py cmd_receive() loop: AoI sampled every 0.1 s, one record per second. */
    private suspend fun tick(kind: ServiceKind, gen: Int) {
        val t0 = SystemClock.elapsedRealtime()
        var nextReport = t0 + 1000
        var lastFrames = 0L
        var lastFpsMs = t0
        val cropBytes = ArrayDeque<Pair<Long, Long>>() // (ms, bytes) samples for the 10 s crop goodput
        while (scope.isActive && gen == generation.get()) {
            delay(100)
            val st = stats
            if (kind == ServiceKind.WEATHER) st?.sampleAoi()
            val now = SystemClock.elapsedRealtime()
            if (now < nextReport) continue
            nextReport += 1000
            if (nextReport < now) nextReport = now + 1000
            val mon = monitor ?: continue
            val cs = clock
            var record: Map<String, Any?>? = null
            if (st != null) {
                val r = st.report(1.0)
                if (r != null && (((r["received"] as Long) > 0) || ((r["total_received"] as Long) > 0))) {
                    r["clock_offset_ns"] = cs?.offsetNs
                    r["clock_rtt_ns"] = cs?.rttNs
                    r["receiver"] = RECEIVER_NAME
                    if (kind == ServiceKind.DRONE) {
                        assembler?.let { a ->
                            r["video_frames_completed"] = a.framesCompleted
                            r["video_frames_dropped"] = a.framesDropped
                            r["video_filler_datagrams"] = a.fillers
                        }
                        decoder?.let { d -> r["video_frames_decoded"] = d.decoded.get(); r["video_frames_skipped"] = d.skipped.get() }
                    }
                    reporter.post(r)
                    mon.addWindow(r)
                    record = r
                }
            }
            val silence = st?.lastArrivalNs?.takeIf { it > 0 }?.let { (WallClock.nowNs() - it) / 1e9 }
            var cropRate: Double? = null
            var inflightS: Double? = null
            cropReceiver?.let { cr ->
                cropBytes.addLast(now to cr.bytesTotal.get())
                while (cropBytes.size > 11) cropBytes.removeFirst()
                val (m0, b0) = cropBytes.first()
                if (now > m0 && cropBytes.size > 1) cropRate = (cr.bytesTotal.get() - b0) * 8.0 / ((now - m0) / 1000.0)
                inflightS = _crop.value.inFlight?.let { (now - it.startedMs) / 1000.0 }
            }
            _sla.value = mon.evaluate(silenceS = silence, cropRateBps = if (firstData) cropRate else null, cropInFlightS = inflightS,
                retransRatio = retransRatio)
            _session.update {
                it?.copy(rttMs = cs?.rttNs?.let { r -> r / 1e6 }, generator = it.orch.un3gppIp ?: cs?.peer ?: settings.generatorAddress,
                    waitingS = (now - openedMs) / 1000.0)
            }
            val delayMs = (record?.get("delay_mean_s") as? Double)?.let { it * 1000 }
            val goodput = (record?.get("goodput_bps") as? Long)?.toDouble() ?: cropRate
            _link.value = LinkUi(delayMs, goodput)
            if (kind == ServiceKind.DRONE) {
                val d = decoder
                val a = assembler
                val frames = d?.decoded?.get() ?: 0
                val fps = (frames - lastFrames) * 1000.0 / maxOf(1L, now - lastFpsMs)
                lastFrames = frames; lastFpsMs = now
                _drone.update {
                    it.copy(fps = fps, framesShown = frames, framesDropped = a?.framesDropped ?: 0, framesSkipped = d?.skipped?.get() ?: 0,
                        goodputBps = goodput, delayMs = delayMs, fillers = a?.fillers ?: 0)
                }
            }
        }
    }

    private var frameVersion = 0L

    private fun onDroneFrame(bmp: Bitmap, decodeMs: Double) {
        _frame.value = DroneFrame(bmp, ++frameVersion)
        if (frameVersion % 15 == 0L) _drone.update { it.copy(decodeMs = decodeMs) }
    }

    private fun onCropObject(res: CropResult, gen: Int) {
        if (gen != generation.get()) return
        reporter.post(res.record)
        monitor?.addObject(res.record)
        if (!res.valid) {
            _crop.update { it.copy(rejected = it.rejected + 1, lastRejectedId = res.objectId) }
            return
        }
        res.meta?.tcpRetransRatioSoFar?.let { retransRatio = it } // a new generator run starts at null: keep the last
        imageExecutor.execute {
            val rgb = decodeScaled(res.rgbJpeg, 1600)
            val ndvi = decodeScaled(res.ndviJpeg, 1600)
            val capture = Capture(
                res.objectId, res.meta, rgb, ndvi, rgb?.let { thumb(it) }, ndvi?.let { thumb(it) }, true,
                res.record["object_transfer_time_s"] as? Double ?: res.record["receive_time_s"] as? Double,
                (res.record["goodput_bps"] as Long).toDouble(), System.currentTimeMillis(),
            )
            if (gen != generation.get()) return@execute
            _crop.update { it.copy(captures = (listOf(capture) + it.captures).take(10)) }
        }
    }

    private fun decodeScaled(jpeg: ByteArray?, targetWidth: Int): Bitmap? {
        if (jpeg == null || jpeg.isEmpty()) return null
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, bounds)
        var sample = 1
        while (bounds.outWidth / (sample * 2) >= targetWidth) sample *= 2
        return BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, BitmapFactory.Options().apply { inSampleSize = sample })
    }

    private fun thumb(src: Bitmap): Bitmap {
        val w = 240
        val h = maxOf(1, src.height * w / maxOf(1, src.width))
        return Bitmap.createScaledBitmap(src, w, h, true)
    }

    // ------------------------------------------------------------------------------------------------- control

    fun startIrrigation(durationS: Int = 60) = control(
        linkedMapOf("v" to 1, "cmd" to "irrigation", "action" to "activate", "duration_s" to durationS, "source" to "app"),
        irrigation = durationS,
    )

    fun setWeatherState(state: String) = control(linkedMapOf("v" to 1, "cmd" to "state", "service" to "weather", "state" to state))

    private fun control(request: Map<String, Any?>, irrigation: Int? = null) {
        val net = network ?: return
        val gen = generation.get()
        // the orchestrator's answer (UN3GPP UE on the slice) first; manual setting only without it
        val target = _session.value?.orch?.un3gppIp ?: clock?.peer ?: settings.generatorAddress.ifBlank { null }
        if (target == null) {
            event("control", mapOf("request" to request, "ok" to false, "detail" to "no generator address"))
            if (irrigation != null) _irrigation.value = IrrigationUi(sending = false, ok = false,
                message = "No generator address yet: start a service through the orchestrator, or set one in Settings.")
            return
        }
        if (irrigation != null) _irrigation.value = IrrigationUi(sending = true)
        scope.launch(Dispatchers.IO) {
            val reply = ControlClient.send(net, target, request)
            if (gen != generation.get()) return@launch
            event("control", mapOf("request" to request, "ok" to reply.ok, "detail" to reply.detail))
            if (irrigation != null) {
                _irrigation.value = IrrigationUi(
                    sending = false, message = reply.detail, ok = reply.ok,
                    localUntilMs = if (reply.ok) SystemClock.elapsedRealtime() + irrigation * 1000L else 0,
                )
            } else if (!reply.ok) {
                _irrigation.update { it.copy(message = reply.detail, ok = false) }
            }
        }
    }

    fun dismissIrrigationMessage() = _irrigation.update { it.copy(message = null) }

    // ------------------------------------------------------------------------------------------- orchestrator

    /** Connection over the phone's default `internet` session (towards PC1), like the collector reports. */
    private fun openDefault(url: java.net.URL): java.net.HttpURLConnection {
        val cm = app.getSystemService(ConnectivityManager::class.java)
        val net = cm.activeNetwork
        return (net?.openConnection(url) ?: url.openConnection()) as java.net.HttpURLConnection
    }

    private fun updateOrch(gen: Int, f: (OrchUi) -> OrchUi) {
        if (gen != generation.get()) return
        _session.update { s ->
            if (s == null) return@update null
            val o = f(s.orch)
            s.copy(orch = o, generator = o.un3gppIp ?: s.generator)
        }
    }

    /** SLA targets and offered rate from the orchestrator's view (SERVICE_CATALOG) replace the built-in ones. */
    private fun applyView(gen: Int, v: Orchestration.View) {
        if (gen != generation.get()) return
        val specs = v.sla
        if (specs != null && specs != slaSpecs) {
            slaSpecs = specs
            monitor?.specs = specs
            event("sla_targets", mapOf("source" to "orchestrator", "offered_rate_bps" to v.offeredRateBps,
                "sla" to specs.map { linkedMapOf("name" to it.metric, "operator" to it.op.name.lowercase(), "threshold" to it.threshold,
                    "window_s" to it.windowS) }))
        }
        v.offeredRateBps?.let { r -> _session.update { it?.copy(offeredBps = r) } }
        _session.update { if (it == null || it.oran == v.oran) it else it.copy(oran = v.oran) }
    }

    /**
     * POST the service request (repeated on timeouts, 5xx and unreadable replies for up to 30 s: requests are
     * idempotent per app_session), then poll (1 s while connecting, 10 s as the lease heartbeat while streaming).
     * A poll that fails or cannot be parsed is logged to the collector and tolerated: the session is given up only
     * after 30 s without a valid reply while connecting, or at once on a 404.
     */
    private suspend fun orchestrate(kind: ServiceKind, phoneIp: String, gen: Int) {
        val base = settings.orchestratorUrl
        val manual = settings.generatorAddress
        val startedMs = SystemClock.elapsedRealtime()
        var view: Orchestration.View? = null
        var why = "not reachable at $base"
        var attempt = 0
        while (gen == generation.get()) {
            attempt++
            var reply: Orchestration.Reply? = null
            var error: Exception? = null
            try {
                reply = Orchestration.call(::openDefault, "POST", Orchestration.serviceUrl(base),
                    Orchestration.requestBody(kind.id, phoneIp, appRunId, settings.oranMode), readTimeoutMs = Orchestration.POST_READ_TIMEOUT_MS)
            } catch (e: Exception) {
                error = e
            }
            var parseError: String? = null
            if (reply?.ok == true) {
                val parsed = Orchestration.parseOrError(reply.text)
                view = parsed.first
                parseError = parsed.second
                if (view != null) break
            }
            why = when {
                parseError != null -> "unreadable reply: $parseError"
                reply != null -> reply.error()
                else -> "not reachable at $base"
            }
            val elapsedMs = SystemClock.elapsedRealtime() - startedMs
            val retry = Orchestration.retryPost(reply?.code, error, attempt) && elapsedMs < Orchestration.CONNECTING_TOLERANCE_MS
            event("orchestrator_request_error", mapOf("method" to "POST", "attempt" to attempt, "code" to reply?.code,
                "error" to (parseError ?: error?.let { "${it.javaClass.simpleName}: ${it.message}" } ?: reply?.error()),
                "body" to reply?.text?.take(300), "retry" to retry, "silent_s" to elapsedMs / 1000.0))
            if (!retry) break
            updateOrch(gen) { it.copy(silentS = elapsedMs / 1000.0) }
            delay(1000)
        }
        if (gen != generation.get()) {
            view?.let { v -> runCatching { Orchestration.call(::openDefault, "DELETE", Orchestration.sessionUrl(base, v.session)) } }
            return
        }
        if (view == null) {
            updateOrch(gen) { it.copy(silentS = null, notice = if (manual.isBlank()) "Lab orchestrator unavailable ($why) and no manual generator address set in Settings."
                else "Lab orchestrator unavailable ($why). Using the manual generator address $manual.") }
            event("orchestrator", mapOf("ok" to false, "detail" to why, "url" to base, "attempts" to attempt))
            return
        }
        orchSession = base to view.session
        event("orchestrator", mapOf("ok" to true, "orchestrator_session" to view.session, "state" to view.state, "attempts" to attempt))
        applyView(gen, view)
        val tolerance = Orchestration.Tolerance({ SystemClock.elapsedRealtime() })
        var last: Pair<String, String?>? = null
        var current: Orchestration.View = view
        while (gen == generation.get()) {
            val key = current.state to current.reason
            if (key != last) {
                last = key
                val v = current
                updateOrch(gen) {
                    it.copy(session = v.session, state = v.state, reached = v.reached, un3gppIp = v.un3gppIp ?: it.un3gppIp,
                        cause = v.cause, reason = v.reason, streamingAtS = it.streamingAtS ?: if (v.state == Orchestration.STREAMING)
                            (SystemClock.elapsedRealtime() - openedMs) / 1000.0 else null)
                }
                event("orchestrator_state", mapOf("orchestrator_session" to v.session, "state" to v.state,
                    "un3gpp_ip" to v.un3gppIp, "cause" to v.cause, "reason" to v.reason))
                if (v.terminal) {
                    if (orchSession?.second == v.session) orchSession = null
                    return
                }
            }
            delay(if (current.state == Orchestration.STREAMING) 10_000 else 1_000)
            if (gen != generation.get()) return
            var polled: Orchestration.Reply? = null
            var error: Exception? = null
            try {
                polled = Orchestration.call(::openDefault, "GET", Orchestration.sessionUrl(base, current.session))
            } catch (e: Exception) {
                error = e
            }
            val parsed = if (polled?.ok == true) Orchestration.parseOrError(polled.text) else null
            val next = parsed?.first
            if (next != null) {
                if (tolerance.misses > 0) updateOrch(gen) { it.copy(silentS = null) }
                tolerance.valid()
                applyView(gen, next)
                current = next
                continue
            }
            tolerance.miss()
            val detail = when {
                parsed?.second != null -> "unreadable reply: ${parsed.second}"
                polled != null -> polled.error()
                else -> "${error?.javaClass?.simpleName}: ${error?.message}"
            }
            val streaming = current.state == Orchestration.STREAMING
            val silentS = tolerance.silentMs() / 1000.0
            event("orchestrator_poll_error", mapOf("orchestrator_session" to current.session, "state" to current.state,
                "code" to polled?.code, "error" to detail, "body" to (if (parsed?.second != null) polled?.text?.take(300) else null),
                "misses" to tolerance.misses, "silent_s" to silentS))
            if (tolerance.giveUp(polled?.code, streaming)) {
                val cause = if (polled?.code == 404) "The lab orchestrator no longer knows this session (restarted?)"
                    else "The lab orchestrator stopped answering for ${silentS.toInt()} s ($detail)"
                updateOrch(gen) { it.copy(state = "failed", cause = cause, silentS = null) }
                event("orchestrator_state", mapOf("orchestrator_session" to current.session, "state" to "failed", "cause" to cause))
                orchSession = null
                return
            }
            if (!streaming) updateOrch(gen) { it.copy(silentS = silentS) }
        }
    }
}
