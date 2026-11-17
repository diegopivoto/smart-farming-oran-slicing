package br.inatel.smartfarm.farm.protocol

import kotlin.math.abs

/** Growable primitive list: per-packet samples at 4 700 packets/s without boxing. */
class DoubleList(capacity: Int = 1024) {
    private var data = DoubleArray(capacity)
    var size = 0
        private set

    fun add(v: Double) {
        if (size == data.size) data = data.copyOf(data.size * 2)
        data[size++] = v
    }

    fun clear() { size = 0 }
    fun isEmpty() = size == 0
    fun sum(): Double { var s = 0.0; for (i in 0 until size) s += data[i]; return s }
    fun max(): Double { var m = Double.NEGATIVE_INFINITY; for (i in 0 until size) if (data[i] > m) m = data[i]; return m }
    fun sortedCopy(): DoubleArray = data.copyOf(size).also { it.sort() }
}

object Stats {
    /** sf_traffic.py percentile(): ordered[min(n - 1, int(round(q * (n - 1))))], Python's half-even round. */
    fun percentile(sorted: DoubleArray, q: Double): Double? {
        if (sorted.isEmpty()) return null
        val idx = Math.rint(q * (sorted.size - 1)).toInt()
        return sorted[minOf(sorted.size - 1, idx)]
    }

    fun percentile(values: List<Double>, q: Double): Double? = percentile(values.sorted().toDoubleArray(), q)

    /** Python round(x, n) (half-even on the scaled value; exact ties are rare with measured floats). */
    fun round(x: Double, digits: Int): Double {
        var scale = 1.0
        repeat(digits) { scale *= 10.0 }
        return Math.rint(x * scale) / scale
    }
}

/**
 * Port of sf_traffic.py UdpStats: per-second receiver windows with the same fields and semantics, so the app is a
 * drop-in replacement of the Termux receiver for the collector and the RL.
 */
class UdpStats(val service: String, private val clock: () -> Long = WallClock::nowNs) {
    private val lock = Any()
    var runId: String? = null
        private set
    private var firstSeq: Long? = null
    private var maxSeq = -1L
    private var totalReceived = 0L
    private var transitPrev: Double? = null
    private var jitterS = 0.0
    private var lastGeneratedRxNs: Long? = null // newest update, receiver clock (AoI)
    private var idleWindows = 0

    private var wReceived = 0L
    private var wBytes = 0L
    private val wDelays = DoubleList(8192)
    private val wGaps = DoubleList(8192)
    private val wAoi = DoubleList(16)
    private var wLastArrival: Double? = null
    private var wSeqLo: Long? = null
    private var wSeqHi = -1L

    /** Arrival time (receiver clock, ns) of the newest datagram, for the live "silence" shown in the UI. */
    @Volatile var lastArrivalNs: Long = 0L
        private set

    private fun resetAll() {
        runId = null; firstSeq = null; maxSeq = -1; totalReceived = 0; transitPrev = null; jitterS = 0.0
        lastGeneratedRxNs = null; idleWindows = 0
        resetWindow()
    }

    private fun resetWindow() {
        wReceived = 0; wBytes = 0; wDelays.clear(); wGaps.clear(); wAoi.clear()
        wLastArrival = null; wSeqLo = null; wSeqHi = -1
    }

    fun add(runId: String, seq: Long, generatedRxNs: Long?, size: Int, arrivalNs: Long) {
        synchronized(lock) {
            if (runId != this.runId) { // new run: start over
                resetAll()
                this.runId = runId
            }
            if (firstSeq == null) firstSeq = seq
            maxSeq = maxOf(maxSeq, seq)
            totalReceived += 1
            wReceived += 1
            wBytes += size
            wSeqLo = wSeqLo?.let { minOf(it, seq) } ?: seq
            wSeqHi = maxOf(wSeqHi, seq)
            val arrival = arrivalNs / 1e9
            wLastArrival?.let { wGaps.add(arrival - it) }
            wLastArrival = arrival
            lastArrivalNs = arrivalNs
            if (generatedRxNs != null) {
                val delay = (arrivalNs - generatedRxNs) / 1e9
                wDelays.add(delay)
                transitPrev?.let { jitterS += (abs(delay - it) - jitterS) / 16 } // RFC 3550 interarrival jitter
                transitPrev = delay
                val last = lastGeneratedRxNs
                if (last == null || generatedRxNs > last) lastGeneratedRxNs = generatedRxNs
            }
        }
    }

    fun sampleAoi() {
        synchronized(lock) {
            lastGeneratedRxNs?.let { wAoi.add((clock() - it) / 1e9) }
        }
    }

    /** One receiver_window record (field names and rounding of sf_traffic.py), or null when idle/no run. */
    fun report(windowS: Double): LinkedHashMap<String, Any?>? {
        synchronized(lock) {
            val run = runId ?: return null
            idleWindows = if (wReceived > 0) 0 else idleWindows + 1
            if (idleWindows > 3) { // stream ended: stay quiet until packets arrive again
                resetWindow()
                return null
            }
            val expectedTotal = maxSeq - (firstSeq ?: 0) + 1
            val lo = wSeqLo
            val expectedWindow = if (lo != null) wSeqHi - lo + 1 else 0
            val delays = wDelays.sortedCopy()
            val aoi = wAoi.sortedCopy()
            val record = linkedMapOf<String, Any?>(
                "kind" to "receiver_window", "service" to service, "run_id" to run, "at_ns" to clock(),
                "window_s" to windowS, "received" to wReceived, "expected_in_window" to expectedWindow,
                "goodput_bps" to Math.rint(wBytes * 8 / windowS).toLong(),
                "total_received" to totalReceived, "total_expected" to expectedTotal,
                "pdr_total" to (if (expectedTotal > 0) Stats.round(totalReceived.toDouble() / expectedTotal, 5) else null),
                "loss_ratio_window" to (if (expectedWindow > 0) Stats.round(1 - wReceived.toDouble() / expectedWindow, 5) else null),
                "jitter_s" to Stats.round(jitterS, 6),
                "delay_mean_s" to (if (delays.isNotEmpty()) Stats.round(delays.sum() / delays.size, 6) else null),
                "delay_p95_s" to Stats.percentile(delays, 0.95),
                "max_receive_gap_s" to (if (!wGaps.isEmpty()) Stats.round(wGaps.max(), 6) else null),
                "aoi_p95_s" to Stats.percentile(aoi, 0.95),
                "aoi_max_s" to (if (aoi.isNotEmpty()) aoi.last() else null),
            )
            resetWindow()
            return record
        }
    }
}
