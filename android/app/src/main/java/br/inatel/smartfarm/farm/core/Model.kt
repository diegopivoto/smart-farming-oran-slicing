package br.inatel.smartfarm.farm.core

import android.content.Context
import android.content.SharedPreferences
import android.net.NetworkCapabilities
import br.inatel.smartfarm.farm.protocol.Wire
import java.net.Inet4Address

/**
 * One Smart Farming service: slice, phone receiver port and the capability set that selects its APN on the phone
 * (APN types: crop = mms,fota; drone = mms,cbs; weather = supl -- docs/runbooks/android-capability-probe.md).
 */
enum class ServiceKind(
    val id: String,
    val title: String,
    val subtitle: String,
    val sliceName: String,
    val sd: String,
    val port: Int,
    val code: Int,
    val defaultCaps: String,
    /** Third octet range of 10.45.x.y that the SMF hands out for this service's DNN. */
    val octetLo: Int,
    val octetHi: Int,
) {
    DRONE("drone", "Drone over the field", "Live flight video", "drone", "000003", Wire.PORT_DRONE, Wire.SERVICE_DRONE, "MMS+CBS", 160, 191),
    CROP("crop", "Field imagery", "Multispectral captures", "field imagery", "000002", Wire.PORT_CROP, Wire.SERVICE_CROP, "MMS+FOTA", 128, 159),
    WEATHER("weather", "Weather station", "Live sensors", "weather", "000001", Wire.PORT_WEATHER, Wire.SERVICE_WEATHER, "SUPL", 192, 223);

    fun owns(ip: Inet4Address?): Boolean {
        val b = ip?.address ?: return false
        val third = b[2].toInt() and 0xff
        return (b[0].toInt() and 0xff) == 10 && (b[1].toInt() and 0xff) == 45 && third in octetLo..octetHi
    }

    companion object {
        fun byId(id: String?) = entries.firstOrNull { it.id == id }
    }
}

/** Capability names accepted in settings ("+"-separated); "-NOT_RESTRICTED" removes that default capability. */
object Capabilities {
    fun byName(name: String): Int = when (name) {
        "MMS" -> NetworkCapabilities.NET_CAPABILITY_MMS
        "SUPL" -> NetworkCapabilities.NET_CAPABILITY_SUPL
        "DUN" -> NetworkCapabilities.NET_CAPABILITY_DUN
        "FOTA" -> NetworkCapabilities.NET_CAPABILITY_FOTA
        "IMS" -> NetworkCapabilities.NET_CAPABILITY_IMS
        "CBS" -> NetworkCapabilities.NET_CAPABILITY_CBS
        "IA" -> NetworkCapabilities.NET_CAPABILITY_IA
        "RCS" -> NetworkCapabilities.NET_CAPABILITY_RCS
        "XCAP" -> NetworkCapabilities.NET_CAPABILITY_XCAP
        "EIMS" -> NetworkCapabilities.NET_CAPABILITY_EIMS
        "INTERNET" -> NetworkCapabilities.NET_CAPABILITY_INTERNET
        "NOT_RESTRICTED" -> NetworkCapabilities.NET_CAPABILITY_NOT_RESTRICTED
        "MCX" -> NetworkCapabilities.NET_CAPABILITY_MCX
        else -> -1
    }
}

/**
 * App settings. With the lab orchestrator on (default), the generator address comes from its response (the UN3GPP
 * UE's address on the slice); [generatorAddress] is the manual fallback when the orchestrator is off or unreachable.
 */
class FarmSettings(context: Context) {
    private val prefs: SharedPreferences = context.getSharedPreferences("farm_settings", Context.MODE_PRIVATE)

    var generatorAddress: String
        get() = prefs.getString("generator", DEFAULT_GENERATOR) ?: DEFAULT_GENERATOR
        set(v) = prefs.edit().putString("generator", v.trim()).apply()

    var collectorUrl: String
        get() = prefs.getString("collector", DEFAULT_COLLECTOR) ?: DEFAULT_COLLECTOR
        set(v) = prefs.edit().putString("collector", v.trim()).apply()

    var reportEnabled: Boolean
        get() = prefs.getBoolean("report", true)
        set(v) = prefs.edit().putBoolean("report", v).apply()

    var orchestratorUrl: String
        get() = prefs.getString("orchestrator", DEFAULT_ORCHESTRATOR) ?: DEFAULT_ORCHESTRATOR
        set(v) = prefs.edit().putString("orchestrator", v.trim()).apply()

    var useOrchestrator: Boolean
        get() = prefs.getBoolean("use_orchestrator", true)
        set(v) = prefs.edit().putBoolean("use_orchestrator", v).apply()

    /** O-RAN radio control asked for each service session (Orchestration.ORAN_MODES); default the RAN defaults. */
    var oranMode: String
        get() = prefs.getString("oran_mode", DEFAULT_ORAN_MODE)?.takeIf { it in br.inatel.smartfarm.farm.protocol.Orchestration.ORAN_MODES }
            ?: DEFAULT_ORAN_MODE
        set(v) = prefs.edit().putString("oran_mode", if (v in br.inatel.smartfarm.farm.protocol.Orchestration.ORAN_MODES) v else DEFAULT_ORAN_MODE).apply()

    fun caps(kind: ServiceKind): String = prefs.getString("caps_" + kind.id, kind.defaultCaps) ?: kind.defaultCaps
    fun setCaps(kind: ServiceKind, v: String) = prefs.edit().putString("caps_" + kind.id, v.trim().uppercase()).apply()

    fun restoreDefaults() = prefs.edit().clear().apply()

    companion object {
        /** No fixed UE address: the orchestrator reports the UN3GPP UE's current IP; set this only to run without it. */
        const val DEFAULT_GENERATOR = ""
        const val DEFAULT_COLLECTOR = "http://10.45.0.1:8088/report"
        const val DEFAULT_ORCHESTRATOR = br.inatel.smartfarm.farm.protocol.Orchestration.DEFAULT_URL
        const val DEFAULT_ORAN_MODE = br.inatel.smartfarm.farm.protocol.Orchestration.ORAN_RAN_DEFAULT
    }
}
