package com.tea.a2

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * The first run: the agent needs a provider to talk to.
 *
 * The agent has a shortcut for exactly this (`/api/config/quickstart`): a
 * provider and a key are enough - it writes the configuration itself. A
 * full provider.yaml can be pasted instead, for everything the shortcut
 * cannot express.
 */
class SetupActivity : Activity() {

    private lateinit var status: TextView
    private lateinit var provider: Spinner
    private lateinit var key: EditText
    private lateinit var yaml: EditText
    private val handler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val settings = A2Settings(this)

        val root = LinearLayout(this)
        root.orientation = LinearLayout.VERTICAL
        root.setPadding(24, 24, 24, 24)

        val title = TextView(this)
        title.text = "A2 - the agent on this phone"
        title.textSize = 20f
        title.gravity = Gravity.CENTER
        root.addView(title)

        status = TextView(this)
        status.text = "starting the agent..."
        root.addView(status)

        provider = Spinner(this)
        provider.adapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            listOf("deepseek", "minimax", "mimo")
        )
        root.addView(provider)

        key = EditText(this)
        key.hint = "API key (sk-... / tp-...)"
        root.addView(key)

        val save = Button(this)
        save.text = "Save and start"
        save.setOnClickListener { quickstart(settings) }
        root.addView(save)

        val upload = Button(this)
        upload.text = "Use a provider.yaml instead"
        upload.setOnClickListener { uploadYaml(settings) }
        root.addView(upload)

        yaml = EditText(this)
        yaml.hint = "provider.yaml"
        yaml.minLines = 4
        yaml.setText(settings.readProvider())
        root.addView(yaml, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f
        ))

        setContentView(root)
        AgentService.start(this)
        waitForTheAgent(settings)
    }

    /** The agent needs a moment to bind its port before it can be asked. */
    private fun waitForTheAgent(settings: A2Settings) {
        handler.postDelayed(object : Runnable {
            override fun run() {
                Thread {
                    val health = get("/api/health", settings.port)
                    runOnUiThread {
                        if (health != null) {
                            status.text = "the agent is up (version " +
                                (health.optString("version", "?")) + ")"
                        } else {
                            status.text = "the agent is not answering yet..."
                            handler.postDelayed(this, 1000)
                        }
                    }
                }.start()
            }
        }, 800)
    }

    private fun quickstart(settings: A2Settings) {
        val chosen = provider.selectedItem?.toString() ?: "deepseek"
        val apiKey = key.text.toString().trim()
        if (apiKey.isEmpty()) {
            Toast.makeText(this, "the key is empty", Toast.LENGTH_SHORT).show()
            return
        }
        status.text = "saving..."
        Thread {
            val body = JSONObject().put("provider", chosen).put("api_key", apiKey)
            val answer = post("/api/config/quickstart", body.toString(), settings.port)
            runOnUiThread {
                if (answer != null) {
                    settings.readProvider()
                    Toast.makeText(this, "the agent is configured", Toast.LENGTH_SHORT).show()
                    startActivity(Intent(this, MainActivity::class.java))
                    finish()
                } else {
                    status.text = "the agent did not take the configuration"
                }
            }
        }.start()
    }

    private fun uploadYaml(settings: A2Settings) {
        val text = yaml.text.toString()
        if (text.isBlank()) {
            Toast.makeText(this, "the configuration is empty", Toast.LENGTH_SHORT).show()
            return
        }
        status.text = "uploading..."
        Thread {
            val answer = post("/api/config", text, settings.port)
            runOnUiThread {
                if (answer != null) {
                    Toast.makeText(this, "the configuration is in", Toast.LENGTH_SHORT).show()
                    startActivity(Intent(this, MainActivity::class.java))
                    finish()
                } else {
                    status.text = "the agent refused the configuration"
                }
            }
        }.start()
    }

    private fun get(path: String, port: Int): JSONObject? = try {
        val connection = URL("http://127.0.0.1:$port$path").openConnection() as HttpURLConnection
        connection.connectTimeout = 2000
        connection.readTimeout = 5000
        val text = connection.inputStream.use { it.readBytes().toString(Charsets.UTF_8) }
        connection.disconnect()
        JSONObject(text)
    } catch (e: Exception) {
        null
    }

    private fun post(path: String, body: String, port: Int): JSONObject? = try {
        val connection = URL("http://127.0.0.1:$port$path").openConnection() as HttpURLConnection
        connection.requestMethod = "POST"
        connection.doOutput = true
        connection.connectTimeout = 3000
        connection.readTimeout = 15000
        connection.setRequestProperty("Content-Type", "application/json")
        connection.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
        val stream = if (connection.responseCode in 200..299) {
            connection.inputStream
        } else {
            connection.errorStream
        }
        val text = stream?.use { it.readBytes().toString(Charsets.UTF_8) } ?: ""
        connection.disconnect()
        JSONObject(text)
    } catch (e: Exception) {
        null
    }
}
