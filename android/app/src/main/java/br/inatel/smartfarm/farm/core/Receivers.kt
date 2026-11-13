package br.inatel.smartfarm.farm.core

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Network
import android.os.SystemClock
import android.util.Log
import br.inatel.smartfarm.farm.protocol.ClockSync
import br.inatel.smartfarm.farm.protocol.CropMeta
import br.inatel.smartfarm.farm.protocol.FrameAssembler
import br.inatel.smartfarm.farm.protocol.RunIdCache
import br.inatel.smartfarm.farm.protocol.SfimStreamParser
import br.inatel.smartfarm.farm.protocol.UdpStats
import br.inatel.smartfarm.farm.protocol.WallClock
import br.inatel.smartfarm.farm.protocol.Wire
import java.io.IOException
import java.io.InputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketException
import java.net.SocketTimeoutException
import java.security.MessageDigest
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.atomic.AtomicLong

interface Stoppable { fun halt() }

/**
 * sf_traffic.py udp_listener(): UDP socket bound to the service network and the phone's address on it.
 * Every valid datagram feeds [stats] (metrics over all datagrams, drone fillers included) and then [onPacket].
 */
class UdpReceiver(
    private val network: Network,
    private val ip: InetAddress,
    private val port: Int,
    private val serviceCode: Int,
    private val stats: UdpStats,
    private val clock: ClockSync,
    private val onError: (String) -> Unit,
    private val onPacket: (buf: ByteArray, len: Int, runId: String, arrivalNs: Long) -> Unit,
) : Thread("udp-$port"), Stoppable {
    @Volatile private var stopped = false
    private var socket: DatagramSocket? = null

    override fun run() {
        // No apply {} on sockets here: inside it, `port` resolved to DatagramSocket.getPort() (-1, unconnected) instead
        // of this receiver's port and the app crashed (1.1.1). Explicit receivers only.
        val servicePort = port
        val ds = DatagramSocket(null)
        try {
            ds.reuseAddress = true
            ds.receiveBufferSize = 8 * 1024 * 1024
            network.bindSocket(ds)
            ds.bind(InetSocketAddress(ip, servicePort))
            ds.soTimeout = 500
        } catch (e: IOException) {
            ds.close()
            onError("Could not open UDP port $servicePort on ${ip.hostAddress}: ${e.message} (is Termux sf_traffic.py still running?)")
            return
        }
        socket = ds
        val buf = ByteArray(65535)
        val p = DatagramPacket(buf, buf.size)
        val runIds = RunIdCache()
        var lastPeer: InetAddress? = null
        var lastPeerText = ""
        ds.use {
            while (!stopped) {
                try {
                    p.setLength(buf.size)
                    ds.receive(p)
                } catch (_: SocketTimeoutException) {
                    continue
                } catch (e: IOException) {
                    if (!stopped) onError("UDP receive failed: ${e.message}")
                    break
                }
                val arrival = WallClock.nowNs()
                val h = Wire.parseDatagram(buf, 0, p.length, serviceCode, runIds) ?: continue
                val peer = p.address
                if (peer != lastPeer) { lastPeer = peer; lastPeerText = peer.hostAddress ?: "" }
                clock.observePeer(lastPeerText)
                stats.add(h.runIdHex, h.seq, clock.toReceiverClock(h.generatedAtNs), p.length, arrival)
                try {
                    onPacket(buf, p.length, h.runIdHex, arrival)
                } catch (e: RuntimeException) {
                    Log.w(TAG, "packet handler failed", e)
                }
            }
        }
    }

    override fun halt() {
        stopped = true
        socket?.close()
    }
}

/**
 * Decodes complete MJPEG frames off the receive thread. Only the newest frame waits (a slower decoder skips frames
 * rather than adding latency); bitmaps are recycled through inBitmap from a small pool.
 */
class JpegDecoder(private val onFrame: (Bitmap, decodeMs: Double) -> Unit) : Thread("jpeg-decoder"), Stoppable {
    private val slot = ArrayBlockingQueue<ByteArray>(1)
    @Volatile private var stopped = false
    private val pool = ArrayDeque<Bitmap>()
    private var avgMs = 0.0
    private var sample = 1
    val skipped = AtomicLong()
    val decoded = AtomicLong()
    val failed = AtomicLong()

    fun submit(jpeg: ByteArray) {
        if (!slot.offer(jpeg)) {
            slot.poll()?.let { skipped.incrementAndGet() }
            slot.offer(jpeg)
        }
    }

    override fun run() {
        var current: Bitmap? = null
        val shown = ArrayDeque<Bitmap>() // the last frames handed to the UI are not reused yet
        while (!stopped) {
            val jpeg = try { slot.take() } catch (_: InterruptedException) { break }
            if (jpeg.isEmpty()) break
            val t0 = SystemClock.elapsedRealtimeNanos()
            val opts = BitmapFactory.Options().apply {
                inMutable = true
                inSampleSize = sample
                inPreferredConfig = Bitmap.Config.ARGB_8888
                inBitmap = pool.removeFirstOrNull()
            }
            val bmp = try {
                BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, opts)
            } catch (_: IllegalArgumentException) {
                opts.inBitmap = null
                BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, opts)
            }
            if (bmp == null) { failed.incrementAndGet(); opts.inBitmap?.let { pool.addLast(it) }; continue }
            val ms = (SystemClock.elapsedRealtimeNanos() - t0) / 1e6
            avgMs = if (decoded.get() == 0L) ms else avgMs * 0.9 + ms * 0.1
            // 1080p at 30 fps needs < 33 ms per frame: fall back to half resolution (960x540) if the CPU cannot keep up
            if (sample == 1 && decoded.get() > 30 && avgMs > 28) { sample = 2; pool.clear(); shown.clear() }
            decoded.incrementAndGet()
            current = bmp
            shown.addLast(bmp)
            while (shown.size > 3) {
                val old = shown.removeFirst()
                if (old.width == bmp.width && old.height == bmp.height) pool.addLast(old)
            }
            onFrame(bmp, avgMs)
        }
    }

    override fun halt() {
        stopped = true
        slot.clear()
        slot.offer(ByteArray(0))
        interrupt()
    }
}

data class CropProgress(val objectId: Long, val received: Long, val size: Long, val startedMs: Long)

data class CropResult(
    val objectId: Long,
    val runId: String,
    val valid: Boolean,
    val meta: CropMeta?,
    val rgbJpeg: ByteArray?,
    val ndviJpeg: ByteArray?,
    val record: Map<String, Any?>,
)

/**
 * sf_traffic.py crop_listener()/crop_connection(): the phone listens on TCP 47002 (bound to its address on the crop
 * network; a ServerSocket cannot be passed to Network.bindSocket, accepted sockets inherit the network), checks
 * the SHA-256 of each object while parsing the SFIM container in the same pass.
 */
class CropReceiver(
    private val ip: InetAddress,
    private val clock: ClockSync,
    private val onError: (String) -> Unit,
    private val onConnected: (String) -> Unit,
    private val onProgress: (CropProgress?) -> Unit,
    private val onObject: (CropResult) -> Unit,
) : Thread("crop-listener"), Stoppable {
    @Volatile private var stopped = false
    private var server: ServerSocket? = null
    private val connections = java.util.Collections.synchronizedSet(HashSet<Socket>())
    /** Payload bytes received (all connections), for the live goodput. */
    val bytesTotal = AtomicLong()

    override fun run() {
        // explicit receivers (no apply {}): ServerSocket has its own inetAddress/localPort that would shadow ours
        val ss = ServerSocket()
        try {
            ss.reuseAddress = true
            ss.bind(InetSocketAddress(ip, Wire.PORT_CROP), 4)
            ss.soTimeout = 500
        } catch (e: IOException) {
            ss.close()
            onError("Could not listen on TCP ${Wire.PORT_CROP} at ${ip.hostAddress}: ${e.message} (is Termux sf_traffic.py still running?)")
            return
        }
        server = ss
        ss.use {
            while (!stopped) {
                val conn = try { ss.accept() } catch (_: SocketTimeoutException) { continue } catch (e: IOException) { break }
                val peer = conn.inetAddress.hostAddress ?: ""
                clock.observePeer(peer)
                onConnected(peer)
                connections += conn
                Thread({ handle(conn) }, "crop-conn").apply { isDaemon = true }.start()
            }
        }
    }

    private fun readExact(input: InputStream, n: Int): ByteArray? {
        val b = ByteArray(n)
        var got = 0
        while (got < n) {
            val r = input.read(b, got, n - got)
            if (r < 0) return null
            got += r
        }
        return b
    }

    private fun handle(conn: Socket) {
        try {
            conn.use { s ->
                s.receiveBufferSize = 4 * 1024 * 1024
                s.soTimeout = 0
                val input = s.getInputStream()
                val buf = ByteArray(1 shl 20)
                while (!stopped) {
                    val headerBytes = readExact(input, Wire.OBJECT_SIZE) ?: return
                    val header = Wire.parseObjectHeader(headerBytes) ?: return
                    val first = WallClock.nowNs()
                    val startedMs = SystemClock.elapsedRealtime()
                    val sha = MessageDigest.getInstance("SHA-256")
                    val parser = SfimStreamParser()
                    var remaining = header.size
                    var lastProgress = 0L
                    onProgress(CropProgress(header.objectId, 0, header.size, startedMs))
                    while (remaining > 0) {
                        val r = input.read(buf, 0, minOf(buf.size.toLong(), remaining).toInt())
                        if (r < 0) { onProgress(null); return } // incomplete objects are dropped, as in sf_traffic.py
                        sha.update(buf, 0, r)
                        parser.feed(buf, 0, r)
                        remaining -= r
                        bytesTotal.addAndGet(r.toLong())
                        val now = SystemClock.elapsedRealtime()
                        if (now - lastProgress > 200) {
                            lastProgress = now
                            onProgress(CropProgress(header.objectId, header.size - remaining, header.size, startedMs))
                        }
                    }
                    val last = WallClock.nowNs()
                    val valid = sha.digest().contentEquals(header.sha256)
                    val generatedRx = clock.toReceiverClock(header.generatedAtNs)
                    val receiveS = (last - first) / 1e9
                    val meta = CropMeta.parse(parser.parts[SfimStreamParser.META])
                    val record = linkedMapOf<String, Any?>(
                        "kind" to "receiver_object", "service" to "crop", "run_id" to header.runIdHex,
                        "object_id" to header.objectId, "at_ns" to last, "size_bytes" to header.size,
                        "sha256_valid" to valid,
                        "receive_time_s" to Math.rint(receiveS * 1e4) / 1e4,
                        "object_transfer_time_s" to (if (generatedRx != null && generatedRx != 0L) Math.rint((last - generatedRx) / 1e9 * 1e4) / 1e4 else null),
                        "goodput_bps" to Math.rint(header.size * 8 / maxOf(receiveS, 1e-9)).toLong(),
                        "receiver" to RECEIVER_NAME,
                        "sfim_valid" to (parser.complete && parser.error == null),
                        // contract v1.1: generator's cumulative TCP retransmitted-bytes ratio when the object started
                        "tcp_retrans_ratio_so_far" to (if (valid) meta?.tcpRetransRatioSoFar else null),
                    )
                    onProgress(null)
                    onObject(
                        CropResult(
                            header.objectId, header.runIdHex, valid, meta,
                            parser.parts[SfimStreamParser.RGB], parser.parts[SfimStreamParser.NDVI], record,
                        )
                    )
                }
            }
        } catch (_: SocketException) {
            onProgress(null) // the generator resets at its deadline
        } catch (e: IOException) {
            onProgress(null)
        } finally {
            connections -= conn
        }
    }

    override fun halt() {
        stopped = true
        server?.close()
        synchronized(connections) { connections.forEach { runCatching { it.close() } } }
    }
}

const val RECEIVER_NAME = "sf-farm-app"
