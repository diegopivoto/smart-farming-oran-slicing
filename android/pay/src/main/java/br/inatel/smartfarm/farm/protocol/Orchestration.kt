package br.inatel.smartfarm.farm.protocol

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Lab demo orchestrator on PC1 (deployment/scripts/demo_orchestrator.py):
 *   POST   /api/v1/service        {"service", "phone_ip", "app_session"} -> {"session", "state", ...}
 *   GET    /api/v1/session/<id>   accepted -> un3gpp_switching -> un3gpp_in_slice -> generator_started -> streaming,
 *                                 or failed / stopped / superseded with "cause"
 *   DELETE /api/v1/session/<id>   stops the generator on the UN3GPP UE
 * While a session is active, its GET view may carry a read-only "oran" object (the radio action in force for the
 * slice, read back from the E2 control xApp): parsed by [parseOran], absent or unreadable -> null, never an error.
 * Pure Kotlin (java.net + org.json), so the JVM tests can run it against the Python orchestrator in --dry-run.
 */
object Orchestration {
    const val DEFAULT_URL = "http://10.45.0.1:8090"
    /** O-RAN radio control of a session ("oran_mode" of the request): the RAN defaults or the frozen DDQN agent. */
    const val ORAN_RAN_DEFAULT = "ran-default"
    const val ORAN_DDQN = "ddqn"
    val ORAN_MODES = listOf(ORAN_RAN_DEFAULT, ORAN_DDQN)
    const val ACCEPTED = "accepted"
    const val SWITCHING = "un3gpp_switching"
    const val IN_SLICE = "un3gpp_in_slice"
    const val GENERATOR_STARTED = "generator_started"
    const val STREAMING = "streaming"
    val TERMINAL = setOf("failed", "stopped", "superseded")
    /** View "reason" while the UN3GPP UE makes an extra slice hop because its address fell in a phone subnet. */
    const val IP_CONFLICT = "un3gpp_ip_conflicts_with_phone_subnet"
    /** Without a valid reply for this long while connecting, the session is given up (a 404 ends it at once). */
    const val CONNECTING_TOLERANCE_MS = 30_000L
    /** The POST may wait for the orchestrator to supersede the previous session (a node agent call, up to 20 s). */
    const val POST_READ_TIMEOUT_MS = 25_000

    data class View(
        val session: String,
        val state: String,
        /** Every state the session went through, in order (history). */
        val reached: List<String>,
        val un3gppIp: String?,
        val cause: String?,
        val sd: String?,
        val runId: String?,
        /** SLA targets in force (SERVICE_CATALOG), null when the orchestrator did not send any the app can evaluate. */
        val sla: List<SlaSpec>? = null,
        val offeredRateBps: Double? = null,
        /** Machine-readable reason of the latest extra step, e.g. [IP_CONFLICT]. */
        val reason: String? = null,
        /** O-RAN control in force for the slice (read-only, optional): null when absent, unavailable or unreadable. */
        val oran: Oran? = null,
    ) {
        val terminal get() = state in TERMINAL
        fun reached(step: String) = step in reached
    }

    /** One radio link as the orchestrator reads it back: [label] = the action in force, [lastStatus] = E2 verdict. */
    data class OranLink(
        val label: String,
        val readback: String?,
        /** Set by an E2 action (true) or by the RAN's own defaults (false). */
        val byE2: Boolean,
        val stale: Boolean,
        val lastStatus: String?,
        /** true = applied/confirmed, false = rejected, null = pending or unknown. */
        val lastOk: Boolean?,
        val lastValue: String?,
        val lastAgeS: Double?,
    )

    data class OranDecision(val link: String, val value: String, val status: String, val ok: Boolean?, val ageS: Double?)

    data class Oran(
        val decidedBy: String,
        val byE2: Boolean,
        val uplink: OranLink?,
        val downlink: OranLink?,
        val history: List<OranDecision>,
        /** Radio-control mode asked for this session and the one in force (e.g. ddqn asked, xApp busy -> ran-default). */
        val modeRequested: String? = null,
        val modeEffective: String? = null,
        val modeDetail: String? = null,
        val agentRunning: Boolean = false,
        val agentStep: Int? = null,
        /** "met" | "not met" | "undetermined" | "skipped" | "error" of the agent's last finished step. */
        val lastVerdict: String? = null,
    ) {
        val ddqn get() = modeEffective == ORAN_DDQN
    }

    data class Reply(val code: Int, val text: String) {
        val ok get() = code in 200..299
        /** The API's {"error": "..."} message, or the HTTP code. */
        fun error(): String = runCatching { JSONObject(text).optString("error") }.getOrNull()?.takeIf { it.isNotBlank() } ?: "HTTP $code"
    }

    fun requestBody(service: String, phoneIp: String, appSession: String, oranMode: String? = null): String {
        val body = linkedMapOf<String, Any?>("service" to service, "phone_ip" to phoneIp, "app_session" to appSession)
        if (oranMode != null && oranMode in ORAN_MODES) body["oran_mode"] = oranMode
        return Json.encode(body)
    }

    fun url(base: String, path: String): URL = URL(base.trim().trimEnd('/') + path)

    fun serviceUrl(base: String) = url(base, "/api/v1/service")
    fun sessionUrl(base: String, session: String) = url(base, "/api/v1/session/$session")

    fun parse(text: String): View? = parseOrError(text).first

    /** The view, or null and why it could not be parsed (logged to the collector: NaN broke org.json live). */
    fun parseOrError(text: String): Pair<View?, String?> = try {
        parseView(text) to null
    } catch (e: Exception) {
        null to (e.message ?: e.javaClass.simpleName)
    }

    private fun str(o: JSONObject, key: String) = o.optString(key).takeIf { it.isNotBlank() && it != "null" }

    /** The orchestrator's "sla" list as app specs; entries the app cannot evaluate are skipped. */
    fun parseSla(o: JSONObject): List<SlaSpec>? {
        val a = o.optJSONArray("sla") ?: return null
        val out = ArrayList<SlaSpec>()
        for (i in 0 until a.length()) {
            val m = a.optJSONObject(i) ?: continue
            SlaCatalog.fromWire(m.optString("name"), m.optString("operator"), m.optDouble("threshold", Double.NaN),
                m.optString("unit"), m.optInt("window_s", 0))?.let { out += it }
        }
        return out.takeIf { it.isNotEmpty() }
    }

    private fun okOf(o: JSONObject, key: String): Boolean? =
        if (!o.has(key) || o.isNull(key)) null else (o.opt(key) as? Boolean)

    private fun ageOf(o: JSONObject, key: String): Double? = o.optDouble(key, Double.NaN).takeIf { it.isFinite() && it >= 0 }

    private fun parseLink(o: JSONObject?): OranLink? {
        if (o == null) return null
        val label = str(o, "label") ?: return null
        val last = o.optJSONObject("last_action")
        return OranLink(label = label, readback = str(o, "readback"), byE2 = o.optBoolean("by_e2", false),
            stale = o.optBoolean("stale", false), lastStatus = last?.let { str(it, "status") }, lastOk = last?.let { okOf(it, "ok") },
            lastValue = last?.let { str(it, "value") }, lastAgeS = last?.let { ageOf(it, "age_s") })
    }

    /** The view's optional "oran" object; null when absent, unavailable or not understood (never throws). */
    fun parseOran(o: JSONObject): Oran? = try {
        val r = o.optJSONObject("oran")
        if (r == null || !r.optBoolean("available", false)) null else {
            val uplink = parseLink(r.optJSONObject("uplink"))
            val downlink = parseLink(r.optJSONObject("downlink"))
            val history = ArrayList<OranDecision>()
            val h = r.optJSONArray("history")
            if (h != null) for (i in 0 until minOf(h.length(), 8)) {
                val d = h.optJSONObject(i) ?: continue
                val value = str(d, "value") ?: continue
                history += OranDecision(str(d, "link") ?: "", value, str(d, "status") ?: "?", okOf(d, "ok"), ageOf(d, "age_s"))
            }
            if (uplink == null && downlink == null) null
            else {
                val mode = r.optJSONObject("mode")
                val agent = r.optJSONObject("agent")
                Oran(decidedBy = str(r, "decided_by") ?: "RAN defaults", byE2 = r.optString("controller") == "e2-action",
                    uplink = uplink, downlink = downlink, history = history,
                    modeRequested = mode?.let { str(it, "requested") }, modeEffective = mode?.let { str(it, "effective") },
                    modeDetail = mode?.let { str(it, "detail") }, agentRunning = r.optBoolean("agent_running", false),
                    agentStep = agent?.let { a -> if (a.isNull("step")) null else a.optInt("step", -1).takeIf { it >= 0 } },
                    lastVerdict = agent?.let { str(it, "last_verdict") })
            }
        }
    } catch (e: Exception) {
        null
    }

    private fun parseView(text: String): View {
        val o = JSONObject(text)
        val history = o.optJSONArray("history")
        val reached = ArrayList<String>()
        if (history != null) for (i in 0 until history.length()) history.optJSONObject(i)?.optString("state")?.let { reached += it }
        val state = o.getString("state")
        if (state !in reached) reached += state
        return View(
            session = o.getString("session"),
            state = state,
            reached = reached,
            un3gppIp = str(o, "un3gpp_ip"),
            cause = str(o, "cause"),
            sd = str(o, "sd"),
            runId = str(o, "generator_run_id"),
            sla = parseSla(o),
            offeredRateBps = o.optDouble("offered_rate_bps", Double.NaN).takeIf { it.isFinite() && it > 0 },
            reason = str(o, "reason"),
            oran = parseOran(o),
        )
    }

    /**
     * Poll tolerance: a reply that cannot be parsed or a short orchestrator stall does not end the session. While
     * connecting, the session is given up only after [toleranceMs] without a valid reply; while streaming (10 s lease
     * heartbeats) never by silence. A 404 (the orchestrator forgot the session) ends it at once.
     */
    class Tolerance(private val now: () -> Long, private val toleranceMs: Long = CONNECTING_TOLERANCE_MS) {
        private var lastValidMs = now()
        var misses = 0; private set

        fun valid() { lastValidMs = now(); misses = 0 }

        fun miss() { misses++ }

        fun silentMs() = now() - lastValidMs

        fun giveUp(code: Int?, streaming: Boolean) = code == 404 || (!streaming && silentMs() >= toleranceMs)
    }

    /**
     * Whether a failed POST is worth repeating (requests are idempotent per app_session): timeouts, 5xx and
     * unparseable replies are; a definitive 4xx is not, and an orchestrator that refuses connections (not running) is
     * retried only [refusedAttempts] times so the manual fallback comes quickly.
     */
    fun retryPost(code: Int?, error: Throwable?, attempt: Int, refusedAttempts: Int = 3): Boolean = when {
        error is java.net.ConnectException -> attempt < refusedAttempts
        error != null -> true
        code == null -> true
        code in 400..499 -> false
        else -> true // 5xx, or a 2xx whose body could not be parsed
    }

    /**
     * One HTTP exchange. [open] chooses the network: on the phone, the default `internet` session
     * (ConnectivityManager.activeNetwork.openConnection), like the collector reports.
     */
    fun call(open: (URL) -> HttpURLConnection, method: String, url: URL, body: String? = null,
             connectTimeoutMs: Int = 3000, readTimeoutMs: Int = 5000): Reply {
        val c = open(url)
        try {
            c.connectTimeout = connectTimeoutMs
            c.readTimeout = readTimeoutMs
            c.requestMethod = method
            c.useCaches = false
            c.setRequestProperty("Accept", "application/json")
            if (body != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write(body.toByteArray()) }
            }
            val code = c.responseCode
            val text = (if (code < 400) c.inputStream else c.errorStream)?.use { it.readBytes().decodeToString() } ?: ""
            return Reply(code, text)
        } finally {
            c.disconnect()
        }
    }
}
