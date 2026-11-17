package br.inatel.smartfarm.farm.protocol

import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress

/**
 * sf_traffic.py ClockSync: offset (receiver clock - generator clock) from the generator's time responder
 * (UDP 47000), minimum-RTT sample of 8, refreshed every 10 s per peer.
 *
 * [socketFactory] returns a socket bound to the service network (Network.bindSocket) on the phone.
 */
class ClockSync(
    private val socketFactory: () -> DatagramSocket,
    private val clock: () -> Long = WallClock::nowNs,
    private val port: Int = Wire.PORT_TIME,
) {
    @Volatile var offsetNs: Long? = null
        private set
    @Volatile var rttNs: Long? = null
        private set
    @Volatile var peer: String? = null
        private set
    private var lastMs = 0L
    private val lock = Any()
    @Volatile private var syncing = false

    fun observePeer(address: String) {
        synchronized(lock) {
            if (address != peer) { peer = address; lastMs = 0L }
            val now = System.currentTimeMillis()
            if (now - lastMs > 10_000 && !syncing) {
                lastMs = now
                syncing = true
                Thread({ try { sync(address) } finally { syncing = false } }, "clock-sync").apply { isDaemon = true }.start()
            }
        }
    }

    fun sync(address: String) {
        var best: Pair<Long, Long>? = null
        try {
            socketFactory().use { sock ->
                sock.soTimeout = 500
                val target = InetSocketAddress(address, port)
                val buf = ByteArray(64)
                repeat(8) {
                    val t0 = clock()
                    try {
                        val q = Wire.packTimeQuery(t0)
                        sock.send(DatagramPacket(q, q.size, target))
                        val p = DatagramPacket(buf, buf.size)
                        sock.receive(p)
                        val t1 = clock()
                        val reply = Wire.parseTimeReply(buf, p.length)
                        if (reply != null && reply.first == t0) {
                            val rtt = t1 - t0
                            if (best == null || rtt < best!!.first) best = rtt to ((t0 + t1) / 2 - reply.second)
                        }
                    } catch (_: IOException) {
                    }
                    Thread.sleep(50)
                }
            }
        } catch (_: IOException) {
        }
        best?.let { (rtt, offset) -> rttNs = rtt; offsetNs = offset }
    }

    fun toReceiverClock(generatedAtNs: Long): Long? = offsetNs?.let { generatedAtNs + it }
}
