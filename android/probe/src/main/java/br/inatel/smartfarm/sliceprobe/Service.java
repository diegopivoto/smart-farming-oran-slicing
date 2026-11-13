package br.inatel.smartfarm.sliceprobe;

import android.net.NetworkCapabilities;

import java.net.Inet4Address;

/** One Smart Farming service: DNN/APN, slice, SMF address range and the receiver port used by sf_traffic.py. */
final class Service {
    final String id;
    final String dnn;
    final String sd;
    final int port;
    final boolean tcp;
    /** Default capability set; each must map to an APN type configured only on this service's APN. */
    final String defaultCaps;
    /** Third octet range of 10.45.x.y that the SMF hands out for this DNN. */
    final int octetLo;
    final int octetHi;

    private Service(String id, String dnn, String sd, int port, boolean tcp, String defaultCaps, int lo, int hi) {
        this.id = id;
        this.dnn = dnn;
        this.sd = sd;
        this.port = port;
        this.tcp = tcp;
        this.defaultCaps = defaultCaps;
        this.octetLo = lo;
        this.octetHi = hi;
    }

    // APN types on the phone: crop = "mms,fota", drone = "mms,cbs", weather = "supl" (see android/README.md).
    static final Service CROP = new Service("crop", "crop", "000002", 47002, true, "MMS+FOTA", 128, 159);
    static final Service DRONE = new Service("drone", "drone", "000003", 47003, false, "MMS+CBS", 160, 191);
    static final Service WEATHER = new Service("weather", "weather", "000001", 47001, false, "SUPL", 192, 223);
    static final Service[] ALL = {CROP, DRONE, WEATHER};

    boolean owns(Inet4Address ip) {
        byte[] b = ip.getAddress();
        int third = b[2] & 0xff;
        return (b[0] & 0xff) == 10 && (b[1] & 0xff) == 45 && third >= octetLo && third <= octetHi;
    }

    /** Which SMF range (and so which DNN/service) an IPv4 address belongs to. */
    static String classify(Inet4Address ip) {
        if (ip == null) return "no IPv4";
        byte[] b = ip.getAddress();
        if ((b[0] & 0xff) != 10 || (b[1] & 0xff) != 45) return "outside the 5G UE pool 10.45.0.0/16";
        int third = b[2] & 0xff;
        if (third <= 127) return "internet (default session, SD 000001, 10.45.0-127.x)";
        for (Service s : ALL) {
            if (third >= s.octetLo && third <= s.octetHi) {
                return s.dnn + " (SD " + s.sd + ", 10.45." + s.octetLo + "-" + s.octetHi + ".x)";
            }
        }
        return "unassigned range 10.45." + third + ".x";
    }

    /** Capability names accepted in the editable field; "-NOT_RESTRICTED" removes that default capability. */
    static int capabilityByName(String name) {
        switch (name) {
            case "MMS": return NetworkCapabilities.NET_CAPABILITY_MMS;
            case "SUPL": return NetworkCapabilities.NET_CAPABILITY_SUPL;
            case "DUN": return NetworkCapabilities.NET_CAPABILITY_DUN;
            case "FOTA": return NetworkCapabilities.NET_CAPABILITY_FOTA;
            case "IMS": return NetworkCapabilities.NET_CAPABILITY_IMS;
            case "CBS": return NetworkCapabilities.NET_CAPABILITY_CBS;
            case "IA": return NetworkCapabilities.NET_CAPABILITY_IA;
            case "RCS": return NetworkCapabilities.NET_CAPABILITY_RCS;
            case "XCAP": return NetworkCapabilities.NET_CAPABILITY_XCAP;
            case "EIMS": return NetworkCapabilities.NET_CAPABILITY_EIMS;
            case "INTERNET": return NetworkCapabilities.NET_CAPABILITY_INTERNET;
            case "NOT_RESTRICTED": return NetworkCapabilities.NET_CAPABILITY_NOT_RESTRICTED;
            case "MCX": return NetworkCapabilities.NET_CAPABILITY_MCX;
            case "ENTERPRISE": return NetworkCapabilities.NET_CAPABILITY_ENTERPRISE;
            case "MMTEL": return NetworkCapabilities.NET_CAPABILITY_MMTEL;
            case "PRIORITIZE_LATENCY": return NetworkCapabilities.NET_CAPABILITY_PRIORITIZE_LATENCY;
            case "PRIORITIZE_BANDWIDTH": return NetworkCapabilities.NET_CAPABILITY_PRIORITIZE_BANDWIDTH;
            default: return -1;
        }
    }
}
