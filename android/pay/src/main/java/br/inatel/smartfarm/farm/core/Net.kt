package br.inatel.smartfarm.farm.core

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Handler
import android.os.Looper
import android.util.Log
import br.inatel.smartfarm.farm.protocol.Json
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URL
import java.util.concurrent.ArrayBlockingQueue

const val TAG = "SmartFarm"

/**
 * Collector client (sf_traffic.py post()): one background thread, bounded queue, never blocks a measurement loop.
 * Uses the process default network, i.e. the phone's default `internet` session (10.45.0.x) towards PC1.
 */
class Reporter(private val urlProvider: () -> String, private val enabled: () -> Boolean) {
    private val queue = ArrayBlockingQueue<String>(10_000)
    @Volatile var sent = 0L; private set
    @Volatile var failed = 0L; private set

    init {
        Thread({
            while (true) {
                val body = queue.take()
                try {
                    val c = URL(urlProvider()).openConnection() as HttpURLConnection
                    c.connectTimeout = 3000; c.readTimeout = 3000
                    c.requestMethod = "POST"; c.doOutput = true
                    c.setRequestProperty("Content-Type", "application/json")
                    c.outputStream.use { it.write(body.toByteArray()) }
                    c.inputStream.use { it.readBytes() }
                    c.disconnect()
                    sent++
                } catch (e: Exception) {
                    failed++ // reporting must never stop the measurement
                }
            }
        }, "collector-reporter").apply { isDaemon = true }.start()
    }

    fun post(record: Map<String, Any?>) {
        if (!enabled()) return
        queue.offer(Json.encode(record))
    }
}

/** requestNetwork() for one service slice (validated by the capability probe: granted in ~0.5 s). */
class SliceSession(
    context: Context,
    val kind: ServiceKind,
    val capsText: String,
    private val listener: Listener,
) {
    interface Listener {
        fun onGranted(network: Network, elapsedS: Double)
        fun onLinkUp(network: Network, ip: Inet4Address, iface: String?)
        fun onLost(network: Network)
        fun onUnavailable(reason: String)
    }

    private val cm = context.getSystemService(ConnectivityManager::class.java)
    private val handler = Handler(Looper.getMainLooper())
    private var callback: ConnectivityManager.NetworkCallback? = null
    private val startedMs = android.os.SystemClock.elapsedRealtime()
    private var linkReported = false
    var appliedCaps = ""; private set

    fun elapsedS() = (android.os.SystemClock.elapsedRealtime() - startedMs) / 1000.0

    fun start() {
        val b = NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_CELLULAR)
        val applied = ArrayList<String>()
        for (raw in capsText.uppercase().split(Regex("[+,\\s]+"))) {
            if (raw.isEmpty()) continue
            val remove = raw.startsWith("-")
            val cap = Capabilities.byName(raw.removePrefix("-"))
            if (cap < 0) continue
            try {
                if (remove) b.removeCapability(cap) else b.addCapability(cap)
                applied += raw
            } catch (e: IllegalArgumentException) {
                Log.w(TAG, "capability $raw rejected: $e")
            }
        }
        appliedCaps = applied.joinToString("+")
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                if (callback !== this) return
                listener.onGranted(network, elapsedS())
                cm.getLinkProperties(network)?.let { checkLink(network, it) }
            }

            override fun onLinkPropertiesChanged(network: Network, lp: LinkProperties) {
                if (callback !== this) return
                checkLink(network, lp)
            }

            override fun onLost(network: Network) {
                if (callback !== this) return
                listener.onLost(network)
            }

            override fun onUnavailable() {
                if (callback !== this) return
                listener.onUnavailable("No network satisfied cellular + $appliedCaps within 30 s (APN type, modem or core)")
            }
        }
        callback = cb
        try {
            cm.requestNetwork(b.build(), cb, handler, 30_000)
        } catch (e: RuntimeException) {
            callback = null
            listener.onUnavailable("The system rejected the request: ${e.message}")
        }
    }

    private fun checkLink(network: Network, lp: LinkProperties) {
        if (linkReported) return
        val ip = lp.linkAddresses.map { it.address }.filterIsInstance<Inet4Address>().firstOrNull() ?: return
        linkReported = true
        listener.onLinkUp(network, ip, lp.interfaceName)
    }

    fun release() {
        val cb = callback ?: return
        callback = null
        try { cm.unregisterNetworkCallback(cb) } catch (_: IllegalArgumentException) {}
    }
}

/** Control channel to the generator: newline-delimited JSON over TCP 47010, bound to the service network. */
object ControlClient {
    data class Reply(val ok: Boolean, val detail: String)

    fun send(network: Network, generator: String, request: Map<String, Any?>): Reply {
        return try {
            Socket().use { s ->
                network.bindSocket(s)
                s.connect(InetSocketAddress(generator, br.inatel.smartfarm.farm.protocol.Wire.PORT_CONTROL), 4000)
                s.soTimeout = 5000
                s.getOutputStream().apply { write((Json.encode(request) + "\n").toByteArray()); flush() }
                val line = BufferedReader(InputStreamReader(s.getInputStream())).readLine()
                    ?: return Reply(false, "The farm controller closed the connection")
                val o = JSONObject(line)
                Reply(o.optBoolean("ok", false), o.optString("detail", if (o.optBoolean("ok")) "done" else "refused"))
            }
        } catch (e: Exception) {
            Reply(false, "Could not reach the farm controller at $generator:${br.inatel.smartfarm.farm.protocol.Wire.PORT_CONTROL} (${e.javaClass.simpleName})")
        }
    }
}
