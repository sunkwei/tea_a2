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
        handleSharedText(intent)
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleSharedText(intent)
    }

    /** Waits for the agent to answer, then shows its web UI. */
    private fun loadWhenReady(settings: A2Settings, attempt: Int = 0) {
        val url = "http://127.0.0.1:${settings.port}/"
        val view = webView
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
                } else {
                    webView?.loadUrl(url)
                }
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

        bar.addView(topButton("Setup") {
            startActivity(Intent(this, SetupActivity::class.java))
        })
        bar.addView(topButton("Capture") {
            CaptureActivity.ask(this)
        })
        bar.addView(topButton("Browser") {
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
        bar.addView(topButton("Stop") {
            AgentService.stop(this)
            Toast.makeText(this, "the agent is stopping", Toast.LENGTH_SHORT).show()
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
                // The agent was not up yet (or it just restarted): try again.
                handler.postDelayed({
                    loadWhenReady(A2Settings(this@MainActivity))
                }, 1500)
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
