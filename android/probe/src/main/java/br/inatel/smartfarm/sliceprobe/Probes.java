package br.inatel.smartfarm.sliceprobe;

import android.net.Network;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashSet;
import java.util.Set;

/** Reachability tests that run entirely on the granted (service) Network, never on the default one. */
final class Probes {
    static final String COLLECTOR = "http://10.45.0.1:8088";
    /** android/tools/apk_server.py on PC1: serves the APK and sends probe packets back on request. */
    static final String HELPER = "http://10.45.0.1:8089";
    static final int PROBE_COUNT = 20;
    static final int LISTEN_MS = 5000;

    interface Sink {
        void result(String step, boolean ok, String detail, JSONObject extra);
    }

    private Probes() {}

    /** HTTP GET of the collector script through network.openConnection (socket bound to the service network). */
    static void httpGet(Network network, Sink sink) {
        long t0 = System.nanoTime();
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) network.openConnection(new URL(COLLECTOR + "/sf_traffic.py"));
            c.setConnectTimeout(5000);
            c.setReadTimeout(5000);
            c.setUseCaches(false);
            int code = c.getResponseCode();
            byte[] body = readAll(code < 400 ? c.getInputStream() : c.getErrorStream(), 4 << 20);
            double ms = (System.nanoTime() - t0) / 1e6;
            boolean ok = code == 200 && body.length > 0;
            sink.result("http_get", ok, String.format("GET %s/sf_traffic.py -> HTTP %d, %d B in %.0f ms",
                    COLLECTOR, code, body.length, ms), extra("http_status", code, "bytes", body.length, "elapsed_ms", ms));
        } catch (IOException e) {
            double ms = (System.nanoTime() - t0) / 1e6;
            sink.result("http_get", false, "GET failed after " + Math.round(ms) + " ms: " + e,
                    extra("error", e.toString(), "elapsed_ms", ms));
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /**
     * Opens the service's receiver port on the network's own IPv4 address, asks the PC1 helper (through the same
     * network, so it sees our service address as the source) to send probe packets back, and counts what arrives.
     */
    static void listen(Network network, Inet4Address ip, Service svc, Sink sink) {
        String step = svc.tcp ? "listen_tcp" : "listen_udp";
        if (svc.tcp) {
            listenTcp(network, ip, svc, step, sink);
        } else {
            listenUdp(network, ip, svc, step, sink);
        }
    }

    private static void listenUdp(Network network, Inet4Address ip, Service svc, String step, Sink sink) {
        try (DatagramSocket ds = new DatagramSocket(null)) {
            ds.setReuseAddress(true);
            network.bindSocket(ds);
            ds.bind(new InetSocketAddress(ip, svc.port));
            ds.setSoTimeout(250);
            String trigger = trigger(network, "udp", svc.port);
            long deadline = System.currentTimeMillis() + LISTEN_MS;
            byte[] buf = new byte[65535];
            int packets = 0, probes = 0;
            long bytes = 0;
            Set<String> sources = new LinkedHashSet<>();
            while (System.currentTimeMillis() < deadline) {
                DatagramPacket p = new DatagramPacket(buf, buf.length);
                try {
                    ds.receive(p);
                } catch (SocketTimeoutException e) {
                    continue;
                }
                packets++;
                bytes += p.getLength();
                sources.add(p.getAddress().getHostAddress());
                if (p.getLength() >= 4 && buf[0] == 'S' && buf[1] == 'F' && buf[2] == 'C' && buf[3] == 'P') probes++;
            }
            sink.result(step, packets > 0, String.format("UDP %s:%d received %d packets (%d probe) %d B from %s in %d s; trigger: %s",
                    ip.getHostAddress(), svc.port, packets, probes, bytes, sources, LISTEN_MS / 1000, trigger),
                    extra("port", svc.port, "packets", packets, "probe_packets", probes, "bytes", bytes,
                            "sources", sources.toString(), "trigger", trigger));
        } catch (IOException e) {
            sink.result(step, false, "UDP " + ip.getHostAddress() + ":" + svc.port + " could not listen: " + e
                    + " (another receiver, e.g. Termux sf_traffic.py, may hold the port)", extra("error", e.toString()));
        }
    }

    private static void listenTcp(Network network, Inet4Address ip, Service svc, String step, Sink sink) {
        // A ServerSocket cannot be passed to Network.bindSocket; binding it to the service network's own address is
        // enough: inbound packets for that address arrive on its interface and accepted sockets inherit the mark.
        try (ServerSocket ss = new ServerSocket()) {
            ss.setReuseAddress(true);
            ss.bind(new InetSocketAddress(ip, svc.port));
            ss.setSoTimeout(250);
            String trigger = trigger(network, "tcp", svc.port);
            long deadline = System.currentTimeMillis() + LISTEN_MS;
            int connections = 0;
            long bytes = 0;
            int lines = 0;
            Set<String> sources = new LinkedHashSet<>();
            byte[] buf = new byte[16384];
            while (System.currentTimeMillis() < deadline) {
                Socket s;
                try {
                    s = ss.accept();
                } catch (SocketTimeoutException e) {
                    continue;
                }
                connections++;
                sources.add(s.getInetAddress().getHostAddress());
                try (Socket conn = s) {
                    conn.setSoTimeout(250);
                    InputStream in = conn.getInputStream();
                    while (System.currentTimeMillis() < deadline) {
                        int n;
                        try {
                            n = in.read(buf);
                        } catch (SocketTimeoutException e) {
                            continue;
                        }
                        if (n < 0) break;
                        bytes += n;
                        for (int i = 0; i < n; i++) if (buf[i] == '\n') lines++;
                    }
                }
            }
            sink.result(step, bytes > 0, String.format("TCP %s:%d accepted %d connection(s), %d B / %d lines from %s in %d s; trigger: %s",
                    ip.getHostAddress(), svc.port, connections, bytes, lines, sources, LISTEN_MS / 1000, trigger),
                    extra("port", svc.port, "connections", connections, "bytes", bytes, "lines", lines,
                            "sources", sources.toString(), "trigger", trigger));
        } catch (IOException e) {
            sink.result(step, false, "TCP " + ip.getHostAddress() + ":" + svc.port + " could not listen: " + e
                    + " (another receiver, e.g. Termux sf_traffic.py, may hold the port)", extra("error", e.toString()));
        }
    }

    /** Asks the PC1 helper to send probe packets back to our source address on this network. */
    private static String trigger(Network network, String proto, int port) {
        HttpURLConnection c = null;
        try {
            URL url = new URL(HELPER + "/probe?proto=" + proto + "&port=" + port + "&count=" + PROBE_COUNT);
            c = (HttpURLConnection) network.openConnection(url);
            c.setConnectTimeout(3000);
            c.setReadTimeout(8000);
            c.setUseCaches(false);
            int code = c.getResponseCode();
            byte[] body = readAll(code < 400 ? c.getInputStream() : c.getErrorStream(), 4096);
            return "HTTP " + code + " " + new String(body, StandardCharsets.UTF_8).trim();
        } catch (IOException e) {
            return "helper unreachable (" + e.getClass().getSimpleName() + ": " + e.getMessage()
                    + "); counting any traffic, e.g. from sf_traffic.py";
        } finally {
            if (c != null) c.disconnect();
        }
    }

    static byte[] readAll(InputStream in, int limit) throws IOException {
        if (in == null) return new byte[0];
        try (InputStream s = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = s.read(buf)) >= 0 && out.size() < limit) out.write(buf, 0, n);
            return out.toByteArray();
        }
    }

    static JSONObject extra(Object... kv) {
        JSONObject o = new JSONObject();
        try {
            for (int i = 0; i + 1 < kv.length; i += 2) o.put((String) kv[i], kv[i + 1]);
        } catch (JSONException ignored) {
            // keys are constant strings; values are primitives or strings
        }
        return o;
    }
}
