package br.inatel.smartfarm.farm.protocol

/**
 * Reassembles the MJPEG drone frames: each datagram is 38 B common header | 12 B frame header "!IHHI"
 * (frame_id, frag_index, frag_count, frame_len) | fragment of <= 1150 B (zero padded to 1200 B).
 * Fragment i carries bytes [i*1150, min((i+1)*1150, frame_len)). A frame is released only when every fragment
 * arrived; frames left incomplete (a lost datagram) are dropped without artifacts. Fillers (frame_id 0xFFFFFFFF)
 * only count for the metrics.
 *
 * Not thread-safe: called from the single receive thread.
 */
class FrameAssembler(
    private val maxInFlight: Int = 8,
    private val timeoutNs: Long = 500_000_000L,
    private val maxFrameBytes: Int = 4 * 1024 * 1024,
) {
    private class Pending(val id: Long, val count: Int, val len: Int, val firstNs: Long) {
        val data = ByteArray(len)
        val got = BooleanArray(count)
        var received = 0
    }

    private val pending = LinkedHashMap<Long, Pending>()
    private var lastCompleted = -1L

    var framesCompleted = 0L
        private set
    /** Frames of which at least one fragment arrived but that never completed (lost fragment or timeout). */
    var framesDropped = 0L
        private set
    var fillers = 0L
        private set
    var malformed = 0L
        private set

    fun reset() {
        pending.clear(); lastCompleted = -1
        framesCompleted = 0; framesDropped = 0; fillers = 0; malformed = 0
    }

    /** [off]/[len] delimit the whole datagram (common header included). Returns a complete JPEG or null. */
    fun feed(buf: ByteArray, off: Int, len: Int, arrivalNs: Long): ByteArray? {
        val h = off + Wire.DGRAM_SIZE
        if (len < Wire.DGRAM_SIZE + Wire.FRAME_HEADER_SIZE) { malformed++; return null }
        val id = Wire.u32(buf, h)
        if (id == Wire.FILLER_FRAME_ID) { fillers++; return null }
        val index = Wire.u16(buf, h + 4)
        val count = Wire.u16(buf, h + 6)
        val frameLen = Wire.u32(buf, h + 8)
        val expectedCount = (frameLen + Wire.FRAGMENT_SIZE - 1) / Wire.FRAGMENT_SIZE
        if (count == 0 || index >= count || frameLen <= 0 || frameLen > maxFrameBytes || expectedCount != count.toLong()) {
            malformed++; return null
        }
        expire(arrivalNs)
        if (lastCompleted >= 0 && id <= lastCompleted) {
            if (lastCompleted - id > 900) { // generator restarted / looped its numbering: start over
                dropAll(); lastCompleted = -1
            } else {
                return null // late fragment of a frame already shown or dropped
            }
        }
        var p = pending[id]
        if (p == null) {
            p = Pending(id, count, frameLen.toInt(), arrivalNs)
            pending[id] = p
            while (pending.size > maxInFlight) {
                val oldest = pending.keys.first()
                pending.remove(oldest); framesDropped++
            }
        }
        if (p.count != count || p.len.toLong() != frameLen) { malformed++; return null }
        if (p.got[index]) return null // duplicate
        val start = index * Wire.FRAGMENT_SIZE
        val n = minOf(Wire.FRAGMENT_SIZE, p.len - start)
        val payload = h + Wire.FRAME_HEADER_SIZE
        if (payload + n > off + len) { malformed++; return null }
        System.arraycopy(buf, payload, p.data, start, n)
        p.got[index] = true
        p.received++
        if (p.received < p.count) return null
        pending.remove(id)
        // frames older than this one can no longer be shown in order: drop them
        val it = pending.entries.iterator()
        while (it.hasNext()) {
            val e = it.next()
            if (e.key < id) { it.remove(); framesDropped++ }
        }
        lastCompleted = id
        framesCompleted++
        return p.data
    }

    private fun expire(nowNs: Long) {
        val it = pending.entries.iterator()
        while (it.hasNext()) {
            val e = it.next()
            if (nowNs - e.value.firstNs > timeoutNs) { it.remove(); framesDropped++ }
        }
    }

    private fun dropAll() { framesDropped += pending.size; pending.clear() }
}
