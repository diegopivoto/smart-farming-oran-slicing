package br.inatel.smartfarm.farm.protocol

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.net.HttpURLConnection
import java.net.ServerSocket
import java.net.URL
import java.util.concurrent.TimeUnit

class OrchestrationTest {
    @Test fun requestBodyMatchesTheApi() {
        val o = JSONObject(Orchestration.requestBody("crop", "10.45.128.2", "ab12"))
        assertEquals("crop", o.getString("service"))
        assertEquals("10.45.128.2", o.getString("phone_ip"))
        assertEquals("ab12", o.getString("app_session"))
        assertEquals(URL("http://10.45.0.1:8090/api/v1/session/abc"), Orchestration.sessionUrl("http://10.45.0.1:8090/ ", "abc"))
    }

    @Test fun requestCarriesTheOranModeOnlyWhenValid() {
        assertFalse("no mode: older request shape", JSONObject(Orchestration.requestBody("crop", "10.45.128.2", "ab12")).has("oran_mode"))
        assertEquals("ddqn", JSONObject(Orchestration.requestBody("crop", "10.45.128.2", "ab12", "ddqn")).getString("oran_mode"))
        assertEquals("ran-default", JSONObject(Orchestration.requestBody("crop", "10.45.128.2", "ab12", Orchestration.ORAN_RAN_DEFAULT)).getString("oran_mode"))
        assertFalse(JSONObject(Orchestration.requestBody("crop", "10.45.128.2", "ab12", "bogus")).has("oran_mode"))
    }

    @Test fun parsesTheOranModeAndAgent() {
        val base = """"session":"0123456789abcdef","state":"streaming","sd":"000003","history":[]"""
        val links = """"uplink":{"label":"MCS 9 · 99 RB","by_e2":true},"downlink":{"label":"52 PRB quota","by_e2":true}"""
        val ddqn = Orchestration.parse("""{$base,"oran":{"available":true,"controller":"e2-action","decided_by":"DDQN agent (step 4)",$links,
            "mode":{"requested":"ddqn","effective":"ddqn","detail":"frozen DDQN agent in control"},"agent_running":true,
            "agent":{"running":true,"step":4,"last_verdict":"met"},
            "history":[{"link":"step","value":"#3 UL MCS 9 / 99 RB + DL 52 PRB","status":"SLA met","ok":true,"age_s":12.0}]}}""")!!.oran!!
        assertTrue(ddqn.ddqn && ddqn.agentRunning)
        assertEquals(4, ddqn.agentStep)
        assertEquals("met", ddqn.lastVerdict)
        assertEquals("#3 UL MCS 9 / 99 RB + DL 52 PRB", ddqn.history[0].value)
        assertEquals("Mode: DDQN agent · step 4 · last step: SLA met", br.inatel.smartfarm.farm.ui.modeLine(ddqn))
        val busy = Orchestration.parse("""{$base,"oran":{"available":true,"controller":"ran-default",$links,
            "mode":{"requested":"ddqn","effective":"ran-default","detail":"xApp busy"},"agent":{"running":false,"step":null}}}""")!!.oran!!
        assertFalse(busy.ddqn)
        assertNull(busy.agentStep)
        assertEquals("Mode: RAN defaults (requested DDQN agent: xApp busy)", br.inatel.smartfarm.farm.ui.modeLine(busy))
        val plain = Orchestration.parse("""{$base,"oran":{"available":true,$links,"mode":{"requested":"ran-default","effective":"ran-default"}}}""")!!.oran!!
        assertEquals("Mode: RAN defaults", br.inatel.smartfarm.farm.ui.modeLine(plain))
        val old = Orchestration.parse("""{$base,"oran":{"available":true,$links}}""")!!.oran!! // 1.3.0-era orchestrator
        assertNull(old.modeEffective)
        assertNull(br.inatel.smartfarm.farm.ui.modeLine(old))
    }

    @Test fun parsesProgressAndFailure() {
        val v = Orchestration.parse("""{"session":"0123456789abcdef","state":"generator_started","un3gpp_ip":"10.45.0.4",
            "cause":null,"sd":"000002","generator_run_id":"aa","history":[{"state":"accepted"},{"state":"un3gpp_switching"},
            {"state":"un3gpp_in_slice","un3gpp_ip":"10.45.0.4"},{"state":"generator_started"}]}""")!!
        assertEquals("10.45.0.4", v.un3gppIp)
        assertNull(v.cause)
        assertTrue(v.reached(Orchestration.IN_SLICE) && !v.reached(Orchestration.STREAMING) && !v.terminal)

        val f = Orchestration.parse("""{"session":"0123456789abcdef","state":"failed","un3gpp_ip":null,
            "cause":"UN3GPP slice switch failed: rejected","history":[{"state":"accepted"},{"state":"un3gpp_switching"},{"state":"failed"}]}""")!!
        assertTrue(f.terminal)
        assertNull(f.un3gppIp)
        assertFalse(f.reached(Orchestration.IN_SLICE))
        assertEquals("UN3GPP slice switch failed: rejected", f.cause)
        assertNull(Orchestration.parse("not json"))
        assertEquals("phone_ip must be inside 10.45.0.0/16", Orchestration.Reply(400, """{"error":"phone_ip must be inside 10.45.0.0/16"}""").error())
        assertEquals("HTTP 502", Orchestration.Reply(502, "<html>").error())
    }

    @Test fun parsesSlaTargetsOfferedRateAndReason() {
        val v = Orchestration.parse("""{"session":"0123456789abcdef","state":"un3gpp_switching","sd":"000003",
            "reason":"un3gpp_ip_conflicts_with_phone_subnet","offered_rate_bps":40000000,
            "sla":[{"name":"goodput_p10","operator":"ge","threshold":34000000.0,"unit":"bit/s","window_s":5},
                   {"name":"packet_loss_ratio","operator":"le","threshold":0.05,"unit":"ratio","window_s":5},
                   {"name":"future_metric","operator":"le","threshold":1,"unit":"J","window_s":5}],
            "history":[{"state":"accepted"},{"state":"un3gpp_switching"},{"state":"un3gpp_switching","reason":"un3gpp_ip_conflicts_with_phone_subnet"}]}""")!!
        assertEquals(Orchestration.IP_CONFLICT, v.reason)
        assertEquals(40_000_000.0, v.offeredRateBps!!, 0.0)
        val sla = v.sla!!
        assertEquals(listOf("goodput_p10", "packet_loss_ratio"), sla.map { it.metric }) // unknown metric skipped
        assertEquals(34_000_000.0, sla[0].threshold, 0.0)
        assertEquals(Op.GE, sla[0].op)
        assertEquals(MetricUnit.BPS, sla[0].unit)
        assertEquals("Video throughput (worst 10 %)", sla[0].friendly)

        val old = Orchestration.parse("""{"session":"0123456789abcdef","state":"streaming"}""")!!
        assertNull("orchestrator without an SLA list: built-in catalog", old.sla)
        assertNull(old.offeredRateBps)
        assertNull(old.reason)
    }

    @Test fun parsesTheOranCardAndToleratesItsAbsence() {
        val v = Orchestration.parse("""{"session":"0123456789abcdef","state":"streaming","sd":"000002","history":[],
            "oran":{"schema":"smartfarm.oran_view.v1","available":true,"controller":"e2-action","decided_by":"E2 action (O-RAN xApp)",
              "agent_running":false,"sd":"000002",
              "uplink":{"node":"un3gpp","mode":"override","by_e2":true,"mcs":11,"rb":66,"label":"MCS 11 · 66 RB","stale":false,
                "readback":"MAC uses MCS 11 · 66 RB · SNR 22.4 dB","age_s":0.2,
                "last_action":{"action_id":1793918602727464001,"status":"REJECTED_STALE","ok":false,"value":"MCS 13 · 99 RB","age_s":1.1}},
              "downlink":{"node":"oai","sd":"000002","prb_quota":78,"by_e2":true,"label":"78 PRB quota","stale":false,"age_s":0.1,
                "last_action":{"action_id":2,"status":"APPLIED","ok":true,"value":"78 PRB","age_s":2.0}},
              "history":[{"node":"un3gpp","link":"uplink","action_id":1,"status":"SENT","ok":null,"value":"UL MCS 13 · 99 RB","age_s":1.1},
                         {"node":"oai","link":"downlink","action_id":2,"status":"APPLIED","ok":true,"value":"DL 78 PRB","age_s":2.0},
                         {"broken":true}]}}""")!!
        val o = v.oran!!
        assertTrue(o.byE2)
        assertEquals("E2 action (O-RAN xApp)", o.decidedBy)
        assertEquals("MCS 11 · 66 RB", o.uplink!!.label)
        assertEquals("REJECTED_STALE", o.uplink!!.lastStatus)
        assertEquals(false, o.uplink!!.lastOk)
        assertEquals("78 PRB quota", o.downlink!!.label)
        assertEquals(true, o.downlink!!.lastOk)
        assertNull(o.downlink!!.readback)
        assertEquals(listOf("UL MCS 13 · 99 RB", "DL 78 PRB"), o.history.map { it.value }) // entry without a value skipped
        assertNull(o.history[0].ok)
        assertEquals(2.0, o.history[1].ageS!!, 0.0)

        val base = """"session":"0123456789abcdef","state":"streaming","sd":"000002","history":[]"""
        assertNull("absent", Orchestration.parse("{$base}")!!.oran)
        assertNull("unavailable", Orchestration.parse("""{$base,"oran":{"available":false,"detail":"xApp journal not readable"}}""")!!.oran)
        assertNull("no link", Orchestration.parse("""{$base,"oran":{"available":true,"uplink":null,"downlink":{}}}""")!!.oran)
        // a malformed oran object never costs the session view
        for (bad in listOf("5", "\"x\"", "[]", "{\"available\":\"yes\"}", "{\"available\":true,\"uplink\":{\"label\":\"L\",\"last_action\":7},\"history\":{}}")) {
            val parsed = Orchestration.parse("{$base,\"oran\":$bad}")
            assertNotNull(bad, parsed)
            assertEquals("streaming", parsed!!.state)
        }
        assertEquals("L", Orchestration.parse("{$base,\"oran\":{\"available\":true,\"uplink\":{\"label\":\"L\",\"last_action\":7}}}")!!.oran!!.uplink!!.label)
    }

    @Test fun parseErrorsAreReported() {
        // live: Python wrote NaN and Android's org.json rejected the whole reply (the JVM org.json used here accepts NaN,
        // so a truncated reply stands in for any unreadable body)
        val (view, error) = Orchestration.parseOrError("""{"session":"0123456789abcdef","state":"strea""")
        assertNull(view)
        assertTrue(!error.isNullOrBlank())
        val (ok, none) = Orchestration.parseOrError("""{"session":"0123456789abcdef","state":"accepted"}""")
        assertNotNull(ok)
        assertNull(none)
    }

    @Test fun pollToleranceWhileConnectingAndStreaming() {
        var now = 0L
        val t = Orchestration.Tolerance({ now })
        now = 29_000
        t.miss()
        assertFalse("29 s without a valid reply while connecting: keep waiting", t.giveUp(200, streaming = false))
        now = 30_000
        assertTrue("30 s: give up", t.giveUp(null, streaming = false))
        assertFalse("streaming: silence alone never ends the session", t.giveUp(null, streaming = true))
        t.valid()
        assertEquals(0, t.misses)
        assertFalse(t.giveUp(500, streaming = false))
        assertTrue("404: the orchestrator forgot the session", t.giveUp(404, streaming = true))
    }

    @Test fun postRetryPolicy() {
        assertTrue(Orchestration.retryPost(null, java.net.SocketTimeoutException("read timed out"), 5))
        assertTrue(Orchestration.retryPost(502, null, 1))
        assertTrue("2xx with an unreadable body", Orchestration.retryPost(202, null, 1))
        assertFalse(Orchestration.retryPost(400, null, 1))
        assertFalse(Orchestration.retryPost(403, null, 1))
        assertFalse(Orchestration.retryPost(409, null, 1))
        assertTrue(Orchestration.retryPost(null, java.net.ConnectException("refused"), 2))
        assertFalse("not running: manual fallback after 3 tries", Orchestration.retryPost(null, java.net.ConnectException("refused"), 3))
    }

    /** The app's client against deployment/scripts/demo_orchestrator.py --dry-run on 127.0.0.1 (fake UE and core). */
    @Test fun drivesTheDryRunOrchestrator() {
        val root = repoRoot()
        val py = File(root, ".venv/bin/python")
        val script = File(root, "deployment/scripts/demo_orchestrator.py")
        assumeTrue("project .venv and demo_orchestrator.py needed", py.canExecute() && script.exists())
        val port = ServerSocket(0).use { it.localPort }
        val logs = File(System.getProperty("java.io.tmpdir"), "sf-orch-it-${System.nanoTime()}")
        val proc = ProcessBuilder(py.path, "-u", script.path, "--dry-run", "--port", "$port", "--hold-s", "0",
            "--settle-s", "0.2", "--output", logs.path).redirectErrorStream(true).start()
        try {
            val base = "http://127.0.0.1:$port"
            val open = { u: URL -> u.openConnection() as HttpURLConnection }
            var health: Orchestration.Reply? = null
            repeat(100) {
                if (health?.ok != true) {
                    health = runCatching { Orchestration.call(open, "GET", Orchestration.url(base, "/api/v1/health")) }.getOrNull()
                    if (health?.ok != true) Thread.sleep(100)
                }
            }
            assertTrue("orchestrator did not start", health?.ok == true)

            val bad = Orchestration.call(open, "POST", Orchestration.serviceUrl(base), Orchestration.requestBody("drone", "8.8.8.8", "it1"))
            assertEquals(400, bad.code)
            assertTrue(bad.error().contains("10.45.0.0/16"))

            val created = Orchestration.call(open, "POST", Orchestration.serviceUrl(base), Orchestration.requestBody("drone", "10.45.160.6", "it2"))
            assertEquals(202, created.code)
            var view = Orchestration.parse(created.text)!!
            val seen = LinkedHashSet<String>()
            val deadline = System.currentTimeMillis() + 10_000
            while (view.state != Orchestration.STREAMING && System.currentTimeMillis() < deadline) {
                seen += view.state
                Thread.sleep(100)
                view = Orchestration.parse(Orchestration.call(open, "GET", Orchestration.sessionUrl(base, view.session)).text)!!
            }
            assertEquals(Orchestration.STREAMING, view.state)
            assertEquals(listOf("accepted", "un3gpp_switching", "un3gpp_in_slice", "generator_started", "streaming"), view.reached)
            assertNotNull(view.un3gppIp)
            assertEquals("000003", view.sd)
            assertEquals(34_000_000.0, view.sla!!.first { it.metric == "goodput_p10" }.threshold, 0.0) // SERVICE_CATALOG
            assertEquals(40_000_000.0, view.offeredRateBps!!, 0.0)
            assertEquals(4, view.sla!!.size)
            val oran = view.oran!! // dry-run: fake radios through the same read-only view
            assertNotNull(oran.uplink?.label)
            assertNotNull(oran.downlink?.label)
            assertEquals("default mode: the RAN defaults", Orchestration.ORAN_RAN_DEFAULT, oran.modeEffective)
            assertEquals(Orchestration.ORAN_RAN_DEFAULT, oran.modeRequested)
            val badMode = Orchestration.call(open, "POST", Orchestration.serviceUrl(base),
                Orchestration.requestBody("drone", "10.45.160.6", "it3").replace("}", ",\"oran_mode\":\"turbo\"}"))
            assertEquals(400, badMode.code)

            val stopped = Orchestration.call(open, "DELETE", Orchestration.sessionUrl(base, view.session))
            assertEquals("stopped", Orchestration.parse(stopped.text)!!.state)
        } finally {
            proc.destroy()
            proc.waitFor(5, TimeUnit.SECONDS)
            logs.deleteRecursively()
        }
    }
}
