package br.inatel.smartfarm.farm

import android.content.Context
import android.net.ConnectivityManager
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * Last-chance crash report: an uncaught exception is written to the app's files and POSTed to the lab collector over
 * the default network (kind "app_crash", English stack trace) before the process dies, so a crash on the phone can be
 * diagnosed without a USB cable. The previous handler still runs afterwards.
 */
object CrashReporter {
    private const val COLLECTOR = "http://10.45.0.1:8088/report"
    private const val RUN_ID = "c0ffee00c0ffee00c0ffee00c0ffee00"

    fun install(context: Context) {
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            val trace = error.stackTraceToString().take(12000)
            runCatching { File(context.filesDir, "last-crash.txt").writeText(trace) }
            val sender = Thread {
                runCatching {
                    val body = org.json.JSONObject(mapOf(
                        "kind" to "app_crash", "run_id" to RUN_ID, "service" to "app", "thread" to thread.name,
                        "error" to error.toString().take(500), "trace" to trace,
                        "at_ns" to System.currentTimeMillis() * 1_000_000L,
                    )).toString()
                    val cm = context.getSystemService(ConnectivityManager::class.java)
                    val url = URL(COLLECTOR)
                    val c = (cm?.activeNetwork?.openConnection(url) ?: url.openConnection()) as HttpURLConnection
                    c.connectTimeout = 2000; c.readTimeout = 2000; c.requestMethod = "POST"; c.doOutput = true
                    c.setRequestProperty("Content-Type", "application/json")
                    c.outputStream.use { it.write(body.toByteArray()) }
                    c.responseCode
                    c.disconnect()
                }
            }
            sender.start()
            runCatching { sender.join(3000) }
            previous?.uncaughtException(thread, error)
        }
    }
}
