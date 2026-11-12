package com.tea.a2

import android.Manifest
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationManager
import android.net.ConnectivityManager
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.StatFs
import android.telephony.SmsManager
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * The phone bridge: a small HTTP server on the loopback interface.
 *
 * The agent (liba2.so) and this server share one process, so the agent can
 * ask the phone for the things only a phone can do - the notifications that
 * came in, where it is, what is on the clipboard, a screenshot, opening an
 * app - through the `toolkit_phone` tool.
 *
 * The server only listens on 127.0.0.1: nothing outside the app can reach
 * it, and every action still needs the permission the user granted.
 */
class PhoneBridge(private val context: Context) {

    private var server: ServerSocket? = null
    private var worker: Thread? = null
    private val pool = Executors.newCachedThreadPool()

    @Volatile
    private var running = false

    fun start(port: Int) {
        try {
            val socket = ServerSocket(port, 16, InetAddress.getByName("127.0.0.1"))
            server = socket
            running = true
            worker = Thread({
                while (running && !socket.isClosed) {
                    try {
                        val client = socket.accept()
                        pool.submit { serve(client) }
                    } catch (e: Exception) {
                        if (!running) break
                    }
                }
            }, "phone-bridge").also {
                it.isDaemon = true
                it.start()
            }
            Log.i(TAG, "the phone bridge listens on 127.0.0.1:$port")
        } catch (e: Exception) {
            Log.e(TAG, "the phone bridge did not start: " + e.message)
        }
    }

    fun stop() {
        running = false
        try {
            server?.close()
        } catch (e: Exception) {
            // closing a socket that is already closed is not an error here
        }
        server = null
        pool.shutdownNow()
    }

    // ---- the wire -----------------------------------------------------------

    private class Request(
        val method: String,
        val path: String,
        val query: Map<String, String>,
        val body: String
    )

    private fun serve(client: Socket) {
        try {
            client.use {
                val input = it.getInputStream()
                val request = readRequest(input) ?: return
                val response = route(request)
                writeResponse(it.getOutputStream(), response.first, response.second)
            }
        } catch (e: Exception) {
            Log.w(TAG, "a request failed: " + e.message)
        }
    }

    /** Reads the head of the request and the body it announces. */
    private fun readRequest(input: InputStream): Request? {
        val head = ByteArrayOutputStream()
        var matched = 0
        while (true) {
            val b = input.read()
            if (b < 0) return null
            head.write(b)
            val expected = if (matched % 2 == 0) 13 else 10
            if (b == expected) {
                matched++
                if (matched == 4) break
            } else {
                matched = 0
            }
        }
        val lines = head.toString("ISO-8859-1").split("\r\n")
        if (lines.isEmpty()) return null
        val first = lines[0].split(" ")
        if (first.size < 2) return null
        val method = first[0]
        val target = first[1]
        var path = target
        val query = mutableMapOf<String, String>()
        val mark = target.indexOf('?')
        if (mark >= 0) {
            path = target.substring(0, mark)
            for (pair in target.substring(mark + 1).split("&")) {
                if (pair.isEmpty()) continue
                val eq = pair.indexOf('=')
                if (eq < 0) {
                    query[pair] = ""
                } else {
                    query[pair.substring(0, eq)] = urlDecode(pair.substring(eq + 1))
                }
            }
        }

        var length = 0
        for (i in 1 until lines.size) {
            val line = lines[i]
            if (line.isEmpty()) continue
            val colon = line.indexOf(':')
            if (colon < 0) continue
            val name = line.substring(0, colon).trim().lowercase()
            val value = line.substring(colon + 1).trim()
            if (name == "content-length") length = value.toIntOrNull() ?: 0
        }
        val body = ByteArray(length)
        var read = 0
        while (read < length) {
            val n = input.read(body, read, length - read)
            if (n < 0) break
            read += n
        }
        return Request(method, path, query, String(body, 0, read, Charsets.UTF_8))
    }

    private fun writeResponse(out: OutputStream, status: Int, body: String) {
        val payload = body.toByteArray(Charsets.UTF_8)
        val head = buildString {
            append("HTTP/1.1 ").append(status).append(' ').append(reason(status)).append("\r\n")
            append("Content-Type: application/json; charset=utf-8\r\n")
            append("Content-Length: ").append(payload.size).append("\r\n")
            append("Cache-Control: no-store\r\n")
            append("Connection: close\r\n\r\n")
        }
        val buffered = BufferedOutputStream(out)
        buffered.write(head.toByteArray(Charsets.UTF_8))
        buffered.write(payload)
        buffered.flush()
    }

    private fun reason(status: Int): String = when (status) {
        200 -> "OK"
        400 -> "Bad Request"
        403 -> "Forbidden"
        404 -> "Not Found"
        500 -> "Internal Server Error"
        else -> "OK"
    }

    // ---- the routes ---------------------------------------------------------

    private fun route(request: Request): Pair<Int, String> = try {
        when {
            request.method == "GET" && request.path == "/ping" ->
                200 to JSONObject().put("ok", true).put("service", "a2-phone-bridge").toString()

            request.method == "GET" && request.path == "/phone/device" ->
                200 to deviceInfo().toString()

            request.method == "GET" && request.path == "/phone/notifications" ->
                200 to notifications(request.query["limit"]?.toIntOrNull() ?: 20).toString()

            request.method == "GET" && request.path == "/phone/location" ->
                200 to location().toString()

            request.method == "GET" && request.path == "/phone/clipboard" ->
                200 to JSONObject().put("text", readClipboard()).toString()

            request.method == "POST" && request.path == "/phone/clipboard" ->
                200 to writeClipboard(request.body).toString()

            request.method == "POST" && request.path == "/phone/open" ->
                200 to open(request.body).toString()

            request.method == "GET" && request.path == "/phone/screenshot" ->
                200 to screenshot().toString()

            request.method == "POST" && request.path == "/phone/sms" ->
                200 to sendSms(request.body).toString()

            request.method == "POST" && request.path == "/phone/call" ->
                200 to call(request.body).toString()

            request.method == "GET" && request.path == "/phone/apps" ->
                200 to apps(request.query["filter"] ?: "").toString()

            request.method == "GET" && request.path == "/phone/shizuku" ->
                200 to ShizukuHelper.status().toString()

            request.method == "POST" && request.path == "/phone/shell" ->
                200 to shell(request.body).toString()

            request.method == "POST" && request.path == "/phone/tap" ->
                200 to tap(request.body).toString()

            request.method == "POST" && request.path == "/phone/swipe" ->
                200 to swipe(request.body).toString()

            request.method == "POST" && request.path == "/phone/type" ->
                200 to typeText(request.body).toString()

            request.method == "POST" && request.path == "/phone/paste" ->
                200 to paste(request.body).toString()

            else -> 404 to JSONObject()
                .put("ok", false)
                .put("error", "no such endpoint: " + request.method + " " + request.path)
                .toString()
        }
    } catch (e: Exception) {
        Log.e(TAG, "a route failed: " + e.message)
        500 to JSONObject().put("ok", false).put("error", e.message ?: "failed").toString()
    }

    // ---- what the phone knows ----------------------------------------------

    private fun deviceInfo(): JSONObject {
        val info = JSONObject()
        info.put("model", Build.MODEL)
        info.put("manufacturer", Build.MANUFACTURER)
        info.put("android", Build.VERSION.RELEASE)
        info.put("sdk", Build.VERSION.SDK_INT)
        info.put("uptime_ms", android.os.SystemClock.elapsedRealtime())

        val battery = context.getSystemService(BatteryManager::class.java)
        if (battery != null) {
            val percent = battery.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
            info.put("battery_percent", percent)
            info.put("charging", battery.isCharging)
        }

        val stat = StatFs(context.filesDir.absolutePath)
        info.put("storage_free_mb", stat.availableBytes / 1024 / 1024)
        info.put("storage_total_mb", stat.totalBytes / 1024 / 1024)
        info.put("data_dir", Environment.getDataDirectory().absolutePath)

        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        if (connectivity != null) {
            val network = connectivity.activeNetwork
            val capabilities = connectivity.getNetworkCapabilities(network)
            info.put("network", when {
                capabilities == null -> "none"
                capabilities.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
                capabilities.hasTransport(android.net.NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
                capabilities.hasTransport(android.net.NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
                else -> "other"
            })
        }
        info.put("agent_url", "http://127.0.0.1:" + A2Settings(context).port)
        return info
    }

    private fun notifications(limit: Int): JSONArray {
        val all = NotificationCollector.recent()
        val out = JSONArray()
        val from = if (all.size > limit) all.size - limit else 0
        for (i in from until all.size) {
            val n = all[i]
            val item = JSONObject()
            item.put("package", n.packageName)
            item.put("title", n.title)
            item.put("text", n.text)
            item.put("time", n.time)
            item.put("ongoing", n.ongoing)
            out.put(item)
        }
        return out
    }

    private fun location(): JSONObject {
        val result = JSONObject()
        val manager = context.getSystemService(LocationManager::class.java)
        if (manager == null) {
            result.put("ok", false)
            result.put("error", "no location service")
            return result
        }
        if (context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) !=
            PackageManager.PERMISSION_GRANTED &&
            context.checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            result.put("ok", false)
            result.put("error", "the location permission has not been granted")
            return result
        }

        var best: Location? = null
        for (provider in manager.getProviders(true)) {
            try {
                val known = manager.getLastKnownLocation(provider) ?: continue
                if (best == null || known.time > best!!.time) best = known
            } catch (e: SecurityException) {
                // a provider without the permission: skip it
            }
        }

        // A fresh fix, if the system can give one quickly.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val latch = CountDownLatch(1)
            var fresh: Location? = null
            try {
                val provider = when {
                    manager.isProviderEnabled(LocationManager.GPS_PROVIDER) -> LocationManager.GPS_PROVIDER
                    else -> LocationManager.NETWORK_PROVIDER
                }
                // Context.getMainExecutor() needs API 28; the app runs on 26.
                val executor = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    context.mainExecutor
                } else {
                    Executors.newSingleThreadExecutor()
                }
                manager.getCurrentLocation(
                    provider, null, executor,
                    { location ->
                        fresh = location
                        latch.countDown()
                    })
                latch.await(8, TimeUnit.SECONDS)
            } catch (e: Exception) {
                // no fresh fix; the last known one will do
            }
            if (fresh != null) best = fresh
        }

        if (best == null) {
            result.put("ok", false)
            result.put("error", "no position yet")
            return result
        }
        result.put("ok", true)
        result.put("latitude", best!!.latitude)
        result.put("longitude", best!!.longitude)
        result.put("accuracy_m", best!!.accuracy.toDouble())
        result.put("provider", best!!.provider)
        result.put("time", best!!.time)
        return result
    }

    private fun readClipboard(): String {
        val manager = context.getSystemService(ClipboardManager::class.java) ?: return ""
        val clip = manager.primaryClip ?: return ""
        if (clip.itemCount == 0) return ""
        return clip.getItemAt(0).coerceToText(context).toString()
    }

    private fun writeClipboard(body: String): JSONObject {
        val payload = parse(body)
        val text = payload.optString("text", "")
        val manager = context.getSystemService(ClipboardManager::class.java)
        if (manager == null) {
            return JSONObject().put("ok", false).put("error", "no clipboard")
        }
        manager.setPrimaryClip(ClipData.newPlainText("a2", text))
        return JSONObject().put("ok", true)
    }

    private fun open(body: String): JSONObject {
        val payload = parse(body)
        val url = payload.optString("url", "")
        val packageName = payload.optString("package", "")
        val query = payload.optString("query", "")

        val intent = Intent()
        when {
            url.isNotEmpty() -> {
                intent.action = Intent.ACTION_VIEW
                intent.data = Uri.parse(url)
            }
            packageName.isNotEmpty() -> {
                intent.action = Intent.ACTION_MAIN
                intent.addCategory(Intent.CATEGORY_LAUNCHER)
                intent.setPackage(packageName)
            }
            query.isNotEmpty() -> {
                intent.action = Intent.ACTION_WEB_SEARCH
                intent.putExtra("query", query)
            }
            else -> return JSONObject().put("ok", false).put("error", "nothing to open")
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            context.startActivity(intent)
            JSONObject().put("ok", true)
        } catch (e: Exception) {
            JSONObject().put("ok", false).put("error", e.message ?: "could not open it")
        }
    }

    private fun screenshot(): JSONObject {
        // With Shizuku the screen can be read directly - no consent dialog,
        // no system prompt, and it works while the screen is off.
        val viaShell = ShizukuHelper.execBytes(listOf("screencap", "-p"), 15000)
        if (viaShell != null && viaShell.size > 8 &&
            viaShell[0] == 0x89.toByte() && viaShell[1] == 'P'.code.toByte()
        ) {
            return JSONObject()
                .put("ok", true)
                .put("format", "png")
                .put("source", "shizuku")
                .put("data", android.util.Base64.encodeToString(viaShell, android.util.Base64.NO_WRAP))
        }
        val png = ScreenCapture.capture()
        if (png == null) {
            return JSONObject()
                .put("ok", false)
                .put("error", "no screen capture yet - the user has to allow it once in the app")
        }
        return JSONObject()
            .put("ok", true)
            .put("format", "png")
            .put("data", android.util.Base64.encodeToString(png, android.util.Base64.NO_WRAP))
    }

    private fun sendSms(body: String): JSONObject {
        if (!A2Settings(context).allowSms) {
            return JSONObject().put("ok", false).put("error", "sending messages is turned off")
        }
        if (context.checkSelfPermission(Manifest.permission.SEND_SMS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            return JSONObject().put("ok", false).put("error", "the SMS permission has not been granted")
        }
        val payload = parse(body)
        val to = payload.optString("to", "")
        val text = payload.optString("text", "")
        if (to.isEmpty() || text.isEmpty()) {
            return JSONObject().put("ok", false).put("error", "'to' and 'text' are required")
        }
        return try {
            SmsManager.getDefault().sendTextMessage(to, null, text, null, null)
            JSONObject().put("ok", true)
        } catch (e: Exception) {
            JSONObject().put("ok", false).put("error", e.message ?: "could not send it")
        }
    }

    private fun call(body: String): JSONObject {
        val payload = parse(body)
        val number = payload.optString("number", "")
        if (number.isEmpty()) {
            return JSONObject().put("ok", false).put("error", "'number' is required")
        }
        val intent = Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(number)))
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            context.startActivity(intent)
            JSONObject().put("ok", true)
        } catch (e: Exception) {
            JSONObject().put("ok", false).put("error", e.message ?: "could not dial it")
        }
    }

    private fun apps(filter: String): JSONArray {
        val out = JSONArray()
        val manager = context.packageManager
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val packages = manager.queryIntentActivities(intent, 0)
        for (info in packages) {
            val item = info.activityInfo?.applicationInfo ?: continue
            val label = manager.getApplicationLabel(item).toString()
            if (filter.isNotEmpty() &&
                !label.contains(filter, ignoreCase = true) &&
                !item.packageName.contains(filter, ignoreCase = true)
            ) {
                continue
            }
            val entry = JSONObject()
            entry.put("package", item.packageName)
            entry.put("label", label)
            out.put(entry)
        }
        return out
    }

    /** Runs a command as the shell user (Shizuku). */
    private fun shell(body: String): JSONObject {
        val payload = parse(body)
        val command = payload.optString("command", "")
        if (command.isEmpty()) {
            return JSONObject().put("ok", false).put("error", "'command' is required")
        }
        if (!ShizukuHelper.permissionGranted()) {
            return JSONObject()
                .put("ok", false)
                .put("error", "Shizuku has not allowed this app yet")
        }
        val result = ShizukuHelper.execFull(command.trim().split(Regex("\\s+")).toList(), 20000)
            ?: return JSONObject().put("ok", false).put("error", "the command did not run")
        return JSONObject()
            .put("ok", true)
            .put("exit_code", result.exitCode)
            .put("stdout", result.stdout)
            .put("stderr", result.stderr)
    }

    /** Taps the screen (Shizuku). */
    private fun tap(body: String): JSONObject {
        val payload = parse(body)
        val x = payload.optInt("x", -1)
        val y = payload.optInt("y", -1)
        if (x < 0 || y < 0) {
            return JSONObject().put("ok", false).put("error", "'x' and 'y' are required")
        }
        val result = ShizukuHelper.execFull(listOf("input", "tap", "$x", "$y"), 10000)
            ?: return shizukuMissing()
        return JSONObject().put("ok", result.exitCode == 0).put("exit_code", result.exitCode)
    }

    /** Drags on the screen (Shizuku). */
    private fun swipe(body: String): JSONObject {
        val payload = parse(body)
        val x1 = payload.optInt("x1", -1)
        val y1 = payload.optInt("y1", -1)
        val x2 = payload.optInt("x2", -1)
        val y2 = payload.optInt("y2", -1)
        val ms = payload.optInt("ms", 300)
        if (x1 < 0 || y1 < 0 || x2 < 0 || y2 < 0) {
            return JSONObject().put("ok", false)
                .put("error", "'x1', 'y1', 'x2' and 'y2' are required")
        }
        val result = ShizukuHelper.execFull(
            listOf("input", "swipe", "$x1", "$y1", "$x2", "$y2", "$ms"), 10000)
            ?: return shizukuMissing()
        return JSONObject().put("ok", result.exitCode == 0).put("exit_code", result.exitCode)
    }

    /** Types on the screen (Shizuku). Only plain ASCII: `input text` cannot
     *  type Chinese - for that the text goes through the clipboard. */
    private fun typeText(body: String): JSONObject {
        val payload = parse(body)
        val text = payload.optString("text", "")
        if (text.isEmpty()) {
            return JSONObject().put("ok", false).put("error", "'text' is required")
        }
        val escaped = text.replace(" ", "%s")
        val result = ShizukuHelper.execFull(listOf("input", "text", escaped), 10000)
            ?: return shizukuMissing()
        return JSONObject().put("ok", result.exitCode == 0).put("exit_code", result.exitCode)
    }

    /** Puts the text on the clipboard and pastes it into the focused field. */
    private fun paste(body: String): JSONObject {
        val payload = parse(body)
        val text = payload.optString("text", "")
        if (text.isEmpty()) {
            return JSONObject().put("ok", false).put("error", "'text' is required")
        }
        val manager = context.getSystemService(ClipboardManager::class.java)
        if (manager == null) {
            return JSONObject().put("ok", false).put("error", "no clipboard")
        }
        manager.setPrimaryClip(ClipData.newPlainText("a2", text))
        val result = ShizukuHelper.execFull(listOf("input", "keyevent", "279"), 10000)
            ?: return shizukuMissing()
        return JSONObject().put("ok", result.exitCode == 0).put("exit_code", result.exitCode)
    }

    private fun shizukuMissing(): JSONObject = JSONObject()
        .put("ok", false)
        .put("error", "Shizuku has not allowed this app yet")

    private fun parse(body: String): JSONObject = try {
        JSONObject(body)
    } catch (e: Exception) {
        JSONObject()
    }

    private fun urlDecode(value: String): String = try {
        java.net.URLDecoder.decode(value, "UTF-8")
    } catch (e: Exception) {
        value
    }

    companion object {
        private const val TAG = "A2Bridge"
    }
}
