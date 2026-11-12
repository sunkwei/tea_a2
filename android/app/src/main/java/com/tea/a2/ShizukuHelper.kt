package com.tea.a2

import android.app.Activity
import android.content.pm.PackageManager
import android.util.Log
import org.json.JSONObject
import rikka.shizuku.Shizuku
import java.io.ByteArrayOutputStream
import java.util.concurrent.TimeUnit

/**
 * Shizuku: the phone's own shell, without root.
 *
 * Shizuku runs a small service with the privileges of the `shell` user
 * (uid 2000) - the same privileges `adb shell` has. That opens the doors
 * the plain Android APIs keep shut: taking a screenshot without a consent
 * dialog, tapping and typing on the screen, reading the SMS inbox through
 * the content providers, and running the system tools (pm, am, dumpsys).
 *
 * The user has to allow this app once (the Shizuku app asks). Everything
 * here answers "no" when that did not happen - the app keeps working with
 * the ordinary Android APIs.
 */
object ShizukuHelper {

    private const val TAG = "A2Shizuku"
    private const val PERMISSION_REQUEST = 4242

    data class Result(val exitCode: Int, val stdout: String, val stderr: String)

    /** Is the Shizuku service there at all? */
    fun serviceRunning(): Boolean = try {
        Shizuku.pingBinder()
    } catch (e: Exception) {
        false
    }

    /** Did the user allow this app to use Shizuku? */
    fun permissionGranted(): Boolean = try {
        Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
    } catch (e: Exception) {
        false
    }

    /** The uid the commands run as. */
    fun uid(): Int = try {
        Shizuku.getUid()
    } catch (e: Exception) {
        -1
    }

    /** What the app should show about Shizuku. */
    fun status(): JSONObject {
        val out = JSONObject()
        out.put("service_running", serviceRunning())
        out.put("permission_granted", permissionGranted())
        out.put("uid", uid())
        out.put("usable", serviceRunning() && permissionGranted())
        return out
    }

    /** Asks the user (through the Shizuku app) to allow this app. */
    fun requestPermission(activity: Activity) {
        try {
            Shizuku.requestPermission(PERMISSION_REQUEST)
        } catch (e: Exception) {
            Log.w(TAG, "the permission request failed: " + e.message)
        }
    }

    /**
     * Starts a command as the shell user.
     *
     * Shizuku's own `newProcess` is private in the API (13.x): the class
     * hands out a `ShizukuRemoteProcess`, which is a `java.lang.Process`, so
     * the private method is called by name. The release build does not
     * shrink the code, so the name stays.
     */
    private fun newProcess(command: List<String>): Process? {
        val method = newProcessMethod ?: return null
        return try {
            method.invoke(null, command.toTypedArray(), null, null) as? Process
        } catch (e: Exception) {
            Log.w(TAG, "the command did not start: " + e.message)
            null
        }
    }

    private val newProcessMethod: java.lang.reflect.Method? by lazy {
        try {
            Shizuku::class.java.getDeclaredMethod(
                "newProcess",
                Array<String>::class.java,
                Array<String>::class.java,
                String::class.java
            ).also { it.isAccessible = true }
        } catch (e: Exception) {
            Log.w(TAG, "the Shizuku API does not look the way it should: " + e.message)
            null
        }
    }

    /**
     * Runs a command as the shell user. Returns null when Shizuku is not
     * available - the caller then says so instead of pretending.
     */
    fun exec(command: List<String>, timeoutMs: Long = 15000): Result? =
        execFull(command, timeoutMs)

    /** The same, but the raw stdout - a screenshot comes out of it. */
    fun execBytes(command: List<String>, timeoutMs: Long = 15000): ByteArray? {
        val result = run(command, timeoutMs) ?: return null
        return result.stdoutBytes
    }

    /** The exit code and the two streams, for the commands that answer. */
    fun execFull(command: List<String>, timeoutMs: Long = 15000): Result? =
        run(command, timeoutMs)?.let {
            Result(it.exitCode, String(it.stdoutBytes, Charsets.UTF_8),
                String(it.stderrBytes, Charsets.UTF_8))
        }

    private class Raw(val exitCode: Int, val stdoutBytes: ByteArray, val stderrBytes: ByteArray)

    private fun run(command: List<String>, timeoutMs: Long): Raw? {
        if (!permissionGranted()) return null
        var process: Process? = null
        return try {
            process = newProcess(command) ?: return null
            val out = ByteArrayOutputStream()
            val err = ByteArrayOutputStream()
            val outThread = Thread { process!!.inputStream.use { it.copyTo(out) } }
            val errThread = Thread { process!!.errorStream.use { it.copyTo(err) } }
            outThread.isDaemon = true
            errThread.isDaemon = true
            outThread.start()
            errThread.start()
            process.waitFor(timeoutMs, TimeUnit.MILLISECONDS)
            outThread.join(500)
            errThread.join(500)
            Raw(process.exitValue(), out.toByteArray(), err.toByteArray())
        } catch (e: Exception) {
            Log.w(TAG, "the command failed: " + e.message)
            null
        } finally {
            try {
                process?.destroy()
            } catch (e: Exception) {
                // the process was already gone
            }
        }
    }
}
