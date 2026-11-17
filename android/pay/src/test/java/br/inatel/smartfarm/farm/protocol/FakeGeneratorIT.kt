package br.inatel.smartfarm.farm.protocol

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

/**
 * Desktop end-to-end check of the parsers against android/tools/fake_generator.py on 127.0.0.1 (ports 57000+):
 * the receive paths of the app (same protocol classes, plain sockets instead of Network-bound ones).
 * Set SF_GENERATOR to another generator command (e.g. the lab generator in local mode) to test it instead.
 */
class FakeGeneratorIT {
    private val base = 57000
    private val lo = InetAddress.getByName("127.0.0.1")

    @Test fun receivesAllServicesFromFakeGenerator() {
        val py = python()
        val script = java.io.File(repoRoot(), "android/tools/fake_generator.py")
        assumeTrue("python3 + Pillow + fake_generator.py needed", py != null && script.exists() &&
            ProcessBuilder(py, "-c", "import PIL").start().waitFor() == 0)

        val stop = java.util.concurrent.atomic.AtomicBoolean(false)
        val weatherStats = UdpStats("weather")
        val droneStats = UdpStats("drone")
        val lastWeather = AtomicReference<WeatherTelemetry?>()
        val frames = AtomicInteger()
        val badJpeg = AtomicInteger()
        val assembler = FrameAssembler()
        val objects = ArrayList<Pair<Boolean, SfimStreamParser>>()
        val clock = ClockSync({ DatagramSocket() }, port = base)

        fun udp(port: Int, code: Int, stats: UdpStats, onPacket: (ByteArray, Int, Long) -> Unit) = Thread {
            DatagramSocket(null).use { ds ->
                ds.reuseAddress = true
                ds.bind(InetSocketAddress(lo, port))
                ds.soTimeout = 200
                val buf = ByteArray(65535)
                val p = DatagramPacket(buf, buf.size)
                val ids = RunIdCache()
                while (!stop.get()) {
                    try { p.length = buf.size; ds.receive(p) } catch (_: SocketTimeoutException) { continue }
                    val arrival = WallClock.nowNs()
                    val h = Wire.parseDatagram(buf, 0, p.length, code, ids) ?: continue
                    clock.observePeer(p.address.hostAddress!!)
                    stats.add(h.runIdHex, h.seq, clock.toReceiverClock(h.generatedAtNs), p.length, arrival)
                    onPacket(buf, p.length, arrival)
                }
            }
        }.apply { isDaemon = true; start() }

        udp(base + 1, 1, weatherStats) { b, n, _ -> WeatherTelemetry.parseDatagram(b, 0, n)?.let { lastWeather.set(it) } }
        udp(base + 3, 3, droneStats) { b, n, t ->
            assembler.feed(b, 0, n, t)?.let { jpeg ->
                frames.incrementAndGet()
                val ok = jpeg[0] == 0xFF.toByte() && jpeg[1] == 0xD8.toByte() && jpeg[jpeg.size - 2] == 0xFF.toByte() && jpeg[jpeg.size - 1] == 0xD9.toByte()
                if (!ok) badJpeg.incrementAndGet()
            }
        }
        val server = ServerSocket().apply { reuseAddress = true; bind(InetSocketAddress(lo, base + 2)); soTimeout = 200 }
        Thread {
            server.use {
                while (!stop.get()) {
                    val s = try { server.accept() } catch (_: SocketTimeoutException) { continue }
                    s.use { conn ->
                        val input = conn.getInputStream()
                        val buf = ByteArray(1 shl 20)
                        while (true) {
                            val hb = ByteArray(Wire.OBJECT_SIZE)
                            var got = 0
                            while (got < hb.size) { val r = input.read(hb, got, hb.size - got); if (r < 0) break; got += r }
                            if (got < hb.size) break
                            val h = Wire.parseObjectHeader(hb) ?: break
                            val sha = MessageDigest.getInstance("SHA-256")
                            val parser = SfimStreamParser()
                            var remaining = h.size
                            while (remaining > 0) {
                                val r = input.read(buf, 0, minOf(buf.size.toLong(), remaining).toInt())
                                if (r < 0) break
                                sha.update(buf, 0, r); parser.feed(buf, 0, r); remaining -= r
                            }
                            if (remaining > 0) break
                            synchronized(objects) { objects += sha.digest().contentEquals(h.sha256) to parser }
                        }
                    }
                }
            }
        }.apply { isDaemon = true; start() }
        Thread.sleep(300)

        val external = System.getenv("SF_GENERATOR") // e.g. the lab generator in local mode (see android/README.md)
        val runMs = if (external != null) (System.getenv("SF_GENERATOR_SECONDS")?.toLong() ?: 16) * 1000 else 6500L
        val cmd = external?.split(" ")
            ?: listOf(py!!, "-I", script.absolutePath, "--service", "all", "--duration", "6", "--port-base", "$base",
                "--drone-drop-every", "500", "--crop-corrupt-every", "3", "--crop-object-bytes", "3145728")
        val gen = ProcessBuilder(cmd).redirectErrorStream(true).start()
        val out = StringBuilder()
        Thread { gen.inputStream.bufferedReader().forEachLine { synchronized(out) { out.appendLine(it) } } }.apply { isDaemon = true; start() }

        // AoI sampling and per-second windows, like the app's ticker
        val windows = ArrayList<Map<String, Any?>>()
        val droneWindows = ArrayList<Map<String, Any?>>()
        val t0 = System.currentTimeMillis()
        var next = t0 + 1000
        var controlReply: JSONObject? = null
        while (System.currentTimeMillis() - t0 < runMs) {
            Thread.sleep(100)
            weatherStats.sampleAoi()
            if (System.currentTimeMillis() >= next) {
                next += 1000
                weatherStats.report(1.0)?.let { windows += it }
                droneStats.report(1.0)?.let { droneWindows += it }
            }
            if (controlReply == null && System.currentTimeMillis() - t0 > 3000) {
                Socket().use { s ->
                    s.connect(InetSocketAddress(lo, base + 10), 2000)
                    s.soTimeout = 3000
                    s.getOutputStream().write((Json.encode(linkedMapOf("v" to 1, "cmd" to "irrigation", "action" to "activate",
                        "duration_s" to 60, "source" to "app")) + "\n").toByteArray())
                    controlReply = JSONObject(BufferedReader(InputStreamReader(s.getInputStream())).readLine())
                }
            }
        }
        if (external != null) { gen.destroy(); gen.waitFor(10, TimeUnit.SECONDS) } else assertTrue(gen.waitFor(20, TimeUnit.SECONDS))
        Thread.sleep(500)
        stop.set(true)
        println(out)

        // clock: same host, so the offset is close to zero and the RTT tiny
        assertNotNull("clock offset", clock.offsetNs)
        assertTrue("offset ${clock.offsetNs}", kotlin.math.abs(clock.offsetNs!!) < 50_000_000L)

        // weather
        val tel = lastWeather.get()
        assertNotNull("weather telemetry", tel)
        println("weather: ${tel!!.state} ${tel.readings} alerts ${tel.alerts} irrigation ${tel.irrigationActive}/${tel.irrigationRemainingS}; control reply $controlReply")
        assertTrue(tel.readings.size >= 11)
        assertTrue("weather windows", windows.size >= 3)
        val steady = windows.drop(1).dropLast(1)
        val pdr = steady.sumOf { (it["received"] as Long).toDouble() } / steady.sumOf { (it["expected_in_window"] as Long).toDouble() }
        assertEquals(1.0, pdr, 0.02)
        assertTrue("AoI measured", steady.any { it["aoi_p95_s"] != null })
        assertEquals(true, controlReply!!.getBoolean("ok"))
        // after "activate" the generator reports irrigation in the telemetry
        assertTrue("irrigation active in telemetry", tel.irrigationActive)

        // drone: frames reassembled, a datagram lost every 500 drops its frame only
        assertTrue("frames ${frames.get()}", frames.get() >= 100)
        assertEquals(0, badJpeg.get())
        assertTrue("fillers counted", assembler.fillers > 0)
        println("drone: frames ${frames.get()} dropped ${assembler.framesDropped} fillers ${assembler.fillers} malformed ${assembler.malformed} windows ${droneWindows.size}")
        assertEquals(0L, assembler.malformed)
        val loss = droneWindows.drop(1).dropLast(1).mapNotNull { it["loss_ratio_window"] as Double? }
        if (external == null) {
            assertTrue("dropped frames counted", assembler.framesDropped > 0)
            assertTrue("loss measured", loss.isNotEmpty() && loss.average() > 0.0005 && loss.average() < 0.01)
        }

        // crop: every 3rd object corrupted on purpose
        synchronized(objects) {
            println("crop: ${objects.size} objects, valid ${objects.count { it.first }}, parts ${objects.firstOrNull()?.second?.partSizes}")
            if (external == null) {
                assertTrue("objects ${objects.size}", objects.size >= 3)
                assertTrue(objects.any { !it.first })
            } else {
                assertTrue("objects ${objects.size}", objects.isNotEmpty())
                assertTrue(objects.all { it.first })
            }
            for ((valid, p) in objects.filter { it.first }) {
                assertTrue(valid && p.complete)
                assertNotNull(CropMeta.parse(p.parts[SfimStreamParser.META]))
                val rgb = p.parts[SfimStreamParser.RGB]!!
                assertEquals(0xFF.toByte(), rgb[0]); assertEquals(0xD8.toByte(), rgb[1])
                assertNotNull(p.parts[SfimStreamParser.NDVI])
            }
        }
    }
}
