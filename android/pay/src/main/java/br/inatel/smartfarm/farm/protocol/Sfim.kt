package br.inatel.smartfarm.farm.protocol

import org.json.JSONObject

/**
 * Streaming parser of the crop object payload (the bytes after the 76 B OBJECT header):
 * `b"SFIM" | u32 version | u32 n_parts | n x (u32 name_len | name | u64 len | bytes)`.
 *
 * It is fed the TCP stream in arbitrary chunks while the receiver hashes the same bytes, keeps the parts named in
 * [keep] (meta.json, rgb.jpg, ndvi.jpg) up to [maxPartBytes] each, and skips the rest (bands.raw) without copying.
 */
class SfimStreamParser(
    private val keep: Set<String> = setOf(META, RGB, NDVI),
    private val maxPartBytes: Int = 32 * 1024 * 1024,
) {
    companion object {
        const val META = "meta.json"
        const val RGB = "rgb.jpg"
        const val NDVI = "ndvi.jpg"
        const val BANDS = "bands.raw"

        /** Whole payload in memory (tests, fake generator check). */
        fun parse(payload: ByteArray): SfimStreamParser = SfimStreamParser().also { it.feed(payload, 0, payload.size) }
    }

    private enum class St { HEADER, NAME_LEN, NAME, PART_LEN, BODY, DONE, INVALID }

    private var st = St.HEADER
    private val small = ByteArray(16)
    private var smallFill = 0
    private var smallNeed = 12
    private var nameBuf = ByteArray(0)
    private var partsLeft = 0L
    private var curName = ""
    private var curLen = 0L
    private var curDone = 0L
    private var curBody: ByteArray? = null

    var version = 0L
        private set
    var declaredParts = 0L
        private set
    val parts = LinkedHashMap<String, ByteArray>()
    /** Every part seen: name -> declared length. */
    val partSizes = LinkedHashMap<String, Long>()
    var error: String? = null
        private set
    val complete: Boolean get() = st == St.DONE

    fun feed(buf: ByteArray, off: Int, len: Int) {
        var i = off
        val end = off + len
        while (i < end) {
            when (st) {
                St.DONE, St.INVALID -> return
                St.HEADER, St.NAME_LEN, St.PART_LEN -> {
                    val n = minOf(smallNeed - smallFill, end - i)
                    System.arraycopy(buf, i, small, smallFill, n)
                    smallFill += n; i += n
                    if (smallFill == smallNeed) onSmall()
                }
                St.NAME -> {
                    val n = minOf(nameBuf.size - smallFill, end - i)
                    System.arraycopy(buf, i, nameBuf, smallFill, n)
                    smallFill += n; i += n
                    if (smallFill == nameBuf.size) {
                        curName = String(nameBuf, Charsets.UTF_8)
                        expect(St.PART_LEN, 8)
                    }
                }
                St.BODY -> {
                    val n = minOf(curLen - curDone, (end - i).toLong()).toInt()
                    curBody?.let { System.arraycopy(buf, i, it, curDone.toInt(), n) }
                    curDone += n; i += n
                    if (curDone == curLen) endPart()
                }
            }
        }
    }

    private fun expect(next: St, need: Int) { st = next; smallFill = 0; smallNeed = need }

    private fun fail(why: String) { error = why; st = St.INVALID }

    private fun onSmall() {
        when (st) {
            St.HEADER -> {
                if (!(small[0] == 'S'.code.toByte() && small[1] == 'F'.code.toByte() && small[2] == 'I'.code.toByte() && small[3] == 'M'.code.toByte())) {
                    return fail("not an SFIM container")
                }
                version = Wire.u32(small, 4)
                declaredParts = Wire.u32(small, 8)
                partsLeft = declaredParts
                if (partsLeft == 0L) st = St.DONE else expect(St.NAME_LEN, 4)
            }
            St.NAME_LEN -> {
                val n = Wire.u32(small, 0)
                if (n > 4096) return fail("part name too long ($n)")
                nameBuf = ByteArray(n.toInt())
                smallFill = 0
                if (n == 0L) { curName = ""; expect(St.PART_LEN, 8) } else st = St.NAME
            }
            St.PART_LEN -> {
                curLen = Wire.u64(small, 0)
                if (curLen < 0) return fail("negative part length")
                curDone = 0
                partSizes[curName] = curLen
                curBody = if (curName in keep && curLen <= maxPartBytes) ByteArray(curLen.toInt()) else null
                st = St.BODY
                if (curLen == 0L) endPart()
            }
            else -> Unit
        }
    }

    private fun endPart() {
        curBody?.let { parts[curName] = it }
        curBody = null
        partsLeft--
        if (partsLeft <= 0) st = St.DONE else expect(St.NAME_LEN, 4)
    }
}

data class CropZone(val x: Float, val y: Float, val w: Float, val h: Float, val label: String, val severity: String)

data class CropMeta(
    val captureId: String,
    val field: String,
    val state: String,
    val width: Int,
    val height: Int,
    val ndviMean: Double?,
    val zones: List<CropZone>,
    val capturedAt: String,
    /**
     * Contract v1.1: the generator's TCP retransmitted-bytes ratio over its run so far (TCP_INFO, as sf_traffic.py),
     * null before the run sent any byte or from a v1 generator.
     */
    val tcpRetransRatioSoFar: Double? = null,
) {
    companion object {
        fun parse(bytes: ByteArray?): CropMeta? {
            if (bytes == null) return null
            return try {
                val o = JSONObject(String(bytes, Charsets.UTF_8))
                val zones = ArrayList<CropZone>()
                o.optJSONArray("zones")?.let { a ->
                    for (i in 0 until a.length()) {
                        val z = a.optJSONObject(i) ?: continue
                        zones += CropZone(
                            z.optDouble("x", 0.0).toFloat().coerceIn(0f, 1f), z.optDouble("y", 0.0).toFloat().coerceIn(0f, 1f),
                            z.optDouble("w", 0.0).toFloat().coerceIn(0f, 1f), z.optDouble("h", 0.0).toFloat().coerceIn(0f, 1f),
                            z.optString("label", "Zone"), z.optString("severity", "medium"),
                        )
                    }
                }
                val ndvi = o.optDouble("ndvi_mean", Double.NaN)
                val retrans = o.optDouble("tcp_retrans_ratio_so_far", Double.NaN)
                CropMeta(
                    captureId = o.opt("capture_id")?.toString() ?: "",
                    field = o.optString("field", "field-A"),
                    state = o.optString("state", "healthy"),
                    width = o.optInt("width", 0), height = o.optInt("height", 0),
                    ndviMean = if (ndvi.isNaN()) null else ndvi,
                    zones = zones,
                    capturedAt = o.optString("captured_at", ""),
                    tcpRetransRatioSoFar = retrans.takeIf { it.isFinite() && it >= 0.0 },
                )
            } catch (_: Exception) {
                null
            }
        }
    }
}
