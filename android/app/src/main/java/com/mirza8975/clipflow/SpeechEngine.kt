package com.mirza8975.clipflow

import android.util.Log
import androidx.annotation.Keep
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.OfflineModelConfig
import com.k2fsa.sherpa.onnx.OfflineRecognizer
import com.k2fsa.sherpa.onnx.OfflineRecognizerConfig
import com.k2fsa.sherpa.onnx.OfflineStream
import com.k2fsa.sherpa.onnx.OfflineTransducerModelConfig
import com.k2fsa.sherpa.onnx.OfflineWhisperModelConfig
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock
import kotlin.jvm.JvmOverloads
import kotlin.jvm.JvmStatic

/**
 * Thread-safe native speech-to-text bridge for sherpa-onnx on Android.
 *
 * Supports both Hindi Whisper Tiny and English Parakeet TDT architectures.
 * Enforces canonical 16 kHz mono Float32 audio input, deterministic native
 * memory management, safe PCM16 conversion, and lifecycle protection against
 * premature teardown while decoding is active.
 */
@Keep
object SpeechEngine {

    private const val TAG = "SpeechEngine"
    private const val EXPECTED_SAMPLE_RATE = 16000

    private val lock = ReentrantLock()

    @Volatile
    private var recognizer: OfflineRecognizer? = null

    @Volatile
    private var loadedModelDir: String? = null

    @Volatile
    private var loadedLanguage: String? = null

    private val activeDecodings = AtomicInteger(0)
    private val pendingRelease = AtomicBoolean(false)

    /**
     * Check if a speech recognizer is currently loaded in memory.
     */
    @Keep
    @JvmStatic
    fun isModelLoaded(): Boolean = recognizer != null

    /**
     * Get the language code of the currently loaded model, or empty string if none.
     */
    @Keep
    @JvmStatic
    fun getLoadedLanguage(): String = loadedLanguage ?: ""

    /**
     * Initialize the native sherpa-onnx OfflineRecognizer.
     *
     * Unloads any previously loaded model first to prevent holding both
     * large models in memory simultaneously.
     */
    @Keep
    @JvmStatic
    @JvmOverloads
    fun initRecognizer(
        modelDir: String,
        isWhisper: Boolean,
        encoderPath: String,
        decoderPath: String,
        joinerPath: String = "",
        tokensPath: String,
        language: String = "hi",
        numThreads: Int = 2
    ): Boolean {
        lock.withLock {
            if (recognizer != null && loadedModelDir == modelDir && loadedLanguage == language) {
                pendingRelease.set(false)
                Log.i(TAG, "Model already loaded: $modelDir ($language)")
                return true
            }

            // Prevent releasing or replacing recognizer if active decoding is running
            if (activeDecodings.get() > 0) {
                val err = "Cannot reinitialize SpeechEngine: active decoding in progress (${activeDecodings.get()})"
                Log.w(TAG, err)
                throw IllegalStateException(err)
            }

            // Release any previously loaded model to preserve memory
            if (recognizer != null) {
                Log.i(TAG, "Unloading previous model ($loadedLanguage) before loading $language")
                releaseInternal()
            }

            // Verify model component files exist on disk
            val encFile = File(encoderPath)
            val decFile = File(decoderPath)
            val tokFile = File(tokensPath)

            require(encFile.isFile) { "Encoder file not found: $encoderPath" }
            require(decFile.isFile) { "Decoder file not found: $decoderPath" }
            require(tokFile.isFile) { "Tokens file not found: $tokensPath" }

            if (!isWhisper) {
                val joinFile = File(joinerPath)
                require(joinFile.isFile) { "Joiner file not found for Transducer: $joinerPath" }
            }

            try {
                val featConfig = FeatureConfig(
                    sampleRate = EXPECTED_SAMPLE_RATE,
                    featureDim = 80
                )

                val modelConfig: OfflineModelConfig = if (isWhisper) {
                    OfflineModelConfig(
                        whisper = OfflineWhisperModelConfig(
                            encoder = encoderPath,
                            decoder = decoderPath,
                            language = language,
                            task = "transcribe",
                            tailPaddings = 1000
                        ),
                        tokens = tokensPath,
                        numThreads = numThreads.coerceAtLeast(1),
                        debug = false,
                        provider = "cpu",
                        modelType = "whisper"
                    )
                } else {
                    OfflineModelConfig(
                        transducer = OfflineTransducerModelConfig(
                            encoder = encoderPath,
                            decoder = decoderPath,
                            joiner = joinerPath
                        ),
                        tokens = tokensPath,
                        numThreads = numThreads.coerceAtLeast(1),
                        debug = false,
                        provider = "cpu",
                        modelType = "nemo_transducer"
                    )
                }

                val config = OfflineRecognizerConfig(
                    featConfig = featConfig,
                    modelConfig = modelConfig,
                    decodingMethod = "greedy_search"
                )

                val newRec = OfflineRecognizer(assetManager = null, config = config)
                recognizer = newRec
                loadedModelDir = modelDir
                loadedLanguage = language
                pendingRelease.set(false)

                Log.i(TAG, "Successfully initialized sherpa-onnx OfflineRecognizer ($language, whisper=$isWhisper)")
                return true
            } catch (e: Exception) {
                Log.e(TAG, "Failed to initialize sherpa-onnx OfflineRecognizer", e)
                releaseInternal()
                throw e
            }
        }
    }

    /**
     * Transcribe canonical 16 kHz mono Float32 audio bytes (little-endian).
     *
     * Validates that the input byte count is a multiple of 4 bytes per sample.
     */
    @Keep
    @JvmStatic
    @JvmOverloads
    fun transcribeFloatBytes(floatBytes: ByteArray, sampleRate: Int = EXPECTED_SAMPLE_RATE): String {
        val result = transcribeFloatBytesDetailed(floatBytes, sampleRate)
        return (result["text"] as? String) ?: ""
    }

    /**
     * Transcribe canonical 16 kHz mono Float32 audio bytes, returning tokens and timestamps.
     */
    @Keep
    @JvmStatic
    @JvmOverloads
    fun transcribeFloatBytesDetailed(
        floatBytes: ByteArray,
        sampleRate: Int = EXPECTED_SAMPLE_RATE
    ): Map<String, Any> {
        require(floatBytes.size % 4 == 0) {
            "Invalid Float32 buffer: byte length ${floatBytes.size} is not divisible by 4"
        }
        require(sampleRate == EXPECTED_SAMPLE_RATE) {
            "Unsupported sample rate: $sampleRate Hz (expected $EXPECTED_SAMPLE_RATE Hz)"
        }

        val floatBuffer = ByteBuffer.wrap(floatBytes)
            .order(ByteOrder.LITTLE_ENDIAN)
            .asFloatBuffer()

        val samples = FloatArray(floatBuffer.remaining())
        floatBuffer.get(samples)

        return decodeSamplesInternal(samples, sampleRate)
    }

    /**
     * Transcribe signed 16-bit PCM audio bytes (sample / 32768.0f normalization).
     *
     * Strictly distinguishes PCM16 from Float32: never reads PCM16 bytes as floats.
     */
    @Keep
    @JvmStatic
    @JvmOverloads
    fun transcribePcm16Bytes(pcmBytes: ByteArray, sampleRate: Int = EXPECTED_SAMPLE_RATE): String {
        val result = transcribePcm16BytesDetailed(pcmBytes, sampleRate)
        return (result["text"] as? String) ?: ""
    }

    /**
     * Transcribe signed 16-bit PCM audio bytes, returning detailed tokens and timestamps.
     */
    @Keep
    @JvmStatic
    @JvmOverloads
    fun transcribePcm16BytesDetailed(
        pcmBytes: ByteArray,
        sampleRate: Int = EXPECTED_SAMPLE_RATE
    ): Map<String, Any> {
        require(pcmBytes.size % 2 == 0) {
            "Invalid PCM16 buffer: byte length ${pcmBytes.size} is not divisible by 2"
        }
        require(sampleRate == EXPECTED_SAMPLE_RATE) {
            "Unsupported sample rate: $sampleRate Hz (expected $EXPECTED_SAMPLE_RATE Hz)"
        }

        val shortBuffer = ByteBuffer.wrap(pcmBytes)
            .order(ByteOrder.LITTLE_ENDIAN)
            .asShortBuffer()

        val samples = FloatArray(shortBuffer.remaining())
        for (i in samples.indices) {
            samples[i] = shortBuffer.get(i) / 32768.0f
        }

        return decodeSamplesInternal(samples, sampleRate)
    }

    /**
     * Internal decoding helper. Manages stream lifecycle and active decoding counter.
     */
    private fun decodeSamplesInternal(samples: FloatArray, sampleRate: Int): Map<String, Any> {
        val rec = lock.withLock {
            val current = recognizer
                ?: throw IllegalStateException("SpeechEngine is not initialized. Call initRecognizer() first.")
            activeDecodings.incrementAndGet()
            current
        }

        try {
            val stream: OfflineStream = rec.createStream()
            try {
                stream.acceptWaveform(samples, sampleRate)
                rec.decode(stream)
                val res = rec.getResult(stream)

                val tokensList = res.tokens.toList()
                val timestampsList = res.timestamps.toList()

                return mapOf(
                    "text" to res.text,
                    "tokens" to tokensList,
                    "timestamps" to timestampsList
                )
            } finally {
                // Ensure native C++ OfflineStream pointer is freed immediately
                stream.release()
            }
        } finally {
            val remaining = activeDecodings.decrementAndGet()
            // If service destruction or release was requested during active decoding, release now
            if (remaining == 0 && pendingRelease.get()) {
                lock.withLock {
                    if (activeDecodings.get() == 0 && pendingRelease.get()) {
                        Log.i(TAG, "Deferred release executed after active decodings finished")
                        releaseInternal()
                        pendingRelease.set(false)
                    }
                }
            }
        }
    }

    /**
     * Safely release native recognizer resources.
     *
     * If decoding is currently active and [force] is false, defers release until
     * all active decodings finish to prevent SIGSEGV or native memory corruption.
     */
    @Keep
    @JvmStatic
    @JvmOverloads
    fun release(force: Boolean = false) {
        if (!force && activeDecodings.get() > 0) {
            Log.w(TAG, "Active decodings in progress (${activeDecodings.get()}). Deferring release.")
            pendingRelease.set(true)
            return
        }

        lock.withLock {
            releaseInternal()
            pendingRelease.set(false)
        }
    }

    private fun releaseInternal() {
        try {
            recognizer?.release()
        } catch (e: Exception) {
            Log.w(TAG, "Exception while releasing native OfflineRecognizer", e)
        } finally {
            recognizer = null
            loadedModelDir = null
            loadedLanguage = null
        }
    }
}
