package br.inatel.smartfarm.farm.protocol

import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Byte layouts shared with deployment/traffic/sf_traffic.py and the PC3 content generator
 * (docs/design/smartfarm-app-protocol.md, contract v1). Network byte order everywhere.
 */
object Wire {
    const val PORT_TIME = 47000
    const val PORT_WEATHER = 47001
    const val PORT_CROP = 47002
    const val PORT_DRONE = 47003
    const val PORT_CONTROL = 47010

    const val SERVICE_WEATHER = 1
    const val SERVICE_CROP = 2
    const val SERVICE_DRONE = 3

    /** DGRAM = "!4sBB16sQQ": magic, version, service, run_id, seq, generated_at_ns. */
    const val DGRAM_SIZE = 38

    /** OBJECT = "!4s16sQQ32sQ": magic, run_id, object_id, size, sha256, generated_at_ns. */
    const val OBJECT_SIZE = 76

    /** TIMEQ = "!4sQ" (b"SFTQ", t0), TIMER = "!4sQQ" (b"SFTR", echoed t0, server ns). */
    const val TIMEQ_SIZE = 12
    const val TIMER_SIZE = 20

    /** Drone frame header "!IHHI": frame_id, frag_index, frag_count, frame_len. */
    const val FRAME_HEADER_SIZE = 12
    const val FRAGMENT_SIZE = 1150
    const val FILLER_FRAME_ID = 0xFFFFFFFFL

    // Canonical magics are those of sf_traffic.py (SFT1 / SFO1). The first draft of the contract named them
    // SFTR / SFOB; both spellings are accepted so a generator built from either text still works.
    private fun magic(s: String) = s.toByteArray(Charsets.US_ASCII)
    private val DGRAM_MAGICS = listOf(magic("SFT1"), magic("SFTR"))
    private val OBJECT_MAGICS = listOf(magic("SFO1"), magic("SFOB"))
    val TIMEQ_MAGIC = magic("SFTQ")
    val TIMER_MAGIC = magic("SFTR")

    private fun hasMagic(buf: ByteArray, off: Int, magics: List<ByteArray>): Boolean =
        magics.any { m -> buf[off] == m[0] && buf[off + 1] == m[1] && buf[off + 2] == m[2] && buf[off + 3] == m[3] }

    fun u64(buf: ByteArray, off: Int): Long = ByteBuffer.wrap(buf, off, 8).order(ByteOrder.BIG_ENDIAN).long
    fun u32(buf: ByteArray, off: Int): Long = ByteBuffer.wrap(buf, off, 4).order(ByteOrder.BIG_ENDIAN).int.toLong() and 0xFFFFFFFFL
    fun u16(buf: ByteArray, off: Int): Int = ((buf[off].toInt() and 0xff) shl 8) or (buf[off + 1].toInt() and 0xff)

    fun hex(buf: ByteArray, off: Int, len: Int): String {
        val digits = "0123456789abcdef"
        val sb = StringBuilder(len * 2)
        for (i in off until off + len) {
            val b = buf[i].toInt() and 0xff
            sb.append(digits[b ushr 4]).append(digits[b and 0x0f])
        }
        return sb.toString()
    }

    /** Parsed common datagram header. [runIdHex] is cached by [RunIdCache] to avoid one string per packet. */
    class DatagramHeader(val service: Int, val runIdHex: String, val seq: Long, val generatedAtNs: Long)

    /** Returns null when the datagram is too short or not ours (wrong magic, version or service). */
    fun parseDatagram(buf: ByteArray, off: Int, len: Int, expectedService: Int, runIds: RunIdCache = RunIdCache()): DatagramHeader? {
        if (len < DGRAM_SIZE || !hasMagic(buf, off, DGRAM_MAGICS)) return null
        if ((buf[off + 4].toInt() and 0xff) != 1) return null
        val service = buf[off + 5].toInt() and 0xff
        if (service != expectedService) return null
        return DatagramHeader(service, runIds.hex(buf, off + 6), u64(buf, off + 22), u64(buf, off + 30))
    }

    class ObjectHeader(val runIdHex: String, val objectId: Long, val size: Long, val sha256: ByteArray, val generatedAtNs: Long)

    fun parseObjectHeader(buf: ByteArray): ObjectHeader? {
        if (buf.size < OBJECT_SIZE || !hasMagic(buf, 0, OBJECT_MAGICS)) return null
        return ObjectHeader(hex(buf, 4, 16), u64(buf, 20), u64(buf, 28), buf.copyOfRange(36, 68), u64(buf, 68))
    }

    fun packTimeQuery(t0: Long): ByteArray =
        ByteBuffer.allocate(TIMEQ_SIZE).order(ByteOrder.BIG_ENDIAN).put(TIMEQ_MAGIC).putLong(t0).array()

    /** Returns (echoed t0, server ns) or null. */
    fun parseTimeReply(buf: ByteArray, len: Int): Pair<Long, Long>? {
        if (len != TIMER_SIZE || !hasMagic(buf, 0, listOf(TIMER_MAGIC))) return null
        return u64(buf, 4) to u64(buf, 12)
    }
}

/** Keeps the hex string of the last run_id so per-packet parsing allocates nothing while the run is unchanged. */
class RunIdCache {
    private val last = ByteArray(16)
    private var lastHex: String? = null

    fun hex(buf: ByteArray, off: Int): String {
        val cached = lastHex
        if (cached != null) {
            var same = true
            for (i in 0 until 16) if (buf[off + i] != last[i]) { same = false; break }
            if (same) return cached
        }
        System.arraycopy(buf, off, last, 0, 16)
        return Wire.hex(buf, off, 16).also { lastHex = it }
    }
}

/**
 * Wall clock in nanoseconds like Python's time.time_ns(), but advanced by the monotonic clock so that
 * inter-arrival deltas (jitter, gaps) have nanosecond resolution and never jump.
 */
object WallClock {
    @Volatile private var mono: () -> Long = System::nanoTime
    @Volatile private var baseWall = System.currentTimeMillis() * 1_000_000L
    @Volatile private var baseMono = System.nanoTime()

    /** Android: pass SystemClock::elapsedRealtimeNanos, which keeps counting while the CPU sleeps. */
    fun install(source: () -> Long) {
        mono = source
        baseWall = System.currentTimeMillis() * 1_000_000L
        baseMono = source()
    }

    fun nowNs(): Long = baseWall + (mono() - baseMono)
}
