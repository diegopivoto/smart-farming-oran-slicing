package br.inatel.smartfarm.farm.protocol

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.util.Random
import java.util.concurrent.TimeUnit

/** Repository root (unit tests run with the module directory android/app as working directory). */
internal fun repoRoot(): File {
    var d: File? = File("").absoluteFile
    while (d != null && !File(d, "deployment/traffic/sf_traffic.py").exists()) d = d.parentFile
    return d ?: File("../..").absoluteFile
}

internal fun python(): String? = listOf("python3", "/usr/bin/python3").firstOrNull {
    try { ProcessBuilder(it, "--version").start().waitFor(5, TimeUnit.SECONDS) } catch (_: Exception) { false }
}

/**
 * Feeds the same synthetic packet stream (losses, reordering, delays, idle seconds, a run change) to this app's
 * UdpStats and to sf_traffic.py's UdpStats, and requires identical receiver_window records.
 */
class PythonParityTest {
    @Test fun udpStatsRecordsMatchSfTraffic() {
        val py = python()
        val script = File(repoRoot(), "deployment/traffic/sf_traffic.py")
        assumeTrue("python3 and sf_traffic.py needed", py != null && script.exists())

        val rnd = Random(7)
        val ops = JSONArray()
        var clock = 1_762_000_000_000_000_000L
        var seq = 0L
        var run = "aa".repeat(16)
        for (second in 0 until 14) {
            if (second == 9) { run = "bb".repeat(16); seq = 1000 }
            val idle = second in 5..8 // > 3 idle windows: the stream "ends", then a new run starts
            if (!idle) {
                for (k in 0 until 40) {
                    clock += 20_000_000L + rnd.nextInt(5_000_000)
                    if (rnd.nextInt(10) == 0) { seq++; continue } // lost
                    val s = if (rnd.nextInt(15) == 0) seq - 1 else seq // occasional reordering / duplicate seq
                    seq++
                    val delay = 60_000_000L + rnd.nextInt(30_000_000)
                    val gen = if (second == 0 && k < 5) null else clock - delay // first packets before the clock sync
                    ops.put(JSONArray(listOf("add", run, s, gen ?: JSONObject.NULL, 1250, clock)))
                    if (k % 4 == 0) ops.put(JSONArray(listOf("aoi", clock + 1_000_000)))
                }
            } else {
                clock += 1_000_000_000L
            }
            ops.put(JSONArray(listOf("report", clock)))
        }

        // ---- Kotlin
        var now = 0L
        val stats = UdpStats("weather") { now }
        val mine = ArrayList<Map<String, Any?>?>()
        for (i in 0 until ops.length()) {
            val op = ops.getJSONArray(i)
            when (op.getString(0)) {
                "add" -> stats.add(op.getString(1), op.getLong(2), if (op.isNull(3)) null else op.getLong(3), op.getInt(4), op.getLong(5))
                "aoi" -> { now = op.getLong(1); stats.sampleAoi() }
                "report" -> { now = op.getLong(1); mine += stats.report(1.0) }
            }
        }

        // ---- Python reference
        val code = """
import json, sys
sys.path.insert(0, sys.argv[1])
import sf_traffic
clock = [0]
sf_traffic.now_ns = lambda: clock[0]
s = sf_traffic.UdpStats("weather")
out = []
for op in json.load(sys.stdin):
    if op[0] == "add":
        s.add(op[1], op[2], op[3], op[4], op[5])
    elif op[0] == "aoi":
        clock[0] = op[1]; s.sample_aoi()
    else:
        clock[0] = op[1]; out.append(s.report(1.0))
print(json.dumps(out))
"""
        val p = ProcessBuilder(py, "-I", "-c", code, script.parentFile.absolutePath).redirectErrorStream(true).start()
        p.outputStream.use { it.write(ops.toString().toByteArray()) }
        val text = p.inputStream.bufferedReader().readText()
        assertTrue("python exited: $text", p.waitFor(30, TimeUnit.SECONDS) && p.exitValue() == 0)
        val ref = JSONArray(text.trim().lines().last())

        assertEquals(ref.length(), mine.size)
        var compared = 0
        for (i in 0 until ref.length()) {
            val r = ref.opt(i)
            val m = mine[i]
            if (r == JSONObject.NULL) { assertEquals("window $i", null, m); continue }
            r as JSONObject
            assertEquals("window $i keys", r.keySet(), m!!.keys)
            for (k in r.keySet()) {
                val rv = r.opt(k)
                val mv = m[k]
                when {
                    rv == JSONObject.NULL -> assertEquals("$i.$k", null, mv)
                    rv is Number -> assertEquals("$i.$k", rv.toDouble(), (mv as Number).toDouble(), 1e-9 * maxOf(1.0, kotlin.math.abs(rv.toDouble())))
                    else -> assertEquals("$i.$k", rv.toString(), mv.toString())
                }
                compared++
            }
        }
        assertTrue("compared fields: $compared", compared > 100)
    }
}
