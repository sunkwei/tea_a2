package com.tea.a2

import android.content.Context
import android.content.SharedPreferences
import java.io.File

/**
 * The app's settings: the port the agent listens on, the secret that guards
 * the peer channel, and where everything lives inside the app's own storage.
 *
 * The agent keeps its state in `<app files>/.tea_agent` (the tea home) and
 * its working files in `<app files>/workspace`. Nothing is written outside
 * the app's private storage: on a phone that is the only place an app may
 * write, and it is removed with the app.
 */
class A2Settings(context: Context) {

    private val prefs: SharedPreferences =
        context.getSharedPreferences("a2", Context.MODE_PRIVATE)

    val context: Context = context.applicationContext

    /** The port the agent's web UI and API listen on. */
    var port: Int
        get() = prefs.getInt("port", 8383)
        set(value) = prefs.edit().putInt("port", value).apply()

    /** The port of the phone bridge (the local HTTP API the tool calls). */
    var bridgePort: Int
        get() = prefs.getInt("bridge_port", 8384)
        set(value) = prefs.edit().putInt("bridge_port", value).apply()

    /**
     * The secret for /api/exec and the peer channel. Empty means the peer
     * channel is off.
     */
    var apiToken: String
        get() = prefs.getString("api_token", "") ?: ""
        set(value) = prefs.edit().putString("api_token", value).apply()

    /**
     * Whether the agent listens on every interface. Off by default: the web
     * UI and the chat API have no password, so anybody on the same Wi-Fi
     * could talk to the agent - and the agent can run commands.
     */
    var lanAccess: Boolean
        get() = prefs.getBoolean("lan_access", false)
        set(value) = prefs.edit().putBoolean("lan_access", value).apply()

    /**
     * Whether the agent holds a partial wake lock. Without it the system
     * may throttle the CPU when the screen is off; with it the agent keeps
     * working (and keeps the battery busy).
     */
    var keepAwake: Boolean
        get() = prefs.getBoolean("keep_awake", true)
        set(value) = prefs.edit().putBoolean("keep_awake", value).apply()

    /** Whether the bridge answers at all. */
    var bridgeEnabled: Boolean
        get() = prefs.getBoolean("bridge_enabled", true)
        set(value) = prefs.edit().putBoolean("bridge_enabled", value).apply()

    /** Whether a text message may be sent from the agent. */
    var allowSms: Boolean
        get() = prefs.getBoolean("allow_sms", false)
        set(value) = prefs.edit().putBoolean("allow_sms", value).apply()

    /** The agent's own directory: provider.yaml, the workspace index, logs. */
    fun teaHome(): String = File(context.filesDir, ".tea_agent").absolutePath

    /** The workspace the agent works in. */
    fun workspaceDir(): String = File(context.filesDir, "workspace").absolutePath

    /** The provider configuration, as the agent reads it. */
    fun providerFile(): File = File(teaHome(), "provider.yaml")

    /** Reads the provider configuration. */
    fun readProvider(): String =
        try {
            if (providerFile().exists()) providerFile().readText() else ""
        } catch (e: Exception) {
            ""
        }

    /** Writes the provider configuration and starts the agent again. */
    fun writeProvider(yaml: String) {
        val home = File(teaHome())
        home.mkdirs()
        providerFile().writeText(yaml)
        File(workspaceDir()).mkdirs()
    }

    /** True when the agent has something to talk to. */
    fun isConfigured(): Boolean = readProvider().isNotBlank()

    /** A fresh secret for the peer channel. */
    fun newApiToken(): String {
        val bytes = ByteArray(18)
        java.security.SecureRandom().nextBytes(bytes)
        val token = bytes.joinToString("") { "%02x".format(it) }
        apiToken = token
        return token
    }
}
