package br.inatel.smartfarm.farm.protocol

/** Minimal JSON writer for the collector records (maps, lists, strings, numbers, booleans, null). */
object Json {
    fun encode(value: Any?): String = StringBuilder().also { write(it, value) }.toString()

    private fun write(sb: StringBuilder, v: Any?) {
        when (v) {
            null -> sb.append("null")
            is String -> string(sb, v)
            is Boolean -> sb.append(v)
            is Double -> if (v.isNaN() || v.isInfinite()) sb.append("null") else sb.append(v)
            is Float -> write(sb, v.toDouble())
            is Number -> sb.append(v.toLong())
            is Map<*, *> -> {
                sb.append('{')
                var first = true
                for ((k, value) in v) {
                    if (!first) sb.append(", ")
                    first = false
                    string(sb, k.toString()); sb.append(": "); write(sb, value)
                }
                sb.append('}')
            }
            is Iterable<*> -> {
                sb.append('[')
                var first = true
                for (item in v) { if (!first) sb.append(", "); first = false; write(sb, item) }
                sb.append(']')
            }
            else -> string(sb, v.toString())
        }
    }

    private fun string(sb: StringBuilder, s: String) {
        sb.append('"')
        for (c in s) {
            when (c) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                else -> if (c < ' ') sb.append(String.format("\\u%04x", c.code)) else sb.append(c)
            }
        }
        sb.append('"')
    }
}
