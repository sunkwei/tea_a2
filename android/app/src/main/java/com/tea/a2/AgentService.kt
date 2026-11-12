package com.tea.a2

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log

/**
 * The foreground service that keeps the agent alive.
 *
 * The agent is not a separate process: it is a library inside the app, so
 * the app's own process is the agent's process. A foreground service with a
 * notification is what keeps that process alive on a phone.
 */
class AgentService : Service() {

    private var bridge: PhoneBridge? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForeground(NOTIFICATION_ID, buildNotification())

        val settings = A2Settings(this)
        val teaHome = settings.teaHome()
        java.io.File(teaHome).mkdirs()
        java.io.File(settings.workspaceDir()).mkdirs()

        // Android has no HOME. The agent resolves "~" and its own home
        // directory from the environment, so it gets one.
        A2Native.setEnv("HOME", filesDir.absolutePath)
        A2Native.setEnv("TMPDIR", cacheDir.absolutePath)

        val argv = mutableListOf(
            "a2",
            "--port", settings.port.toString(),
            "--tea-home", teaHome,
            "--workspace", settings.workspaceDir(),
            "--quiet"
        )
        if (settings.lanAccess) {
            argv += listOf("--host", "0.0.0.0")
        }
        val token = settings.apiToken
        if (token.isNotEmpty()) {
            argv += listOf("--api-token", token)
        }
        Log.i(TAG, "starting the agent: " + argv.joinToString(" "))
        A2Native.start(argv.toTypedArray())

        if (settings.bridgeEnabled) {
            bridge = PhoneBridge(this).also { it.start(settings.bridgePort) }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_MEDIA_PROJECTION) {
            // The service is already in the foreground; this adds the type
            // the screen capture needs.
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    startForeground(
                        NOTIFICATION_ID,
                        buildNotification(),
                        android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
                    )
                }
            } catch (e: Exception) {
                Log.w(TAG, "the media projection type could not be set: " + e.message)
            }
            return START_STICKY
        }
        // The agent is worth keeping alive: START_STICKY brings the service
        // back after the system killed it.
        return START_STICKY
    }

    override fun onDestroy() {
        bridge?.stop()
        bridge = null
        A2Native.stop()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(NotificationManager::class.java) ?: return
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notification_channel),
            NotificationManager.IMPORTANCE_LOW
        )
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notification_running))
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val TAG = "A2Service"
        private const val CHANNEL_ID = "a2_agent"
        private const val NOTIFICATION_ID = 42
        private const val ACTION_MEDIA_PROJECTION = "com.tea.a2.MEDIA_PROJECTION"

        /** Starts (or restarts) the agent service. */
        fun start(context: android.content.Context) {
            val intent = Intent(context, AgentService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        /**
         * Android 14 wants a foreground service of the type mediaProjection
         * before the app may take a screenshot. The agent's service is
         * already in the foreground; this adds the second type to it.
         */
        fun enableMediaProjection(context: android.content.Context) {
            val intent = Intent(context, AgentService::class.java)
            intent.action = ACTION_MEDIA_PROJECTION
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (e: Exception) {
                Log.w(TAG, "the media projection type could not be set: " + e.message)
            }
        }

        /** Stops the agent. */
        fun stop(context: android.content.Context) {
            context.stopService(Intent(context, AgentService::class.java))
        }
    }
}
