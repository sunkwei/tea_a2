package com.tea.a2

import android.content.Context
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.DisplayMetrics
import android.util.Log
import android.view.WindowManager
import java.io.ByteArrayOutputStream

/**
 * The screen capture.
 *
 * Android asks the user once per session: the consent comes from
 * CaptureActivity, and the projection it hands over is kept here. The agent
 * takes a picture through `toolkit_phone` (action=screenshot).
 *
 * Three things Android 14 insists on, and one that is only a trap:
 *   * the projection needs a callback before anything is done with it;
 *   * a projection may create only ONE virtual display - so the display is
 *     built once (when the consent arrives) and every picture is read from
 *     its image reader;
 *   * the foreground service has to carry the mediaProjection type.
 *
 * The trap: `ImageReader.acquireLatestImage()` BLOCKS until a frame shows up,
 * and the mirror only produces a frame when the screen changes. Asking for
 * a picture of a still screen would hang forever - so the frames are taken
 * by a listener and the last one is kept.
 */
object ScreenCapture {

    private const val TAG = "A2Capture"

    private var projection: MediaProjection? = null
    private var callback: MediaProjection.Callback? = null
    private var imageReader: ImageReader? = null
    private var display: VirtualDisplay? = null
    private var width = 1080
    private var height = 2400
    private var density = 420
    private val lock = Object()

    /** The frames that came in. */
    private val frameLock = Object()
    private var frameCounter = 0L
    private var latestPng: ByteArray? = null

    /** Whether a capture is possible right now. */
    fun ready(): Boolean = synchronized(lock) { projection != null }

    /** Takes over a projection the user allowed. */
    fun set(context: Context, newProjection: MediaProjection) {
        synchronized(lock) {
            try {
                projection?.stop()
            } catch (e: Exception) {
                // stopping twice is not an error
            }
            try {
                display?.release()
            } catch (e: Exception) {
                // the display was already gone
            }
            try {
                imageReader?.close()
            } catch (e: Exception) {
                // the reader was already closed
            }

            val metrics = DisplayMetrics()
            val window = context.getSystemService(Context.WINDOW_SERVICE) as? WindowManager
            window?.defaultDisplay?.getRealMetrics(metrics)
            width = metrics.widthPixels.coerceAtLeast(320)
            height = metrics.heightPixels.coerceAtLeast(320)
            density = metrics.densityDpi

            val reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2)
            imageReader = reader
            projection = newProjection

            // The frames: taken by a listener, because asking for one
            // directly blocks when the screen does not change.
            reader.setOnImageAvailableListener({ source ->
                try {
                    val image = source.acquireLatestImage()
                    if (image != null) {
                        val png = toPng(image)
                        image.close()
                        synchronized(frameLock) {
                            latestPng = png
                            frameCounter++
                            frameLock.notifyAll()
                        }
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "a frame was lost: " + e.message)
                }
            }, Handler(Looper.getMainLooper()))

            // Android 14: no callback, no capture.
            val onStop = object : MediaProjection.Callback() {
                override fun onStop() {
                    Log.i(TAG, "the screen capture was stopped")
                    clear()
                }
            }
            callback = onStop
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                    newProjection.registerCallback(onStop, Handler(Looper.getMainLooper()))
                } else {
                    // Android 14 replaced registerCallback(callback) with the
                    // two argument one; the old one is gone from the SDK, so
                    // it is called by name.
                    val method = MediaProjection::class.java.getMethod(
                        "registerCallback", MediaProjection.Callback::class.java
                    )
                    method.invoke(newProjection, onStop)
                }
            } catch (e: Exception) {
                Log.w(TAG, "the callback could not be registered: " + e.message)
            }

            // The one virtual display this projection is allowed to have.
            display = newProjection.createVirtualDisplay(
                "a2-capture",
                width,
                height,
                density,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                reader.surface,
                null,
                null
            )
            Log.i(TAG, "the screen capture is ready (${width}x$height)")
        }
    }

    /** Forgets the projection (the user revoked it, or the service died). */
    fun clear() {
        synchronized(lock) {
            try {
                display?.release()
            } catch (e: Exception) {
                // the display was already gone
            }
            display = null
            try {
                projection?.stop()
            } catch (e: Exception) {
                // stopping twice is not an error
            }
            projection = null
            callback = null
            try {
                imageReader?.close()
            } catch (e: Exception) {
                // the reader was already closed
            }
            imageReader = null
        }
        synchronized(frameLock) {
            latestPng = null
            frameLock.notifyAll()
        }
    }

    /**
     * A PNG of the screen. Waits a moment for a new frame, but never longer
     * than a few seconds - the last frame is better than no answer.
     */
    fun capture(): ByteArray? {
        if (!ready()) return null
        synchronized(frameLock) {
            val before = frameCounter
            val deadline = System.currentTimeMillis() + 2500
            while (frameCounter == before && System.currentTimeMillis() < deadline) {
                try {
                    frameLock.wait(200)
                } catch (e: InterruptedException) {
                    Thread.currentThread().interrupt()
                    break
                }
            }
            return latestPng
        }
    }

    /** One frame as a PNG. */
    private fun toPng(image: Image): ByteArray? {
        return try {
            val plane = image.planes[0]
            val buffer = plane.buffer
            val pixelStride = plane.pixelStride
            val rowStride = plane.rowStride
            val w = image.width
            val h = image.height
            val pixels = IntArray(w * h)
            for (row in 0 until h) {
                for (col in 0 until w) {
                    val index = row * rowStride + col * pixelStride
                    val r = buffer.get(index).toInt() and 0xFF
                    val g = buffer.get(index + 1).toInt() and 0xFF
                    val b = buffer.get(index + 2).toInt() and 0xFF
                    val a = buffer.get(index + 3).toInt() and 0xFF
                    pixels[row * w + col] = (a shl 24) or (r shl 16) or (g shl 8) or b
                }
            }
            val bitmap = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
            bitmap.setPixels(pixels, 0, w, 0, 0, w, h)
            val stream = ByteArrayOutputStream()
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, stream)
            bitmap.recycle()
            stream.toByteArray()
        } catch (e: Exception) {
            Log.w(TAG, "the frame could not be turned into a picture: " + e.message)
            null
        }
    }
}
