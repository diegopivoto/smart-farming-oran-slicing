package br.inatel.smartfarm.farm.protocol

/**
 * Live SLA evaluation on the phone with the metric definitions of deployment/scripts/traffic_run.py evaluate(), over a
 * sliding window of window_s seconds. The targets come from the lab orchestrator's session view (its "sla" list is
 * SERVICE_CATALOG, src/smartfarm/contracts/service.py) when it answers; [SlaCatalog] holds the built-in copy used
 * without it.
 */
enum class Op(val symbol: String) { GE("≥"), LE("≤"), EQ("=") }

enum class MetricUnit { RATIO_PCT, SECONDS, BPS, COUNT }

data class SlaSpec(
    val metric: String,
    val friendly: String,
    val op: Op,
    val threshold: Double,
    val unit: MetricUnit,
    val windowS: Int,
)

data class SlaCheck(val spec: SlaSpec, val value: Double?, val met: Boolean?)

data class SlaSnapshot(val service: String, val checks: List<SlaCheck>) {
    /** traffic_run.py: false if any check fails, true if all pass, null otherwise. */
    val metStrict: Boolean? get() = when {
        checks.any { it.met == false } -> false
        checks.all { it.met == true } -> true
        else -> null
    }

    /** UI pill: metrics not reported yet (TCP retransmissions before the first crop object) do not block "SLA met". */
    val metShown: Boolean? get() = when {
        checks.any { it.met == false } -> false
        checks.any { it.met == true } -> true
        else -> null
    }
}

object SlaCatalog {
    /** Metrics whose value is the generator's cumulative figure for the stream so far, not a sliding window. */
    const val RETRANS = "retransmitted_bytes_ratio"

    val WEATHER = listOf(
        SlaSpec("pdr", "Readings delivered", Op.GE, 0.99, MetricUnit.RATIO_PCT, 10),
        SlaSpec("aoi_p95", "Data age (95 %)", Op.LE, 2.0, MetricUnit.SECONDS, 10),
        SlaSpec("max_freshness_gap", "Longest silence", Op.LE, 5.0, MetricUnit.SECONDS, 10),
    )
    val CROP = listOf(
        SlaSpec("goodput", "Download speed", Op.GE, 15_000_000.0, MetricUnit.BPS, 10),
        SlaSpec("object_transfer_time", "Capture delivery time", Op.LE, 40.0, MetricUnit.SECONDS, 40),
        SlaSpec("sha256_valid_ratio", "Captures intact", Op.EQ, 1.0, MetricUnit.RATIO_PCT, 40),
        SlaSpec(RETRANS, "Resent data (TCP)", Op.LE, 0.02, MetricUnit.RATIO_PCT, 40),
        SlaSpec("accepted_corrupt_objects", "Corrupted captures", Op.EQ, 0.0, MetricUnit.COUNT, 40),
    )
    val DRONE = listOf(
        SlaSpec("goodput_p10", "Video throughput (worst 10 %)", Op.GE, 34_000_000.0, MetricUnit.BPS, 5),
        SlaSpec("packet_loss_ratio", "Packet loss", Op.LE, 0.05, MetricUnit.RATIO_PCT, 5),
        SlaSpec("jitter_p95", "Jitter (95 %)", Op.LE, 0.02, MetricUnit.SECONDS, 5),
        SlaSpec("max_receive_gap", "Longest stall", Op.LE, 0.5, MetricUnit.SECONDS, 5),
    )

    /** Built-in offered load (SERVICE_CATALOG offered_rate_bps), shown until the orchestrator reports its own. */
    val DEFAULT_OFFERED_BPS = mapOf("weather" to 100_000.0, "crop" to 20_000_000.0, "drone" to 40_000_000.0)

    fun forService(service: String) = when (service) {
        "weather" -> WEATHER
        "crop" -> CROP
        else -> DRONE
    }

    /** Friendly names of every metric the app can compute (the same names as SERVICE_CATALOG). */
    private val FRIENDLY: Map<String, String> = (WEATHER + CROP + DRONE).associate { it.metric to it.friendly }

    fun computable(metric: String) = metric in FRIENDLY

    fun unitOf(wire: String): MetricUnit? = when (wire) {
        "ratio" -> MetricUnit.RATIO_PCT
        "s" -> MetricUnit.SECONDS
        "bit/s" -> MetricUnit.BPS
        "count" -> MetricUnit.COUNT
        else -> null
    }

    fun opOf(wire: String): Op? = when (wire) {
        "ge" -> Op.GE
        "le" -> Op.LE
        "eq" -> Op.EQ
        else -> null
    }

    /**
     * One entry of the orchestrator's "sla" list ({"name", "operator", "threshold", "unit", "window_s"}), or null when
     * the app cannot evaluate it (unknown metric, operator or unit, or a non-finite threshold).
     */
    fun fromWire(name: String, operator: String, threshold: Double, unit: String, windowS: Int): SlaSpec? {
        val friendly = FRIENDLY[name] ?: return null
        val op = opOf(operator) ?: return null
        val u = unitOf(unit) ?: return null
        if (!threshold.isFinite() || windowS <= 0) return null
        return SlaSpec(name, friendly, op, threshold, u, windowS)
    }

    fun check(spec: SlaSpec, value: Double?): SlaCheck {
        val met = value?.let {
            when (spec.op) {
                Op.GE -> it >= spec.threshold
                Op.LE -> it <= spec.threshold
                Op.EQ -> it == spec.threshold
            }
        }
        return SlaCheck(spec, value, met)
    }
}

class SlaMonitor(
    val service: String,
    specs: List<SlaSpec> = SlaCatalog.forService(service),
    private val clock: () -> Long = WallClock::nowNs,
) {
    /** Targets in force: the built-in catalog until the orchestrator's session view replaces them. */
    @Volatile var specs: List<SlaSpec> = specs

    private val windows = ArrayDeque<Map<String, Any?>>()
    private val objects = ArrayDeque<Map<String, Any?>>()
    private val lock = Any()

    fun addWindow(record: Map<String, Any?>) = synchronized(lock) {
        windows.addLast(record)
        while (windows.size > 64) windows.removeFirst()
    }

    fun addObject(record: Map<String, Any?>) = synchronized(lock) {
        objects.addLast(record)
        while (objects.size > 64) objects.removeFirst()
    }

    fun clear() = synchronized(lock) { windows.clear(); objects.clear() }

    private fun num(r: Map<String, Any?>, k: String): Double? = (r[k] as? Number)?.toDouble()
    private fun atNs(r: Map<String, Any?>): Long = (r["at_ns"] as? Number)?.toLong() ?: 0L

    /**
     * [silenceS]: seconds since the last datagram (UDP services), so a stalled stream shows up immediately.
     * [cropRateBps]: payload bytes on the crop socket over the last 10 s. [cropInFlightS]: age of the object being
     * received. [retransRatio]: the generator's cumulative TCP retransmitted-bytes ratio of the stream so far, from the
     * latest crop object's meta.json (contract v1.1); null before the first object ("n/a").
     */
    fun evaluate(
        silenceS: Double? = null,
        cropRateBps: Double? = null,
        cropInFlightS: Double? = null,
        retransRatio: Double? = null,
    ): SlaSnapshot = synchronized(lock) {
        val specs = this.specs
        val now = clock()
        fun recent(src: ArrayDeque<Map<String, Any?>>, s: Int) = src.filter { now - atNs(it) <= s * 1_000_000_000L + 500_000_000L }
        val checks = specs.map { spec ->
            val value: Double? = when (service) {
                "crop" -> {
                    val objs = recent(objects, spec.windowS).filter { it["sha256_valid"] != null }
                    when (spec.metric) {
                        "goodput" -> cropRateBps
                        "object_transfer_time" -> {
                            val done = objs.mapNotNull { num(it, "object_transfer_time_s") ?: num(it, "receive_time_s") }.maxOrNull()
                            val inflight = cropInFlightS?.takeIf { it > spec.threshold }
                            listOfNotNull(done, inflight).maxOrNull()
                        }
                        "sha256_valid_ratio" -> if (objs.isEmpty()) null else objs.count { it["sha256_valid"] == true }.toDouble() / objs.size
                        SlaCatalog.RETRANS -> retransRatio
                        "accepted_corrupt_objects" -> if (objs.isEmpty()) null else objs.count { it["sha256_valid"] == false }.toDouble()
                        else -> null
                    }
                }
                else -> {
                    val ws = recent(windows, spec.windowS).takeLast(spec.windowS)
                    if (ws.isEmpty()) null else when (spec.metric) {
                        "pdr" -> Stats.round(ws.sumOf { num(it, "received") ?: 0.0 } / maxOf(1.0, ws.sumOf { num(it, "expected_in_window") ?: 0.0 }), 5)
                        "packet_loss_ratio" -> Stats.round(1 - ws.sumOf { num(it, "received") ?: 0.0 } / maxOf(1.0, ws.sumOf { num(it, "expected_in_window") ?: 0.0 }), 5)
                        "aoi_p95" -> Stats.percentile(ws.mapNotNull { num(it, "aoi_p95_s") }, 0.95)
                        "max_freshness_gap", "max_receive_gap" ->
                            listOfNotNull(ws.maxOfOrNull { num(it, "max_receive_gap_s") ?: 0.0 }, silenceS).maxOrNull()
                        "goodput_p10" -> ws.map { num(it, "goodput_bps") ?: 0.0 }.sorted().let { it[(0.1 * (it.size - 1)).toInt()] }
                        "jitter_p95" -> Stats.percentile(ws.mapNotNull { num(it, "jitter_s") }, 0.95)
                        else -> null
                    }
                }
            }
            SlaCatalog.check(spec, value)
        }
        SlaSnapshot(service, checks)
    }
}
