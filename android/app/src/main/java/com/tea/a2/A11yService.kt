package com.tea.a2

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * The accessibility service: the light way to drive the phone.
 *
 * Shizuku gives the shell's powers (screenshots, the SMS inbox, `input tap`),
 * but it is not something every phone has. The accessibility service is the
 * standard Android way to read a screen and act on it - the user turns it on
 * in the system settings, no root and no extra app needed.
 *
 * What it adds over the shell:
 *   * the screen as a **node tree** (text, what is clickable, where) - the
 *     shell's `uiautomator dump` gives coordinates, this gives meaning;
 *   * **clicking by text** ("点那个叫『确认』的按钮") - no pixel arithmetic;
 *   * typing into the focused field (ACTION_SET_TEXT) - works with Chinese;
 *   * the **global actions**: back, home, recents, the notification shade.
 *
 * What it cannot do: take a screenshot (MediaProjection), read other apps'
 * files, read the SMS inbox. Those stay with Shizuku.
 */
class A11yService : AccessibilityService() {

    companion object {
        private const val TAG = "A2A11y"

        @Volatile
        var instance: A11yService? = null
            private set

        /** Is the service turned on? */
        fun enabled(): Boolean = instance != null

        /** A short status: is the service on, is there a window, how big is the tree. */
        fun status(): String {
            val service = instance ?: return "off"
            val root = service.rootInActiveWindow ?: return "on, but no active window"
            var count = 0
            val lines = mutableListOf<String>()
            service.walk(root, lines, 0)
            count = lines.size
            return "on, window=" + (root.windowId ?: "?") + ", nodes=" + count
        }

        /** The screen as text: one line per node that says something. */
        fun screenText(): String {
            val service = instance ?: return ""
            return service.dumpTree()
        }

        /** Clicks the first node whose text or description contains [needle]. */
        fun clickText(needle: String): Boolean {
            val service = instance ?: return false
            val node = service.findNode(needle) ?: return false
            val target = if (node.isClickable) node else node.parent ?: node
            val done = target.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            if (!done) {
                // Some apps only react to a click on the parent, or to a tap
                // at the node's centre.
                service.tapAt(node)
            }
            return true
        }

        /** Types into the focused field (or the first editable one). */
        fun typeText(text: String): Boolean {
            val service = instance ?: return false
            val node = service.findEditable() ?: return false
            val bundle = Bundle()
            bundle.putCharSequence(
                AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                text
            )
            return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, bundle)
        }

        /** The global actions: back, home, recents, notifications, power. */
        fun globalAction(action: String): Boolean {
            val service = instance ?: return false
            val id = when (action.lowercase()) {
                "back" -> GLOBAL_ACTION_BACK
                "home" -> GLOBAL_ACTION_HOME
                "recents" -> GLOBAL_ACTION_RECENTS
                "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
                "power" -> GLOBAL_ACTION_POWER_DIALOG
                else -> return false
            }
            return service.performGlobalAction(id)
        }

        /** A swipe with a gesture (the shell's `input swipe` is the fallback). */
        fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, ms: Long): Boolean {
            val service = instance ?: return false
            val path = Path().apply { moveTo(x1.toFloat(), y1.toFloat()); lineTo(x2.toFloat(), y2.toFloat()) }
            val gesture = GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(path, 0, ms.coerceIn(50, 2000)))
                .build()
            return service.dispatchGesture(gesture, null, null)
        }
    }

    override fun onServiceConnected() {
        instance = this
        Log.i(TAG, "the accessibility service is on")
    }

    override fun onDestroy() {
        instance = null
        Log.i(TAG, "the accessibility service is off")
    }

    override fun onInterrupt() {}

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Nothing to do per event: the tree is read on demand.
    }

    // ---- the tree ----------------------------------------------------------

    private fun dumpTree(): String {
        val root = rootInActiveWindow ?: return ""
        val lines = mutableListOf<String>()
        walk(root, lines, 0)
        return lines.joinToString("\n")
    }

    private fun walk(node: AccessibilityNodeInfo?, lines: MutableList<String>, depth: Int) {
        if (node == null || depth > 30) return
        val text = node.text?.toString()?.trim().orEmpty()
        val description = node.contentDescription?.toString()?.trim().orEmpty()
        val className = node.className?.toString().orEmpty()
        val clickable = node.isClickable
        val editable = node.isEditable
        val id = node.viewIdResourceName?.toString().orEmpty()
        if (text.isNotEmpty() || description.isNotEmpty() || clickable || editable) {
            val bounds = Rect()
            node.getBoundsInScreen(bounds)
            val parts = mutableListOf<String>()
            if (text.isNotEmpty()) parts.add("\"$text\"")
            if (description.isNotEmpty() && description != text) parts.add("[$description]")
            if (id.isNotEmpty()) parts.add("id=$id")
            if (clickable) parts.add("clickable")
            if (editable) parts.add("editable")
            if (className.isNotEmpty()) parts.add(className.substringAfterLast('.'))
            parts.add("(${bounds.left},${bounds.top},${bounds.right},${bounds.bottom})")
            lines.add("  ".repeat(depth) + parts.joinToString(" "))
        }
        for (i in 0 until node.childCount) {
            walk(node.getChild(i), lines, depth + 1)
        }
    }

    private fun findNode(needle: String): AccessibilityNodeInfo? {
        val root = rootInActiveWindow ?: return null
        val wanted = needle.trim()
        if (wanted.isEmpty()) return null
        return findIn(root, wanted)
    }

    private fun findIn(node: AccessibilityNodeInfo?, wanted: String): AccessibilityNodeInfo? {
        if (node == null) return null
        val text = node.text?.toString()?.trim().orEmpty()
        val description = node.contentDescription?.toString()?.trim().orEmpty()
        if (text.contains(wanted, ignoreCase = true) ||
            description.contains(wanted, ignoreCase = true)
        ) {
            return node
        }
        for (i in 0 until node.childCount) {
            val found = findIn(node.getChild(i), wanted)
            if (found != null) return found
        }
        return null
    }

    private fun findEditable(): AccessibilityNodeInfo? {
        val root = rootInActiveWindow ?: return null
        val focused = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
        if (focused != null && focused.isEditable) return focused
        return findEditableIn(root)
    }

    private fun findEditableIn(node: AccessibilityNodeInfo?): AccessibilityNodeInfo? {
        if (node == null) return null
        if (node.isEditable && node.isVisibleToUser) return node
        for (i in 0 until node.childCount) {
            val found = findEditableIn(node.getChild(i))
            if (found != null) return found
        }
        return null
    }

    /** A tap on the node's centre (the last resort when ACTION_CLICK did nothing). */
    private fun tapAt(node: AccessibilityNodeInfo) {
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        val x = (bounds.left + bounds.right) / 2
        val y = (bounds.top + bounds.bottom) / 2
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 60))
            .build()
        dispatchGesture(gesture, null, null)
    }
}
