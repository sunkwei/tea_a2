package com.tea.a2

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast

/**
 * The app's window: the agent's own web UI in a web view.
 *
 * The agent serves its UI on the loopback interface; this activity only
 * shows it. The buttons on top do the things the web UI cannot do for
 * itself: the setup, the screen capture consent, opening the UI in the
 * system browser and stopping the agent.
 */
class MainActivity : Activity() {

    private var webView: WebView? = null
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private val handler = android.os.Handler(android.os.Looper.getMainLooper())
    /// True once the agent's page has loaded: after that a failed load is
    /// reported, not retried in a loop.
    @Volatile
    private var pageLoaded: Boolean = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val settings = A2Settings(this)
        if (!settings.isConfigured()) {
            startActivity(Intent(this, SetupActivity::class.java))
            finish()
            return
        }
        requestPermissions()
        AgentService.start(this)
        setContentView(buildUi(settings))
        // The agent needs a moment to bind its port. Loading the page before
        // that gives the user "ERR_CONNECTION_REFUSED" and a dead screen.
        loadWhenReady(settings)
        startWatchdog(settings)
        checkKeepAlive(settings)
        handleSharedText(intent)
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleSharedText(intent)
    }

    /**
     * Keeps the agent alive when the screen is off.
     *
     * When the phone dozes, the system freezes the app: the agent keeps its
     * process but stops answering - the web UI shows a dead page. Two things
     * have to be true: the app must be exempt from the battery optimisation,
     * and its notifications must be on (a foreground service without a
     * notification is a foreground service the system may drop).
     */
    private fun keepAlive() {
        val power = getSystemService(android.os.PowerManager::class.java)
        val exempt = power?.isIgnoringBatteryOptimizations(packageName) == true
        if (exempt) {
            // The battery exemption is already there. The vendor's own policy
            // ("省电策略") is the one that really matters, and it cannot be
            // read from here - so send the user to the app's page anyway.
            Toast.makeText(
                this,
                "电量优化已豁免；还要确认「省电策略」是无限制",
                Toast.LENGTH_LONG
            ).show()
        }
        if (!exempt) {
            // Xiaomi / HyperOS: the per app power policy ("省电策略") is what
            // really decides whether the app keeps running with the screen
            // off. That screen has its own intent; the standard one only
            // leads to the battery list.
            if (openVendorPowerSettings()) return
            try {
                startActivity(
                    Intent(
                        android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:" + packageName)
                    )
                )
                return
            } catch (e: Exception) {
                // Some ROMs do not have that screen; fall through to the
                // manual settings.
            }
        }
        val notifications = getSystemService(android.app.NotificationManager::class.java)
        if (notifications != null && !notifications.areNotificationsEnabled()) {
            try {
                startActivity(
                    Intent(android.provider.Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                        .putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, packageName)
                )
                return
            } catch (e: Exception) {
                // no such screen either: the manual settings it is
            }
        }
        try {
            startActivity(
                Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                    .setData(Uri.parse("package:" + packageName))
            )
        } catch (e: Exception) {
            Toast.makeText(this, "no settings screen on this phone", Toast.LENGTH_SHORT).show()
        }
    }

    /**
     * The vendor screen where the per app power policy lives. MIUI and
     * HyperOS keep it here; other ROMs do not have it, and the caller falls
     * back to the standard battery optimisation screen.
     */
    private fun openVendorPowerSettings(): Boolean {
        val candidates = listOf(
            Intent("miui.intent.action.POWER_HIDE_MODE_APP_LIST")
                .putExtra("package_name", packageName)
                .putExtra("packageName", packageName)
                .putExtra("package_label", getString(R.string.app_name)),
            Intent("com.miui.powerkeeper.ui.HiddenAppsConfigActivity")
                .putExtra("package_name", packageName)
                .putExtra("packageName", packageName)
        )
        for (intent in candidates) {
            try {
                startActivity(intent)
                return true
            } catch (e: Exception) {
                // this ROM does not have that screen
            }
        }
        return false
    }

    /**
     * Watches the agent: when it is gone (restarted, crashed) the page is
     * loaded again once it is back. The page itself is never reloaded just
     * because a subresource failed - that was a reload loop.
     */
    private fun startWatchdog(settings: A2Settings) {
        handler.postDelayed(object : Runnable {
            override fun run() {
                Thread {
                    val alive = probe(settings.port)
                    if (!alive && pageLoaded) {
                        pageLoaded = false
                        handler.post { loadWhenReady(settings) }
                    }
                }.start()
                handler.postDelayed(this, 10000)
            }
        }, 10000)
    }

    /** Copies the snippet for the desktop's ~/.tea_agent/tea_agent.yaml. */
    private fun copyPeerSnippet(settings: A2Settings, url: String) {
        if (url.isEmpty()) {
            Toast.makeText(this, "还没有局域网地址", Toast.LENGTH_SHORT).show()
            return
        }
        val snippet = buildString {
            append("# 桌面上的 ~/.tea_agent/tea_agent.yaml 里加这段：\n")
            append("peers:\n")
            append("  phone:\n")
            append("    url: ").append(url).append("\n")
            append("    token: ").append(settings.token()).append("\n")
        }
        val clipboard = getSystemService(android.content.ClipboardManager::class.java)
        clipboard?.setPrimaryClip(android.content.ClipData.newPlainText("a2-peer", snippet))
        Toast.makeText(this, "已复制：粘到电脑的 tea_agent.yaml", Toast.LENGTH_LONG).show()
    }

    /** Tells the user (once) that the agent needs the exemption. */
    private fun checkKeepAlive(settings: A2Settings) {
        val power = getSystemService(android.os.PowerManager::class.java)
        if (power?.isIgnoringBatteryOptimizations(packageName) == true) return
        handler.postDelayed({
            Toast.makeText(
                this,
                "点顶栏的「后台」放行一次，息屏后 agent 才不会被冻住",
                Toast.LENGTH_LONG
            ).show()
        }, 1500)
    }

    /** Waits for the agent to answer, then shows its web UI. */
    private fun loadWhenReady(settings: A2Settings, attempt: Int = 0) {
        val url = "http://127.0.0.1:${settings.port}/"
        val view = webView
        pageLoaded = false
        Thread {
            val alive = probe(settings.port)
            handler.post {
                if (alive) {
                    webView?.loadUrl(url)
                    // 看一眼手机上的排版：视口有多宽、侧栏是不是抽屉。
                    webView?.postDelayed({
                        view?.evaluateJavascript(
                            "window.innerWidth + '|' + (document.getElementById('app')||{}).className"
                        ) { value -> android.util.Log.i("A2WebView", value) }
                    }, 2500)
                } else if (attempt < 60) {
                    handler.postDelayed({ loadWhenReady(settings, attempt + 1) }, 500)
                }
                // Out of tries: do not load a page that cannot work. The
                // watchdog below tries again when the agent is back.
            }
        }.start()
    }

    /** A GET on /api/health: does the agent answer yet? */
    private fun probe(port: Int): Boolean = try {
        val connection = java.net.URL("http://127.0.0.1:$port/api/health").openConnection()
                as java.net.HttpURLConnection
        connection.connectTimeout = 800
        connection.readTimeout = 2000
        val code = connection.responseCode
        connection.disconnect()
        code == 200
    } catch (e: Exception) {
        false
    }

    /** Asks for the permissions the phone bridge needs, when they are missing. */
    private fun requestPermissions() {
        val wanted = mutableListOf<String>()
        if (checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            wanted.add(android.Manifest.permission.ACCESS_FINE_LOCATION)
            wanted.add(android.Manifest.permission.ACCESS_COARSE_LOCATION)
        }
        // The foreground notification needs POST_NOTIFICATIONS on Android 13+.
        if (checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            wanted.add(android.Manifest.permission.POST_NOTIFICATIONS)
        }
        if (wanted.isNotEmpty()) requestPermissions(wanted.toTypedArray(), 1)
    }

    private fun buildUi(settings: A2Settings): View {
        val root = LinearLayout(this)
        root.orientation = LinearLayout.VERTICAL
        root.layoutParams = ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT
        )

        val bar = LinearLayout(this)
        bar.orientation = LinearLayout.HORIZONTAL
        bar.gravity = Gravity.CENTER_VERTICAL
        bar.setPadding(8, 8, 8, 8)

        bar.addView(topButton("配置") {
            startActivity(Intent(this, SetupActivity::class.java))
        })
        bar.addView(topButton("截屏") {
            // Two ways to take a picture:
            //  * Shizuku's `screencap` - no consent, works everywhere;
            //  * MediaProjection - needs the user's consent, but Android 16
            //    only gives that type of foreground service to the system
            //    (it wants the privileged CAPTURE_VIDEO_OUTPUT permission),
            //    so on Android 16 it is a dead end for a normal app.
            when {
                ShizukuHelper.permissionGranted() -> Toast.makeText(
                    this,
                    "截屏走 Shizuku 的 screencap，不用授权；让 agent 直接截就行",
                    Toast.LENGTH_LONG
                ).show()
                android.os.Build.VERSION.SDK_INT >= 36 -> Toast.makeText(
                    this,
                    "这台 Android 不让普通 app 用录屏授权，截屏请开 Shizuku",
                    Toast.LENGTH_LONG
                ).show()
                else -> CaptureActivity.ask(this)
            }
        })
        bar.addView(topButton("浏览器") {
            val url = "http://127.0.0.1:${settings.port}/"
            startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse(url))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        })
        bar.addView(topButton("Shizuku") {
            if (ShizukuHelper.permissionGranted()) {
                Toast.makeText(
                    this,
                    "Shizuku is ready (uid " + ShizukuHelper.uid() + ")",
                    Toast.LENGTH_SHORT
                ).show()
            } else if (ShizukuHelper.serviceRunning()) {
                ShizukuHelper.requestPermission(this)
            } else {
                Toast.makeText(
                    this,
                    "Shizuku is not running - start it and try again",
                    Toast.LENGTH_LONG
                ).show()
            }
        })
        bar.addView(topButton("无障碍") {
            // The accessibility service is turned on in the system settings -
            // an app cannot ask for it by itself.
            if (A11yService.enabled()) {
                Toast.makeText(
                    this,
                    "无障碍已开启：agent 能读屏、按文字点击、输入",
                    Toast.LENGTH_LONG
                ).show()
            } else {
                try {
                    startActivity(
                        Intent(android.provider.Settings.ACTION_ACCESSIBILITY_SETTINGS)
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    )
                } catch (e: Exception) {
                    Toast.makeText(this, "打不开无障碍设置页", Toast.LENGTH_SHORT).show()
                }
            }
        })
        bar.addView(topButton("保活") {
            keepAlive()
        })
        bar.addView(topButton("停止") {
            AgentService.stop(this)
            Toast.makeText(
                this,
                "agent 已停止；重新打开 app 就会再起来",
                Toast.LENGTH_LONG
            ).show()
        })

        // The address a computer on the same Wi-Fi has to use, and a button
        // that copies the peer snippet for the desktop's tea_agent.yaml.
        val addressRow = LinearLayout(this)
        addressRow.orientation = LinearLayout.HORIZONTAL
        addressRow.gravity = Gravity.CENTER_VERTICAL
        addressRow.setPadding(12, 0, 12, 0)
        val addressText = TextView(this)
        addressText.textSize = 12f
        addressText.setTextColor(0xFF9AA0A6.toInt())
        val lanIp = PhoneNet.primaryAddress(this)
        val url = if (lanIp.isEmpty()) "" else "http://$lanIp:${settings.port}"
        addressText.text = if (url.isEmpty()) {
            "没拿到局域网地址（Wi-Fi 没连？）"
        } else {
            "电脑打开：$url"
        }
        addressRow.addView(addressText, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        addressRow.addView(topButton("复制") {
            copyPeerSnippet(settings, addressText.text.toString())
        })

        val view = WebView(this)
        view.settings.javaScriptEnabled = true
        view.settings.domStorageEnabled = true
        view.settings.allowFileAccess = false
        view.webViewClient = object : WebViewClient() {
            override fun onReceivedError(
                view: WebView?,
                request: android.webkit.WebResourceRequest?,
                error: android.webkit.WebResourceError?
            ) {
                // Only a broken **main frame** means "the page itself is gone".
                // A failing subresource (the agent's event stream, a font) is
                // the agent's business - reloading the page for it turns into
                // a reload loop: the screen flickers once a second and the
                // phone burns a third of a core.
                val mainFrame = request?.isForMainFrame ?: true
                if (!mainFrame) return
                // The page is already loaded and working: a failed reload
                // should not turn into an endless retry.
                if (pageLoaded) {
                    android.util.Log.w("A2WebView", "the page failed to load: " + error)
                    return
                }
                // The agent was not up yet (or it just restarted): try again.
                handler.postDelayed({
                    loadWhenReady(A2Settings(this@MainActivity))
                }, 1500)
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                pageLoaded = true
            }
        }
        view.webChromeClient = object : WebChromeClient() {
            // The web UI uploads provider.yaml through a file input.
            override fun onShowFileChooser(
                view: WebView?,
                callback: ValueCallback<Array<Uri>>?,
                params: FileChooserParams?
            ): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                try {
                    startActivityForResult(params!!.createIntent(), FILE_CHOOSER)
                    return true
                } catch (e: Exception) {
                    fileCallback = null
                    return false
                }
            }
        }
        webView = view

        // 五个按钮在手机的宽度上放不下：让这一条能横向滚，而不是把字挤成竖排。
        val barScroll = android.widget.HorizontalScrollView(this)
        barScroll.isHorizontalScrollBarEnabled = false
        barScroll.addView(bar, ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT
        ))
        root.addView(barScroll)
        root.addView(addressRow)
        root.addView(view, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f
        ))
        return root
    }

    private fun topButton(label: String, onClick: () -> Unit): Button {
        val button = Button(this)
        button.text = label
        button.textSize = 12f
        button.setAllCaps(false)
        button.minWidth = 0
        button.minHeight = 0
        button.setPadding(24, 16, 24, 16)
        button.setOnClickListener { onClick() }
        return button
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == FILE_CHOOSER) {
            val callback = fileCallback
            fileCallback = null
            callback?.onReceiveValue(
                if (resultCode == RESULT_OK) {
                    data?.let {
                        WebChromeClient.FileChooserParams.parseResult(resultCode, it)
                    }
                } else {
                    null
                }
            )
        }
    }

    /** A text shared into the app becomes a turn of the agent. */
    private fun handleSharedText(intent: Intent?) {
        if (intent == null || intent.action != Intent.ACTION_SEND) return
        val text = intent.getStringExtra(Intent.EXTRA_TEXT) ?: return
        intent.removeExtra(Intent.EXTRA_TEXT)
        Thread {
            try {
                val settings = A2Settings(this)
                val connection = java.net.URL("http://127.0.0.1:${settings.port}/api/chat")
                    .openConnection() as java.net.HttpURLConnection
                connection.requestMethod = "POST"
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(
                    org.json.JSONObject().put("message", text).toString().toByteArray()
                ) }
                connection.inputStream.use { it.readBytes() }
                connection.disconnect()
                runOnUiThread {
                    Toast.makeText(this, "sent to the agent", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(
                        this,
                        "the agent did not take it: " + (e.message ?: "no answer"),
                        Toast.LENGTH_LONG
                    ).show()
                }
            }
        }.start()
    }

    companion object {
        private const val FILE_CHOOSER = 2001
    }
}
