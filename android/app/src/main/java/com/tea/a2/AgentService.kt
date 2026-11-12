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
    private var wakeLock: android.os.PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        // The type matters: the two argument startForeground() takes every
        // type the manifest declares - and on Android 16 a service that
        // declares mediaProjection needs the system permission
        // CAPTURE_VIDEO_OUTPUT to start at all. So the service starts with
        // its own type only, and the capture type is added later (if the
        // system allows it).
        startInForeground()

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
        // The peer secret: made once and kept, so a computer can drive this
        // phone (the peer channel and /api/exec are the ones it guards).
        argv += listOf("--api-token", settings.token())
        // A partial wake lock: with the screen off the system is free to
        // throttle the CPU, and then a turn takes minutes instead of
        // seconds. The cost is the battery.
        if (A2Settings(this).keepAwake) {
            try {
                val power = getSystemService(android.os.PowerManager::class.java)
                wakeLock = power?.newWakeLock(
                    android.os.PowerManager.PARTIAL_WAKE_LOCK, "a2:agent"
                )?.also {
                    it.acquire()
                    Log.i(TAG, "the wake lock is held")
                }
            } catch (e: Exception) {
                Log.w(TAG, "the wake lock could not be taken: " + e.message)
            }
        }

        Log.i(TAG, "starting the agent: " + argv.joinToString(" "))
        A2Native.start(argv.toTypedArray())

        if (settings.bridgeEnabled) {
            bridge = PhoneBridge(this).also { it.start(settings.bridgePort) }
        }
    }

    /**
     * Starts (or keeps) the service in the foreground with its own type.
     * A failure here must not take the app down: the agent can run without
     * the notification.
     */
    private fun startInForeground(type: Int = 0) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE && type != 0) {
                // Android 14+: the type is named explicitly.
                startForeground(NOTIFICATION_ID, buildNotification(), type)
            } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                startForeground(
                    NOTIFICATION_ID,
                    buildNotification(),
                    android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
                )
            } else {
                // Before Android 14 the types come from the manifest.
                startForeground(NOTIFICATION_ID, buildNotification())
            }
        } catch (e: Exception) {
            Log.w(TAG, "the foreground service did not start: " + e.message)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_MEDIA_PROJECTION) {
            // The service is already in the foreground; this adds the type
            // the screen capture needs. On Android 16 the system may refuse
            // it (CAPTURE_VIDEO_OUTPUT is a system permission) - then the
            // capture goes through Shizuku instead.
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
        Log.i(TAG, "the service is going away")
        try {
            wakeLock?.release()
        } catch (e: Exception) {
            // releasing twice is not an error
        }
        wakeLock = null
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
        @Volatile
        var mediaProjectionAllowed: Boolean = true

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
