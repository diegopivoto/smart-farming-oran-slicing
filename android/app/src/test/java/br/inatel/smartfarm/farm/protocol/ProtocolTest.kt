package br.inatel.smartfarm.farm.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.util.Random

/** Byte builders following the contract (docs/design/smartfarm-app-protocol.md), independent of the parsers. */
object Build {
    fun dgram(magic: String = "SFT1", service: Int, runId: ByteArray, seq: Long, genNs: Long, payload: ByteArray = ByteArray(0), padTo: Int = 0): ByteArray {
        val b = ByteBuffer.allocate(maxOf(padTo, 38 + payload.size))
        b.put(magic.toByteArray()).put(1).put(service.toByte()).put(runId).putLong(seq).putLong(genNs).put(payload)
        return b.array()
    }

    fun fragment(frameId: Long, index: Int, count: Int, frameLen: Int, bytes: ByteArray): ByteArray =
        ByteBuffer.allocate(12 + bytes.size).putInt(frameId.toInt()).putShort(index.toShort()).putShort(count.toShort())
            .putInt(frameLen).put(bytes).array()

    fun sfim(parts: List<Pair<String, ByteArray>>): ByteArray {
        val out = ByteArrayOutputStream()
        out.write("SFIM".toByteArray())
        out.write(ByteBuffer.allocate(8).putInt(1).putInt(parts.size).array())
        for ((name, data) in parts) {
            val n = name.toByteArray()
            out.write(ByteBuffer.allocate(4).putInt(n.size).array())
            out.write(n)
            out.write(ByteBuffer.allocate(8).putLong(data.size.toLong()).array())
            out.write(data)
        }
        return out.toByteArray()
    }

    /** All fragments (with the 38 B header) of one frame. */
    fun frameDatagrams(runId: ByteArray, firstSeq: Long, frameId: Long, jpeg: ByteArray): List<ByteArray> {
        val count = (jpeg.size + 1149) / 1150
        return (0 until count).map { i ->
            val part = jpeg.copyOfRange(i * 1150, minOf(jpeg.size, (i + 1) * 1150))
            dgram(service = 3, runId = runId, seq = firstSeq + i, genNs = 0, payload = fragment(frameId, i, count, jpeg.size, part), padTo = 1200)
        }
    }
}

class WireTest {
    private val run = ByteArray(16) { it.toByte() }

    @Test fun parsesCanonicalAndDraftMagic() {
        for (m in listOf("SFT1", "SFTR")) {
            val d = Build.dgram(m, 1, run, 42, 1_700_000_000_000_000_000L, padTo = 1250)
            val h = Wire.parseDatagram(d, 0, d.size, 1)
            assertNotNull(m, h)
            assertEquals(42L, h!!.seq)
            assertEquals(1_700_000_000_000_000_000L, h.generatedAtNs)
            assertEquals("000102030405060708090a0b0c0d0e0f", h.runIdHex)
        }
    }

    @Test fun rejectsForeignDatagrams() {
        val d = Build.dgram("SFT1", 3, run, 1, 1)
        assertNull("wrong service", Wire.parseDatagram(d, 0, d.size, 1))
        assertNull("too short", Wire.parseDatagram(d, 0, 37, 3))
        val probe = Build.dgram("SFCP", 3, run, 1, 1)
        assertNull("probe packets", Wire.parseDatagram(probe, 0, probe.size, 3))
    }

    @Test fun runIdCacheReturnsSameStringWhileUnchanged() {
        val cache = RunIdCache()
        val d = Build.dgram(service = 1, runId = run, seq = 1, genNs = 1)
        val a = Wire.parseDatagram(d, 0, d.size, 1, cache)!!.runIdHex
        val b = Wire.parseDatagram(d, 0, d.size, 1, cache)!!.runIdHex
        assertTrue(a === b)
        val other = Build.dgram(service = 1, runId = ByteArray(16) { 9 }, seq = 1, genNs = 1)
        assertEquals("09".repeat(16), Wire.parseDatagram(other, 0, other.size, 1, cache)!!.runIdHex)
    }

    @Test fun objectHeader() {
        val sha = ByteArray(32) { (it * 3).toByte() }
        for (m in listOf("SFO1", "SFOB")) {
            val h = ByteBuffer.allocate(76).put(m.toByteArray()).put(run).putLong(7).putLong(26_214_400).put(sha).putLong(123).array()
            val o = Wire.parseObjectHeader(h)!!
            assertEquals(7L, o.objectId); assertEquals(26_214_400L, o.size); assertEquals(123L, o.generatedAtNs)
            assertArrayEquals(sha, o.sha256)
        }
        assertNull(Wire.parseObjectHeader(ByteArray(76)))
    }

    @Test fun timeExchange() {
        val q = Wire.packTimeQuery(99)
        assertEquals(12, q.size)
        assertEquals("SFTQ", String(q, 0, 4))
        val r = ByteBuffer.allocate(20).put("SFTR".toByteArray()).putLong(99).putLong(555).array()
        assertEquals(99L to 555L, Wire.parseTimeReply(r, 20))
        assertNull(Wire.parseTimeReply(r, 19))
    }
}

class WeatherTest {
    private val json = """{"v": 1, "type": "weather", "station": "field-A", "state": "drought",
 "readings": {"air_temperature_c": 24.3, "air_humidity_pct": 66, "soil_moisture_pct": 28.5,
              "rain_rate_mm_h": 0.0, "rain_today_mm": 4.2, "wind_speed_kmh": 7.8, "wind_direction_deg": 135,
              "solar_radiation_w_m2": 610, "uv_index": 5.1, "pressure_hpa": 1012.6, "leaf_wetness_pct": 12},
 "thresholds": {"soil_moisture_pct": {"min": 30}, "air_humidity_pct": {"min": 40}, "rain_rate_mm_h": {"max": 20}},
 "alerts": [{"id": "dry_soil", "severity": "warning", "text": "Soil moisture below 30 % — irrigation recommended", "action": "irrigation"},
            {"id": "excess_rain", "severity": "warning", "text": "Heavy rain"}],
 "irrigation": {"active": true, "remaining_s": 41}}"""

    @Test fun parsesPaddedDatagram() {
        val d = Build.dgram(service = 1, runId = ByteArray(16), seq = 0, genNs = 0, payload = json.toByteArray(Charsets.UTF_8), padTo = 1250)
        assertEquals(1250, d.size)
        val t = WeatherTelemetry.parseDatagram(d, 0, d.size)!!
        assertEquals("drought", t.state)
        assertEquals(28.5, t.reading(WeatherTelemetry.SOIL_MOISTURE)!!, 1e-9)
        assertEquals(135.0, t.reading(WeatherTelemetry.WIND_DIRECTION)!!, 1e-9)
        assertEquals(30.0, t.min(WeatherTelemetry.SOIL_MOISTURE)!!, 1e-9)
        assertEquals(20.0, t.max(WeatherTelemetry.RAIN_RATE)!!, 1e-9)
        assertEquals("dry_soil", t.irrigationAlert!!.id)
        assertTrue(t.irrigationAlert!!.text.contains("—")) // UTF-8 em dash survives
        assertNull(t.alerts[1].action)
        assertTrue(t.irrigationActive)
        assertEquals(41.0, t.irrigationRemainingS, 1e-9)
    }

    @Test fun jsonEndsAtFirstNul() {
        val payload = json.toByteArray() + byteArrayOf(0) + "garbage{".toByteArray()
        val d = Build.dgram(service = 1, runId = ByteArray(16), seq = 0, genNs = 0, payload = payload, padTo = 1250)
        assertNotNull(WeatherTelemetry.parseDatagram(d, 0, d.size))
    }

    @Test fun malformedPayloadIsNull() {
        val d = Build.dgram(service = 1, runId = ByteArray(16), seq = 0, genNs = 0, payload = "{not json".toByteArray(), padTo = 1250)
        assertNull(WeatherTelemetry.parseDatagram(d, 0, d.size))
        val e = Build.dgram(service = 1, runId = ByteArray(16), seq = 0, genNs = 0, padTo = 1250) // calibration padding only
        assertNull(WeatherTelemetry.parseDatagram(e, 0, e.size))
    }

    @Test fun emptyAlerts() {
        val t = WeatherTelemetry.parseJson("""{"v":1,"type":"weather","state":"stable","readings":{},"alerts":[],"irrigation":{"active":false,"remaining_s":0}}""")!!
        assertNull(t.irrigationAlert)
        assertFalse(t.irrigationActive)
    }
}

class SfimTest {
    private val meta = """{"v":1,"capture_id":12,"field":"field-A","state":"pest","bands":["blue","green","red","red_edge","nir"],
        "width":2048,"height":1152,"ndvi_mean":0.71,"zones":[{"x":0.12,"y":0.40,"w":0.10,"h":0.08,"label":"Pest hotspot","severity":"high"}],
        "captured_at":"2026-11-03T14:36:02Z"}""".toByteArray()
    private val rgb = ByteArray(300_000) { (it % 251).toByte() }
    private val ndvi = ByteArray(200_000) { (it % 13).toByte() }
    private val bands = ByteArray(2_000_000) { (it % 7).toByte() }
    private val payload = Build.sfim(listOf("meta.json" to meta, "rgb.jpg" to rgb, "ndvi.jpg" to ndvi, "bands.raw" to bands))

    @Test fun streamingInRandomChunks() {
        val rnd = Random(1)
        repeat(5) {
            val p = SfimStreamParser()
            var off = 0
            while (off < payload.size) {
                val n = minOf(payload.size - off, 1 + rnd.nextInt(if (rnd.nextBoolean()) 7 else 100_000))
                p.feed(payload, off, n)
                off += n
            }
            assertTrue(p.complete)
            assertNull(p.error)
            assertEquals(4L, p.declaredParts)
            assertArrayEquals(rgb, p.parts["rgb.jpg"])
            assertArrayEquals(ndvi, p.parts["ndvi.jpg"])
            assertNull("bulk bands are not kept", p.parts["bands.raw"])
            assertEquals(bands.size.toLong(), p.partSizes["bands.raw"])
            val m = CropMeta.parse(p.parts["meta.json"])!!
            assertEquals("pest", m.state)
            assertEquals("12", m.captureId)
            assertEquals(0.71, m.ndviMean!!, 1e-9)
            assertEquals(1, m.zones.size)
            assertEquals(0.12f, m.zones[0].x, 1e-6f)
            assertEquals("Pest hotspot", m.zones[0].label)
        }
    }

    @Test fun metaCarriesTheGeneratorRetransmissionsSoFar() {
        assertNull("v1 generator: no field", CropMeta.parse(meta)!!.tcpRetransRatioSoFar)
        val v11 = """{"v":1,"capture_id":"c","state":"healthy","zones":[],"tcp_retrans_ratio_so_far":0.0123,
            "tcp_bytes_sent_so_far":52428800,"tcp_bytes_retrans_so_far":644891}""".toByteArray()
        assertEquals(0.0123, CropMeta.parse(v11)!!.tcpRetransRatioSoFar!!, 0.0)
        val first = """{"v":1,"capture_id":"c","state":"healthy","zones":[],"tcp_retrans_ratio_so_far":null}""".toByteArray()
        assertNull("null before the run sent bytes", CropMeta.parse(first)!!.tcpRetransRatioSoFar)
    }

    @Test fun badMagicIsInvalid() {
        val bad = payload.copyOf().also { it[0] = 'X'.code.toByte() }
        val p = SfimStreamParser.parse(bad)
        assertFalse(p.complete)
        assertNotNull(p.error)
        assertTrue(p.parts.isEmpty())
    }

    @Test fun truncatedIsIncomplete() {
        val p = SfimStreamParser()
        p.feed(payload, 0, payload.size - 10)
        assertFalse(p.complete)
    }
}

class FrameAssemblerTest {
    private val run = ByteArray(16)
    private fun jpeg(n: Int, seed: Int) = ByteArray(n) { ((it * 31 + seed) % 256).toByte() }.also { it[0] = 0xFF.toByte(); it[1] = 0xD8.toByte() }

    @Test fun reassemblesInAnyOrder() {
        val a = FrameAssembler()
        val img = jpeg(150_000, 1)
        val dgrams = Build.frameDatagrams(run, 0, 5, img).shuffled(Random(3))
        var out: ByteArray? = null
        dgrams.forEachIndexed { i, d ->
            val r = a.feed(d, 0, d.size, i.toLong())
            if (i < dgrams.size - 1) assertNull(r) else out = r
        }
        assertArrayEquals(img, out)
        assertEquals(1L, a.framesCompleted)
        assertEquals(0L, a.framesDropped)
    }

    @Test fun lostFragmentDropsOnlyThatFrame() {
        val a = FrameAssembler()
        val f1 = Build.frameDatagrams(run, 0, 1, jpeg(5000, 1))
        val f2 = Build.frameDatagrams(run, 100, 2, jpeg(7000, 2))
        val f3 = Build.frameDatagrams(run, 200, 3, jpeg(3000, 3))
        val shown = ArrayList<ByteArray>()
        (f1 + f2.drop(1) + f3).forEach { d -> a.feed(d, 0, d.size, 0)?.let { shown += it } }
        assertEquals(2, shown.size)
        assertEquals(2L, a.framesCompleted)
        assertEquals(1L, a.framesDropped) // frame 2 dropped when frame 3 completed
        // a late fragment of frame 2 is ignored
        assertNull(a.feed(f2[0], 0, f2[0].size, 0))
    }

    @Test fun fillersAndDuplicates() {
        val a = FrameAssembler()
        val filler = Build.dgram(service = 3, runId = run, seq = 9, genNs = 0, payload = Build.fragment(0xFFFFFFFFL, 0, 0, 0, ByteArray(0)), padTo = 1200)
        assertNull(a.feed(filler, 0, filler.size, 0))
        assertEquals(1L, a.fillers)
        val f = Build.frameDatagrams(run, 0, 1, jpeg(2400, 1))
        assertNull(a.feed(f[0], 0, f[0].size, 0))
        assertNull(a.feed(f[0], 0, f[0].size, 0)) // duplicate
        assertNull(a.feed(f[1], 0, f[1].size, 0))
        assertNotNull(a.feed(f[2], 0, f[2].size, 0))
    }

    @Test fun timeoutDropsStaleFrames() {
        val a = FrameAssembler(timeoutNs = 100)
        val f = Build.frameDatagrams(run, 0, 1, jpeg(2400, 1))
        a.feed(f[0], 0, f[0].size, 0)
        val g = Build.frameDatagrams(run, 10, 2, jpeg(1000, 1))
        assertNotNull(a.feed(g[0], 0, g[0].size, 1000))
        assertEquals(1L, a.framesDropped)
    }

    @Test fun restartedNumberingStartsOver() {
        val a = FrameAssembler()
        Build.frameDatagrams(run, 0, 5000, jpeg(500, 1)).forEach { a.feed(it, 0, it.size, 0) }
        val again = Build.frameDatagrams(run, 0, 0, jpeg(500, 2))
        assertNotNull(a.feed(again[0], 0, again[0].size, 0))
    }

    @Test fun malformedHeaders() {
        val a = FrameAssembler()
        // frag_count inconsistent with frame_len
        val d = Build.dgram(service = 3, runId = run, seq = 0, genNs = 0, payload = Build.fragment(1, 0, 3, 100, ByteArray(100)), padTo = 1200)
        assertNull(a.feed(d, 0, d.size, 0))
        assertEquals(1L, a.malformed)
    }
}

class SlaTest {
    private fun window(at: Long, received: Long, expected: Long, goodput: Long, jitter: Double, gap: Double?, aoi: Double?) =
        mapOf("kind" to "receiver_window", "at_ns" to at, "received" to received, "expected_in_window" to expected,
            "goodput_bps" to goodput, "jitter_s" to jitter, "max_receive_gap_s" to gap, "aoi_p95_s" to aoi)

    @Test fun droneWindowMetrics() {
        val now = 100_000_000_000L
        val m = SlaMonitor("drone") { now }
        val goodputs = listOf(45e6, 44e6, 30e6, 46e6, 45e6, 45e6) // the first one is outside the 5 s window
        goodputs.forEachIndexed { i, g ->
            m.addWindow(window(now - (5 - i) * 1_000_000_000L - 1_000_000_000L * (if (i == 0) 1 else 0), 4600, 4700, g.toLong(), 0.002 + i * 0.001, 0.01, null))
        }
        val s = m.evaluate(silenceS = 0.001)
        val byName = s.checks.associateBy { it.spec.metric }
        // last 5 windows: 44, 30, 46, 45, 45 -> sorted[int(0.1*4)=0] = 30 Mbit/s
        assertEquals(30e6, byName["goodput_p10"]!!.value!!, 1.0)
        assertEquals(false, byName["goodput_p10"]!!.met)
        assertEquals(Stats.round(1 - 4600.0 / 4700, 5), byName["packet_loss_ratio"]!!.value!!, 1e-12)
        assertEquals(true, byName["packet_loss_ratio"]!!.met)
        assertEquals(0.007, byName["jitter_p95"]!!.value!!, 1e-12) // p95 of 0.003..0.007 -> index round(3.8)=4
        assertEquals(false, s.metStrict)
    }

    @Test fun silenceCountsAsGap() {
        val now = 50_000_000_000L
        val m = SlaMonitor("weather") { now }
        repeat(10) { i -> m.addWindow(window(now - i * 1_000_000_000L, 10, 10, 100_000, 0.001, 0.1, 0.2)) }
        val ok = m.evaluate(silenceS = 0.05)
        assertEquals(true, ok.metStrict)
        assertEquals(1.0, ok.checks[0].value!!, 0.0)
        val stalled = m.evaluate(silenceS = 6.0)
        assertEquals(false, stalled.checks.first { it.spec.metric == "max_freshness_gap" }.met)
    }

    @Test fun cropObjectsAndRetransNa() {
        val now = 500_000_000_000L
        val m = SlaMonitor("crop") { now }
        m.addObject(mapOf("at_ns" to now - 5_000_000_000L, "sha256_valid" to true, "object_transfer_time_s" to 10.9, "receive_time_s" to 10.5))
        m.addObject(mapOf("at_ns" to now - 15_000_000_000L, "sha256_valid" to true, "object_transfer_time_s" to null, "receive_time_s" to 12.0))
        val s = m.evaluate(cropRateBps = 19.6e6)
        val c = s.checks.associateBy { it.spec.metric }
        assertEquals(12.0, c["object_transfer_time"]!!.value!!, 1e-9)
        assertEquals(1.0, c["sha256_valid_ratio"]!!.value!!, 0.0)
        assertEquals(0.0, c["accepted_corrupt_objects"]!!.value!!, 0.0)
        assertNull(c["retransmitted_bytes_ratio"]!!.met)
        assertNull("strict evaluation needs the generator's retransmissions", s.metStrict)
        assertEquals(true, s.metShown)
        m.addObject(mapOf("at_ns" to now, "sha256_valid" to false, "receive_time_s" to 11.0))
        assertEquals(false, m.evaluate(cropRateBps = 19.6e6).metShown)
    }

    @Test fun cropRetransFromTheGeneratorIsLive() {
        val now = 500_000_000_000L
        val m = SlaMonitor("crop") { now }
        m.addObject(mapOf("at_ns" to now - 5_000_000_000L, "sha256_valid" to true, "object_transfer_time_s" to 10.9))
        val ok = m.evaluate(cropRateBps = 19.6e6, retransRatio = 0.004)
        val c = ok.checks.associateBy { it.spec.metric }
        assertEquals(0.004, c[SlaCatalog.RETRANS]!!.value!!, 0.0)
        assertEquals(true, c[SlaCatalog.RETRANS]!!.met)
        assertEquals("all crop metrics known: strict evaluation too", true, ok.metStrict)
        val bad = m.evaluate(cropRateBps = 19.6e6, retransRatio = 0.031)
        assertEquals(false, bad.metShown)
    }

    @Test fun builtInCatalogMatchesServiceCatalog() {
        // src/smartfarm/contracts/service.py (2026-11-03): drone 40 Mbit/s offered, goodput_p10 >= 34 Mbit/s
        val p10 = SlaCatalog.DRONE.first { it.metric == "goodput_p10" }
        assertEquals(34_000_000.0, p10.threshold, 0.0)
        assertEquals(40_000_000.0, SlaCatalog.DEFAULT_OFFERED_BPS["drone"]!!, 0.0)
        val m = SlaMonitor("drone") { 0L }
        // the live case: ~36 Mbit/s in the worst 10 % of the windows is now within the SLA
        repeat(5) { i -> m.addWindow(mapOf("at_ns" to 0L - i * 1_000_000_000L, "received" to 3750L, "expected_in_window" to 3750L,
            "goodput_bps" to 36_000_000L, "jitter_s" to 0.002, "max_receive_gap_s" to 0.03)) }
        assertEquals(true, m.evaluate(silenceS = 0.01).checks.first { it.spec.metric == "goodput_p10" }.met)
    }

    @Test fun orchestratorTargetsReplaceTheBuiltInOnes() {
        val m = SlaMonitor("drone") { 0L }
        repeat(5) { i -> m.addWindow(mapOf("at_ns" to 0L - i * 1_000_000_000L, "received" to 3750L, "expected_in_window" to 3750L,
            "goodput_bps" to 36_000_000L, "jitter_s" to 0.002, "max_receive_gap_s" to 0.03)) }
        m.specs = listOf(SlaCatalog.fromWire("goodput_p10", "ge", 38_000_000.0, "bit/s", 5)!!)
        val s = m.evaluate(silenceS = 0.01)
        assertEquals(1, s.checks.size)
        assertEquals(false, s.metStrict)
        assertNull(SlaCatalog.fromWire("energy", "le", 1.0, "J", 5))
        assertNull(SlaCatalog.fromWire("pdr", "gt", 1.0, "ratio", 5))
        assertNull(SlaCatalog.fromWire("pdr", "ge", Double.NaN, "ratio", 5))
        assertEquals(MetricUnit.RATIO_PCT, SlaCatalog.fromWire("pdr", "ge", 0.99, "ratio", 10)!!.unit)
        assertEquals("Readings delivered", SlaCatalog.fromWire("pdr", "ge", 0.99, "ratio", 10)!!.friendly)
    }

    @Test fun percentileMatchesPython() {
        // Python: ordered[min(n-1, int(round(q*(n-1))))] with half-even rounding: n=11, q=0.95 -> round(9.5) = 10
        val v = DoubleArray(11) { it.toDouble() }
        assertEquals(10.0, Stats.percentile(v, 0.95)!!, 0.0)
        // n=3 -> round(1.9) = 2 ; n=2 -> round(0.95) = 1
        assertEquals(2.0, Stats.percentile(doubleArrayOf(0.0, 1.0, 2.0), 0.95)!!, 0.0)
        assertEquals(1.0, Stats.percentile(doubleArrayOf(0.0, 1.0), 0.95)!!, 0.0)
        assertNull(Stats.percentile(DoubleArray(0), 0.95))
    }
}

class JsonTest {
    @Test fun encodesCollectorRecords() {
        val s = Json.encode(linkedMapOf("kind" to "receiver_window", "x" to null, "ok" to true, "n" to 3L, "f" to 0.5, "q" to "a\"b", "nan" to Double.NaN,
            "list" to listOf(1, "z")))
        assertEquals("""{"kind": "receiver_window", "x": null, "ok": true, "n": 3, "f": 0.5, "q": "a\"b", "nan": null, "list": [1, "z"]}""", s)
        // round trip through org.json
        val o = org.json.JSONObject(s)
        assertEquals(3, o.getInt("n"))
    }
}
