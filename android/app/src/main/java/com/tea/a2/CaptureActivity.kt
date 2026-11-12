package com.tea.a2

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle

/**
 * The activity that asks the user for the screen capture consent.
 *
 * Android only shows that system dialog from an activity, so the capture
 * needs one of its own. The projection it gets is handed to ScreenCapture.
 */
class CaptureActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        android.util.Log.i("A2Capture", "the capture activity is up")
        val manager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as? MediaProjectionManager
        if (manager == null) {
            finish()
            return
        }
        try {
            startActivityForResult(manager.createScreenCaptureIntent(), REQUEST)
        } catch (e: Exception) {
            finish()
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        android.util.Log.i(
            "A2Capture",
            "the consent came back: request=" + requestCode + " result=" + resultCode +
                " data=" + (data != null)
        )
        if (requestCode == REQUEST && resultCode == RESULT_OK && data != null) {
            try {
                // Android 14: the service has to be in the foreground with the
                // mediaProjection type before the projection is handed out.
                AgentService.enableMediaProjection(this)
                Thread.sleep(300)
                val manager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as? MediaProjectionManager
                val projection = manager?.getMediaProjection(resultCode, data)
                android.util.Log.i("A2Capture", "the projection is " + (projection != null))
                if (projection != null) {
                    ScreenCapture.set(this, projection)
                }
            } catch (e: Exception) {
                android.util.Log.w("A2Capture", "the projection failed: " + e.message)
            }
        }
        finish()
    }

    companion object {
        private const val REQUEST = 1001

        /** Asks the user for the screen capture consent. */
        fun ask(context: Context) {
            context.startActivity(
                Intent(context, CaptureActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        }
    }
}
