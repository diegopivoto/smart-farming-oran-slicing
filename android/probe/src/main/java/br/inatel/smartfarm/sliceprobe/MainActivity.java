package br.inatel.smartfarm.sliceprobe;

import android.app.Activity;
import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Phase-1 capability probe: can an ordinary app make the modem open an extra PDU session on a service slice (per
 * DNN/APN) with ConnectivityManager.requestNetwork, keep the default session, and pass traffic on the new one?
 */
public class MainActivity extends Activity {
    private static final int REQUEST_TIMEOUT_MS = 30000;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final ExecutorService reporter = Executors.newSingleThreadExecutor();
    private final SimpleDateFormat clock = new SimpleDateFormat("HH:mm:ss.SSS", Locale.US);

    private ConnectivityManager cm;
    private String runId;
    private TextView logView;
    private TextView statusView;
    private ScrollView logScroll;
    private final EditText[] capsFields = new EditText[Service.ALL.length];
    private ActiveRequest active;
    private volatile boolean reportFailureLogged;

    /** The one outstanding service request (a new one releases the previous). */
    private final class ActiveRequest {
        final Service svc;
        final String caps;
        final long requestedAtNs = System.nanoTime();
        ConnectivityManager.NetworkCallback callback;
        Network network;
        NetworkCapabilities nc;
        LinkProperties lp;
        boolean tested;

        ActiveRequest(Service svc, String caps) {
            this.svc = svc;
            this.caps = caps;
        }

        double elapsedS() {
            return (System.nanoTime() - requestedAtNs) / 1e9;
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        runId = UUID.randomUUID().toString().replace("-", "");
        logView = findViewById(R.id.log);
        statusView = findViewById(R.id.status);
        logScroll = findViewById(R.id.log_scroll);

        int[] buttons = {R.id.btn_crop, R.id.btn_drone, R.id.btn_weather};
        int[] fields = {R.id.caps_crop, R.id.caps_drone, R.id.caps_weather};
        for (int i = 0; i < Service.ALL.length; i++) {
            final int idx = i;
            capsFields[i] = findViewById(fields[i]);
            capsFields[i].setText(Service.ALL[i].defaultCaps);
            ((Button) findViewById(buttons[i])).setOnClickListener(v -> request(Service.ALL[idx], capsFields[idx].getText().toString()));
        }
        findViewById(R.id.btn_release).setOnClickListener(v -> release("user"));
        findViewById(R.id.btn_retest).setOnClickListener(v -> retest());
        findViewById(R.id.btn_diag).setOnClickListener(v -> diagnostics());
        findViewById(R.id.btn_clear).setOnClickListener(v -> logView.setText(""));

        ((TextView) findViewById(R.id.device)).setText(String.format(Locale.US,
                "%s %s · Android %s (SDK %d) · %s · run %s", Build.MANUFACTURER, Build.MODEL, Build.VERSION.RELEASE,
                Build.VERSION.SDK_INT, Build.DISPLAY, runId.substring(0, 8)));
        log("App started, run_id " + runId);
        log("Default network: " + describeShort(cm.getActiveNetwork()));
        report("app", "app_start", true, "default network " + describeShort(cm.getActiveNetwork()), null);
        updateStatus();
    }

    @Override
    protected void onDestroy() {
        release("activity destroyed");
        worker.shutdownNow();
        reporter.shutdown();
        super.onDestroy();
    }

    // ---- request / release -------------------------------------------------------------------------------------

    private void request(Service svc, String capsText) {
        release("new request");
        NetworkRequest.Builder b = new NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_CELLULAR);
        List<String> applied = new ArrayList<>();
        for (String raw : capsText.toUpperCase(Locale.US).split("[+,\\s]+")) {
            if (raw.isEmpty()) continue;
            boolean remove = raw.startsWith("-");
            String name = remove ? raw.substring(1) : raw;
            int cap = Service.capabilityByName(name);
            if (cap < 0) {
                log("Unknown capability '" + name + "' ignored");
                continue;
            }
            try {
                if (remove) b.removeCapability(cap); else b.addCapability(cap);
                applied.add(raw);
            } catch (IllegalArgumentException e) {
                log("Capability " + name + " rejected by this Android version: " + e.getMessage());
            }
        }
        String caps = String.join("+", applied);
        NetworkRequest req = b.build();
        final ActiveRequest ar = new ActiveRequest(svc, caps);
        ar.callback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                if (active != ar) return;
                ar.network = network;
                log(String.format(Locale.US, "AVAILABLE %s netId %s after %.2f s", svc.id, network, ar.elapsedS()));
                report(svc.id, "available", true, String.format(Locale.US, "network %s granted after %.2f s", network, ar.elapsedS()),
                        Probes.extra("caps_requested", ar.caps, "elapsed_s", ar.elapsedS()));
                updateStatus();
            }

            @Override
            public void onCapabilitiesChanged(Network network, NetworkCapabilities nc) {
                if (active != ar) return;
                ar.network = network;
                ar.nc = nc;
                updateStatus();
                maybeStartTests(ar);
            }

            @Override
            public void onLinkPropertiesChanged(Network network, LinkProperties lp) {
                if (active != ar) return;
                ar.network = network;
                ar.lp = lp;
                updateStatus();
                maybeStartTests(ar);
            }

            @Override
            public void onBlockedStatusChanged(Network network, boolean blocked) {
                if (active != ar) return;
                log("Network " + network + (blocked ? " BLOCKED for this app (data saver/background?)" : " not blocked"));
            }

            @Override
            public void onLost(Network network) {
                if (active != ar) return;
                log(String.format(Locale.US, "LOST %s netId %s after %.2f s", svc.id, network, ar.elapsedS()));
                report(svc.id, "lost", false, "network " + network + " lost", Probes.extra("elapsed_s", ar.elapsedS()));
                ar.network = null;
                updateStatus();
            }

            @Override
            public void onUnavailable() {
                if (active != ar) return;
                log(String.format(Locale.US, "UNAVAILABLE %s: nothing satisfied %s within %d s (no matching APN, or the modem/core refused)",
                        svc.id, caps, REQUEST_TIMEOUT_MS / 1000));
                report(svc.id, "unavailable", false, "request " + caps + " not satisfied within " + REQUEST_TIMEOUT_MS / 1000 + " s",
                        Probes.extra("caps_requested", caps));
                active = null;
                updateStatus();
            }
        };
        active = ar;
        log(String.format("REQUEST %s (DNN %s, SD %s): cellular + %s", svc.id, svc.dnn, svc.sd, caps));
        log("  " + req);
        try {
            cm.requestNetwork(req, ar.callback, ui, REQUEST_TIMEOUT_MS);
            report(svc.id, "request", true, "requestNetwork accepted for " + caps, Probes.extra("caps_requested", caps,
                    "request", req.toString()));
        } catch (SecurityException | IllegalArgumentException e) {
            active = null;
            log("REJECTED by ConnectivityService: " + e);
            report(svc.id, "request", false, "requestNetwork threw " + e, Probes.extra("caps_requested", caps));
        }
        updateStatus();
    }

    private void release(String why) {
        ActiveRequest ar = active;
        if (ar == null) return;
        active = null;
        try {
            cm.unregisterNetworkCallback(ar.callback);
        } catch (IllegalArgumentException ignored) {
            // already released (e.g. after onUnavailable)
        }
        log(String.format(Locale.US, "RELEASE %s (%s): the SMF should log the release of the %s session", ar.svc.id, why, ar.svc.dnn));
        report(ar.svc.id, "release", true, "released (" + why + ")", Probes.extra("held_s", ar.elapsedS()));
        updateStatus();
    }

    private void retest() {
        ActiveRequest ar = active;
        if (ar == null || ar.network == null || ar.lp == null) {
            log("Nothing to re-test: no granted network");
            return;
        }
        ar.tested = false;
        maybeStartTests(ar);
    }

    // ---- tests ---------------------------------------------------------------------------------------------------

    private void maybeStartTests(ActiveRequest ar) {
        if (ar.tested || ar.network == null || ar.lp == null || ipv4(ar.lp) == null) return;
        ar.tested = true;
        final Network network = ar.network;
        final LinkProperties lp = ar.lp;
        final NetworkCapabilities nc = ar.nc;
        worker.execute(() -> runTests(ar, network, lp, nc));
    }

    private void runTests(ActiveRequest ar, Network network, LinkProperties lp, NetworkCapabilities nc) {
        Service svc = ar.svc;
        Probes.Sink sink = (step, ok, detail, extra) -> {
            log((ok ? "[OK]   " : "[FAIL] ") + step + ": " + detail);
            report(svc.id, step, ok, detail, extra);
        };
        Inet4Address ip = ipv4(lp);
        boolean owns = svc.owns(ip);
        sink.result("address", owns, String.format("iface %s IPv4 %s -> %s (expected %s 10.45.%d-%d.x)",
                lp.getInterfaceName(), addresses(lp), Service.classify(ip), svc.dnn, svc.octetLo, svc.octetHi),
                Probes.extra("iface", String.valueOf(lp.getInterfaceName()), "ipv4", ip.getHostAddress(),
                        "range", Service.classify(ip), "caps_requested", ar.caps,
                        "capabilities", nc == null ? "" : nc.toString(), "link_properties", lp.toString()));

        Network def = cm.getActiveNetwork();
        LinkProperties dlp = def == null ? null : cm.getLinkProperties(def);
        Inet4Address dip = dlp == null ? null : ipv4(dlp);
        boolean kept = def != null && !def.equals(network) && dip != null && !dip.equals(ip);
        String hint = def != null && def.equals(network)
                ? " -- the DEFAULT session satisfied the request: its APN type is empty/'*' or includes " + ar.caps : "";
        sink.result("default_kept", kept, String.format("default network %s iface %s IPv4 %s -> %s%s",
                def, dlp == null ? "-" : dlp.getInterfaceName(), dip == null ? "-" : dip.getHostAddress(), Service.classify(dip), hint),
                Probes.extra("default_ipv4", dip == null ? "" : dip.getHostAddress(), "service_ipv4", ip.getHostAddress()));

        Probes.httpGet(network, sink);
        Probes.listen(network, ip, svc, sink);
        log("Tests done for " + svc.id + " (Re-test repeats them; Release ends the session)");
    }

    // ---- diagnostics -----------------------------------------------------------------------------------------------

    /**
     * Lists every network and checks, per capability, whether a request WITHOUT NET_CAPABILITY_NOT_RESTRICTED (the only
     * way to reach a restricted APN such as ims/cbs/fota alone) is refused for this app. Nothing stays filed.
     */
    private void diagnostics() {
        log("---- Diagnostics: SDK " + Build.VERSION.SDK_INT + ", " + Build.DISPLAY);
        for (Network n : cm.getAllNetworks()) log("  " + describeShort(n));
        String[] names = {"MMS", "SUPL", "DUN", "FOTA", "IMS", "CBS", "IA", "RCS", "XCAP", "EIMS", "MCX", "ENTERPRISE", "MMTEL"};
        JSONObject matrix = new JSONObject();
        for (String name : names) {
            String verdict;
            try {
                NetworkRequest req = new NetworkRequest.Builder()
                        .addTransportType(NetworkCapabilities.TRANSPORT_CELLULAR)
                        .addCapability(Service.capabilityByName(name))
                        .removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_RESTRICTED)
                        .build();
                ConnectivityManager.NetworkCallback cb = new ConnectivityManager.NetworkCallback();
                try {
                    cm.requestNetwork(req, cb, ui, 1000);
                    cm.unregisterNetworkCallback(cb);
                    verdict = "ACCEPTED (unexpected: OEM allows restricted requests)";
                } catch (SecurityException e) {
                    verdict = "DENIED (needs CONNECTIVITY_USE_RESTRICTED_NETWORKS)";
                }
            } catch (IllegalArgumentException e) {
                verdict = "INVALID on this SDK (" + e.getMessage() + ")";
            }
            log(String.format("  restricted %-10s %s", name, verdict));
            try {
                matrix.put(name, verdict);
            } catch (JSONException ignored) {
                // constant keys
            }
        }
        log("  Unrestricted requests (NOT_RESTRICTED kept) are always accepted by the API; they are satisfied only by"
                + " an APN whose types include every requested capability plus mms/supl/default.");
        report("app", "diagnostics", true, "restricted-request matrix", Probes.extra("restricted_matrix", matrix.toString(),
                "networks", String.valueOf(cm.getAllNetworks().length)));
    }

    // ---- helpers -----------------------------------------------------------------------------------------------------

    private static Inet4Address ipv4(LinkProperties lp) {
        if (lp == null) return null;
        for (LinkAddress la : lp.getLinkAddresses()) {
            InetAddress a = la.getAddress();
            if (a instanceof Inet4Address) return (Inet4Address) a;
        }
        return null;
    }

    private static String addresses(LinkProperties lp) {
        List<String> out = new ArrayList<>();
        for (LinkAddress la : lp.getLinkAddresses()) out.add(la.toString());
        return out.toString();
    }

    private String describeShort(Network n) {
        if (n == null) return "none";
        LinkProperties lp = cm.getLinkProperties(n);
        NetworkCapabilities nc = cm.getNetworkCapabilities(n);
        Inet4Address ip = ipv4(lp);
        return String.format("netId %s iface %s IPv4 %s [%s] caps %s", n, lp == null ? "-" : lp.getInterfaceName(),
                ip == null ? "-" : ip.getHostAddress(), Service.classify(ip), nc == null ? "-" : capsOnly(nc));
    }

    /** "[ Transports: CELLULAR Capabilities: MMS&FOTA&NOT_RESTRICTED&... ...]" -> "CELLULAR MMS&FOTA&NOT_RESTRICTED&...". */
    private static String capsOnly(NetworkCapabilities nc) {
        String s = nc.toString();
        StringBuilder out = new StringBuilder();
        int t = s.indexOf("Transports: ");
        if (t >= 0) out.append(s.substring(t + 12).split(" ")[0]).append(' ');
        int c = s.indexOf("Capabilities: ");
        if (c >= 0) out.append(s.substring(c + 14).split(" ")[0]);
        return out.length() > 0 ? out.toString().trim() : s;
    }

    private void updateStatus() {
        ui.post(() -> {
            ActiveRequest ar = active;
            StringBuilder sb = new StringBuilder();
            sb.append("Default: ").append(describeShort(cm.getActiveNetwork())).append('\n');
            if (ar == null) {
                sb.append("Service network: none requested");
            } else {
                sb.append("Request: ").append(ar.svc.id).append(" (DNN ").append(ar.svc.dnn).append(", SD ").append(ar.svc.sd)
                        .append(") caps ").append(ar.caps).append('\n');
                if (ar.network == null) {
                    sb.append("Waiting for Android/modem (").append(String.format(Locale.US, "%.1f s", ar.elapsedS())).append(")");
                } else {
                    Inet4Address ip = ipv4(ar.lp);
                    sb.append("Network: netId ").append(ar.network);
                    if (ar.lp != null) {
                        sb.append(" iface ").append(ar.lp.getInterfaceName()).append('\n')
                                .append("IPv4: ").append(addresses(ar.lp)).append(" -> ").append(Service.classify(ip)).append('\n')
                                .append("MTU ").append(ar.lp.getMtu()).append(" routes ").append(ar.lp.getRoutes())
                                .append(" dns ").append(ar.lp.getDnsServers()).append('\n');
                    }
                    if (ar.nc != null) sb.append("Caps: ").append(capsOnly(ar.nc));
                }
            }
            statusView.setText(sb.toString());
        });
    }

    private void log(String line) {
        String stamped = clock.format(new Date()) + " " + line + "\n";
        ui.post(() -> {
            logView.append(stamped);
            logScroll.post(() -> logScroll.fullScroll(ScrollView.FOCUS_DOWN));
        });
    }

    /** One JSON line per result to the PC1 collector, over the DEFAULT network (the control channel). */
    private void report(String service, String step, boolean ok, String detail, JSONObject extra) {
        final JSONObject rec = new JSONObject();
        try {
            rec.put("kind", "capability_probe");
            rec.put("run_id", runId);
            rec.put("service", service);
            rec.put("step", step);
            rec.put("ok", ok);
            rec.put("detail", detail);
            rec.put("at_ns", System.currentTimeMillis() * 1_000_000L);
            rec.put("device", Build.MANUFACTURER + " " + Build.MODEL);
            rec.put("android_sdk", Build.VERSION.SDK_INT);
            rec.put("build", Build.DISPLAY);
            if (extra != null) {
                for (Iterator<String> it = extra.keys(); it.hasNext(); ) {
                    String k = it.next();
                    rec.put(k, extra.get(k));
                }
            }
        } catch (JSONException e) {
            return;
        }
        reporter.execute(() -> {
            HttpURLConnection c = null;
            try {
                c = (HttpURLConnection) new URL(Probes.COLLECTOR + "/report").openConnection();
                c.setConnectTimeout(3000);
                c.setReadTimeout(3000);
                c.setDoOutput(true);
                c.setRequestMethod("POST");
                c.setRequestProperty("Content-Type", "application/json");
                byte[] body = rec.toString().getBytes(StandardCharsets.UTF_8);
                c.setFixedLengthStreamingMode(body.length);
                try (OutputStream out = c.getOutputStream()) {
                    out.write(body);
                }
                int code = c.getResponseCode();
                if (code != 204 && code != 200 && !reportFailureLogged) {
                    reportFailureLogged = true;
                    log("Collector rejected the report (HTTP " + code + "); results stay in this log only");
                }
            } catch (Exception e) {
                if (!reportFailureLogged) {
                    reportFailureLogged = true;
                    log("Collector unreachable on the default network (" + e + "); results stay in this log only");
                }
            } finally {
                if (c != null) c.disconnect();
            }
        });
    }
}
