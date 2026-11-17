package br.inatel.smartfarm.farm.protocol

import org.json.JSONObject

data class WeatherAlert(val id: String, val severity: String, val text: String, val action: String?)

data class WeatherTelemetry(
    val station: String,
    val state: String,
    val readings: Map<String, Double>,
    /** field -> ("min"|"max" -> value) */
    val thresholds: Map<String, Map<String, Double>>,
    val alerts: List<WeatherAlert>,
    val irrigationActive: Boolean,
    val irrigationRemainingS: Double,
) {
    fun reading(key: String): Double? = readings[key]
    fun min(key: String): Double? = thresholds[key]?.get("min")
    fun max(key: String): Double? = thresholds[key]?.get("max")
    val irrigationAlert: WeatherAlert? get() = alerts.firstOrNull { it.action == "irrigation" }

    companion object {
        const val AIR_TEMPERATURE = "air_temperature_c"
        const val AIR_HUMIDITY = "air_humidity_pct"
        const val SOIL_MOISTURE = "soil_moisture_pct"
        const val RAIN_RATE = "rain_rate_mm_h"
        const val RAIN_TODAY = "rain_today_mm"
        const val WIND_SPEED = "wind_speed_kmh"
        const val WIND_DIRECTION = "wind_direction_deg"
        const val SOLAR = "solar_radiation_w_m2"
        const val UV = "uv_index"
        const val PRESSURE = "pressure_hpa"
        const val LEAF_WETNESS = "leaf_wetness_pct"

        /**
         * Weather payload of one datagram: UTF-8 JSON after the 38 B header, terminated by the first NUL
         * (zero padding up to 1250 B). Returns null for malformed payloads (the packet still counts for the metrics).
         */
        fun parseDatagram(buf: ByteArray, off: Int, len: Int): WeatherTelemetry? {
            val start = off + Wire.DGRAM_SIZE
            val end = off + len
            if (start >= end) return null
            var stop = start
            while (stop < end && buf[stop] != 0.toByte()) stop++
            return parseJson(String(buf, start, stop - start, Charsets.UTF_8))
        }

        fun parseJson(text: String): WeatherTelemetry? = try {
            val o = JSONObject(text)
            if (o.optString("type", "weather") != "weather") null else fromJson(o)
        } catch (_: Exception) {
            null
        }

        private fun fromJson(o: JSONObject): WeatherTelemetry {
            val readings = LinkedHashMap<String, Double>()
            o.optJSONObject("readings")?.let { r ->
                for (k in r.keys()) {
                    val v = r.opt(k)
                    if (v is Number) readings[k] = v.toDouble()
                }
            }
            val thresholds = LinkedHashMap<String, Map<String, Double>>()
            o.optJSONObject("thresholds")?.let { t ->
                for (k in t.keys()) {
                    val lim = t.optJSONObject(k) ?: continue
                    val m = LinkedHashMap<String, Double>()
                    for (b in lim.keys()) (lim.opt(b) as? Number)?.let { m[b] = it.toDouble() }
                    thresholds[k] = m
                }
            }
            val alerts = ArrayList<WeatherAlert>()
            o.optJSONArray("alerts")?.let { a ->
                for (i in 0 until a.length()) {
                    val x = a.optJSONObject(i) ?: continue
                    val action = if (x.isNull("action")) null else x.optString("action").ifEmpty { null }
                    alerts += WeatherAlert(x.optString("id"), x.optString("severity", "warning"), x.optString("text"), action)
                }
            }
            val irr = o.optJSONObject("irrigation")
            return WeatherTelemetry(
                station = o.optString("station", "field-A"),
                state = o.optString("state", "stable"),
                readings = readings,
                thresholds = thresholds,
                alerts = alerts,
                irrigationActive = irr?.optBoolean("active", false) ?: false,
                irrigationRemainingS = irr?.optDouble("remaining_s", 0.0)?.takeUnless { it.isNaN() } ?: 0.0,
            )
        }
    }
}
