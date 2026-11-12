package com.tea.a2

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/**
 * Keeps the notifications that came in.
 *
 * Android does not let an app ask for the notification access: the user
 * turns it on in the system settings. The agent's `toolkit_phone` tool reads
 * what this service collected.
 */
class NotificationCollector : NotificationListenerService() {

    data class Entry(
        val packageName: String,
        val title: String,
        val text: String,
        val time: Long,
        val ongoing: Boolean
    )

    companion object {
        private const val MAX_ENTRIES = 100
        private val entries = ArrayDeque<Entry>()

        /** The notifications that came in, oldest first. */
        @Synchronized
        fun recent(): List<Entry> = entries.toList()

        /** Whether the user granted the notification access. */
        @Volatile
        var connected: Boolean = false
            private set
    }

    override fun onListenerConnected() {
        connected = true
    }

    override fun onListenerDisconnected() {
        connected = false
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        val notification = sbn?.notification ?: return
        val extras = notification.extras
        val title = extras?.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: ""
        var text = extras?.getCharSequence(Notification.EXTRA_TEXT)?.toString() ?: ""
        if (text.isEmpty()) {
            text = extras?.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString() ?: ""
        }
        if (title.isEmpty() && text.isEmpty()) return
        val entry = Entry(
            sbn.packageName,
            title,
            text,
            sbn.postTime,
            notification.flags and Notification.FLAG_ONGOING_EVENT != 0
        )
        synchronized(entries) {
            entries.addLast(entry)
            while (entries.size > MAX_ENTRIES) entries.removeFirst()
        }
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?) {
        // The history stays: the agent may still want to know what came in.
    }
}
